"""Audit trail writer (FR-08). Every change to a bug, membership or profile passes through here."""
from sqlalchemy.orm import Session

from ..models import AuditLog


def record(
    db: Session,
    actor_id: int | None,
    action: str,
    *,
    bug_id: int | None = None,
    profile_id: int | None = None,
    project_id: int | None = None,
    field: str | None = None,
    old=None,
    new=None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        bug_id=bug_id,
        profile_id=profile_id,
        project_id=project_id,
        field=field,
        old_value=None if old is None else str(old),
        new_value=None if new is None else str(new),
    )
    db.add(entry)
    return entry
