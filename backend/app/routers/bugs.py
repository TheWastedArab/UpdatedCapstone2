"""Bug lifecycle, triage pipeline, comments, attachments and audit trail (FR-05 to FR-14)."""
import logging
import re
import time
import uuid
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, literal_column, or_, select
from sqlalchemy.orm import Session, selectinload

from ..ai import assignment
from ..ai.engine import engine
from ..ai.text import extract_keywords, map_canonical
from ..config import get_settings
from ..database import DbSession, utcnow
from ..deps import FORBIDDEN, at_least, get_current_user, project_role, require_bug, require_project
from ..models import (
    ACTIVE_STATUSES, ROLE_DEVELOPER, ROLE_MANAGER, SEVERITY_UNCLASSIFIED, STATUS_CLOSED, STATUS_IN_PROGRESS,
    STATUS_OPEN, STATUS_RESOLVED, STATUS_VERIFIED, UNAVAILABLE, Attachment, AuditLog, Bug, BugWatcher, Comment,
    DeveloperProfile, ProjectMember, User,
)
from ..schemas import (
    AssignIn, AttachmentOut, AuditOut, BugCreatedOut, BugIn, BugListItem, BugOut, BugPage, BugUpdateIn, CommentIn,
    CommentOut, DuplicateCheckIn, DuplicateCheckOut, DuplicateCandidate, DuplicateIn, Message, RecommendationsOut,
    SeverityIn, StatusIn,
)
from ..services import audit, notifications, profiles, settings_service
from ..services.notifications import Outbox

log = logging.getLogger("bugtracker.bugs")
router = APIRouter(tags=["Bugs and triage"])

DEGRADED_NOTICE = (
    "AI triage is temporarily unavailable. Your report was saved; severity is Unclassified and "
    "duplicate checking was skipped."
)


# ---- helpers ----------------------------------------------------------------
def allowed_transitions(bug: Bug, user: User, role: str) -> list[str]:
    """Statuses this user may move the bug to next (FR-06)."""
    developer = at_least(role, ROLE_DEVELOPER)
    manager = at_least(role, ROLE_MANAGER)
    # A fix is verified by the person who reported it or by a manager, never by its own author alone.
    verifier = manager or (bug.reporter_id == user.id and bug.assignee_id != user.id)
    if bug.status == STATUS_OPEN and developer:
        return [STATUS_IN_PROGRESS]
    if bug.status == STATUS_IN_PROGRESS and developer:
        return [STATUS_RESOLVED]
    if bug.status == STATUS_RESOLVED and verifier:
        return [STATUS_VERIFIED, STATUS_IN_PROGRESS]  # verify, or send back when the fix does not hold
    if bug.status == STATUS_VERIFIED and manager:
        return [STATUS_CLOSED]
    return []


def bug_out(db: Session, bug: Bug, user: User, role: str) -> BugOut:
    out = BugOut.model_validate(bug)
    out.watching = db.scalar(select(BugWatcher.id).where(BugWatcher.bug_id == bug.id, BugWatcher.user_id == user.id)) is not None
    out.allowed_transitions = allowed_transitions(bug, user, role)
    out.my_role = role
    return out


def _fts_condition(q: str):
    """PostgreSQL full-text match; written to hit the GIN index created in the migration."""
    document = func.to_tsvector(literal_column("'english'"), Bug.title + literal_column("' '") + Bug.description)
    return document.op("@@")(func.plainto_tsquery(literal_column("'english'"), q))


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---- duplicate check while typing (FR-10) --------------------------------------
@router.post("/projects/{project_id}/duplicates/check", response_model=DuplicateCheckOut,
             summary="Find similar existing bugs while a report is being written")
