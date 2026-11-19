"""Bug lifecycle, search, comments, attachments, notifications and dashboard (FR-05 to FR-17)."""
from datetime import timedelta

from app.ai.engine import engine
from app.database import utcnow
from app.models import Bug, BugEmbedding
from app.services import email, ntfy
from tests.conftest import LONG_TEXT

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_submission_validates_input(world):
    post = lambda body: world.reporter.post(f"/projects/{world.pid}/bugs", body)  # noqa: E731
    assert post({"title": "Good title", "description": "too short"}).status_code == 422  # 20 character minimum
    assert post({"title": "abc", "description": LONG_TEXT}).status_code == 422
    assert post({"description": LONG_TEXT}).status_code == 422
    r = post({"title": "  Login button   ", "description": LONG_TEXT + "\x00\x07", "steps_to_reproduce": "1. Open\n2. Click",
              "environment": "Windows 11, Chrome 130", "component": "auth"})
    assert r.status_code == 201
    bug = r.json()["bug"]
    assert bug["title"] == "Login button" and "\x00" not in bug["description"]  # control characters stripped
    assert bug["status"] == "Open" and bug["reporter"]["id"] == world.reporter.id and bug["watching"] is True
    assert bug["keywords"] and bug["component"] == "auth"


def test_markup_and_sql_are_stored_verbatim_not_executed(world, db):
    payload = "<script>alert(1)</script> '; DROP TABLE bugs; -- List<String> fails to parse"
    bug = world.report(title="Injection attempt in title", description=payload)
    assert world.reporter.get(f"/bugs/{bug['id']}").json()["description"] == payload
    assert db.query(Bug).count() == 1
    r = world.reporter.get(f"/projects/{world.pid}/bugs", params={"q": "'; DROP TABLE bugs; --"})
    assert r.status_code == 200


def test_severity_is_assigned_on_submission_and_can_be_overridden(world):
    created = world.reporter.post(f"/projects/{world.pid}/bugs", {
        "title": "Typo in the footer", "description": "There is a spelling mistake in the footer wording of the page."}).json()
    assert created["ai_available"] is True and created["notice"] is None
    bug = created["bug"]
    assert bug["severity"] == "Low" and bug["severity_source"] == "rules"
    assert "severity_ms" in created["timings_ms"] and "duplicate_ms" in created["timings_ms"]
    # reporters may not override; developers and managers may (FR-11)
    assert world.reporter.post(f"/bugs/{bug['id']}/severity", {"severity": "High"}).status_code == 403
    assert world.dev1.post(f"/bugs/{bug['id']}/severity", {"severity": "Catastrophic"}).status_code == 422
    r = world.dev1.post(f"/bugs/{bug['id']}/severity", {"severity": "High"})
    assert r.json()["severity"] == "High" and r.json()["severity_source"] == "manual"
    trail = world.reporter.get(f"/bugs/{bug['id']}/audit").json()
    override = next(a for a in trail if a["action"] == "severity_override")
    assert override["old_value"] == "Low (rules)" and override["new_value"] == "High (manual)"
    assert override["actor"]["id"] == world.dev1.id


def test_status_lifecycle_is_sequential_and_role_checked(world):
    bug = world.report()
    bid = bug["id"]
    assert world.reporter.get(f"/bugs/{bid}").json()["allowed_transitions"] == []
    assert world.reporter.post(f"/bugs/{bid}/status", {"status": "In Progress"}).status_code == 409
    assert world.dev1.post(f"/bugs/{bid}/status", {"status": "Resolved"}).status_code == 409  # cannot skip a step
    assert world.dev1.post(f"/bugs/{bid}/status", {"status": "Nonsense"}).status_code == 422
    world.manager.post(f"/bugs/{bid}/assign", {"assignee_id": world.dev1.id})
    assert world.dev1.post(f"/bugs/{bid}/status", {"status": "In Progress"}).status_code == 200
    assert world.dev1.post(f"/bugs/{bid}/status", {"status": "Resolved"}).json()["resolved_at"] is not None
    # the developer who fixed it cannot verify their own fix; the reporter can
    assert world.dev1.post(f"/bugs/{bid}/status", {"status": "Verified"}).status_code == 409
    assert world.reporter.get(f"/bugs/{bid}").json()["allowed_transitions"] == ["Verified", "In Progress"]
    # a failed verification sends the bug back
    r = world.reporter.post(f"/bugs/{bid}/status", {"status": "In Progress"})
    assert r.json()["status"] == "In Progress" and r.json()["resolved_at"] is None
    world.dev1.post(f"/bugs/{bid}/status", {"status": "Resolved"})
    assert world.reporter.post(f"/bugs/{bid}/status", {"status": "Verified"}).json()["verified_at"] is not None
    assert world.reporter.post(f"/bugs/{bid}/status", {"status": "Closed"}).status_code == 409
    assert world.manager.post(f"/bugs/{bid}/status", {"status": "Closed"}).json()["closed_at"] is not None
    assert world.manager.get(f"/bugs/{bid}").json()["allowed_transitions"] == []
    changes = [(a["old_value"], a["new_value"]) for a in world.manager.get(f"/bugs/{bid}/audit").json() if a["action"] == "status_changed"]
    assert changes == [("Open", "In Progress"), ("In Progress", "Resolved"), ("Resolved", "In Progress"),
                       ("In Progress", "Resolved"), ("Resolved", "Verified"), ("Verified", "Closed")]


