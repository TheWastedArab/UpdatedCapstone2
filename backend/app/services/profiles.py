"""Developer profile service (FR-20 to FR-23)."""
import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import database
from ..ai.text import extract_keywords
from ..config import get_settings
from ..models import ACTIVE_STATUSES, Bug, DeveloperProfile, ResolutionHistory, User
from ..schemas import HistoryOut, ProfileOut, UserBrief
from . import audit

log = logging.getLogger("bugtracker.profiles")


def ensure_profile(db: Session, user: User, actor_id: int | None = None) -> DeveloperProfile:
    """Create the profile the moment a user becomes a Developer (FR-20)."""
    profile = db.scalar(select(DeveloperProfile).where(DeveloperProfile.user_id == user.id))
    if profile is None:
        profile = DeveloperProfile(user_id=user.id, max_capacity=get_settings().default_capacity)
        db.add(profile)
        db.flush()
        audit.record(db, actor_id, "profile_created", profile_id=profile.id, new=user.email)
    return profile


def open_counts(db: Session, user_ids: list[int]) -> dict[int, int]:
    """Live count of bugs assigned and still Open or In Progress (FR-23).

    Computed from the bug table on every read, so it cannot drift from reality.
    """
    if not user_ids:
        return {}
    rows = db.execute(
        select(Bug.assignee_id, func.count(Bug.id))
        .where(Bug.assignee_id.in_(user_ids), Bug.status.in_(ACTIVE_STATUSES))
        .group_by(Bug.assignee_id)
    ).all()
    counts = dict.fromkeys(user_ids, 0)
    counts.update({uid: n for uid, n in rows})
    return counts


def to_out(db: Session, profile: DeveloperProfile, with_history: bool = False) -> ProfileOut:
    history = profile.history
    return ProfileOut(
        id=profile.id,
        user=UserBrief.model_validate(profile.user),
        active=profile.active,
        availability=profile.availability,
        max_capacity=profile.max_capacity,
        skills=[s.name for s in profile.skills],
        components=[c.name for c in profile.components],
        open_bugs=open_counts(db, [profile.user_id])[profile.user_id],
        resolved_count=len(history),
        history=[HistoryOut.model_validate(h) for h in history[-50:]] if with_history else [],
        updated_at=profile.updated_at,
    )


def enrich_from_verified_bug(db: Session, bug: Bug) -> bool:
    """Append a verified bug to its assignee's resolution history (FR-22). Idempotent."""
    if bug.assignee_id is None or bug.verified_at is None:
        return False
    assignee = db.get(User, bug.assignee_id)
    if assignee is None:
        return False
    profile = ensure_profile(db, assignee)
    already = db.scalar(
        select(ResolutionHistory.id).where(ResolutionHistory.profile_id == profile.id, ResolutionHistory.bug_id == bug.id)
    )
    if already is not None:
        return False
    end = bug.resolved_at or bug.verified_at
    db.add(ResolutionHistory(
        profile_id=profile.id,
        project_id=bug.project_id,
        bug_id=bug.id,
        component=bug.component,
        severity=bug.severity,
        keywords=bug.keywords or extract_keywords(bug.text),
        resolution_seconds=max(0, int((end - bug.created_at).total_seconds())),
    ))
    audit.record(db, None, "profile_enriched", profile_id=profile.id, bug_id=bug.id, project_id=bug.project_id,
                 new=f"bug #{bug.id} verified")
    return True


def enrich_task(bug_id: int) -> None:
    """Background task run after the Verified transition has been returned to the user."""
    from . import coldstart

    with database.SessionLocal() as db:
        bug = db.get(Bug, bug_id)
        if bug is None:
            return
        try:
            enrich_from_verified_bug(db, bug)
            db.commit()
        except IntegrityError:  # another instance wrote the same entry first
            db.rollback()
        project_id = bug.project_id
    coldstart.after_verification(project_id)
