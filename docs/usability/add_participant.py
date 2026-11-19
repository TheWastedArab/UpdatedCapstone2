"""Facilitator helper for the usability study (NFR-11).

    python docs/usability/add_participant.py --setup             # once, before the first session
    python docs/usability/add_participant.py p3@study.example.com  # after a participant registers
    python docs/usability/add_participant.py --status                   # before Task 5

--setup files the open "login" bug that Task 3 asks participants to find.
An email address adds that new account to the Demo Shop project as a Reporter, standing in
for the project manager who would invite a real new user (pause the stopwatch meanwhile).
--status moves the Task 3 bug, which the participant now watches, to "In Progress" for Task 5
(run --setup again before the next participant's Task 3 to file a fresh open copy).

Signs in as the seeded project manager. Server URL: BT_URL, default http://127.0.0.1:8000.
Development data only: never run this against a deployed system.
"""
import os
import sys

import httpx

BASE = os.environ.get("BT_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1"
MANAGER = "manager@demo.example.com"
PASSWORD = "Demo!Pass123"  # the seed's DEMO_PASSWORD
PROJECT = "Demo Shop"
TASK3_TITLE = "Login page rejects a correct password after the session has expired"
TASK3_TEXT = ("Steps: sign in, leave the tab idle for 40 minutes, sign out and sign in again with the same "
              "password. Expected: signed in. Actual: 'Invalid email or password' until the page is reloaded.")


def main(args: list[str]) -> None:
    if not args:
        sys.exit(__doc__)
    client = httpx.Client(base_url=BASE, timeout=30)
    token = client.post("/auth/login", json={"email": MANAGER, "password": PASSWORD}).raise_for_status().json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    project = next((p for p in client.get("/projects").json() if p["name"] == PROJECT), None)
    if project is None:
        sys.exit(f'Project "{PROJECT}" not found: run python -m scripts.seed in backend/ first')
    pid = project["id"]

    def open_task3_bugs() -> list[dict]:
        found = client.get(f"/projects/{pid}/bugs", params={"q": "Login page rejects", "status": "Open"}).json()
        items = found["items"] if isinstance(found, dict) else found
        return [b for b in items if b["title"] == TASK3_TITLE]

    if args[0] == "--setup":
        if open_task3_bugs():
            print("An open Task 3 bug is already present")
        else:
            bug = client.post(f"/projects/{pid}/bugs", json={"title": TASK3_TITLE, "description": TASK3_TEXT}).raise_for_status().json()["bug"]
            print(f"Filed Task 3 bug #{bug['id']}")
    elif args[0] == "--status":
        bugs = open_task3_bugs()
        if not bugs:
            sys.exit("No open Task 3 bug: run --setup before the session")
        response = client.post(f"/bugs/{bugs[0]['id']}/status", json={"status": "In Progress"})
        print(response.status_code, "status changed to In Progress" if response.is_success else response.text)
    else:
        response = client.post(f"/projects/{pid}/members", json={"email": args[0], "role": "reporter"})
        if response.status_code == 404:
            sys.exit("No active account with that email yet: has the participant finished verifying it?")
        print(response.status_code, "added as Reporter" if response.is_success else response.text)


if __name__ == "__main__":
    main(sys.argv[1:])
