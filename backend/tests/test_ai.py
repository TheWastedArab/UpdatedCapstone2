"""AI triage: duplicates, severity, developer profiles, cold start and recommendations (FR-10 to FR-12, FR-20 to FR-29)."""
from datetime import timedelta

import joblib
import numpy as np
import pytest

from app.ai import cnn_lstm
from app.ai.duplicates import BM25Index
from app.ai.engine import engine
from app.ai.severity import MODEL_FILENAME, SeverityClassifier, build_ensemble, classify_by_rules, vote_share
from app.ai.text import extract_keywords, map_canonical, severity_rank, tokenize
from app.config import get_settings
from app.database import utcnow
from app.models import Bug, ModelState, ResolutionHistory
from app.openapi30 import downgrade_to_openapi_30
from app.services import coldstart
from tests.conftest import API

PDF = "The application crashes immediately when a PDF document is opened from the file picker dialog."
PAYMENT = ["Payment gateway rejects valid card", "Checkout total is wrong after discount", "Refund is issued twice for one order",
           "Invoice shows wrong currency symbol", "Payment confirmation email is missing", "Card form loses focus while typing",
           "Checkout button stays disabled", "Order total ignores shipping cost"]
LOGIN = ["Login fails with correct password", "Session expires too early after login", "Password reset link is invalid",
         "Two factor code is never accepted", "Logout does not clear the session", "Signup form rejects valid email",
         "Remember me checkbox is ignored", "Account lockout never ends"]


def describe(title: str) -> str:
    """A description in the vocabulary of its area, as real reports about one subsystem tend to be."""
    if title.split(" variant")[0] in PAYMENT:
        return f"{title}. The payment step of the order fails for the customer and the card is not charged correctly."
    if title in LOGIN:
        return f"{title}. The account cannot be reached because authentication of the user credentials goes wrong."
    return f"{title}. This happens every time in the production environment and blocks the user from finishing."


# ---- duplicate detection ------------------------------------------------------
def test_duplicate_check_while_typing(world, make_user):
    original = world.report(title="App crashes opening PDF", description=PDF)
    world.report(title="Dark mode colours are wrong", description="The dark theme uses a light background on the settings page.")
    for i in range(4):
        world.report(title=f"PDF export problem {i}", description=f"Exporting report number {i} to a PDF document produces an empty file.")
    check = lambda body: world.reporter.post(f"/projects/{world.pid}/duplicates/check", body)  # noqa: E731
    assert check({"description": "too short"}).status_code == 422  # only runs from 20 characters
    r = check({"title": "Crash when opening a PDF", "description": "Application crashes when I open a PDF document from the file picker."})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] and body["method"] == "bm25+lexical"
    assert 1 <= len(body["candidates"]) <= 3  # at most three, each with a match percentage
    assert body["candidates"][0]["bug_id"] == original["id"] and 0 < body["candidates"][0]["match_percent"] <= 100
    assert body["elapsed_ms"] < 1500
    assert check({"description": "Completely unrelated zebra giraffe elephant safari"}).json()["candidates"] == []
    # the search never crosses project boundaries (NFR-06)
    other_manager = make_user("pm2", "project_manager")
    other = other_manager.post("/projects", {"name": "Other"}).json()["id"]
    assert other_manager.post(f"/projects/{other}/duplicates/check", {"description": PDF}).json()["candidates"] == []
    assert world.outsider.post(f"/projects/{world.pid}/duplicates/check", {"description": PDF}).status_code == 403


def test_submission_flags_probable_duplicate_and_threshold_is_configurable(world):
    original = world.report(title="App crashes opening PDF", description=PDF)
    copy = world.report(title="App crashes opening a PDF", description=PDF)
    assert copy["probable_duplicate_of_id"] == original["id"] and copy["duplicate_score"] >= 0.7
    assert any(a["action"] == "duplicate_flagged" for a in world.reporter.get(f"/bugs/{copy['id']}/audit").json())
    # the same report is no longer flagged once the administrator raises the threshold (NFR-14)
    assert world.manager.put("/admin/settings", {"duplicate_threshold": 1.0}).status_code == 403
    assert world.admin.put("/admin/settings", {"duplicate_threshold": 1.5}).status_code == 422
    assert world.admin.put("/admin/settings", {"duplicate_threshold": 1.0}).json()["duplicate_threshold"] == 1.0
    third = world.report(title="App crashes opening a PDF file", description=PDF + " Again.")
    assert third["probable_duplicate_of_id"] is None and third["duplicate_score"] > 0.7


