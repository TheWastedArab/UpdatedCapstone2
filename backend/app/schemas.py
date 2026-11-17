"""Request and response schemas. All user input is validated here (NFR-07)."""
import re
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, field_validator

from .models import AVAILABILITY_STATES, NOTIFICATION_TYPES, PROJECT_ROLES, STATUSES, SYSTEM_ROLES
from .security import password_problems

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(value):
    """Strip control characters and surrounding whitespace.

    Markup is deliberately not stripped: bug reports legitimately contain
    snippets such as List<String>. Output is escaped by the client instead.
    """
    if not isinstance(value, str):
        return value
    return _CONTROL.sub("", value).strip()


# Cleaned before the length constraints run, so whitespace cannot satisfy a minimum length.
Text = Annotated[str, BeforeValidator(clean)]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth and users ---------------------------------------------------------
def _check_password(value: str) -> str:
    problems = password_problems(value)
    if problems:
        raise ValueError("Password needs " + ", ".join(problems))
    return value


Password = Annotated[str, Field(max_length=128), AfterValidator(_check_password)]


class RegisterIn(BaseModel):
    email: EmailStr
    password: Password
    display_name: Text = Field(min_length=1, max_length=100)


class RegisterOut(BaseModel):
    message: str
    verification_code: str | None = Field(None, description="Only returned in development mode")


class VerifyEmailIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=16)


class EmailIn(BaseModel):
    email: EmailStr


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class UserOut(ORM):
    id: int
    email: str
    display_name: str
    role: str
    is_active: bool
    email_verified: bool
    pending_email: str | None = None
    notification_prefs: dict[str, bool]
    created_at: datetime


class UserBrief(ORM):
    id: int
    display_name: str
    email: str


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ProfileUpdateIn(BaseModel):
    display_name: Text | None = Field(None, min_length=1, max_length=100)
    email: EmailStr | None = None
    notification_prefs: dict[str, bool] | None = None

    @field_validator("notification_prefs")
    @classmethod
    def _known_types(cls, v):
        if v is not None and set(v) - set(NOTIFICATION_TYPES):
            raise ValueError(f"Unknown notification type; allowed: {', '.join(NOTIFICATION_TYPES)}")
        return v


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: Password


class AdminUserUpdateIn(BaseModel):
    role: str | None = None
    is_active: bool | None = None

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v is not None and v not in SYSTEM_ROLES:
            raise ValueError(f"Role must be one of {', '.join(SYSTEM_ROLES)}")
        return v


# ---- projects ---------------------------------------------------------------
class ProjectIn(BaseModel):
    name: Text = Field(min_length=2, max_length=120)
    description: Text = Field("", max_length=2000)


class ProjectUpdateIn(BaseModel):
    name: Text | None = Field(None, min_length=2, max_length=120)
    description: Text | None = Field(None, max_length=2000)
    archived: bool | None = None


class ProjectOut(ORM):
    id: int
    name: str
    description: str
    archived: bool
    created_at: datetime
    my_role: str | None = None


def _project_role(value: str) -> str:
    if value not in PROJECT_ROLES:
        raise ValueError(f"Role must be one of {', '.join(PROJECT_ROLES)}")
    return value


ProjectRole = Annotated[str, AfterValidator(_project_role)]


class MemberIn(BaseModel):
    email: EmailStr
    role: ProjectRole = "reporter"


class MemberRoleIn(BaseModel):
    role: ProjectRole


class MemberOut(BaseModel):
    user_id: int
    display_name: str
    email: str
    role: str
    added_at: datetime


# ---- bugs -------------------------------------------------------------------
class BugIn(BaseModel):
    title: Text = Field(min_length=5, max_length=200)
    description: Text = Field(min_length=20, max_length=20000)
    steps_to_reproduce: Text = Field("", max_length=10000)
    environment: Text = Field("", max_length=500)
    component: Text | None = Field(None, max_length=100)


class BugUpdateIn(BaseModel):
    title: Text | None = Field(None, min_length=5, max_length=200)
    description: Text | None = Field(None, min_length=20, max_length=20000)
    steps_to_reproduce: Text | None = Field(None, max_length=10000)
    environment: Text | None = Field(None, max_length=500)
    component: Text | None = Field(None, max_length=100)


class BugListItem(ORM):
    id: int
    project_id: int
    title: str
    status: str
    severity: str
    component: str | None
    reporter: UserBrief | None
    assignee: UserBrief | None
    duplicate_of_id: int | None
    probable_duplicate_of_id: int | None
    created_at: datetime
    updated_at: datetime


class BugOut(BugListItem):
    description: str
    steps_to_reproduce: str
    environment: str
    severity_source: str
    severity_confidence: float | None
    duplicate_score: float | None
    keywords: list[str]
    reassignment_count: int
    assigned_at: datetime | None
    resolved_at: datetime | None
    verified_at: datetime | None
    closed_at: datetime | None
    watching: bool = False
    allowed_transitions: list[str] = []
    my_role: str | None = None


class BugCreatedOut(BaseModel):
    bug: BugOut
    ai_available: bool
    notice: str | None = Field(None, description="Set when the AI service was degraded (NFR-10)")
    timings_ms: dict[str, float]


class BugPage(BaseModel):
    items: list[BugListItem]
    total: int
    page: int
    page_size: int