def test_audit_trail_records_edits_assignments_and_comments(world):
    bug = world.report()
    bid = bug["id"]
    assert world.outsider.patch(f"/bugs/{bid}", {"title": "Hacked title"}).status_code == 403
    r = world.reporter.patch(f"/bugs/{bid}", {"title": "Checkout fails on submit (Safari)", "component": "checkout"})
    assert r.status_code == 200 and r.json()["component"] == "checkout"
    other = world.report(title="Different report entirely", actor=world.dev2)
    assert world.reporter.patch(f"/bugs/{other['id']}", {"title": "Not mine to edit"}).status_code == 403
    world.manager.post(f"/bugs/{bid}/assign", {"assignee_id": world.dev1.id})
    world.manager.post(f"/bugs/{bid}/assign", {"assignee_id": world.dev2.id})
    world.dev1.post(f"/bugs/{bid}/comments", {"body": "Looking into it"})
    trail = world.reporter.get(f"/bugs/{bid}/audit").json()
    actions = [a["action"] for a in trail]
    assert actions[0] == "created" and actions.count("field_edited") == 2 and actions.count("assigned") == 2
    assert "comment_added" in actions and "severity_classified" in actions
    assert all(a["created_at"] for a in trail)
    edit = next(a for a in trail if a["field"] == "title")
    assert edit["old_value"] == "Checkout page fails on submit" and edit["actor"]["id"] == world.reporter.id
    reassigned = [a for a in trail if a["action"] == "assigned"][1]
    assert reassigned["old_value"] == "Dev1" and reassigned["new_value"] == "Dev2"
    assert world.manager.get(f"/bugs/{bid}").json()["reassignment_count"] == 1


def test_assignment_rules(world):
    bug = world.report()
    bid = bug["id"]
    assert world.dev1.post(f"/bugs/{bid}/assign", {"assignee_id": world.dev1.id}).status_code == 403  # manager decides
    assert world.manager.post(f"/bugs/{bid}/assign", {"assignee_id": world.outsider.id}).status_code == 422
    r = world.manager.post(f"/bugs/{bid}/assign", {"assignee_id": world.reporter.id})  # any project member is allowed
    assert r.json()["assignee"]["id"] == world.reporter.id and r.json()["assigned_at"]
    assert world.manager.post(f"/bugs/{bid}/assign", {"assignee_id": None}).json()["assignee"] is None


def test_comments_are_chronological_with_author(world):
    bug = world.report()
    assert world.dev1.post(f"/bugs/{bug['id']}/comments", {"body": "   "}).status_code == 422
    world.dev1.post(f"/bugs/{bug['id']}/comments", {"body": "First"})
    world.reporter.post(f"/bugs/{bug['id']}/comments", {"body": "Second"})
    comments = world.manager.get(f"/bugs/{bug['id']}/comments").json()
    assert [(c["body"], c["author"]["display_name"]) for c in comments] == [("First", "Dev1"), ("Second", "Reporter")]
    assert comments[0]["created_at"] <= comments[1]["created_at"]