def check_duplicates(project_id: int, data: DuplicateCheckIn, user: User = Depends(get_current_user),
                     db: Session = DbSession):
    require_project(db, user, project_id)
    started = time.perf_counter()
    settings = get_settings()
    if not engine.available:
        return DuplicateCheckOut(available=False, method="unavailable", candidates=[], elapsed_ms=0.0,
                                 notice="Duplicate detection is temporarily unavailable. You can still submit your report.")
    try:
        result = engine.duplicates.check(db, project_id, data.title, data.description,
                                         candidates=settings.duplicate_candidates, top_k=3, fusion=settings.duplicate_fusion)
    except Exception:
        log.exception("Duplicate check failed")
        return DuplicateCheckOut(available=False, method="unavailable", candidates=[], elapsed_ms=0.0,
                                 notice="Duplicate detection is temporarily unavailable. You can still submit your report.")
    threshold = settings_service.duplicate_threshold(db)
    shown = [(i, s) for i, s in result.candidates if s >= settings.duplicate_display_floor]
    bugs = {b.id: b for b in db.scalars(select(Bug).where(Bug.id.in_([i for i, _ in shown])))} if shown else {}
    candidates = [
        DuplicateCandidate(bug_id=i, title=bugs[i].title, status=bugs[i].status, severity=bugs[i].severity,
                           match_percent=round(s * 100), probable_duplicate=s >= threshold)
        for i, s in shown if i in bugs
    ]
    return DuplicateCheckOut(available=True, method=result.method, candidates=candidates,
                             elapsed_ms=round((time.perf_counter() - started) * 1000, 1))


# ---- submission and the triage pipeline (FR-05, FR-10, FR-11) --------------------
@router.post("/projects/{project_id}/bugs", response_model=BugCreatedOut, status_code=201, summary="Submit a bug report")
def create_bug(project_id: int, data: BugIn, background: BackgroundTasks, user: User = Depends(get_current_user),
               db: Session = DbSession):
    project, role = require_project(db, user, project_id)
    if project.archived:
        raise HTTPException(409, "This project is archived and does not accept new bug reports")
    bug = Bug(
        project_id=project_id, title=data.title, description=data.description,
        steps_to_reproduce=data.steps_to_reproduce, environment=data.environment, component=data.component or None,
        reporter_id=user.id, keywords=extract_keywords(f"{data.title}\n{data.description}"),
    )
    db.add(bug)
    db.flush()
    audit.record(db, user.id, "created", bug_id=bug.id, project_id=project_id, new=bug.title)
    notifications.watch(db, bug.id, user.id)
    # The report is durable before any model runs: an AI failure can never lose it (NFR-10).
    db.commit()

    bug_id = bug.id
    timings: dict[str, float] = {}
    labels = settings_service.severity_labels(db)
    ai_ok = engine.available
    query_vector = None
    if ai_ok:
        started = time.perf_counter()
        try:
            result = engine.duplicates.check(db, project_id, bug.title, bug.description,
                                             candidates=get_settings().duplicate_candidates, top_k=1, exclude_id=bug.id,
                                             fusion=get_settings().duplicate_fusion)
            query_vector = result.query_vector
            if result.candidates:
                original_id, score = result.candidates[0]
                bug.duplicate_score = round(score, 4)
                if score >= settings_service.duplicate_threshold(db):
                    bug.probable_duplicate_of_id = original_id
                    audit.record(db, None, "duplicate_flagged", bug_id=bug.id, project_id=project_id,
                                 new=f"probable duplicate of #{original_id} ({score:.0%})")
        except Exception:
            db.rollback()  # before touching the bug again: the session is unusable until then
            log.exception("Duplicate stage failed for bug %s", bug_id)
            ai_ok = False
        timings["duplicate_ms"] = round((time.perf_counter() - started) * 1000, 1)

        started = time.perf_counter()
        try:
            label, confidence, source = engine.severity.predict(bug.title, bug.description)
            bug.severity = map_canonical(label, labels)
            bug.severity_confidence = round(confidence, 3)
            bug.severity_source = source
            audit.record(db, None, "severity_classified", bug_id=bug.id, project_id=project_id, field="severity",
                         new=f"{bug.severity} ({source})")
        except Exception:
            db.rollback()
            log.exception("Severity stage failed for bug %s", bug_id)
            bug.severity, bug.severity_source = SEVERITY_UNCLASSIFIED, "none"
            ai_ok = False
        timings["severity_ms"] = round((time.perf_counter() - started) * 1000, 1)

        try:
            engine.duplicates.store_embedding(db, bug, query_vector)
        except Exception:
            db.rollback()
            log.exception("Could not store embedding for bug %s", bug_id)

    outbox = Outbox()
    if notifications.is_critical(bug.severity, labels):
        notifications.notify_critical(db, outbox, bug, user.id)
    db.commit()
    background.add_task(notifications.deliver, outbox)
    return BugCreatedOut(bug=bug_out(db, bug, user, role), ai_available=ai_ok,
                         notice=None if ai_ok else DEGRADED_NOTICE, timings_ms=timings)