def test_bm25_index():
    index = BM25Index([10, 20, 30], ["null pointer exception in checkout", "login page typo", "checkout total wrong"])
    assert [i for i, _ in index.top("checkout exception", 5)] == [10, 30]
    assert [i for i, _ in index.top("checkout exception", 5, exclude_id=10)] == [30]
    assert index.top("zebra", 5) == [] and index.top("", 5) == []
    assert BM25Index([], []).top("anything", 5) == [] and BM25Index([1], ["!!!"]).top("x", 5) == []
    assert len(index) == 3


def test_text_utilities():
    assert tokenize("NullPointerException in checkoutService.java") == ["null", "pointer", "exception", "in", "checkout", "service.java"]
    assert extract_keywords("The checkout crashes. Checkout crashes on submit, checkout is broken")[:2] == ["checkout", "crashes"]
    labels = ["Low", "Medium", "High", "Critical"]
    assert [severity_rank(label, labels) for label in labels] == [0, 1, 2, 3]
    assert severity_rank("Unclassified", labels) is None and severity_rank("P1", ["P3", "P2", "P1"]) == 3
    assert map_canonical("Critical", ["P3", "P2", "P1"]) == "P1" and map_canonical("Low", ["P3", "P2", "P1"]) == "P3"
    assert map_canonical("High", labels) == "High"


# ---- severity -----------------------------------------------------------------
def test_rule_based_severity_fallback():
    assert classify_by_rules("Server crash with data loss")[0] == "Critical"
    assert classify_by_rules("NullPointerException when saving")[0] == "High"
    assert classify_by_rules("Typo in the tooltip")[0] == "Low"
    assert classify_by_rules("Improve the onboarding flow")[0] == "Medium"


def test_trained_severity_ensemble_is_loaded_and_used(tmp_path):
    samples = {
        "Critical": "system crash data loss corruption outage security breach",
        "High": "exception error failure broken feature cannot save timeout",
        "Medium": "unexpected behaviour slow response inconsistent sorting",
        "Low": "typo cosmetic alignment colour wording tooltip spelling",
    }
    texts, labels = [], []
    rng = np.random.default_rng(0)
    for label, words in samples.items():
        vocabulary = words.split()
        for _ in range(30):
            texts.append(" ".join(rng.choice(vocabulary, size=6)))
            labels.append(label)
    pipeline = build_ensemble(rf_features="all", rf_trees=20).fit(texts, labels)
    predicted, share = vote_share(pipeline, ["cosmetic typo in tooltip wording", "crash with data loss and corruption"])
    assert list(predicted) == ["Low", "Critical"] and all(0.3 < s <= 1.0 for s in share)
    joblib.dump({"pipeline": pipeline, "meta": {"trained_at": "test"}}, tmp_path / MODEL_FILENAME)
    classifier = SeverityClassifier()
    assert classifier.load(tmp_path) is True and classifier.mode == "model"
    assert classifier.predict("Tooltip typo", "cosmetic spelling wording alignment")[::2] == ("Low", "model")
    # a corrupt file degrades to rules instead of stopping the service
    (tmp_path / MODEL_FILENAME).write_bytes(b"not a model")
    assert classifier.load(tmp_path) is False and classifier.mode == "rules"


def test_configured_severity_labels_are_applied(world):
    assert world.admin.put("/admin/settings", {"severity_labels": ["P3"]}).status_code == 422
    r = world.admin.put("/admin/settings", {"severity_labels": ["P4", "P3", "P2", "P1"]})
    assert r.json()["severity_labels"] == ["P4", "P3", "P2", "P1"]
    bug = world.report(title="Payment crash loses data", description="The payment service crashes and the order data is lost.")
    assert bug["severity"] == "P1"
    assert world.dev1.post(f"/bugs/{bug['id']}/severity", {"severity": "High"}).status_code == 422
    assert world.dev1.post(f"/bugs/{bug['id']}/severity", {"severity": "P3"}).json()["severity"] == "P3"
    assert world.admin.get("/admin/settings").json()["severity_labels"][-1] == "P1"