def test_search_and_filters(world):
    a = world.report(title="Login page crashes on Safari", description="The browser tab crashes whenever the login form is submitted.")
    b = world.report(title="Typo on pricing page", description="There is a spelling mistake in the pricing table header row.")
    c = world.report(title="Export to CSV times out", description="Exporting a large report to CSV fails with a timeout error.", actor=world.dev2)
    world.manager.post(f"/bugs/{c['id']}/assign", {"assignee_id": world.dev1.id})
    world.dev1.post(f"/bugs/{c['id']}/status", {"status": "In Progress"})
    search = lambda **params: world.reporter.get(f"/projects/{world.pid}/bugs", params=params).json()  # noqa: E731
    assert search()["total"] == 3 and search()["items"][0]["id"] == c["id"]  # newest first
    assert [i["id"] for i in search(q="pricing")["items"]] == [b["id"]]
    assert [i["id"] for i in search(q=f"#{a['id']}")["items"]] == [a["id"]]
    assert [i["id"] for i in search(status="In Progress")["items"]] == [c["id"]]
    assert {i["id"] for i in search(status=["Open", "In Progress"])["items"]} == {a["id"], b["id"], c["id"]}
    assert [i["id"] for i in search(severity="Low")["items"]] == [b["id"]]
    assert [i["id"] for i in search(assignee_id=world.dev1.id)["items"]] == [c["id"]]
    assert search(assignee_id=0)["total"] == 2
    assert [i["id"] for i in search(reporter_id=world.dev2.id)["items"]] == [c["id"]]
    today = utcnow().date()
    assert search(created_from=str(today), created_to=str(today))["total"] == 3
    assert search(created_from=str(today + timedelta(days=1)))["total"] == 0
    assert search(created_to=str(today - timedelta(days=1)))["total"] == 0
    page = search(page=2, page_size=2)
    assert page["total"] == 3 and len(page["items"]) == 1
    assert search(q="100%_literal")["total"] == 0


