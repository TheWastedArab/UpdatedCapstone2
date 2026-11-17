"""User management, access control, projects and team membership (FR-01 to FR-04, FR-18, FR-19)."""
from datetime import timedelta

import pytest

from app.database import utcnow
from app.models import AuditLog, DeveloperProfile, User, UserSession
from app.services import email
from tests.conftest import API, PASSWORD, register


@pytest.mark.parametrize("password", ["short1!A", "nouppercase1!", "NoNumber!!", "NoSpecial123"][1:] + ["Ab1!"])
def test_registration_rejects_weak_passwords(client, password):
    r = client.post(API + "/auth/register", json={"email": "a@example.com", "password": password, "display_name": "A"})
    assert r.status_code == 422


def test_registration_requires_email_verification(client):
    r = client.post(API + "/auth/register", json={"email": "new@example.com", "password": PASSWORD, "display_name": "New"})
    assert r.status_code == 201
    code = r.json()["verification_code"]
    assert email.sent_messages[-1]["to"] == "new@example.com" and code in email.sent_messages[-1]["body"]
    # not usable before verification
    assert client.post(API + "/auth/login", json={"email": "new@example.com", "password": PASSWORD}).status_code == 403
    assert client.post(API + "/auth/verify-email", json={"email": "new@example.com", "code": "WRONG123"}).status_code == 400
    assert client.post(API + "/auth/verify-email", json={"email": "new@example.com", "code": code}).status_code == 200
    assert client.post(API + "/auth/login", json={"email": "new@example.com", "password": PASSWORD}).status_code == 200
    # duplicate registration is refused
    r = client.post(API + "/auth/register", json={"email": "NEW@example.com", "password": PASSWORD, "display_name": "X"})
    assert r.status_code == 409


