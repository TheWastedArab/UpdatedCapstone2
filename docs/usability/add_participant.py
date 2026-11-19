"""Facilitator helper for the usability study (NFR-11).

    python docs/usability/add_participant.py --setup             # once, before the first session
    python docs/usability/add_participant.py p3@study.example.com  # just before handing over Task 1
    python docs/usability/add_participant.py --status                # before Task 5

--setup archives what earlier sessions left (the bugs participants filed and the used Task 3
copy: neutral title and description, moved to Closed), so every participant starts from the
same state, then files the open "login" bug that Task 3 asks participants to find.
An email address waits until that account is registered and then adds it to the Demo Shop
project as a Reporter at once, standing in for the project manager who would invite a new
user. The participant is never told about it and the stopwatch is never paused.
--status moves the Task 3 bug, which the participant now watches, to "In Progress" for Task 5
(run --setup again before the next participant's Task 3 to file a fresh open copy).

Signs in as the seeded project manager. Server URL: BT_URL, default http://127.0.0.1:8000.
Development data only: never run this against a deployed system.
"""
import os
import sys
import time

import httpx

BASE = os.environ.get("BT_URL", "http://127.0.0.1:8000").rstrip("/") + "/api/v1"
MANAGER = "manager@demo.example.com"
PASSWORD = "Demo!Pass123"  # the seed's DEMO_PASSWORD
PROJECT = "Demo Shop"
WAIT_SECONDS = 15 * 60
TASK3_TITLE = "Login page rejects a correct password after the session has expired"
TASK3_TEXT = ("Steps: sign in, leave the tab idle for 40 minutes, sign out and sign in again with the same "
              "password. Expected: signed in. Actual: 'Invalid email or password' until the page is reloaded.")
STUDY_DOMAIN = "@study.example.com"
ARCHIVED_TITLE = "Archived usability-study item"
ARCHIVED_TEXT = "Archived after a usability-study session; not a real defect report."
CLOSE_PATH = ["Open", "In Progress", "Resolved", "Verified", "Closed"]


def archive_earlier_sessions(client: httpx.Client, pid: int) -> None:
    """Give every participant the same starting point.

    Bugs filed by earlier participants (Task 2) would trigger the duplicate warning for the
    next one, and used Task 3 copies would show up beside the fresh one in a "login" search.
    Each is given a neutral title and description, so search and duplicate detection no
    longer match it, and is moved on to Closed.
    """
    stale, page = [], 1
    while True:
        found = client.get(f"/projects/{pid}/bugs", params={"page": page, "page_size": 100}).raise_for_status().json()
        for bug in found["items"]:
            by_participant = (bug.get("reporter") or {}).get("email", "").endswith(STUDY_DOMAIN)
            used_task3 = bug["title"] == TASK3_TITLE and bug["status"] != "Open"
            archived = bug["title"] == ARCHIVED_TITLE
            # An archived bug that did not reach Closed last time is retried, not skipped.
            if (by_participant or used_task3 or archived) and not (archived and bug["status"] == "Closed"):
                stale.append(bug)
        if page * found["page_size"] >= found["total"]:
            break
        page += 1
    closed = 0
    for bug in stale:
        if bug["title"] != ARCHIVED_TITLE:
            client.patch(f"/bugs/{bug['id']}", json={"title": ARCHIVED_TITLE, "description": ARCHIVED_TEXT}).raise_for_status()
        status = bug["status"]
        for target in CLOSE_PATH[CLOSE_PATH.index(status) + 1:] if status in CLOSE_PATH else []:
            response = client.post(f"/bugs/{bug['id']}/status", json={"status": target})
            if not response.is_success:
                # The neutral title already keeps it out of the next session; the next --setup retries.
                print(f"Bug #{bug['id']} archived but left at {status}: {response.status_code} {response.text}")
                break
            status = target
        closed += status == "Closed"
    if stale:
        print(f"Archived {len(stale)} bug(s) from earlier sessions, {closed} of them closed")


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
        archive_earlier_sessions(client, pid)
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
        # Started before Task 1: wait until the participant's registration creates the account,
        # then add it at once, so the project is there when they first sign in.
        print(f"Waiting for {args[0]} to register (Ctrl+C to stop)...", flush=True)
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            response = client.post(f"/projects/{pid}/members", json={"email": args[0], "role": "reporter"})
            if response.status_code != 404:
                break
            if time.monotonic() > deadline:
                sys.exit(f"No account for {args[0]} after {WAIT_SECONDS // 60} minutes")
            time.sleep(0.5)
        print(response.status_code, "added as Reporter" if response.is_success else response.text)


if __name__ == "__main__":
    main(sys.argv[1:])