def test_attachments_upload_validation_and_download(world):
    bug = world.report()
    upload = lambda actor, name, data: actor.post(f"/bugs/{bug['id']}/attachments", files={"file": (name, data)})  # noqa: E731
    r = upload(world.reporter, "screen.png", PNG)
    assert r.status_code == 201 and r.json()["content_type"] == "image/png" and r.json()["size"] == len(PNG)
    assert upload(world.reporter, "server.log", b"ERROR NullPointerException at line 4\n").status_code == 201
    # path traversal in the supplied name is neutralised
    r = upload(world.reporter, "../../etc/passwd.txt", b"harmless text")
    assert r.status_code == 201 and r.json()["filename"] == "passwd.txt"
    assert upload(world.reporter, "tool.exe", b"MZ\x90\x00").status_code == 415
    assert upload(world.reporter, "fake.png", b"MZ this is not a png").status_code == 415
    assert upload(world.reporter, "script.txt", b"#!/bin/sh\nrm -rf /").status_code == 415
    assert upload(world.reporter, "virus.txt", b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*").status_code == 415
    assert upload(world.reporter, "empty.txt", b"").status_code == 422
    assert upload(world.reporter, "big.log", b"a" * (10 * 1024 * 1024 + 1)).status_code == 413
    assert upload(world.outsider, "x.png", PNG).status_code == 403
    listed = world.dev1.get(f"/bugs/{bug['id']}/attachments").json()
    assert [a["filename"] for a in listed] == ["screen.png", "server.log", "passwd.txt"]
    download = world.dev1.get(f"/attachments/{listed[0]['id']}")
    assert download.status_code == 200 and download.content == PNG
    assert "attachment" in download.headers["content-disposition"]
    assert world.outsider.get(f"/attachments/{listed[0]['id']}").status_code == 403  # members only (FR-14)


def test_notifications_in_app_email_and_preferences(world):
    world.dev2.patch("/users/me", {"notification_prefs": {"assignment": False}})
    email.sent_messages.clear()
    bug = world.report()
    bid = bug["id"]
    world.manager.post(f"/bugs/{bid}/assign", {"assignee_id": world.dev1.id})
    assert world.dev1.get("/notifications/unread-count").json()["unread"] == 1
    assert [m["to"] for m in email.sent_messages] == ["dev1@example.com"]
    note = world.dev1.get("/notifications").json()[0]
    assert note["type"] == "assignment" and note["bug_id"] == bid and note["read"] is False
    # status change reaches reporter and assignee, but not the person who made it
    email.sent_messages.clear()
    world.dev1.post(f"/bugs/{bid}/status", {"status": "In Progress"})
    assert [m["to"] for m in email.sent_messages] == ["reporter@example.com"]
    # comments reach watchers; the reporter and assignee watch automatically
    email.sent_messages.clear()
    world.manager.post(f"/bugs/{bid}/comments", {"body": "Any update?"})
    assert sorted(m["to"] for m in email.sent_messages) == ["dev1@example.com", "reporter@example.com"]
    world.reporter.delete(f"/bugs/{bid}/watch")
    world.dev2.put(f"/bugs/{bid}/watch")
    email.sent_messages.clear()
    world.manager.post(f"/bugs/{bid}/comments", {"body": "Ping"})
    assert sorted(m["to"] for m in email.sent_messages) == ["dev1@example.com", "dev2@example.com"]
    # a disabled type suppresses the email but the in-app alert is still recorded
    email.sent_messages.clear()
    world.manager.post(f"/bugs/{bid}/assign", {"assignee_id": world.dev2.id})
    assert email.sent_messages == []
    assert any(n["type"] == "assignment" for n in world.dev2.get("/notifications").json())
    # read state
    assert world.dev2.post(f"/notifications/{note['id']}/read").status_code == 403  # someone else's
    assert world.dev1.post(f"/notifications/{note['id']}/read").status_code == 200
    assert all(n["id"] != note["id"] for n in world.dev1.get("/notifications", params={"unread_only": True}).json())
    world.dev1.post("/notifications/read-all")
    assert world.dev1.get("/notifications/unread-count").json()["unread"] == 0


def test_critical_bug_alerts_every_member_and_pushes(world, monkeypatch):
    pushed = []
    monkeypatch.setattr(ntfy, "publish", lambda topic, title, message, **kw: pushed.append((topic, title)) or True)
    email.sent_messages.clear()
    bug = world.report(title="Payment service crash causes data loss", description="Orders are lost when the payment service crashes in production.")
    assert bug["severity"] == "Critical"
    assert sorted(m["to"] for m in email.sent_messages) == ["dev1@example.com", "dev2@example.com", "manager@example.com"]
    assert pushed == [(f"bugtracker-project-{world.pid}", f"Critical bug #{bug['id']}")]
    assert world.manager.get("/notifications").json()[0]["type"] == "critical_bug"
    # an override to Critical alerts as well
    other = world.report(title="Typo in the header", description="A small spelling mistake in the header wording.")
    world.dev1.post(f"/bugs/{other['id']}/severity", {"severity": "Critical"})
    assert len(pushed) == 2


def test_ntfy_publish(monkeypatch):
    from app.config import get_settings

    assert ntfy.publish("topic", "title", "message") is False  # disabled when no server is configured
    calls = {}

    class Response:
        def raise_for_status(self):
            pass

    def fake_post(url, content, headers, timeout):
        calls.update(url=url, headers=headers, content=content)
        return Response()

    monkeypatch.setattr(get_settings(), "ntfy_url", "https://ntfy.internal/")
    monkeypatch.setattr(get_settings(), "ntfy_token", "tk_secret")
    monkeypatch.setattr(ntfy.httpx, "post", fake_post)
    assert ntfy.publish("bugs", "Critical bug #1", "Détails") is True
    assert calls["url"] == "https://ntfy.internal/bugs" and calls["headers"]["Authorization"] == "Bearer tk_secret"
    assert calls["headers"]["Priority"] == "urgent" and calls["content"] == "Détails".encode()


def test_email_backends(monkeypatch, caplog):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "email_backend", "console")
    with caplog.at_level("INFO"):
        assert email.send_email("a@example.com", "Subject", "Body") is True
    assert "a@example.com" in caplog.text
    monkeypatch.setattr(get_settings(), "email_backend", "smtp")
    monkeypatch.setattr(get_settings(), "smtp_host", "127.0.0.1")
    monkeypatch.setattr(get_settings(), "smtp_port", 1)  # nothing listens here
    assert email.send_email("a@example.com", "Subject", "Body") is False  # failure is logged, never raised


