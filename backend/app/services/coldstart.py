"""Cold start manager: detection, model activation and retraining (FR-25, FR-29)."""
import logging
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, undefer

from .. import database
from ..ai import cnn_lstm
from ..ai.text import severity_rank
from ..config import get_settings
from ..database import utcnow
from ..models import MODEL_ACTIVE, MODEL_COLD, MODEL_TRAINING, Bug, ModelState, Project
from ..schemas import ColdStartOut
from . import settings_service

log = logging.getLogger("bugtracker.coldstart")

TRAINING_LOCK_TIMEOUT = timedelta(hours=1)
_model_cache: dict[int, tuple[object, cnn_lstm.AssignmentModel]] = {}


def verified_count(db: Session, project_id: int) -> int:
    """Bugs that reached Verified with a recorded assignee (the FR-25 measure)."""
    return db.scalar(
        select(func.count(Bug.id)).where(
            Bug.project_id == project_id, Bug.verified_at.is_not(None), Bug.assignee_id.is_not(None)
        )
    ) or 0


def get_state(db: Session, project_id: int) -> ModelState:
    """Return the project's model state with a freshly recalculated verified count."""
    state = db.get(ModelState, project_id)
    if state is None:
        state = ModelState(project_id=project_id, state=MODEL_COLD)
        db.add(state)
    count = verified_count(db, project_id)
    if state.verified_count != count:
        state.verified_count = count
    db.flush()
    return state


def to_out(state: ModelState) -> ColdStartOut:
    threshold = get_settings().cold_start_threshold
    return ColdStartOut(
        project_id=state.project_id,
        state=state.state,
        cold_start=state.state != MODEL_ACTIVE,
        verified_count=state.verified_count,
        threshold=threshold,
        remaining=max(0, threshold - state.verified_count),
        last_trained_at=state.last_trained_at,
        metrics=state.metrics,
        last_error=state.last_error,
    )


def training_due(state: ModelState) -> bool:
    settings = get_settings()
    if state.verified_count < settings.cold_start_threshold:
        return False
    if state.last_trained_at is None:
        return True  # threshold just crossed: activate the model
    if state.verified_count - state.verified_at_last_training >= settings.retrain_after_verified:
        return True
    return utcnow() - state.last_trained_at >= timedelta(days=settings.retrain_interval_days)


def _claim(db: Session, project_id: int) -> bool:
    """Atomically take the training lock so two instances never train the same project."""
    now = utcnow()
    result = db.execute(
        update(ModelState)
        .where(
            ModelState.project_id == project_id,
            (ModelState.training_started_at.is_(None)) | (ModelState.training_started_at < now - TRAINING_LOCK_TIMEOUT),
        )
        .values(training_started_at=now)
    )
    db.commit()
    return result.rowcount == 1


def training_data(db: Session, project_id: int) -> tuple[list[str], list[list[float]], list[int]]:
    labels = settings_service.severity_labels(db)
    rows = db.execute(
        select(Bug.title, Bug.description, Bug.severity, Bug.duplicate_of_id, Bug.probable_duplicate_of_id, Bug.assignee_id)
        .where(Bug.project_id == project_id, Bug.verified_at.is_not(None), Bug.assignee_id.is_not(None))
    ).all()
    texts = [f"{r[0]}\n{r[1]}" for r in rows]
    metas = [cnn_lstm.encode_meta(severity_rank(r[2], labels), r[3] is not None or r[4] is not None) for r in rows]
    return texts, metas, [r[5] for r in rows]


def train_project(project_id: int, force: bool = False) -> bool:
    """Train or retrain one project's model. Serving continues from the previous model meanwhile."""
    with database.SessionLocal() as db:
        state = get_state(db, project_id)
        db.commit()
        if not force and not training_due(state):
            return False
        if state.verified_count < 2 or not _claim(db, project_id):
            return False
        first_training = state.last_trained_at is None
        if first_training:
            state.state = MODEL_TRAINING
            db.commit()
        try:
            texts, metas, assignees = training_data(db, project_id)
            if len(set(assignees)) < 2:
                raise ValueError("At least two developers with verified bugs are needed to train")
            blob, metrics = cnn_lstm.train_model(texts, metas, assignees)
            state = db.get(ModelState, project_id, options=[undefer(ModelState.artifact)])
            state.artifact = blob
            state.metrics = {k: v for k, v in metrics.items() if k != "epochs"}
            state.state = MODEL_ACTIVE
            state.last_trained_at = utcnow()
            state.verified_at_last_training = len(assignees)
            state.last_error = None
            log.info("Assignment model trained for project %s on %s bugs", project_id, len(assignees))
            return True
        except Exception as exc:
            log.exception("Training failed for project %s", project_id)
            db.rollback()
            state = db.get(ModelState, project_id)
            state.last_error = str(exc)[:500]
            if first_training:
                state.state = MODEL_COLD
            return False
        finally:
            state = db.get(ModelState, project_id)
            state.training_started_at = None
            db.commit()


def after_verification(project_id: int) -> None:
    """Recalculate the state after every verification event and train when due (FR-25)."""
    with database.SessionLocal() as db:
        state = get_state(db, project_id)
        db.commit()
        due = training_due(state)
    if due:
        train_project(project_id)


def retrain_due_projects() -> int:
    """Periodic sweep for the 30-day rule; called by the scheduler in main.py."""
    with database.SessionLocal() as db:
        project_ids = list(db.scalars(select(Project.id).where(Project.archived.is_(False))))
    return sum(1 for pid in project_ids if train_project(pid))


def load_model(db: Session, state: ModelState) -> cnn_lstm.AssignmentModel | None:
    """Loaded model for a project, cached in memory and refreshed when a newer one is trained."""
    if state.state != MODEL_ACTIVE or state.last_trained_at is None:
        return None
    cached = _model_cache.get(state.project_id)
    if cached and cached[0] == state.last_trained_at:
        return cached[1]
    blob = db.scalar(select(ModelState.artifact).where(ModelState.project_id == state.project_id))
    if not blob:
        return None
    model = cnn_lstm.AssignmentModel(blob)
    _model_cache[state.project_id] = (state.last_trained_at, model)
    return model