# ---- search and filtering (FR-07) ---------------------------------------------
@router.get("/projects/{project_id}/bugs", response_model=BugPage, summary="Search and filter bugs in a project")
def list_bugs(
    project_id: int,
    q: str | None = Query(None, max_length=200, description="Keyword search over title and description; '#12' or '12' also matches the id"),
    status: list[str] | None = Query(None),
    severity: list[str] | None = Query(None),
    assignee_id: int | None = Query(None, description="Use 0 for unassigned"),
    reporter_id: int | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = DbSession,
):
    require_project(db, user, project_id)
    conditions = [Bug.project_id == project_id]
    if q and q.strip():
        q = q.strip()
        text_match = _fts_condition(q) if db.bind.dialect.name == "postgresql" else or_(
            Bug.title.ilike(_like(q), escape="\\"), Bug.description.ilike(_like(q), escape="\\"))
        number = re.fullmatch(r"#?(\d{1,9})", q)
        conditions.append(or_(text_match, Bug.id == int(number.group(1))) if number else text_match)
    if status:
        conditions.append(Bug.status.in_(status))
    if severity:
        conditions.append(Bug.severity.in_(severity))
    if assignee_id is not None:
        conditions.append(Bug.assignee_id.is_(None) if assignee_id == 0 else Bug.assignee_id == assignee_id)
    if reporter_id is not None:
        conditions.append(Bug.reporter_id == reporter_id)
    if created_from:
        conditions.append(Bug.created_at >= datetime.combine(created_from, dtime.min, timezone.utc))
    if created_to:
        conditions.append(Bug.created_at <= datetime.combine(created_to, dtime.max, timezone.utc))
    total = db.scalar(select(func.count(Bug.id)).where(*conditions)) or 0
    items = db.scalars(
        select(Bug).where(*conditions).options(selectinload(Bug.reporter), selectinload(Bug.assignee))
        .order_by(Bug.created_at.desc(), Bug.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return BugPage(items=[BugListItem.model_validate(b) for b in items], total=total, page=page, page_size=page_size)


@router.get("/bugs/{bug_id}", response_model=BugOut, summary="Bug details")
def get_bug(bug_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    bug, role = require_bug(db, user, bug_id)
    return bug_out(db, bug, user, role)


@router.patch("/bugs/{bug_id}", response_model=BugOut, summary="Edit the fields of a bug report")
def update_bug(bug_id: int, data: BugUpdateIn, user: User = Depends(get_current_user), db: Session = DbSession):
    bug, role = require_bug(db, user, bug_id)
    if not (at_least(role, ROLE_DEVELOPER) or bug.reporter_id == user.id):
        raise FORBIDDEN
    text_changed = False
    for name, value in data.model_dump(exclude_unset=True).items():
        if name == "component":
            value = value or None
        elif value is None:
            continue
        if getattr(bug, name) != value:
            audit.record(db, user.id, "field_edited", bug_id=bug.id, project_id=bug.project_id, field=name,
                         old=getattr(bug, name), new=value)
            setattr(bug, name, value)
            text_changed = text_changed or name in ("title", "description")
    if text_changed:
        bug.keywords = extract_keywords(bug.text)
        engine.duplicates.invalidate(bug.project_id)
        if engine.available:
            try:
                engine.duplicates.store_embedding(db, bug)
            except Exception:
                log.exception("Could not refresh embedding for bug %s", bug.id)
    db.commit()
    return bug_out(db, bug, user, role)


# ---- status lifecycle (FR-06, FR-22) --------------------------------------------
@router.post("/bugs/{bug_id}/status", response_model=BugOut, summary="Move a bug to its next status")
def change_status(bug_id: int, data: StatusIn, background: BackgroundTasks, user: User = Depends(get_current_user),
                  db: Session = DbSession):
    bug, role = require_bug(db, user, bug_id)
    if data.status not in allowed_transitions(bug, user, role):
        raise HTTPException(409, f"A bug in status '{bug.status}' cannot be moved to '{data.status}' by your role")
    old, now = bug.status, utcnow()
    bug.status = data.status
    if data.status == STATUS_RESOLVED:
        bug.resolved_at = now
    elif data.status == STATUS_IN_PROGRESS and old == STATUS_RESOLVED:
        bug.resolved_at = None
    elif data.status == STATUS_VERIFIED:
        bug.verified_at = now
    elif data.status == STATUS_CLOSED:
        bug.closed_at = now
    audit.record(db, user.id, "status_changed", bug_id=bug.id, project_id=bug.project_id, field="status", old=old, new=data.status)
    outbox = Outbox()
    notifications.notify(
        db, outbox, "status_change", [bug.reporter, bug.assignee, *notifications.watchers(db, bug)],
        f"{user.display_name} moved bug #{bug.id} from {old} to {data.status}", bug, user.id)
    db.commit()
    background.add_task(notifications.deliver, outbox)
    if data.status == STATUS_VERIFIED:
        # Profile enrichment runs after the response is returned (Section 3.5.4).
        background.add_task(profiles.enrich_task, bug.id)
    return bug_out(db, bug, user, role)


# ---- severity override (FR-11) ---------------------------------------------------
@router.post("/bugs/{bug_id}/severity", response_model=BugOut, summary="Override the severity assigned by the classifier")
def override_severity(bug_id: int, data: SeverityIn, background: BackgroundTasks, user: User = Depends(get_current_user),
                      db: Session = DbSession):
    bug, role = require_bug(db, user, bug_id, ROLE_DEVELOPER)
    labels = settings_service.severity_labels(db)
    if data.severity not in labels:
        raise HTTPException(422, f"Severity must be one of: {', '.join(labels)}")
    outbox = Outbox()
    if data.severity != bug.severity:
        audit.record(db, user.id, "severity_override", bug_id=bug.id, project_id=bug.project_id, field="severity",
                     old=f"{bug.severity} ({bug.severity_source})", new=f"{data.severity} (manual)")
        bug.severity, bug.severity_source, bug.severity_confidence = data.severity, "manual", None
        if notifications.is_critical(bug.severity, labels):
            notifications.notify_critical(db, outbox, bug, user.id)
    db.commit()
    background.add_task(notifications.deliver, outbox)
    return bug_out(db, bug, user, role)


# ---- assignment (FR-12, FR-23) ---------------------------------------------------
@router.get("/bugs/{bug_id}/recommendations", response_model=RecommendationsOut,
            summary="Top three recommended developers, with the reason for each")
def recommendations(bug_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    bug, _ = require_bug(db, user, bug_id, ROLE_DEVELOPER)
    started = time.perf_counter()
    cold = None
    try:
        if not engine.available:
            raise RuntimeError("AI engine unavailable")
        result = assignment.recommend(db, bug)
        db.commit()  # profiles created on first sight of a developer
    except Exception:
        log.exception("Recommendation failed for bug %s", bug_id)
        db.rollback()
        from ..services import coldstart

        cold = coldstart.to_out(coldstart.get_state(db, bug.project_id))
        db.commit()
        result = RecommendationsOut(mode="unavailable", message="Assignment recommendations are temporarily unavailable. Assign manually.",
                                    recommendations=[], excluded=[], cold_start=cold, elapsed_ms=0.0)
    result.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return result


@router.post("/bugs/{bug_id}/assign", response_model=BugOut, summary="Assign the bug to a project member (Project Manager decision)")
def assign(bug_id: int, data: AssignIn, background: BackgroundTasks, user: User = Depends(get_current_user),
           db: Session = DbSession):
    bug, role = require_bug(db, user, bug_id, ROLE_MANAGER)
    previous = bug.assignee
    outbox = Outbox()
    if data.assignee_id is None:
        if previous is not None:
            audit.record(db, user.id, "unassigned", bug_id=bug.id, project_id=bug.project_id, field="assignee", old=previous.display_name)
            bug.assignee_id, bug.assigned_at = None, None
    elif previous is None or previous.id != data.assignee_id:
        member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == bug.project_id, ProjectMember.user_id == data.assignee_id))
        if member is None or not member.user.is_active:
            raise HTTPException(422, "The assignee must be an active member of this project")
        target = member.user
        profile = db.scalar(select(DeveloperProfile).where(DeveloperProfile.user_id == target.id))
        if profile is not None and not data.override_exclusion:
            open_bugs = profiles.open_counts(db, [target.id])[target.id]
            if profile.availability == UNAVAILABLE:
                raise HTTPException(409, f"{target.display_name} is marked Unavailable. Confirm the override to assign anyway.")
            if open_bugs >= profile.max_capacity and bug.status in ACTIVE_STATUSES:
                raise HTTPException(409, f"{target.display_name} is at capacity ({open_bugs} of {profile.max_capacity} open bugs). "
                                         "Confirm the override to assign anyway.")
        audit.record(db, user.id, "assigned", bug_id=bug.id, project_id=bug.project_id, field="assignee",
                     old=previous.display_name if previous else None,
                     new=target.display_name + (" (exclusion overridden)" if data.override_exclusion else ""))
        if previous is not None:
            bug.reassignment_count += 1
        bug.assignee_id, bug.assigned_at = target.id, utcnow()
        notifications.watch(db, bug.id, target.id)
        notifications.notify(db, outbox, "assignment", [target], f"{user.display_name} assigned bug #{bug.id} to you: {bug.title}", bug, user.id)
    db.commit()
    db.refresh(bug)
    background.add_task(notifications.deliver, outbox)
    return bug_out(db, bug, user, role)


@router.post("/bugs/{bug_id}/duplicate", response_model=BugOut, summary="Confirm or clear a duplicate link")
def mark_duplicate(bug_id: int, data: DuplicateIn, user: User = Depends(get_current_user), db: Session = DbSession):
    bug, role = require_bug(db, user, bug_id, ROLE_DEVELOPER)
    if data.duplicate_of_id is not None:
        original = db.get(Bug, data.duplicate_of_id)
        if original is None or original.project_id != bug.project_id or original.id == bug.id:
            raise HTTPException(422, "The original must be a different bug in the same project")
        if original.duplicate_of_id == bug.id:
            raise HTTPException(422, "These two bugs would point at each other")
    if data.duplicate_of_id != bug.duplicate_of_id:
        audit.record(db, user.id, "duplicate_confirmed" if data.duplicate_of_id else "duplicate_cleared", bug_id=bug.id,
                     project_id=bug.project_id, field="duplicate_of",
                     old=f"#{bug.duplicate_of_id}" if bug.duplicate_of_id else None,
                     new=f"#{data.duplicate_of_id}" if data.duplicate_of_id else None)
        bug.duplicate_of_id = data.duplicate_of_id
        if data.duplicate_of_id is None:
            bug.probable_duplicate_of_id = None
    db.commit()
    return bug_out(db, bug, user, role)


# ---- comments (FR-09) -----------------------------------------------------------
@router.get("/bugs/{bug_id}/comments", response_model=list[CommentOut], summary="Comments in chronological order")
def list_comments(bug_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    require_bug(db, user, bug_id)
    return list(db.scalars(select(Comment).where(Comment.bug_id == bug_id).options(selectinload(Comment.author))
                           .order_by(Comment.created_at, Comment.id)))


@router.post("/bugs/{bug_id}/comments", response_model=CommentOut, status_code=201, summary="Add a plain-text comment")
def add_comment(bug_id: int, data: CommentIn, background: BackgroundTasks, user: User = Depends(get_current_user),
                db: Session = DbSession):
    bug, _ = require_bug(db, user, bug_id)
    comment = Comment(bug_id=bug.id, author_id=user.id, body=data.body)
    db.add(comment)
    audit.record(db, user.id, "comment_added", bug_id=bug.id, project_id=bug.project_id, new=data.body[:200])
    outbox = Outbox()
    notifications.notify(db, outbox, "comment", notifications.watchers(db, bug),
                         f"{user.display_name} commented on bug #{bug.id}: {data.body[:140]}", bug, user.id)
    notifications.watch(db, bug.id, user.id)
    db.commit()
    db.refresh(comment)
    background.add_task(notifications.deliver, outbox)
    return comment


@router.put("/bugs/{bug_id}/watch", response_model=Message, summary="Watch a bug")
def watch_bug(bug_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    bug, _ = require_bug(db, user, bug_id)
    notifications.watch(db, bug.id, user.id)
    db.commit()
    return Message(message="Watching")


@router.delete("/bugs/{bug_id}/watch", response_model=Message, summary="Stop watching a bug")
def unwatch_bug(bug_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    require_bug(db, user, bug_id)
    for row in db.scalars(select(BugWatcher).where(BugWatcher.bug_id == bug_id, BugWatcher.user_id == user.id)):
        db.delete(row)
    db.commit()
    return Message(message="Not watching")


@router.get("/bugs/{bug_id}/audit", response_model=list[AuditOut], summary="Audit trail of a bug (FR-08)")
def bug_audit(bug_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    require_bug(db, user, bug_id)
    return list(db.scalars(select(AuditLog).where(AuditLog.bug_id == bug_id).options(selectinload(AuditLog.actor))
                           .order_by(AuditLog.created_at, AuditLog.id)))


# ---- attachments (FR-05, FR-14, NFR-07) -------------------------------------------
_IMAGE_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"), ".bmp": (b"BM",), ".webp": (b"RIFF",),
}
_TEXT_TYPES = {".txt": "text/plain", ".log": "text/plain", ".json": "application/json", ".xml": "application/xml",
               ".csv": "text/csv", ".md": "text/markdown"}
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                ".bmp": "image/bmp", ".webp": "image/webp"}
_EXECUTABLE_SIGNATURES = (b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe", b"#!", b"PK\x03\x04")
_EICAR = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE"


def scan_upload(suffix: str, content: bytes) -> str:
    """Type check and content scan before storage. Returns the content type or raises 415.

    The declared extension must be on the allow-list and the bytes must actually
    be that type; executables, archives and the EICAR test signature are refused.
    """
    if _EICAR in content[:4096]:
        raise HTTPException(415, "The file was rejected by the content scan")
    if suffix in _IMAGE_TYPES:
        if not content.startswith(_IMAGE_SIGNATURES[suffix]) or (suffix == ".webp" and content[8:12] != b"WEBP"):
            raise HTTPException(415, "The file content does not match its image type")
        return _IMAGE_TYPES[suffix]
    if suffix in _TEXT_TYPES:
        if content.startswith(_EXECUTABLE_SIGNATURES) or b"\x00" in content:
            raise HTTPException(415, "Log and text attachments must be plain text")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                content.decode("cp1252")
            except UnicodeDecodeError:
                raise HTTPException(415, "Log and text attachments must be plain text") from None
        return _TEXT_TYPES[suffix]
    raise HTTPException(415, "Only screenshots (png, jpg, gif, bmp, webp) and text or log files (txt, log, json, xml, csv, md) are accepted")


def _safe_filename(name: str | None) -> str:
    name = Path((name or "attachment").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .") or "attachment"
    return name[:255]


@router.post("/bugs/{bug_id}/attachments", response_model=AttachmentOut, status_code=201,
             summary="Attach a screenshot or log file (10 MB maximum)")
def upload_attachment(bug_id: int, file: UploadFile, user: User = Depends(get_current_user), db: Session = DbSession):
    bug, _ = require_bug(db, user, bug_id)
    settings = get_settings()
    content = file.file.read(settings.max_attachment_bytes + 1)
    if len(content) > settings.max_attachment_bytes:
        raise HTTPException(413, "Attachments are limited to 10 MB each")
    if not content:
        raise HTTPException(422, "The file is empty")
    filename = _safe_filename(file.filename)
    content_type = scan_upload(Path(filename).suffix.lower(), content)
    # Stored under a random name: nothing the user supplies ever becomes part of a path.
    stored_name = uuid.uuid4().hex
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    (settings.upload_dir / stored_name).write_bytes(content)
    attachment = Attachment(bug_id=bug.id, uploader_id=user.id, filename=filename, stored_name=stored_name,
                            content_type=content_type, size=len(content))
    db.add(attachment)
    audit.record(db, user.id, "attachment_added", bug_id=bug.id, project_id=bug.project_id, new=filename)
    db.commit()
    db.refresh(attachment)
    return attachment


@router.get("/bugs/{bug_id}/attachments", response_model=list[AttachmentOut], summary="Attachments of a bug")
def list_attachments(bug_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    require_bug(db, user, bug_id)
    return list(db.scalars(select(Attachment).where(Attachment.bug_id == bug_id).order_by(Attachment.id)))


@router.get("/attachments/{attachment_id}", summary="Download an attachment", response_class=FileResponse,
            responses={200: {"content": {"application/octet-stream": {}}, "description": "The file"}})
def download_attachment(attachment_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise FORBIDDEN
    bug = db.get(Bug, attachment.bug_id)
    if bug is None or project_role(db, user, bug.project_id) is None:
        raise FORBIDDEN
    path = get_settings().upload_dir / attachment.stored_name
    if not path.is_file():
        raise HTTPException(410, "The stored file is no longer available")
    return FileResponse(path, media_type=attachment.content_type, filename=attachment.filename,
                        content_disposition_type="attachment", headers={"X-Content-Type-Options": "nosniff"})