class StatusIn(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v not in STATUSES:
            raise ValueError(f"Status must be one of {', '.join(STATUSES)}")
        return v


class SeverityIn(BaseModel):
    severity: Text = Field(min_length=1, max_length=30)


class AssignIn(BaseModel):
    assignee_id: int | None = Field(description="Project member to assign; null to unassign")
    override_exclusion: bool = Field(False, description="Assign even if the developer is at capacity or unavailable (FR-23)")


class DuplicateIn(BaseModel):
    duplicate_of_id: int | None = Field(description="Original bug in the same project; null clears the link")


class CommentIn(BaseModel):
    body: Text = Field(min_length=1, max_length=10000)


class CommentOut(ORM):
    id: int
    bug_id: int
    author: UserBrief | None
    body: str
    created_at: datetime


class AttachmentOut(ORM):
    id: int
    bug_id: int
    filename: str
    content_type: str
    size: int
    created_at: datetime


class AuditOut(ORM):
    id: int
    bug_id: int | None
    profile_id: int | None
    project_id: int | None
    actor: UserBrief | None
    action: str
    field: str | None
    old_value: str | None
    new_value: str | None
    created_at: datetime


# ---- AI ---------------------------------------------------------------------
class DuplicateCheckIn(BaseModel):
    title: Text = Field("", max_length=200)
    description: Text = Field(min_length=20, max_length=20000)


class DuplicateCandidate(BaseModel):
    bug_id: int
    title: str
    status: str
    severity: str
    match_percent: int
    probable_duplicate: bool


class DuplicateCheckOut(BaseModel):
    available: bool
    method: str = Field(description="bm25+sbert, bm25+lexical or unavailable")
    candidates: list[DuplicateCandidate]
    elapsed_ms: float
    notice: str | None = None


class Recommendation(BaseModel):
    user_id: int
    display_name: str
    score: float
    reasons: list[str]
    limited_history: bool
    open_bugs: int
    capacity: int
    availability: str


class ExcludedDeveloper(BaseModel):
    user_id: int
    display_name: str
    reason: str


class ColdStartOut(BaseModel):
    project_id: int
    state: str
    cold_start: bool
    verified_count: int
    threshold: int
    remaining: int
    last_trained_at: datetime | None
    metrics: dict[str, Any] | None = None
    last_error: str | None = None


class RecommendationsOut(BaseModel):
    mode: str = Field(description="model, rule_based, manual or unavailable")
    message: str
    recommendations: list[Recommendation]
    excluded: list[ExcludedDeveloper]
    cold_start: ColdStartOut
    elapsed_ms: float


class AIStatusOut(BaseModel):
    enabled: bool
    severity: str
    duplicates: str
    assignment: str
    detail: dict[str, Any]


# ---- dashboard --------------------------------------------------------------
class WeekPoint(BaseModel):
    week_start: date
    reported: int
    resolved: int


class DashboardOut(BaseModel):
    project_id: int
    generated_at: datetime
    counts: dict[str, int]
    critical_open: int
    trend: list[WeekPoint]
    avg_resolution_hours: dict[str, float | None]
    cold_start: ColdStartOut


# ---- notifications ----------------------------------------------------------
class NotificationOut(ORM):
    id: int
    type: str
    message: str
    bug_id: int | None
    project_id: int | None
    read: bool
    created_at: datetime


class UnreadOut(BaseModel):
    unread: int


# ---- developer profiles -----------------------------------------------------
def _names(values: list[str], limit: int) -> list[str]:
    out, seen = [], set()
    for v in values:
        v = clean(v)[:limit]
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out


class ProfileIn(BaseModel):
    """Only the Administrator-declared fields are writable (FR-21)."""

    skills: list[str] | None = Field(None, max_length=50)
    components: list[str] | None = Field(None, max_length=50)
    availability: str | None = None
    max_capacity: int | None = Field(None, ge=1, le=100)

    @field_validator("skills")
    @classmethod
    def _skills(cls, v):
        return None if v is None else _names(v, 60)

    @field_validator("components")
    @classmethod
    def _components(cls, v):
        return None if v is None else _names(v, 100)

    @field_validator("availability")
    @classmethod
    def _availability(cls, v):
        if v is not None and v not in AVAILABILITY_STATES:
            raise ValueError(f"Availability must be one of {', '.join(AVAILABILITY_STATES)}")
        return v


class ProfileCreateIn(ProfileIn):
    user_id: int


class HistoryOut(ORM):
    id: int
    project_id: int
    bug_id: int | None
    component: str | None
    severity: str
    keywords: list[str]
    resolution_seconds: int
    created_at: datetime


class ProfileOut(BaseModel):
    id: int
    user: UserBrief
    active: bool
    availability: str
    max_capacity: int
    skills: list[str]
    components: list[str]
    # read-only, derived from system activity
    open_bugs: int
    resolved_count: int
    history: list[HistoryOut] = []
    updated_at: datetime


class SettingsOut(BaseModel):
    duplicate_threshold: float
    severity_labels: list[str]


class SettingsIn(BaseModel):
    duplicate_threshold: float | None = Field(None, ge=0.0, le=1.0)
    severity_labels: list[str] | None = Field(None, min_length=2, max_length=8)

    @field_validator("severity_labels")
    @classmethod
    def _labels(cls, v):
        if v is None:
            return v
        v = _names(v, 30)
        if len(v) < 2:
            raise ValueError("At least two distinct severity labels are required")
        return v


class Message(BaseModel):
    message: str


class MetaOut(BaseModel):
    severity_labels: list[str]
    statuses: list[str]
