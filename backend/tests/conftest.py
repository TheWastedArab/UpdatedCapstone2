"""Test fixtures.

Tests run against in-memory SQLite by default. Set BT_TEST_DATABASE_URL to a
PostgreSQL database to run the same suite against the production engine.
"""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="bugtracker-tests-"))
os.environ.update({
    "BT_DATABASE_URL": os.environ.get("BT_TEST_DATABASE_URL", "sqlite://"),
    "BT_ENVIRONMENT": "test",
    "BT_SECRET_KEY": "test-secret-key-test-secret-key-0123456789",
    "BT_BCRYPT_ROUNDS": "4",
    "BT_SEMANTIC_ENABLED": "false",
    "BT_EMAIL_BACKEND": "memory",
    "BT_EXPOSE_VERIFICATION_CODE": "true",
    "BT_COLD_START_THRESHOLD": "12",
    "BT_LIMITED_HISTORY_THRESHOLD": "3",
    "BT_UPLOAD_DIR": str(_TMP / "uploads"),
    "BT_MODEL_DIR": str(_TMP / "models"),
    "BT_NTFY_URL": "",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import database  # noqa: E402
from app.ai.engine import engine as ai_engine  # noqa: E402
from app.database import Base  # noqa: E402
from app.main import app  # noqa: E402
from app.services import coldstart, email  # noqa: E402

API = "/api/v1"
PASSWORD = "Str0ng!Passw0rd"
LONG_TEXT = "The application throws an error when the described action is performed by the user."


@pytest.fixture()
def client():
    Base.metadata.drop_all(database.engine)
    Base.metadata.create_all(database.engine)
    email.sent_messages.clear()
    coldstart._model_cache.clear()
    ai_engine.duplicates._indexes.clear()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db():
    with database.SessionLocal() as session:
        yield session


class Actor:
    """A signed-in user with convenience HTTP helpers."""

    def __init__(self, client: TestClient, token: str, user: dict):
        self.client, self.user, self.id = client, user, user["id"]
        self.headers = {"Authorization": f"Bearer {token}"}

    def get(self, path, **kw):
        return self.client.get(API + path, headers=self.headers, **kw)

    def post(self, path, json=None, **kw):
        return self.client.post(API + path, headers=self.headers, json=json, **kw)

    def patch(self, path, json=None):
        return self.client.patch(API + path, headers=self.headers, json=json)

    def put(self, path, json=None):
        return self.client.put(API + path, headers=self.headers, json=json)

    def delete(self, path):
        return self.client.delete(API + path, headers=self.headers)


def register(client: TestClient, email_address: str, name: str | None = None) -> Actor:
    r = client.post(API + "/auth/register", json={"email": email_address, "password": PASSWORD,
                                                  "display_name": name or email_address.split("@")[0].title()})
    assert r.status_code == 201, r.text
    code = r.json()["verification_code"]
    assert client.post(API + "/auth/verify-email", json={"email": email_address, "code": code}).status_code == 200
    r = client.post(API + "/auth/login", json={"email": email_address, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return Actor(client, r.json()["access_token"], r.json()["user"])


@pytest.fixture()
def admin(client):
    return register(client, "admin@example.com", "Ada Admin")  # first account becomes Administrator


@pytest.fixture()
def make_user(client, admin):
    def _make(name: str, system_role: str | None = None) -> Actor:
        actor = register(client, f"{name}@example.com", name.title())
        if system_role:
            assert admin.patch(f"/admin/users/{actor.id}", {"role": system_role}).status_code == 200
        return actor
    return _make


class World:
    """A project with a manager, two developers and a reporter."""

    def __init__(self, admin, make_user):
        self.admin = admin
        self.manager = make_user("manager", "project_manager")
        self.dev1, self.dev2 = make_user("dev1"), make_user("dev2")
        self.reporter = make_user("reporter")
        self.outsider = make_user("outsider")
        r = self.manager.post("/projects", {"name": "Shop", "description": "Online shop"})
        assert r.status_code == 201, r.text
        self.pid = r.json()["id"]
        for actor, role in ((self.dev1, "developer"), (self.dev2, "developer"), (self.reporter, "reporter")):
            r = self.manager.post(f"/projects/{self.pid}/members", {"email": actor.user["email"], "role": role})
            assert r.status_code == 201, r.text

    def report(self, title="Checkout page fails on submit", description=LONG_TEXT, actor=None, **extra) -> dict:
        r = (actor or self.reporter).post(f"/projects/{self.pid}/bugs", {"title": title, "description": description, **extra})
        assert r.status_code == 201, r.text
        return r.json()["bug"]

    def profile_id(self, actor) -> int:
        return next(p["id"] for p in self.admin.get("/admin/profiles").json() if p["user"]["id"] == actor.id)

    def resolve(self, bug_id: int, developer) -> None:
        """Assign, fix and verify a bug so that it lands in the developer's history."""
        assert self.manager.post(f"/bugs/{bug_id}/assign", {"assignee_id": developer.id, "override_exclusion": True}).status_code == 200
        for actor, status in ((developer, "In Progress"), (developer, "Resolved"), (self.manager, "Verified")):
            r = actor.post(f"/bugs/{bug_id}/status", {"status": status})
            assert r.status_code == 200, r.text


@pytest.fixture()
def world(admin, make_user):
    return World(admin, make_user)