def test_resend_verification_does_not_reveal_accounts(client):
    client.post(API + "/auth/register", json={"email": "p@example.com", "password": PASSWORD, "display_name": "P"})
    known = client.post(API + "/auth/resend-verification", json={"email": "p@example.com"})
    unknown = client.post(API + "/auth/resend-verification", json={"email": "ghost@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]
    assert known.json()["verification_code"] and unknown.json()["verification_code"] is None


def test_password_is_stored_as_bcrypt_hash(client, admin, db):
    user = db.get(User, admin.id)
    assert user.password_hash.startswith("$2") and PASSWORD not in user.password_hash


def test_login_failures_and_logout(client, admin):
    assert client.post(API + "/auth/login", json={"email": "admin@example.com", "password": "Wrong!Pass1"}).status_code == 401
    assert client.post(API + "/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}).status_code == 401
    assert client.get(API + "/users/me").status_code == 401
    assert client.get(API + "/users/me", headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    assert admin.get("/users/me").json()["role"] == "administrator"
    assert admin.post("/auth/logout").status_code == 200
    assert admin.get("/users/me").status_code == 401


def test_session_expires_after_eight_idle_hours(client, admin, db):
    session = db.query(UserSession).filter_by(user_id=admin.id).one()
    session.last_seen_at = utcnow() - timedelta(hours=7, minutes=59)
    db.commit()
    assert admin.get("/users/me").status_code == 200  # activity renews the idle timer
    db.refresh(session)
    session.last_seen_at = utcnow() - timedelta(hours=8, minutes=1)
    db.commit()
    assert admin.get("/users/me").status_code == 401


def test_profile_settings(client, admin):
    r = admin.patch("/users/me", {"display_name": "Ada L.", "notification_prefs": {"comment": False}})
    assert r.status_code == 200
    assert r.json()["display_name"] == "Ada L." and r.json()["notification_prefs"]["comment"] is False
    assert r.json()["notification_prefs"]["assignment"] is True
    assert admin.patch("/users/me", {"notification_prefs": {"bogus": True}}).status_code == 422
    # email changes only after the new address is confirmed
    r = admin.patch("/users/me", {"email": "ada@example.com"})
    assert r.json()["email"] == "admin@example.com" and r.json()["pending_email"] == "ada@example.com"
    code = email.sent_messages[-1]["body"].split("code is ")[1].split()[0]
    assert admin.post("/users/me/confirm-email", {"email": "ada@example.com", "code": "BADCODE1"}).status_code == 400
    assert admin.post("/users/me/confirm-email", {"email": "ada@example.com", "code": code}).json()["email"] == "ada@example.com"


def test_change_password_ends_other_sessions(client, admin):
    other = client.post(API + "/auth/login", json={"email": "admin@example.com", "password": PASSWORD}).json()["access_token"]
    assert admin.post("/users/me/password", {"current_password": "Wrong!Pass1", "new_password": "N3w!Password"}).status_code == 400
    assert admin.post("/users/me/password", {"current_password": PASSWORD, "new_password": "weak"}).status_code == 422
    assert admin.post("/users/me/password", {"current_password": PASSWORD, "new_password": "N3w!Password"}).status_code == 200
    assert client.get(API + "/users/me", headers={"Authorization": f"Bearer {other}"}).status_code == 401
    assert admin.get("/users/me").status_code == 200
    assert client.post(API + "/auth/login", json={"email": "admin@example.com", "password": "N3w!Password"}).status_code == 200


def test_admin_user_management(client, admin, make_user):
    user = make_user("someone")
    assert user.get("/admin/users").status_code == 403
    assert len(admin.get("/admin/users").json()) == 2
    assert admin.patch(f"/admin/users/{user.id}", {"role": "wizard"}).status_code == 422
    assert admin.patch("/admin/users/9999", {"role": "developer"}).status_code == 404
    assert admin.patch(f"/admin/users/{user.id}", {"is_active": False}).status_code == 200
    assert user.get("/users/me").status_code == 401  # sessions end on deactivation
    # the last administrator cannot be demoted or deactivated
    assert admin.patch(f"/admin/users/{admin.id}", {"role": "reporter"}).status_code == 409
    assert admin.patch(f"/admin/users/{admin.id}", {"is_active": False}).status_code == 409


def test_only_managers_and_admins_create_projects(client, admin, make_user):
    reporter = make_user("rep")
    assert reporter.post("/projects", {"name": "Nope"}).status_code == 403
    manager = make_user("pm", "project_manager")
    assert manager.post("/projects", {"name": "Alpha"}).status_code == 201
    assert manager.post("/projects", {"name": "alpha"}).status_code == 409
    assert [p["name"] for p in manager.get("/projects").json()] == ["Alpha"]
    assert reporter.get("/projects").json() == []
    assert len(admin.get("/projects").json()) == 1  # administrators see every project


def test_non_members_get_403_and_existence_is_not_disclosed(world):
    bug = world.report()
    for path in (f"/projects/{world.pid}", f"/projects/{world.pid}/bugs", f"/projects/{world.pid}/dashboard",
                 f"/bugs/{bug['id']}", f"/bugs/{bug['id']}/comments", f"/bugs/{bug['id']}/audit"):
        assert world.outsider.get(path).status_code == 403, path
    # a resource that does not exist answers exactly like one that is forbidden
    missing, forbidden = world.outsider.get("/bugs/99999"), world.outsider.get(f"/bugs/{bug['id']}")
    assert missing.status_code == forbidden.status_code == 403 and missing.json() == forbidden.json()
    assert world.outsider.get("/projects/99999").json() == world.outsider.get(f"/projects/{world.pid}").json()
    assert world.outsider.post(f"/projects/{world.pid}/bugs", {"title": "Sneaky title", "description": "x" * 30}).status_code == 403
    assert world.outsider.get("/attachments/1").status_code == 403


def test_membership_management_is_audited(world, db):
    members = world.manager.get(f"/projects/{world.pid}/members").json()
    assert {m["role"] for m in members} == {"project_manager", "developer", "reporter"}
    assert world.reporter.post(f"/projects/{world.pid}/members", {"email": "outsider@example.com"}).status_code == 403
    assert world.manager.post(f"/projects/{world.pid}/members", {"email": "ghost@example.com"}).status_code == 404
    assert world.manager.post(f"/projects/{world.pid}/members", {"email": "dev1@example.com", "role": "developer"}).status_code == 409
    assert world.manager.post(f"/projects/{world.pid}/members", {"email": "outsider@example.com", "role": "boss"}).status_code == 422
    assert world.manager.post(f"/projects/{world.pid}/members", {"email": "outsider@example.com"}).status_code == 201
    assert world.outsider.get(f"/projects/{world.pid}").status_code == 200
    # promoting to developer creates the developer profile automatically (FR-20)
    assert db.query(DeveloperProfile).filter_by(user_id=world.outsider.id).count() == 0
    r = world.manager.patch(f"/projects/{world.pid}/members/{world.outsider.id}", {"role": "developer"})
    assert r.json()["role"] == "developer"
    assert db.query(DeveloperProfile).filter_by(user_id=world.outsider.id).count() == 1
    assert world.manager.delete(f"/projects/{world.pid}/members/{world.outsider.id}").status_code == 200
    assert world.outsider.get(f"/projects/{world.pid}").status_code == 403
    assert world.manager.delete(f"/projects/{world.pid}/members/{world.outsider.id}").status_code == 404
    # the last manager cannot leave or be demoted
    assert world.manager.delete(f"/projects/{world.pid}/members/{world.manager.id}").status_code == 409
    assert world.manager.patch(f"/projects/{world.pid}/members/{world.manager.id}", {"role": "reporter"}).status_code == 409
    actions = [a.action for a in db.query(AuditLog).filter_by(project_id=world.pid, bug_id=None)]
    for expected in ("project_created", "member_added", "member_role_changed", "member_removed"):
        assert expected in actions
    trail = world.manager.get(f"/projects/{world.pid}/audit")
    assert trail.status_code == 200 and trail.json()[0]["action"] == "member_removed"
    assert world.dev1.get(f"/projects/{world.pid}/audit").status_code == 403


def test_removing_a_member_unassigns_their_open_bugs(world):
    bug = world.report()
    world.manager.post(f"/bugs/{bug['id']}/assign", {"assignee_id": world.dev1.id})
    world.manager.delete(f"/projects/{world.pid}/members/{world.dev1.id}")
    assert world.manager.get(f"/bugs/{bug['id']}").json()["assignee"] is None


def test_rename_and_archive_project(world):
    assert world.dev1.patch(f"/projects/{world.pid}", {"name": "Hack"}).status_code == 403
    r = world.manager.patch(f"/projects/{world.pid}", {"name": "Shop v2", "description": "Renamed"})
    assert r.json()["name"] == "Shop v2"
    bug = world.report()
    assert world.manager.patch(f"/projects/{world.pid}", {"archived": True}).json()["archived"] is True
    # archived projects stay readable but refuse new reports (FR-18)
    assert world.reporter.get(f"/bugs/{bug['id']}").status_code == 200
    r = world.reporter.post(f"/projects/{world.pid}/bugs", {"title": "Another problem", "description": "x" * 30})
    assert r.status_code == 409


def test_register_helper_first_user_only_is_admin(client):
    first = register(client, "one@example.com")
    second = register(client, "two@example.com")
    assert first.user["role"] == "administrator" and second.user["role"] == "reporter"
