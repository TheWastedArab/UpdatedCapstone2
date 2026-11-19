"""FR-07 volume check: keyword search and filters against a project of 50,000 bugs.

    python -m scripts.seed --bugs 50000          (into an empty database)
    uvicorn app.main:app --port 8000
    python -m scripts.search_volume http://127.0.0.1:8000

Each query is sent 30 times by one user, then once more by each of 20 users at the
same time. FR-07 requires results within two seconds. Writes
docs/evaluation/search_50k.json.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from scripts.loadtest import summarise
from scripts.seed import DEMO_PASSWORD, PROJECT_NAME

ROOT = Path(__file__).resolve().parents[2]
TARGET_MS = 2000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--users", type=int, default=20)
    args = parser.parse_args()
    base = args.base.rstrip("/") + "/api/v1"
    client = httpx.Client(timeout=60, limits=httpx.Limits(max_connections=100))
    token = client.post(f"{base}/auth/login", json={"email": "reporter@demo.example.com", "password": DEMO_PASSWORD}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    pid = next(p["id"] for p in client.get(f"{base}/projects", headers=headers).json() if p["name"] == PROJECT_NAME)
    total = client.get(f"{base}/projects/{pid}/bugs", headers=headers, params={"page_size": 1}).json()["total"]
    week_ago = (datetime.now(timezone.utc) - timedelta(days=21)).date().isoformat()
    queries = {
        "keyword": {"q": "checkout timeout"},
        "keyword, rare term": {"q": "invoice rounding"},
        "keyword + status + severity": {"q": "crash", "status": ["Open", "In Progress"], "severity": "Critical"},
        "keyword + date range": {"q": "login", "created_from": week_ago},
        "filters only (status, severity)": {"status": "Open", "severity": "High"},
        "no filter, first page": {},
    }

    def timed(params: dict) -> tuple[float, int]:
        r = client.get(f"{base}/projects/{pid}/bugs", headers=headers, params=params)
        r.raise_for_status()
        return r.elapsed.total_seconds() * 1000, r.json()["total"]

    results = {}
    for name, params in queries.items():
        timed(params)  # warm-up
        single = [timed(params)[0] for _ in range(args.repeats)]
        with ThreadPoolExecutor(args.users) as pool:
            concurrent = [ms for ms, _ in pool.map(lambda _: timed(params), range(args.users))]
        matches = timed(params)[1]
        results[name] = {"params": params, "matches": matches, "single_user": summarise(single), f"{args.users}_users_at_once": summarise(concurrent)}
        print(f"{name:<34} {matches:>6} matches  single p95 {results[name]['single_user']['p95_ms']:>7} ms  "
              f"{args.users} at once p95 {results[name][f'{args.users}_users_at_once']['p95_ms']:>7} ms")
    worst = max(max(r["single_user"]["p95_ms"], r[f"{args.users}_users_at_once"]["p95_ms"]) for r in results.values())
    report = {"measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "bugs_in_project": total,
              "target_ms": TARGET_MS, "worst_p95_ms": worst, "target_met": worst <= TARGET_MS, "queries": results}
    out = ROOT / "docs" / "evaluation" / "search_50k.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{total} bugs; worst p95 {worst} ms against {TARGET_MS} ms: {'met' if worst <= TARGET_MS else 'NOT met'}. Written to {out}")


if __name__ == "__main__":
    main()