# ---- developer profiles -------------------------------------------------------
def test_profile_is_created_with_developer_role_and_managed_by_admin(world):
    listed = world.admin.get("/admin/profiles").json()
    assert {p["user"]["email"] for p in listed} == {"dev1@example.com", "dev2@example.com"}
    assert world.manager.get("/admin/profiles").status_code == 403  # Administrator only (FR-21)
    pid = world.profile_id(world.dev1)
    r = world.admin.patch(f"/admin/profiles/{pid}", {
        "skills": ["Python", "python", " SQL "], "components": ["checkout"], "availability": "Busy", "max_capacity": 3,
        "history": [{"bug_id": 1}], "open_bugs": 99, "resolved_count": 99})  # derived fields are ignored
    assert r.status_code == 200
    profile = r.json()
    assert profile["skills"] == ["Python", "SQL"] and profile["components"] == ["checkout"]
    assert profile["availability"] == "Busy" and profile["max_capacity"] == 3
    assert profile["open_bugs"] == 0 and profile["resolved_count"] == 0 and profile["history"] == []
    assert world.admin.patch(f"/admin/profiles/{pid}", {"availability": "On holiday"}).status_code == 422
    assert world.admin.patch(f"/admin/profiles/{pid}", {"max_capacity": 0}).status_code == 422
    assert world.admin.patch("/admin/profiles/999", {"max_capacity": 2}).status_code == 404
    trail = world.admin.get(f"/admin/profiles/{pid}/audit").json()
    assert {a["field"] for a in trail if a["action"] == "profile_edited"} == {"skills", "components", "availability", "max_capacity"}
    assert all(a["actor"]["id"] == world.admin.id for a in trail if a["action"] == "profile_edited")
    assert world.admin.post(f"/admin/profiles/{pid}/deactivate").json()["active"] is False
    assert world.admin.post(f"/admin/profiles/{pid}/activate").json()["active"] is True
    # explicit creation for a user who is not yet a developer anywhere
    r = world.admin.post("/admin/profiles", {"user_id": world.reporter.id, "skills": ["QA"]})
    assert r.status_code == 201 and r.json()["skills"] == ["QA"]
    assert world.admin.post("/admin/profiles", {"user_id": world.reporter.id}).status_code == 409
    assert world.admin.post("/admin/profiles", {"user_id": 999}).status_code == 404
    assert world.dev1.get("/users/me/developer-profile").json()["skills"] == ["Python", "SQL"]
    assert world.manager.get("/users/me/developer-profile").json() is None


def test_verified_bug_enriches_profile_and_updates_workload(world, db):
    bug = world.report(title="Checkout total is wrong", description=describe("Checkout total is wrong after discount"), component="checkout")
    pid = world.profile_id(world.dev1)
    world.manager.post(f"/bugs/{bug['id']}/assign", {"assignee_id": world.dev1.id})
    assert world.admin.get(f"/admin/profiles/{pid}").json()["open_bugs"] == 1
    world.dev1.post(f"/bugs/{bug['id']}/status", {"status": "In Progress"})
    world.dev1.post(f"/bugs/{bug['id']}/status", {"status": "Resolved"})
    assert world.admin.get(f"/admin/profiles/{pid}").json()["open_bugs"] == 0  # only Open and In Progress count
    assert db.query(ResolutionHistory).count() == 0
    world.reporter.post(f"/bugs/{bug['id']}/status", {"status": "Verified"})
    profile = world.admin.get(f"/admin/profiles/{pid}").json()
    assert profile["resolved_count"] == 1
    entry = profile["history"][0]
    assert entry["bug_id"] == bug["id"] and entry["component"] == "checkout" and entry["severity"] == bug["severity"]
    assert "checkout" in entry["keywords"] and entry["resolution_seconds"] >= 0
    assert any(a["action"] == "profile_enriched" for a in world.admin.get(f"/admin/profiles/{pid}/audit").json())
    # running the enrichment again does not duplicate the entry
    from app.services import profiles as profile_service
    profile_service.enrich_task(bug["id"])
    profile_service.enrich_task(99999)
    assert db.query(ResolutionHistory).count() == 1


