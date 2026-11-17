"""End-to-end smoke check against a running server seeded with scripts.seed.

    python -m scripts.smoke [BASE_URL]

Walks the workflow of Section 3.5.4 through the public API and prints the
latency of each AI stage next to its NFR-02 budget.
"""
import sys
import time

import httpx

from scripts.seed import DEMO_PASSWORD, PROJECT_NAME

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/") + "/api/v1"


def login(client: httpx.Client, email: str) -> dict:
    r = client.post(f"{BASE}/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def timed(label: str, budget_ms: float, call):
    started = time.perf_counter()
    response = call()
    elapsed = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    verdict = "ok" if elapsed <= budget_ms else "OVER BUDGET"
    print(f"  {label:<34}{elapsed:8.0f} ms   (budget {budget_ms:.0f} ms)  {verdict}")
    return response.json(), elapsed


def main() -> int:
    over = 0
    with httpx.Client(timeout=30) as client:
        reporter, manager = login(client, "reporter@demo.example.com"), login(client, "manager@demo.example.com")
        developer = login(client, "checkout.dev1@demo.example.com")
        project = next(p for p in client.get(f"{BASE}/projects", headers=reporter).json() if p["name"] == PROJECT_NAME)
        pid = project["id"]
        print("AI status:", client.get(f"{BASE}/ai/status", headers=reporter).json())

        text = {"title": "Paying for an order kills the app",
                "description": "Whenever I try to pay on the checkout screen the program dies and everything I typed is gone."}
        client.post(f"{BASE}/projects/{pid}/duplicates/check", headers=reporter, json=text)  # warm the project index
        print("Latency:")
        dup, ms = timed("duplicate check while typing", 1500, lambda: client.post(f"{BASE}/projects/{pid}/duplicates/check", headers=reporter, json=text))
        over += ms > 1500
        for c in dup["candidates"]:
            print(f"      {c['match_percent']:3d}%  #{c['bug_id']} {c['title'][:70]}")
        created, ms = timed("submit (duplicate + severity)", 1500, lambda: client.post(f"{BASE}/projects/{pid}/bugs", headers=reporter, json={**text, "component": "checkout"}))
        over += ms > 1500
        bug = created["bug"]
        print(f"      severity {bug['severity']} ({bug['severity_source']}), stage timings {created['timings_ms']}")
        recs, ms = timed("assignment recommendation", 3000, lambda: client.get(f"{BASE}/bugs/{bug['id']}/recommendations", headers=manager))
        over += ms > 3000
        print(f"      mode: {recs['mode']}")
        for r in recs["recommendations"]:
            print(f"      {r['score']:.2f}  {r['display_name']}: {r['reasons'][0]}")
        _, ms = timed("bug list page", 2000, lambda: client.get(f"{BASE}/projects/{pid}/bugs", headers=reporter))
        over += ms > 2000
        _, ms = timed("keyword search", 2000, lambda: client.get(f"{BASE}/projects/{pid}/bugs", headers=reporter, params={"q": "checkout timeout"}))
        over += ms > 2000
        _, ms = timed("dashboard", 2000, lambda: client.get(f"{BASE}/projects/{pid}/dashboard", headers=reporter))
        over += ms > 2000

        if recs["recommendations"]:
            assignee = recs["recommendations"][0]["user_id"]
            client.post(f"{BASE}/bugs/{bug['id']}/assign", headers=manager, json={"assignee_id": assignee, "override_exclusion": True}).raise_for_status()
            for status in ("In Progress", "Resolved"):
                client.post(f"{BASE}/bugs/{bug['id']}/status", headers=manager, json={"status": status}).raise_for_status()
            client.post(f"{BASE}/bugs/{bug['id']}/status", headers=reporter, json={"status": "Verified"}).raise_for_status()
            trail = client.get(f"{BASE}/bugs/{bug['id']}/audit", headers=developer).json()
            print("Lifecycle: assigned, resolved and verified;", len(trail), "audit entries")
    print("All latency budgets met." if not over else f"{over} budget(s) exceeded.")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
