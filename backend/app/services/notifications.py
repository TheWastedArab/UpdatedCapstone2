"""Notification pipeline: in-app rows now, email and push after the response (FR-16, FR-17)."""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SEVERITY_UNCLASSIFIED, Bug, BugWatcher, Notification, ProjectMember, User
from . import email, ntfy


@dataclass
class Outbox:
    """Deliveries collected during a request and sent by a background task after commit."""

    emails: list[tuple[str, str, str]] = field(default_factory=list)
    pushes: list[tuple[str, str, str]] = field(default_factory=list)


def deliver(outbox: Outbox) -> None:
    for to, subject, body in outbox.emails:
        email.send_email(to, subject, body)
    for topic, title, message in outbox.pushes:
        ntfy.publish(topic, title, message)


def notify(
    db: Session, outbox: Outbox, type_: str, recipients: list[User], message: str, bug: Bug,
    actor_id: int | None = None,
) -> None:
    seen: set[int] = set()
    for user in recipients:
        if user is None or user.id in seen or user.id == actor_id or not user.is_active:
            continue
        seen.add(user.id)
        db.add(Notification(user_id=user.id, type=type_, message=message[:500], bug_id=bug.id, project_id=bug.project_id))
        if (user.notification_prefs or {}).get(type_, True):
            outbox.emails.append((user.email, f"[Bug #{bug.id}] {message[:120]}", f"{message}\n\nBug #{bug.id}: {bug.title}"))


def watchers(db: Session, bug: Bug) -> list[User]:
    return list(db.scalars(select(User).join(BugWatcher, BugWatcher.user_id == User.id).where(BugWatcher.bug_id == bug.id)))


def watch(db: Session, bug_id: int, user_id: int | None) -> None:
    if user_id is None:
        return
    exists = db.scalar(select(BugWatcher.id).where(BugWatcher.bug_id == bug_id, BugWatcher.user_id == user_id))
    if exists is None:
        db.add(BugWatcher(bug_id=bug_id, user_id=user_id))


def project_members(db: Session, project_id: int) -> list[User]:
    return list(db.scalars(select(User).join(ProjectMember, ProjectMember.user_id == User.id).where(ProjectMember.project_id == project_id)))


def is_critical(severity: str, labels: list[str]) -> bool:
    """The most severe configured label is the one that triggers the urgent alert."""
    return severity != SEVERITY_UNCLASSIFIED and bool(labels) and severity == labels[-1]


def notify_critical(db: Session, outbox: Outbox, bug: Bug, actor_id: int | None) -> None:
    message = f"{bug.severity} bug reported in {bug.project.name}: {bug.title}"
    notify(db, outbox, "critical_bug", project_members(db, bug.project_id), message, bug, actor_id)
    outbox.pushes.append((ntfy.topic_for_project(bug.project_id), f"{bug.severity} bug #{bug.id}", message))