def test_capacity_and_availability_exclude_developers(world):
    for actor in (world.dev1, world.dev2):
        world.admin.patch(f"/admin/profiles/{world.profile_id(actor)}", {"skills": ["checkout"], "components": ["checkout"], "max_capacity": 1})
    first = world.report(title="Checkout button broken", description=describe("Checkout button is broken"), component="checkout")
    second = world.report(title="Checkout page is slow", description=describe("Checkout page loads slowly"), component="checkout")
    world.manager.post(f"/bugs/{first['id']}/assign", {"assignee_id": world.dev1.id})
    world.admin.patch(f"/admin/profiles/{world.profile_id(world.dev2)}", {"availability": "Unavailable"})
    recs = world.manager.get(f"/bugs/{second['id']}/recommendations").json()
    assert recs["recommendations"] == []
    assert {e["display_name"]: e["reason"] for e in recs["excluded"]} == {
        "Dev1": "At capacity (1 of 1 open bugs)", "Dev2": "Marked Unavailable"}
    # the Project Manager can still override the exclusion (FR-23)
    for developer in (world.dev1, world.dev2):
        assert world.manager.post(f"/bugs/{second['id']}/assign", {"assignee_id": developer.id}).status_code == 409
    r = world.manager.post(f"/bugs/{second['id']}/assign", {"assignee_id": world.dev2.id, "override_exclusion": True})
    assert r.status_code == 200 and r.json()["assignee"]["id"] == world.dev2.id
    trail = world.manager.get(f"/bugs/{second['id']}/audit").json()
    assert "exclusion overridden" in next(a for a in trail if a["action"] == "assigned")["new_value"]
    world.admin.post(f"/admin/profiles/{world.profile_id(world.dev1)}/deactivate")
    excluded = world.manager.get(f"/bugs/{second['id']}/recommendations").json()["excluded"]
    assert any(e["reason"] == "Profile deactivated" for e in excluded)
    assert world.reporter.get(f"/bugs/{second['id']}/recommendations").status_code == 403


# ---- cold start ---------------------------------------------------------------
def test_cold_start_manual_then_rule_based(world):
    bug = world.report(title="Checkout total is wrong", description=describe("Checkout total is wrong in the Python service"), component="checkout")
    recs = world.manager.get(f"/bugs/{bug['id']}/recommendations").json()
    # FR-26: no recommendations, and the interface is told plainly why
    assert recs["mode"] == "manual" and recs["recommendations"] == []
    assert "0 of 12 verified bugs" in recs["message"] and "Assign manually" in recs["message"]
    assert recs["cold_start"]["cold_start"] is True and recs["cold_start"]["remaining"] == 12
    # FR-27: once a profile declares skills and components, rule-based suggestions appear, labelled as such
    world.admin.patch(f"/admin/profiles/{world.profile_id(world.dev1)}", {"skills": ["Python"], "components": ["checkout"]})
    world.admin.patch(f"/admin/profiles/{world.profile_id(world.dev2)}", {"skills": ["Swift"], "components": ["ios"]})
    recs = world.manager.get(f"/bugs/{bug['id']}/recommendations").json()
    assert recs["mode"] == "rule_based" and "rule-based" in recs["message"] and "no historical evidence" in recs["message"]
    assert [r["display_name"] for r in recs["recommendations"]] == ["Dev1"]
    reasons = " ".join(recs["recommendations"][0]["reasons"])
    assert "Declared component match: checkout" in reasons and "Declared skill match: Python" in reasons and "Workload: 0 of 5" in reasons
    assert recs["elapsed_ms"] < 3000
    unrelated = world.report(title="Translation missing in footer", description=describe("A translation is missing in the footer"))
    recs = world.manager.get(f"/bugs/{unrelated['id']}/recommendations").json()
    assert recs["mode"] == "rule_based" and recs["recommendations"] == [] and "no rule-based suggestion" in recs["message"]


