"""Operational commands.

    python -m app.cli create-admin EMAIL "Display Name"     create or promote an Administrator
    python -m app.cli backfill-embeddings                    embed every bug that has no stored vector
    python -m app.cli export-openapi [PATH]                  write the OpenAPI 3.0 document
    python -m app.cli retrain                                run the retraining sweep once
"""
import getpass
import json
import sys
from pathlib import Path

from sqlalchemy import func, select

from . import database
from .models import ROLE_ADMIN, Bug, BugEmbedding, User
from .security import hash_password, password_problems


def create_admin(email: str, name: str) -> None:
    with database.SessionLocal() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email.lower()))
        if user is None:
            password = getpass.getpass("Password: ")
            problems = password_problems(password)
            if problems:
                sys.exit("Password needs " + ", ".join(problems))
            user = User(email=email.lower(), display_name=name, password_hash=hash_password(password), email_verified=True)
            db.add(user)
        user.role, user.is_active = ROLE_ADMIN, True
        db.commit()
        print(f"{user.email} is an Administrator")


def backfill_embeddings(batch: int = 256) -> None:
    from .ai.engine import engine

    engine.load()
    embedder = engine.duplicates.embedder
    if not embedder.ready:
        sys.exit("Sentence-BERT is not available")
    with database.SessionLocal() as db:
        done = 0
        while True:
            rows = db.execute(
                select(Bug.id, Bug.title, Bug.description)
                .outerjoin(BugEmbedding, (BugEmbedding.bug_id == Bug.id) & (BugEmbedding.model == embedder.name))
                .where(BugEmbedding.bug_id.is_(None)).limit(batch)
            ).all()
            if not rows:
                break
            vectors = embedder.encode([f"{r[1]}\n{r[2]}" for r in rows])
            for (bug_id, _, _), vector in zip(rows, vectors):
                db.merge(BugEmbedding(bug_id=bug_id, model=embedder.name, vector=vector.tobytes()))
            db.commit()
            done += len(rows)
            print(f"  {done} embedded", end="\r")
    print(f"\nDone: {done} bugs embedded")


def export_openapi(path: str = "../docs/openapi.json") -> None:
    from .main import app

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Written with LF endings on every platform so the CI comparison is stable.
    target.write_bytes((json.dumps(app.openapi(), indent=2) + "\n").encode("utf-8"))
    print(f"OpenAPI {app.openapi()['openapi']} document written to {target.resolve()}")


def main(argv: list[str]) -> None:
    if len(argv) >= 3 and argv[0] == "create-admin":
        create_admin(argv[1], argv[2])
    elif argv[:1] == ["backfill-embeddings"]:
        backfill_embeddings()
    elif argv[:1] == ["export-openapi"]:
        export_openapi(*argv[1:2])
    elif argv[:1] == ["retrain"]:
        from .services import coldstart

        print(f"{coldstart.retrain_due_projects()} project(s) retrained")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