def test_dashboard(world, db):
    a = world.report(title="Payment crash loses data", description="The payment service crashes and loses the order data.")
    world.report(title="Typo in footer text", description="There is a spelling mistake in the footer wording.")
    c = world.report()
    world.resolve(c["id"], world.dev1)
    old = db.get(Bug, c["id"])
    old.created_at = old.resolved_at - timedelta(hours=6)
    db.commit()
    board = world.reporter.get(f"/projects/{world.pid}/dashboard").json()
    assert board["counts"] == {"Open": 2, "In Progress": 0, "Resolved": 0, "Verified": 1, "Closed": 0}
    assert board["critical_open"] == 1 and a["severity"] == "Critical"
    assert len(board["trend"]) == 8
    assert board["trend"][-1]["resolved"] == 1 and sum(w["reported"] for w in board["trend"]) == 3
    assert board["avg_resolution_hours"]["High"] == 6.0 and board["avg_resolution_hours"]["Low"] is None
    cold = board["cold_start"]
    assert cold["cold_start"] is True and cold["verified_count"] == 1 and cold["remaining"] == 11 and cold["threshold"] == 12


def test_duplicate_link_management(world):
    original = world.report()
    copy = world.report(title="Checkout submit is broken", description=LONG_TEXT)
    assert world.reporter.post(f"/bugs/{copy['id']}/duplicate", {"duplicate_of_id": original["id"]}).status_code == 403
    assert world.dev1.post(f"/bugs/{copy['id']}/duplicate", {"duplicate_of_id": copy["id"]}).status_code == 422
    assert world.dev1.post(f"/bugs/{copy['id']}/duplicate", {"duplicate_of_id": 9999}).status_code == 422
    r = world.dev1.post(f"/bugs/{copy['id']}/duplicate", {"duplicate_of_id": original["id"]})
    assert r.json()["duplicate_of_id"] == original["id"]
    assert world.dev1.post(f"/bugs/{original['id']}/duplicate", {"duplicate_of_id": copy["id"]}).status_code == 422
    # the duplicate stays in the database and keeps pointing at its original
    assert world.reporter.get(f"/bugs/{copy['id']}").json()["duplicate_of_id"] == original["id"]
    assert world.dev1.post(f"/bugs/{copy['id']}/duplicate", {"duplicate_of_id": None}).json()["duplicate_of_id"] is None


def test_submission_survives_ai_failure(world, monkeypatch):
    """NFR-10: the report is saved as Unclassified with a notice when the AI service is down."""
    def boom(*args, **kwargs):
        raise RuntimeError("model server down")

    monkeypatch.setattr(engine.severity, "predict", boom)
    monkeypatch.setattr(engine.duplicates, "check", boom)
    r = world.reporter.post(f"/projects/{world.pid}/bugs", {"title": "Crash on start", "description": LONG_TEXT})
    assert r.status_code == 201
    body = r.json()
    assert body["ai_available"] is False and "Unclassified" in body["notice"]
    assert body["bug"]["severity"] == "Unclassified"
    check = world.reporter.post(f"/projects/{world.pid}/duplicates/check", {"description": LONG_TEXT})
    assert check.status_code == 200 and check.json()["available"] is False and check.json()["notice"]
    assert world.reporter.get(f"/bugs/{body['bug']['id']}").status_code == 200


def test_everything_degrades_when_ai_is_disabled(world, monkeypatch):
    monkeypatch.setattr(engine, "loaded", False)
    created = world.reporter.post(f"/projects/{world.pid}/bugs", {"title": "Crash on start", "description": LONG_TEXT}).json()
    assert created["ai_available"] is False and created["bug"]["severity"] == "Unclassified"
    assert world.reporter.post(f"/projects/{world.pid}/duplicates/check", {"description": LONG_TEXT}).json()["method"] == "unavailable"
    recs = world.manager.get(f"/bugs/{created['bug']['id']}/recommendations").json()
    assert recs["mode"] == "unavailable" and recs["recommendations"] == []
    assert world.reporter.get("/ai/status").json()["enabled"] is False
    # manual assignment still works
    assert world.manager.post(f"/bugs/{created['bug']['id']}/assign", {"assignee_id": world.dev1.id}).status_code == 200


def test_editing_the_text_drops_a_stale_embedding(world, db, monkeypatch):
    """An embedding of the old text must not survive an edit it could not be refreshed for."""
    bug = world.reporter.post(f"/projects/{world.pid}/bugs", {"title": "Crash on start", "description": LONG_TEXT}).json()["bug"]
    db.merge(BugEmbedding(bug_id=bug["id"], model="old-text", vector=b"x" * 16))
    db.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("encoder down")

    monkeypatch.setattr(engine.duplicates, "store_embedding", boom)
    r = world.reporter.patch(f"/bugs/{bug['id']}", {"title": "Crash when the app starts offline"})
    assert r.status_code == 200
    db.expire_all()
    assert db.get(BugEmbedding, bug["id"]) is None