def test_no_developers_in_project(admin, make_user):
    manager = make_user("solo", "project_manager")
    project = manager.post("/projects", {"name": "Empty"}).json()["id"]
    bug = manager.post(f"/projects/{project}/bugs", {"title": "Something is broken", "description": describe("Something is broken")}).json()["bug"]
    recs = manager.get(f"/bugs/{bug['id']}/recommendations").json()
    assert recs["mode"] == "manual" and "no members with the Developer role" in recs["message"]


@pytest.fixture()
def trained_world(world):
    """A project taken past the cold start threshold: dev1 fixes payment bugs, dev2 fixes login bugs."""
    world.admin.patch(f"/admin/profiles/{world.profile_id(world.dev1)}", {"skills": ["payments"], "components": ["checkout"], "max_capacity": 20})
    world.admin.patch(f"/admin/profiles/{world.profile_id(world.dev2)}", {"skills": ["oauth"], "components": ["auth"], "max_capacity": 20})
    for i in range(6):
        for titles, developer, component in ((PAYMENT, world.dev1, "checkout"), (LOGIN, world.dev2, "auth")):
            bug = world.report(title=titles[i], description=describe(titles[i]), component=component)
            world.resolve(bug["id"], developer)
    return world


def test_model_activates_at_threshold_and_recommends_with_reasons(trained_world, db):
    world = trained_world
    board = world.manager.get(f"/projects/{world.pid}/dashboard").json()["cold_start"]
    assert board["state"] == "active" and board["cold_start"] is False and board["verified_count"] == 12
    assert board["last_trained_at"] and board["metrics"]["samples"] == 12 and board["metrics"]["developers"] == 2
    states = world.admin.get("/admin/model-states").json()
    assert states[0]["project_name"] == "Shop" and states[0]["last_trained_at"]  # shown to the Administrator (FR-29)
    assert world.manager.get("/admin/model-states").status_code == 403

    bug = world.report(title=PAYMENT[6], description=describe(PAYMENT[6]), component="checkout")
    recs = world.manager.get(f"/bugs/{bug['id']}/recommendations").json()
    assert recs["mode"] == "model" and recs["elapsed_ms"] < 3000
    top = recs["recommendations"][0]
    assert top["display_name"] == "Dev1" and top["limited_history"] is False
    reasons = " ".join(top["reasons"])
    assert "Resolved 6 earlier bugs in component 'checkout'" in reasons
    assert "History matches this report on" in reasons and "Workload: 0 of 20 open bugs" in reasons
    login_bug = world.report(title=LOGIN[6], description=describe(LOGIN[6]), component="auth")
    assert world.manager.get(f"/bugs/{login_bug['id']}/recommendations").json()["recommendations"][0]["display_name"] == "Dev2"


def test_duplicate_flag_and_severity_feed_the_recommendation(trained_world):
    world = trained_world
    # An ambiguous report: nothing in its text or component points at either developer.
    original = world.report(title="Export of the weekly report is empty", description=describe("Export of the weekly report is empty"))
    world.manager.post(f"/bugs/{original['id']}/assign", {"assignee_id": world.dev2.id})
    plain = world.report(title="Weekly summary shows nothing", description=describe("Weekly summary shows nothing"))
    world.dev1.post(f"/bugs/{plain['id']}/duplicate", {"duplicate_of_id": original["id"]})
    recs = world.manager.get(f"/bugs/{plain['id']}/recommendations").json()["recommendations"]
    assert recs[0]["display_name"] == "Dev2"
    assert f"Assigned to bug #{original['id']}, of which this is a confirmed duplicate" in recs[0]["reasons"][0]
    # Severity changes how much spare capacity matters: load a developer up and compare Low with Critical.
    world.admin.patch(f"/admin/profiles/{world.profile_id(world.dev1)}", {"max_capacity": 4})
    for i in range(3):
        filler = world.report(title=f"Checkout filler task {i}", description=describe(f"Checkout filler task number {i}"), component="checkout")
        world.manager.post(f"/bugs/{filler['id']}/assign", {"assignee_id": world.dev1.id})
    bug = world.report(title=PAYMENT[7], description=describe(PAYMENT[7]), component="checkout")

    def dev1_score(severity):
        world.dev2.post(f"/bugs/{bug['id']}/severity", {"severity": severity})
        recs = world.manager.get(f"/bugs/{bug['id']}/recommendations").json()["recommendations"]
        return next(r for r in recs if r["display_name"] == "Dev1")

    low, critical = dev1_score("Low"), dev1_score("Critical")
    assert critical["score"] < low["score"]
    assert "weighted heavily at this severity" in critical["reasons"][-1] and "weighted heavily" not in low["reasons"][-1]


