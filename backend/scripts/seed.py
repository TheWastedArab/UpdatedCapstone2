"""Seed a development database with demo accounts, a project and synthetic bugs.

    python -m scripts.seed                 # demo project with 60 bugs, still in cold start
    python -m scripts.seed --bugs 400      # enough verified history to activate the assignment model
    python -m scripts.seed --bugs 50000    # volume for the FR-07 / NFR-01 performance checks

Demo accounts (development only, never use on a deployed system): every
account uses the password in DEMO_PASSWORD below.
"""
import argparse
import random
from datetime import timedelta

from sqlalchemy import select

from app import database
from app.ai.text import extract_keywords
from app.config import get_settings
from app.database import utcnow
from app.models import (
    ROLE_ADMIN, ROLE_DEVELOPER, ROLE_MANAGER, ROLE_REPORTER, STATUS_CLOSED, STATUS_IN_PROGRESS, STATUS_OPEN,
    STATUS_RESOLVED, STATUS_VERIFIED, AuditLog, Bug, BugWatcher, Project, ProjectMember, ProfileComponent,
    ProfileSkill, User,
)
from app.security import hash_password
from app.services import coldstart, profiles
from ml.synthetic import AREAS, generate

DEMO_PASSWORD = "Demo!Pass123"
PROJECT_NAME = "Demo Shop"
SEVERITY_MAP = {"blocker": "Critical", "critical": "Critical", "major": "High", "normal": "Medium", "minor": "Low", "trivial": "Low"}

ACCOUNTS = [
    ("admin@demo.example.com", "Ada Admin", ROLE_ADMIN, None),
    ("manager@demo.example.com", "Mona Manager", ROLE_MANAGER, ROLE_MANAGER),
    ("reporter@demo.example.com", "Rory Reporter", ROLE_REPORTER, ROLE_REPORTER),
    ("tester@demo.example.com", "Tess Tester", ROLE_REPORTER, ROLE_REPORTER),
]


def get_or_create_user(db, email: str, name: str, role: str) -> User:
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, display_name=name, role=role, email_verified=True, password_hash=hash_password(DEMO_PASSWORD))
        db.add(user)
        db.flush()
    return user


def seed(n_bugs: int) -> None:
    rng = random.Random(11)
    with database.SessionLocal() as db:
        if db.scalar(select(Project.id).where(Project.name == PROJECT_NAME)) is not None:
            print(f"Project '{PROJECT_NAME}' already exists; nothing to do.")
            return
        users = {email: get_or_create_user(db, email, name, role) for email, name, role, _ in ACCOUNTS}
        manager = users["manager@demo.example.com"]
        project = Project(name=PROJECT_NAME, description="Synthetic demo project", created_by=manager.id)
        db.add(project)
        db.flush()
        for email, _, _, project_role in ACCOUNTS:
            if project_role:
                db.add(ProjectMember(project_id=project.id, user_id=users[email].id, role=project_role))

        frame = generate(n_bugs, seed=11)
        developers: dict[str, User] = {}
        for address in sorted(frame["assignee"].unique()):
            area, number = address.split("@")[0].split(".dev")
            user = get_or_create_user(db, f"{area}.dev{number}@demo.example.com", f"{area.title()} Dev {number}", ROLE_DEVELOPER)
            developers[address] = user
            db.add(ProjectMember(project_id=project.id, user_id=user.id, role=ROLE_DEVELOPER))
            profile = profiles.ensure_profile(db, user, users["admin@demo.example.com"].id)
            profile.max_capacity = max(5, n_bugs // 40)
            profile.components = [ProfileComponent(name=area)]
            profile.skills = [ProfileSkill(name=s) for s in sorted({w for t in AREAS[area]["tech"][:3] for w in t.split()[:1]})]
        db.flush()

        reporters = [users["reporter@demo.example.com"], users["tester@demo.example.com"]]
        now = utcnow()
        id_map: dict[int, int] = {}
        span_hours = 24 * 7 * 10  # spread over ten weeks so the dashboard trend has data
        for position, row in enumerate(frame.itertuples(index=False)):
            created = now - timedelta(hours=span_hours * (1 - position / max(1, n_bugs)))
            # Older bugs are mostly finished; the newest are still open.
            age = 1 - position / max(1, n_bugs)
            status = rng.choices(
                [STATUS_CLOSED, STATUS_VERIFIED, STATUS_RESOLVED, STATUS_IN_PROGRESS, STATUS_OPEN],
                weights=[4 * age, 3 * age, 0.4, 0.5 * (1.2 - age), 1.2 - age],
            )[0]
            assignee = developers[row.assignee] if status != STATUS_OPEN or rng.random() < 0.3 else None
            bug = Bug(
                project_id=project.id, title=row.title, description=row.description, component=row.component,
                environment="Seeded demo data", status=status, severity=SEVERITY_MAP[row.severity], severity_source="rules",
                reporter_id=rng.choice(reporters).id, assignee_id=assignee.id if assignee else None,
                keywords=extract_keywords(f"{row.title}\n{row.description}"), created_at=created, updated_at=created,
                assigned_at=created if assignee else None,
            )
            if status in (STATUS_RESOLVED, STATUS_VERIFIED, STATUS_CLOSED):
                bug.resolved_at = min(now, created + timedelta(hours=rng.uniform(2, 96)))
            if status in (STATUS_VERIFIED, STATUS_CLOSED):
                bug.verified_at = min(now, bug.resolved_at + timedelta(hours=rng.uniform(1, 24)))
            if status == STATUS_CLOSED:
                bug.closed_at = bug.verified_at
            db.add(bug)
            db.flush()
            id_map[row.bug_id] = bug.id
            if row.dup_id is not None and not pd_isna(row.dup_id):
                bug.duplicate_of_id = id_map.get(int(row.dup_id))
            db.add(AuditLog(bug_id=bug.id, project_id=project.id, actor_id=bug.reporter_id, action="created", new_value=bug.title, created_at=created))
            db.add(BugWatcher(bug_id=bug.id, user_id=bug.reporter_id))
            if bug.verified_at is not None:
                profiles.enrich_from_verified_bug(db, bug)
            if position % 2000 == 1999:
                db.commit()
                print(f"  {position + 1} bugs")
        db.commit()
        project_id = project.id
        state = coldstart.get_state(db, project_id)
        db.commit()
        verified, threshold = state.verified_count, get_settings().cold_start_threshold
    print(f"Seeded '{PROJECT_NAME}' with {n_bugs} bugs ({verified} verified with an assignee; model threshold {threshold}).")
    if verified >= threshold:
        print("Training the assignment model...")
        print("  trained" if coldstart.train_project(project_id) else "  training did not complete; see model state")
    print("Sign in with admin@demo.example.com, manager@demo.example.com, checkout.dev1@demo.example.com or reporter@demo.example.com")
    print("(password: see DEMO_PASSWORD in scripts/seed.py).")


def pd_isna(value) -> bool:
    return value != value  # NaN is the only value not equal to itself


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bugs", type=int, default=60)
    seed(parser.parse_args().bugs)
