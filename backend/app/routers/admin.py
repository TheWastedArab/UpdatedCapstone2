"""Administrator interface: users, developer profiles, model state and settings (FR-21, FR-29, NFR-14)."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import DbSession
from ..deps import require_admin
from ..models import (
    ROLE_ADMIN, AuditLog, DeveloperProfile, ProfileComponent, ProfileSkill, Project, User, UserSession,
)
from ..schemas import (
    AdminUserUpdateIn, AuditOut, ColdStartOut, Message, ProfileCreateIn, ProfileIn, ProfileOut, SettingsIn,
    SettingsOut, UserOut,
)
from ..services import audit, coldstart, profiles, settings_service

router = APIRouter(prefix="/admin", tags=["Administration"])


# ---- users ------------------------------------------------------------------
@router.get("/users", response_model=list[UserOut], summary="All user accounts")
def list_users(admin: User = Depends(require_admin), db: Session = DbSession):
    return list(db.scalars(select(User).order_by(User.display_name)))


@router.patch("/users/{user_id}", response_model=UserOut, summary="Change a system role or deactivate an account")
def update_user(user_id: int, data: AdminUserUpdateIn, admin: User = Depends(require_admin), db: Session = DbSession):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    losing_admin = user.role == ROLE_ADMIN and (
        (data.role is not None and data.role != ROLE_ADMIN) or data.is_active is False)
    if losing_admin:
        others = db.scalar(select(func.count(User.id)).where(User.role == ROLE_ADMIN, User.is_active.is_(True), User.id != user.id))
        if not others:
            raise HTTPException(409, "The system must keep at least one active Administrator")
    if data.role is not None and data.role != user.role:
        audit.record(db, admin.id, "user_role_changed", field=user.email, old=user.role, new=data.role)
        user.role = data.role
    if data.is_active is not None and data.is_active != user.is_active:
        audit.record(db, admin.id, "user_activated" if data.is_active else "user_deactivated", field=user.email)
        user.is_active = data.is_active
        if not data.is_active:
            for session in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
                session.revoked = True
    db.commit()
    return user


# ---- developer profiles -------------------------------------------------------
def _get_profile(db: Session, profile_id: int) -> DeveloperProfile:
    profile = db.get(DeveloperProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Developer profile not found")
    return profile


def _apply(db: Session, profile: DeveloperProfile, data: ProfileIn, admin: User) -> None:
    """Apply only the declared fields; history and counts are never writable (FR-21)."""
    if data.skills is not None:
        old = [s.name for s in profile.skills]
        if sorted(old, key=str.lower) != sorted(data.skills, key=str.lower):
            audit.record(db, admin.id, "profile_edited", profile_id=profile.id, field="skills", old=", ".join(old), new=", ".join(data.skills))
            profile.skills.clear()
            db.flush()
            profile.skills.extend(ProfileSkill(name=n) for n in data.skills)
    if data.components is not None:
        old = [c.name for c in profile.components]
        if sorted(old, key=str.lower) != sorted(data.components, key=str.lower):
            audit.record(db, admin.id, "profile_edited", profile_id=profile.id, field="components", old=", ".join(old), new=", ".join(data.components))
            profile.components.clear()
            db.flush()
            profile.components.extend(ProfileComponent(name=n) for n in data.components)
    if data.availability is not None and data.availability != profile.availability:
        audit.record(db, admin.id, "profile_edited", profile_id=profile.id, field="availability", old=profile.availability, new=data.availability)
        profile.availability = data.availability
    if data.max_capacity is not None and data.max_capacity != profile.max_capacity:
        audit.record(db, admin.id, "profile_edited", profile_id=profile.id, field="max_capacity", old=profile.max_capacity, new=data.max_capacity)
        profile.max_capacity = data.max_capacity


@router.get("/profiles", response_model=list[ProfileOut], summary="All developer profiles")
def list_profiles(admin: User = Depends(require_admin), db: Session = DbSession):
    rows = db.scalars(select(DeveloperProfile).join(User).order_by(User.display_name))
    return [profiles.to_out(db, p) for p in rows]


@router.post("/profiles", response_model=ProfileOut, status_code=201, summary="Create a developer profile for a user")
def create_profile(data: ProfileCreateIn, admin: User = Depends(require_admin), db: Session = DbSession):
    user = db.get(User, data.user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if db.scalar(select(DeveloperProfile.id).where(DeveloperProfile.user_id == user.id)) is not None:
        raise HTTPException(409, "This user already has a developer profile")
    profile = profiles.ensure_profile(db, user, admin.id)
    _apply(db, profile, data, admin)
    db.commit()
    db.refresh(profile)
    return profiles.to_out(db, profile, with_history=True)


@router.get("/profiles/{profile_id}", response_model=ProfileOut, summary="One profile with its read-only history")
def get_profile(profile_id: int, admin: User = Depends(require_admin), db: Session = DbSession):
    return profiles.to_out(db, _get_profile(db, profile_id), with_history=True)


@router.patch("/profiles/{profile_id}", response_model=ProfileOut, summary="Edit declared skills, components, availability or capacity")
def update_profile(profile_id: int, data: ProfileIn, admin: User = Depends(require_admin), db: Session = DbSession):
    profile = _get_profile(db, profile_id)
    _apply(db, profile, data, admin)
    db.commit()
    db.refresh(profile)
    return profiles.to_out(db, profile, with_history=True)


def _set_active(db: Session, profile_id: int, active: bool, admin: User) -> ProfileOut:
    profile = _get_profile(db, profile_id)
    if profile.active != active:
        audit.record(db, admin.id, "profile_activated" if active else "profile_deactivated", profile_id=profile.id)
        profile.active = active
    db.commit()
    return profiles.to_out(db, profile, with_history=True)


@router.post("/profiles/{profile_id}/deactivate", response_model=ProfileOut, summary="Deactivate a profile (excluded from recommendations)")
def deactivate_profile(profile_id: int, admin: User = Depends(require_admin), db: Session = DbSession):
    return _set_active(db, profile_id, False, admin)


@router.post("/profiles/{profile_id}/activate", response_model=ProfileOut, summary="Reactivate a profile")
def activate_profile(profile_id: int, admin: User = Depends(require_admin), db: Session = DbSession):
    return _set_active(db, profile_id, True, admin)


@router.get("/profiles/{profile_id}/audit", response_model=list[AuditOut], summary="Audit trail of a profile")
def profile_audit(profile_id: int, admin: User = Depends(require_admin), db: Session = DbSession):
    _get_profile(db, profile_id)
    return list(db.scalars(select(AuditLog).where(AuditLog.profile_id == profile_id).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(200)))


# ---- assignment model state ---------------------------------------------------
class ModelStateOut(ColdStartOut):
    project_name: str


@router.get("/model-states", response_model=list[ModelStateOut], summary="Assignment model state and last training date per project")
def model_states(admin: User = Depends(require_admin), db: Session = DbSession):
    out = []
    for project in db.scalars(select(Project).order_by(Project.name)).all():
        state = coldstart.to_out(coldstart.get_state(db, project.id))
        out.append(ModelStateOut(project_name=project.name, **state.model_dump()))
    db.commit()
    return out


@router.post("/projects/{project_id}/retrain", response_model=Message, status_code=202, summary="Retrain the assignment model of a project now")
def retrain(project_id: int, background: BackgroundTasks, admin: User = Depends(require_admin), db: Session = DbSession):
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    state = coldstart.get_state(db, project_id)
    db.commit()
    if coldstart.to_out(state).remaining > 0:
        raise HTTPException(409, f"This project is still in the cold start state ({state.verified_count} verified bugs)")
    background.add_task(coldstart.train_project, project_id, True)
    return Message(message="Training started in the background")


# ---- settings -----------------------------------------------------------------
def _settings_out(db: Session) -> SettingsOut:
    return SettingsOut(duplicate_threshold=settings_service.duplicate_threshold(db), severity_labels=settings_service.severity_labels(db))


@router.get("/settings", response_model=SettingsOut, summary="Runtime settings")
def get_runtime_settings(admin: User = Depends(require_admin), db: Session = DbSession):
    return _settings_out(db)


@router.put("/settings", response_model=SettingsOut, summary="Change the duplicate threshold or the severity labels without redeploying")
def put_runtime_settings(data: SettingsIn, admin: User = Depends(require_admin), db: Session = DbSession):
    current = _settings_out(db)
    if data.duplicate_threshold is not None and data.duplicate_threshold != current.duplicate_threshold:
        settings_service.set_value(db, "duplicate_threshold", data.duplicate_threshold, admin.id)
        audit.record(db, admin.id, "setting_changed", field="duplicate_threshold", old=current.duplicate_threshold, new=data.duplicate_threshold)
    if data.severity_labels is not None and data.severity_labels != current.severity_labels:
        settings_service.set_value(db, "severity_labels", data.severity_labels, admin.id)
        audit.record(db, admin.id, "setting_changed", field="severity_labels", old=", ".join(current.severity_labels), new=", ".join(data.severity_labels))
    db.commit()
    return _settings_out(db)
