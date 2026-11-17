"""Projects, team membership and the project dashboard (FR-15, FR-18, FR-19)."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import DbSession, utcnow
from ..deps import FORBIDDEN, get_current_user, require_project
from ..models import (
    ACTIVE_STATUSES, ROLE_ADMIN, ROLE_DEVELOPER, ROLE_MANAGER, STATUSES, Bug, DeveloperProfile, Project,
    ProfileComponent, ProjectMember, User,
)
from ..schemas import (
    AuditOut, DashboardOut, MemberIn, MemberOut, MemberRoleIn, Message, ProjectIn, ProjectOut, ProjectUpdateIn,
    WeekPoint,
)
from ..models import AuditLog
from ..services import audit, coldstart, profiles, settings_service

router = APIRouter(tags=["Projects and teams"])


def _out(project: Project, role: str | None) -> ProjectOut:
    out = ProjectOut.model_validate(project)
    out.my_role = role
    return out


@router.post("/projects", response_model=ProjectOut, status_code=201, summary="Create a project")
def create_project(data: ProjectIn, user: User = Depends(get_current_user), db: Session = DbSession):
    if user.role not in (ROLE_MANAGER, ROLE_ADMIN):
        raise FORBIDDEN
    if db.scalar(select(Project.id).where(func.lower(Project.name) == data.name.lower())) is not None:
        raise HTTPException(409, "A project with this name already exists")
    project = Project(name=data.name, description=data.description, created_by=user.id)
    project.members.append(ProjectMember(user_id=user.id, role=ROLE_MANAGER))
    db.add(project)
    db.flush()
    audit.record(db, user.id, "project_created", project_id=project.id, new=project.name)
    coldstart.get_state(db, project.id)
    db.commit()
    return _out(project, ROLE_MANAGER)


@router.get("/projects", response_model=list[ProjectOut], summary="Projects the current user belongs to")
def list_projects(user: User = Depends(get_current_user), db: Session = DbSession):
    if user.role == ROLE_ADMIN:
        return [_out(p, ROLE_MANAGER) for p in db.scalars(select(Project).order_by(Project.name))]
    rows = db.execute(
        select(Project, ProjectMember.role).join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user.id).order_by(Project.name)
    ).all()
    return [_out(p, role) for p, role in rows]


@router.get("/projects/{project_id}", response_model=ProjectOut, summary="Project details")
def get_project(project_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    project, role = require_project(db, user, project_id)
    return _out(project, role)


@router.patch("/projects/{project_id}", response_model=ProjectOut, summary="Rename, describe or archive a project")
def update_project(project_id: int, data: ProjectUpdateIn, user: User = Depends(get_current_user),
                   db: Session = DbSession):
    project, role = require_project(db, user, project_id, ROLE_MANAGER)
    if data.name is not None and data.name != project.name:
        clash = db.scalar(select(Project.id).where(func.lower(Project.name) == data.name.lower(), Project.id != project.id))
        if clash is not None:
            raise HTTPException(409, "A project with this name already exists")
        audit.record(db, user.id, "project_renamed", project_id=project.id, field="name", old=project.name, new=data.name)
        project.name = data.name
    if data.description is not None:
        project.description = data.description
    if data.archived is not None and data.archived != project.archived:
        audit.record(db, user.id, "project_archived" if data.archived else "project_restored", project_id=project.id)
        project.archived = data.archived
    db.commit()
    return _out(project, role)


# ---- membership -------------------------------------------------------------
def _member_out(member: ProjectMember) -> MemberOut:
    return MemberOut(user_id=member.user_id, display_name=member.user.display_name, email=member.user.email,
                     role=member.role, added_at=member.added_at)


def _manager_count(db: Session, project_id: int) -> int:
    return db.scalar(select(func.count(ProjectMember.id)).where(
        ProjectMember.project_id == project_id, ProjectMember.role == ROLE_MANAGER)) or 0


@router.get("/projects/{project_id}/members", response_model=list[MemberOut], summary="Team members")
def list_members(project_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    require_project(db, user, project_id)
    members = db.scalars(select(ProjectMember).where(ProjectMember.project_id == project_id).join(User).order_by(User.display_name))
    return [_member_out(m) for m in members]


@router.post("/projects/{project_id}/members", response_model=MemberOut, status_code=201, summary="Add a user to the project")
def add_member(project_id: int, data: MemberIn, user: User = Depends(get_current_user), db: Session = DbSession):
    require_project(db, user, project_id, ROLE_MANAGER)
    target = db.scalar(select(User).where(func.lower(User.email) == data.email.lower(), User.is_active.is_(True)))
    if target is None:
        raise HTTPException(404, "No active account with that email address")
    if db.scalar(select(ProjectMember.id).where(ProjectMember.project_id == project_id, ProjectMember.user_id == target.id)):
        raise HTTPException(409, "This user is already a member of the project")
    member = ProjectMember(project_id=project_id, user_id=target.id, role=data.role)
    db.add(member)
    audit.record(db, user.id, "member_added", project_id=project_id, field=target.email, new=data.role)
    if data.role == ROLE_DEVELOPER:
        profiles.ensure_profile(db, target, user.id)
    db.commit()
    db.refresh(member)
    return _member_out(member)


def _get_member(db: Session, project_id: int, user_id: int) -> ProjectMember:
    member = db.scalar(select(ProjectMember).where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id))
    if member is None:
        raise HTTPException(404, "This user is not a member of the project")
    return member


@router.patch("/projects/{project_id}/members/{user_id}", response_model=MemberOut, summary="Change a member's project role")
def change_member_role(project_id: int, user_id: int, data: MemberRoleIn, user: User = Depends(get_current_user),
                       db: Session = DbSession):
    require_project(db, user, project_id, ROLE_MANAGER)
    member = _get_member(db, project_id, user_id)
    if member.role == ROLE_MANAGER and data.role != ROLE_MANAGER and _manager_count(db, project_id) <= 1:
        raise HTTPException(409, "A project must keep at least one Project Manager")
    if data.role != member.role:
        audit.record(db, user.id, "member_role_changed", project_id=project_id, field=member.user.email, old=member.role, new=data.role)
        member.role = data.role
        if data.role == ROLE_DEVELOPER:
            profiles.ensure_profile(db, member.user, user.id)
    db.commit()
    return _member_out(member)


@router.delete("/projects/{project_id}/members/{user_id}", response_model=Message, summary="Remove a user from the project")
def remove_member(project_id: int, user_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    require_project(db, user, project_id, ROLE_MANAGER)
    member = _get_member(db, project_id, user_id)
    if member.role == ROLE_MANAGER and _manager_count(db, project_id) <= 1:
        raise HTTPException(409, "A project must keep at least one Project Manager")
    # Their unfinished bugs go back to the unassigned pool rather than staying with someone who has left.
    for bug in db.scalars(select(Bug).where(Bug.project_id == project_id, Bug.assignee_id == user_id, Bug.status.in_(ACTIVE_STATUSES))):
        audit.record(db, user.id, "unassigned", bug_id=bug.id, project_id=project_id, field="assignee",
                     old=member.user.display_name, new="member removed from project")
        bug.assignee_id = None
    audit.record(db, user.id, "member_removed", project_id=project_id, field=member.user.email, old=member.role)
    db.delete(member)
    db.commit()
    return Message(message="Member removed")


@router.get("/projects/{project_id}/components", response_model=list[str], summary="Component names known in this project")
def list_components(project_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    require_project(db, user, project_id)
    names = set(db.scalars(select(Bug.component).where(Bug.project_id == project_id, Bug.component.is_not(None)).distinct()))
    names |= set(db.scalars(
        select(ProfileComponent.name)
        .join(DeveloperProfile, DeveloperProfile.id == ProfileComponent.profile_id)
        .join(ProjectMember, ProjectMember.user_id == DeveloperProfile.user_id)
        .where(ProjectMember.project_id == project_id)
    ))
    return sorted(names, key=str.lower)


@router.get("/projects/{project_id}/audit", response_model=list[AuditOut], summary="Project-level audit entries (membership and settings)")
def project_audit(project_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    require_project(db, user, project_id, ROLE_MANAGER)
    return list(db.scalars(
        select(AuditLog).where(AuditLog.project_id == project_id, AuditLog.bug_id.is_(None))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(200)
    ))


# ---- dashboard --------------------------------------------------------------
@router.get("/projects/{project_id}/dashboard", response_model=DashboardOut, summary="Project dashboard (FR-15)")
def dashboard(project_id: int, user: User = Depends(get_current_user), db: Session = DbSession):
    require_project(db, user, project_id)
    labels = settings_service.severity_labels(db)
    counts = dict.fromkeys(STATUSES, 0)
    counts.update(dict(db.execute(select(Bug.status, func.count(Bug.id)).where(Bug.project_id == project_id).group_by(Bug.status)).all()))
    critical_open = db.scalar(select(func.count(Bug.id)).where(
        Bug.project_id == project_id, Bug.severity == labels[-1], Bug.status.in_(ACTIVE_STATUSES))) or 0

    # Eight weekly buckets, Monday to Sunday, ending with the current week.
    now = utcnow()
    this_monday = (now - timedelta(days=now.weekday())).date()
    weeks = [this_monday - timedelta(weeks=i) for i in range(7, -1, -1)]
    start = weeks[0]
    reported = dict.fromkeys(weeks, 0)
    resolved = dict.fromkeys(weeks, 0)

    def bucket(moment):
        day = moment.date()
        return day - timedelta(days=day.weekday())

    window = now - timedelta(days=(now.date() - start).days + 1)
    for (created,) in db.execute(select(Bug.created_at).where(Bug.project_id == project_id, Bug.created_at >= window)):
        if bucket(created) in reported:
            reported[bucket(created)] += 1
    for (done,) in db.execute(select(Bug.resolved_at).where(Bug.project_id == project_id, Bug.resolved_at >= window)):
        if bucket(done) in resolved:
            resolved[bucket(done)] += 1

    totals: dict[str, list[float]] = {}
    for severity, created, done in db.execute(
        select(Bug.severity, Bug.created_at, Bug.resolved_at).where(Bug.project_id == project_id, Bug.resolved_at.is_not(None))
    ):
        totals.setdefault(severity, []).append((done - created).total_seconds() / 3600)
    averages = {label: (round(sum(totals[label]) / len(totals[label]), 1) if label in totals else None) for label in labels}

    state = coldstart.get_state(db, project_id)
    db.commit()
    return DashboardOut(
        project_id=project_id, generated_at=now, counts=counts, critical_open=critical_open,
        trend=[WeekPoint(week_start=w, reported=reported[w], resolved=resolved[w]) for w in weeks],
        avg_resolution_hours=averages, cold_start=coldstart.to_out(state),
    )