def test_new_developer_is_included_when_declared_skills_match(trained_world, make_user):
    world = trained_world
    extra = [make_user(f"senior{i}") for i in range(2)]
    newcomer = make_user("newcomer")
    for actor in (*extra, newcomer):
        world.manager.post(f"/projects/{world.pid}/members", {"email": actor.user["email"], "role": "developer"})
    # two more developers with a real history on payments, so the top three is full without the newcomer
    for n, actor in enumerate(extra):
        world.admin.patch(f"/admin/profiles/{world.profile_id(actor)}", {"components": ["checkout"], "max_capacity": 20})
        for i in range(3):
            title = f"{PAYMENT[i]} variant {n}{i}"
            bug = world.report(title=title, description=describe(title), component="checkout")
            world.resolve(bug["id"], actor)
    world.admin.patch(f"/admin/profiles/{world.profile_id(newcomer)}", {"skills": ["checkout"], "components": []})
    bug = world.report(title=PAYMENT[7], description=describe(PAYMENT[7]), component="checkout")
    recs = world.manager.get(f"/bugs/{bug['id']}/recommendations").json()["recommendations"]
    assert len(recs) == 3
    new = next(r for r in recs if r["display_name"] == "Newcomer")  # FR-28
    assert new["limited_history"] is True and "Limited history (0 verified bugs here)" in " ".join(new["reasons"])
    assert sum(r["limited_history"] for r in recs) == 1
    # without matching declared skills the absence of history earns no place
    world.admin.patch(f"/admin/profiles/{world.profile_id(newcomer)}", {"skills": ["cobol"]})
    recs = world.manager.get(f"/bugs/{bug['id']}/recommendations").json()["recommendations"]
    assert all(r["display_name"] != "Newcomer" for r in recs)


def test_retraining_schedule(trained_world, db, monkeypatch):
    world = trained_world
    state = db.get(ModelState, world.pid)
    first_trained = state.last_trained_at
    assert coldstart.training_due(state) is False
    state.verified_count = state.verified_at_last_training + get_settings().retrain_after_verified
    assert coldstart.training_due(state) is True  # 200 further verified bugs
    state.verified_count = state.verified_at_last_training
    state.last_trained_at = utcnow() - timedelta(days=31)
    assert coldstart.training_due(state) is True  # 30 days
    db.commit()
    assert coldstart.retrain_due_projects() == 1
    db.expire_all()
    assert db.get(ModelState, world.pid).last_trained_at > first_trained
    assert coldstart.retrain_due_projects() == 0
    # manual retrain by the Administrator
    assert world.manager.post(f"/admin/projects/{world.pid}/retrain").status_code == 403
    assert world.admin.post("/admin/projects/999/retrain").status_code == 404
    assert world.admin.post(f"/admin/projects/{world.pid}/retrain").status_code == 202
    # a failed retraining keeps the previous model in service and records the error
    monkeypatch.setattr(cnn_lstm, "train_model", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("out of memory")))
    assert coldstart.train_project(world.pid, force=True) is False
    db.expire_all()
    state = db.get(ModelState, world.pid)
    assert state.state == "active" and state.last_error == "out of memory" and state.training_started_at is None


def test_retrain_refused_during_cold_start_and_training_needs_two_developers(world, db):
    assert world.admin.post(f"/admin/projects/{world.pid}/retrain").status_code == 409
    for i in range(12):
        bug = world.report(title=PAYMENT[i % 8] + f" {i}", description=describe(PAYMENT[i % 8]))
        world.resolve(bug["id"], world.dev1)
    state = db.get(ModelState, world.pid)
    assert state.state == "cold_start" and "two developers" in state.last_error


