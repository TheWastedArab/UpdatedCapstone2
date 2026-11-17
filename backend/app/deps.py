"""Request dependencies: authentication and API-layer authorisation (FR-03, NFR-06)."""
from datetime import timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import DbSession, utcnow
from .models import (
    ROLE_ADMIN, ROLE_DEVELOPER, ROLE_MANAGER, ROLE_RANK, ROLE_REPORTER, Bug, Project, ProjectMember, User,
    UserSession,
)
from .security import decode_token

bearer = HTTPBearer(auto_error=False)

UNAUTHENTICATED = HTTPException(
    status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"}
)
# One response for "does not exist" and "not yours", so existence is never disclosed (NFR-06).
FORBIDDEN = HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to this resource")


class CurrentUser:
    def __init__(self, user: User, session: UserSession):
        self.user = user
        self.session = session


def get_auth(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = DbSession
) -> CurrentUser:
    if creds is None:
        raise UNAUTHENTICATED
    payload = decode_token(creds.credentials)
    if not payload:
        raise UNAUTHENTICATED
    session = db.get(UserSession, payload.get("jti", ""))
    if session is None or session.revoked or str(session.user_id) != payload.get("sub"):
        raise UNAUTHENTICATED
    now = utcnow()
    if now - session.last_seen_at > timedelta(hours=get_settings().session_idle_hours):
        session.revoked = True
        db.commit()
        raise UNAUTHENTICATED
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise UNAUTHENTICATED
    if now - session.last_seen_at > timedelta(seconds=60):  # avoid a write on every request
        session.last_seen_at = now
    # Always end the transaction here: it hands the connection back to the pool, so a request
    # waiting its turn for a handler thread does not sit on a connection while it waits.
    db.commit()
    return CurrentUser(user, session)


def get_current_user(auth: CurrentUser = Depends(get_auth)) -> User:
    return auth.user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise FORBIDDEN
    return user


def project_role(db: Session, user: User, project_id: int) -> str | None:
    """Effective role of the user in a project, or None when not a member."""
    if user.role == ROLE_ADMIN:
        return ROLE_MANAGER
    return db.scalar(
        select(ProjectMember.role).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user.id)
    )


def require_project(db: Session, user: User, project_id: int, min_role: str = ROLE_REPORTER) -> tuple[Project, str]:
    project = db.get(Project, project_id)
    role = project_role(db, user, project_id) if project else None
    if project is None or role is None or ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise FORBIDDEN
    return project, role


def require_bug(db: Session, user: User, bug_id: int, min_role: str = ROLE_REPORTER) -> tuple[Bug, str]:
    bug = db.get(Bug, bug_id)
    if bug is None:
        raise FORBIDDEN
    _, role = require_project(db, user, bug.project_id, min_role)
    return bug, role


def at_least(role: str, minimum: str) -> bool:
    return ROLE_RANK[role] >= ROLE_RANK[minimum]


__all__ = [
    "CurrentUser", "FORBIDDEN", "ROLE_DEVELOPER", "ROLE_MANAGER", "ROLE_REPORTER", "at_least", "get_auth",
    "get_current_user", "project_role", "require_admin", "require_bug", "require_project",
]
