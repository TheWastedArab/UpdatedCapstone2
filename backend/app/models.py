"""Relational schema (Figure 3.4 of the report)."""
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON, Boolean, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

from .database import Base, UTCDateTime, utcnow

# ---- vocabulary -----------------------------------------------------------
ROLE_REPORTER = "reporter"
ROLE_DEVELOPER = "developer"
ROLE_MANAGER = "project_manager"
ROLE_ADMIN = "administrator"
SYSTEM_ROLES = (ROLE_REPORTER, ROLE_DEVELOPER, ROLE_MANAGER, ROLE_ADMIN)
PROJECT_ROLES = (ROLE_REPORTER, ROLE_DEVELOPER, ROLE_MANAGER)
ROLE_RANK = {ROLE_REPORTER: 1, ROLE_DEVELOPER: 2, ROLE_MANAGER: 3, ROLE_ADMIN: 3}

STATUS_OPEN = "Open"
STATUS_IN_PROGRESS = "In Progress"
STATUS_RESOLVED = "Resolved"
STATUS_VERIFIED = "Verified"
STATUS_CLOSED = "Closed"
STATUSES = (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_VERIFIED, STATUS_CLOSED)
ACTIVE_STATUSES = (STATUS_OPEN, STATUS_IN_PROGRESS)

SEVERITY_UNCLASSIFIED = "Unclassified"

AVAILABLE = "Available"
BUSY = "Busy"
UNAVAILABLE = "Unavailable"
AVAILABILITY_STATES = (AVAILABLE, BUSY, UNAVAILABLE)

MODEL_COLD = "cold_start"
MODEL_TRAINING = "training"
MODEL_ACTIVE = "active"

NOTIFICATION_TYPES = ("assignment", "status_change", "comment", "critical_bug")


def default_prefs() -> dict:
    return {t: True for t in NOTIFICATION_TYPES}


# ---- users ----------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default=ROLE_REPORTER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    verification_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    notification_prefs: Mapped[dict] = mapped_column(JSON, default=default_prefs)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    profile: Mapped["DeveloperProfile | None"] = relationship(back_populates="user", uselist=False)


class UserSession(Base):
    """Server-side session row behind each JWT: idle expiry and logout (FR-02)."""

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# ---- projects -------------------------------------------------------------
class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    members: Mapped[list["ProjectMember"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default=ROLE_REPORTER)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped[User] = relationship()


# ---- bugs -----------------------------------------------------------------
class Bug(Base):
    __tablename__ = "bugs"
    __table_args__ = (
        Index("ix_bugs_project_status", "project_id", "status"),
        Index("ix_bugs_project_created", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    steps_to_reproduce: Mapped[str] = mapped_column(Text, default="")
    environment: Mapped[str] = mapped_column(String(500), default="")
    component: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default=STATUS_OPEN)
    severity: Mapped[str] = mapped_column(String(30), default=SEVERITY_UNCLASSIFIED, index=True)
    severity_source: Mapped[str] = mapped_column(String(10), default="none")  # model | rules | manual | none
    severity_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reporter_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    reassignment_count: Mapped[int] = mapped_column(Integer, default=0)
    # Duplicate link (FR-10): a duplicate stays in the database and points at its original.
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("bugs.id", ondelete="SET NULL"), nullable=True)
    probable_duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("bugs.id", ondelete="SET NULL"), nullable=True)
    duplicate_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    assigned_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    reporter: Mapped[User | None] = relationship(foreign_keys=[reporter_id])
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    project: Mapped[Project] = relationship()

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.description}"

    @property
    def is_duplicate_flagged(self) -> bool:
        return self.duplicate_of_id is not None or self.probable_duplicate_of_id is not None


class BugEmbedding(Base):
    """Cached sentence embedding so the semantic stage only embeds the query."""

    __tablename__ = "bug_embeddings"

    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(String(100))
    vector: Mapped[bytes] = mapped_column(LargeBinary)


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    author: Mapped[User | None] = relationship()


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), index=True)
    uploader_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(64), unique=True)
    content_type: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class BugWatcher(Base):
    __tablename__ = "bug_watchers"
    __table_args__ = (UniqueConstraint("bug_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class AuditLog(Base):
    """One trail for bugs, memberships and developer profiles (FR-08, FR-19, FR-21)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    bug_id: Mapped[int | None] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), nullable=True, index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("developer_profiles.id", ondelete="CASCADE"), nullable=True, index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(40))
    field: Mapped[str | None] = mapped_column(String(60), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    actor: Mapped[User | None] = relationship()


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(String(500))
    bug_id: Mapped[int | None] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


# ---- developer profiles and model state (FR-20 to FR-29) --------------------
class DeveloperProfile(Base):
    __tablename__ = "developer_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    availability: Mapped[str] = mapped_column(String(15), default=AVAILABLE)
    max_capacity: Mapped[int] = mapped_column(Integer, default=5)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="profile")
    skills: Mapped[list["ProfileSkill"]] = relationship(cascade="all, delete-orphan", order_by="ProfileSkill.name")
    components: Mapped[list["ProfileComponent"]] = relationship(cascade="all, delete-orphan", order_by="ProfileComponent.name")
    history: Mapped[list["ResolutionHistory"]] = relationship(cascade="all, delete-orphan", order_by="ResolutionHistory.created_at")


class ProfileSkill(Base):
    __tablename__ = "profile_skills"
    __table_args__ = (UniqueConstraint("profile_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("developer_profiles.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(60))


class ProfileComponent(Base):
    __tablename__ = "profile_components"
    __table_args__ = (UniqueConstraint("profile_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("developer_profiles.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))


class ResolutionHistory(Base):
    """System-written evidence the assignment model learns from (FR-22). Never edited by hand."""

    __tablename__ = "resolution_history"
    __table_args__ = (UniqueConstraint("profile_id", "bug_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("developer_profiles.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    bug_id: Mapped[int | None] = mapped_column(ForeignKey("bugs.id", ondelete="SET NULL"), nullable=True)
    component: Mapped[str | None] = mapped_column(String(100), nullable=True)
    severity: Mapped[str] = mapped_column(String(30))
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    resolution_seconds: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ModelState(Base):
    """Per-project state of the assignment model (FR-25, FR-29)."""

    __tablename__ = "model_states"

    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    state: Mapped[str] = mapped_column(String(15), default=MODEL_COLD)
    verified_count: Mapped[int] = mapped_column(Integer, default=0)
    verified_at_last_training: Mapped[int] = mapped_column(Integer, default=0)
    last_trained_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    training_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stored in the database so any backend instance can serve it (NFR-08).
    artifact: Mapped[bytes | None] = deferred(mapped_column(LargeBinary, nullable=True))


class SystemSetting(Base):
    """Administrative settings panel values; override the environment defaults (NFR-14)."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
