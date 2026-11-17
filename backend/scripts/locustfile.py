"""Locust scenario: a realistic mix of user actions for soak testing.

    locust -f scripts/locustfile.py --headless -u 50 -r 10 -t 2m --host http://127.0.0.1:8000

Needs a server seeded with scripts.seed.
"""
import random

from locust import HttpUser, between, task

from scripts.seed import DEMO_PASSWORD, PROJECT_NAME

API = "/api/v1"
ACCOUNTS = ["reporter@demo.example.com", "tester@demo.example.com", "manager@demo.example.com",
            "checkout.dev1@demo.example.com", "auth.dev1@demo.example.com", "search.dev2@demo.example.com"]


class TrackerUser(HttpUser):
    wait_time = between(1, 4)

    def on_start(self):
        self.email = random.choice(ACCOUNTS)
        token = self.client.post(f"{API}/auth/login", json={"email": self.email, "password": DEMO_PASSWORD}).json()["access_token"]
        self.client.headers["Authorization"] = f"Bearer {token}"
        self.project = next(p["id"] for p in self.client.get(f"{API}/projects").json() if p["name"] == PROJECT_NAME)
        self.bug_ids = [b["id"] for b in self.client.get(f"{API}/projects/{self.project}/bugs").json()["items"]]

    @task(5)
    def list_bugs(self):
        self.client.get(f"{API}/projects/{self.project}/bugs", name="bug list")

    @task(5)
    def view_bug(self):
        bug = random.choice(self.bug_ids)
        self.client.get(f"{API}/bugs/{bug}", name="bug detail")
        self.client.get(f"{API}/bugs/{bug}/comments", name="bug comments")

    @task(2)
    def search(self):
        self.client.get(f"{API}/projects/{self.project}/bugs", params={"q": random.choice(["crash", "timeout", "checkout", "login typo"])}, name="search")

    @task(1)
    def dashboard(self):
        self.client.get(f"{API}/projects/{self.project}/dashboard", name="dashboard")

    @task(3)
    def type_a_report(self):
        text = f"The {random.choice(['checkout page', 'login form', 'PDF export'])} fails with a timeout error when a large input is used."
        self.client.post(f"{API}/projects/{self.project}/duplicates/check", json={"title": "", "description": text}, name="duplicate check")

    @task(1)
    def submit_report(self):
        n = random.randrange(10**9)
        r = self.client.post(f"{API}/projects/{self.project}/bugs", name="submit", json={
            "title": f"Soak test report {n}", "description": f"The search box returns the wrong result for query number {n} in the soak test."})
        if r.status_code == 201:
            self.bug_ids.append(r.json()["bug"]["id"])

    @task(1)
    def notifications(self):
        self.client.get(f"{API}/notifications/unread-count", name="unread count")