def test_cnn_lstm_learns_and_round_trips():
    texts = [f"payment checkout card refund {i}" for i in range(30)] + [f"login password session oauth {i}" for i in range(30)]
    labels = [101] * 30 + [202] * 30
    metas = [cnn_lstm.encode_meta(i % 4, i % 5 == 0) for i in range(60)]
    blob, metrics = cnn_lstm.train_model(texts, metas, labels, epochs=8, device="cpu")
    assert metrics["developers"] == 2 and metrics["validation"]["top1"] >= 0.8
    model = cnn_lstm.AssignmentModel(blob)
    probabilities = model.probabilities("refund failed at checkout with card", 2, False)
    assert set(probabilities) == {101, 202} and probabilities[101] > probabilities[202]
    assert abs(sum(probabilities.values()) - 1.0) < 1e-5
    assert cnn_lstm.encode_meta(None, True) == [0, 0, 0, 0, 1.0, 1.0]
    # an explicit validation set with an unseen developer is handled
    _, metrics = cnn_lstm.train_model(texts, metas, labels, epochs=1, device="cpu", val_data=(texts[:4] + ["x"], metas[:5], labels[:4] + [999]))
    assert metrics["validation_samples"] == 4


# ---- service-level ------------------------------------------------------------
def test_ai_status_and_health(client, world):
    status = world.reporter.get("/ai/status").json()
    assert status == {"enabled": True, "severity": "rules", "duplicates": "bm25+lexical",
                      "assignment": "per-project (see project dashboard)", "detail": {"severity_model": {}, "sbert_model": None}}
    assert client.get("/health").json() == {"status": "ok", "ai": True}
    assert engine.available


def test_openapi_document_is_version_3_0(client):
    schema = client.get(API + "/openapi.json").json()
    assert schema["openapi"] == "3.0.3" and len(schema["paths"]) >= 45
    text = str(schema)
    assert "'type': 'null'" not in text and "'const'" not in text and "contentMediaType" not in text
    assert schema["components"]["schemas"]["BugOut"]["properties"]["component"] == {"type": "string", "title": "Component", "nullable": True}
    for path, operations in schema["paths"].items():
        for operation in operations.values():
            assert operation.get("summary"), path  # every endpoint is documented (NFR-13)


def test_openapi_downgrade_rules():
    converted = downgrade_to_openapi_30({"openapi": "3.1.0", "x": {
        "a": {"anyOf": [{"$ref": "#/c/T"}, {"type": "null"}], "title": "A"},
        "b": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
        "c": {"type": ["string", "null"]}, "d": {"const": "bearer"}, "e": {"type": "integer", "exclusiveMinimum": 0},
        "f": {"type": "string", "examples": ["x"]}, "g": {"type": "string", "contentMediaType": "application/octet-stream"},
        "h": {"type": "null"}}})["x"]
    assert converted["a"] == {"allOf": [{"$ref": "#/c/T"}], "title": "A", "nullable": True}
    assert converted["b"] == {"anyOf": [{"type": "string"}, {"type": "integer"}], "nullable": True}
    assert converted["c"] == {"type": "string", "nullable": True} and converted["d"] == {"enum": ["bearer"]}
    assert converted["e"] == {"type": "integer", "minimum": 0, "exclusiveMinimum": True}
    assert converted["f"] == {"type": "string", "example": "x"} and converted["g"] == {"type": "string", "format": "binary"}
    assert converted["h"] == {"nullable": True}


def test_bug_text_edit_refreshes_keywords(world, db):
    bug = world.report()
    world.reporter.patch(f"/bugs/{bug['id']}", {"description": "The invoice generator produces an empty spreadsheet every time."})
    assert "invoice" in db.get(Bug, bug["id"]).keywords
    r = world.reporter.post(f"/projects/{world.pid}/duplicates/check", {"description": "Invoice generator gives an empty spreadsheet"})
    assert r.json()["candidates"][0]["bug_id"] == bug["id"]
