"""System-level latency and throughput check (Section 3.6.4, NFR-01 to NFR-03).

    python -m scripts.loadtest [BASE_URL] [--users 50]

Needs a server seeded with scripts.seed. Measures:
  NFR-02  duplicate check, submission (duplicate + severity) and recommendation latency
  NFR-03  the same submission with 50 reports sent simultaneously, against a single-user baseline
  NFR-01  bug list, bug detail and dashboard with 100 concurrent users

Writes docs/evaluation/system_latency.json. For a longer soak test with a
realistic mix of actions use scripts/locustfile.py.
"""
import argparse
import itertools
import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scripts.seed import DEMO_PASSWORD, PROJECT_NAME

ROOT = Path(__file__).resolve().parents[2]
SUBJECTS = ["checkout page", "login form", "search box", "PDF export", "text editor", "file sync", "invoice", "session handling"]
FAILURES = ["crashes with a NullPointerException", "times out before finishing", "shows stale information", "has a typo in the tooltip",
            "loses unsaved data", "is slower than expected", "returns the wrong result", "hangs and never completes"]


def summarise(samples: list[float]) -> dict:
    ordered = sorted(samples)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
    return {"n": len(ordered), "median_ms": round(statistics.median(ordered), 1), "p95_ms": round(pick(0.95), 1), "max_ms": round(ordered[-1], 1)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("base", nargs="?", default="http://127.0.0.1:8000")
    parser.add_argument("--users", type=int, default=50)
    args = parser.parse_args()
    # Several comma-separated servers are used round-robin, standing in for a load balancer (NFR-08).
    bases = [b.rstrip("/") + "/api/v1" for b in args.base.split(",")]
    base = bases[0]
    turn = itertools.count()
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    client = httpx.Client(timeout=60, limits=limits)

    def login(email: str) -> dict:
        r = client.post(f"{base}/auth/login", json={"email": email, "password": DEMO_PASSWORD})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    reporter, manager = login("reporter@demo.example.com"), login("manager@demo.example.com")
    pid = next(p["id"] for p in client.get(f"{base}/projects", headers=reporter).json() if p["name"] == PROJECT_NAME)
    corpus = client.get(f"{base}/projects/{pid}/bugs", headers=reporter, params={"page_size": 1}).json()["total"]
    counter = iter(range(10_000_000))
    lock = threading.Lock()

    def report_body() -> dict:
        with lock:
            n = next(counter)
        subject, failure = SUBJECTS[n % len(SUBJECTS)], FAILURES[(n // len(SUBJECTS)) % len(FAILURES)]
        return {"title": f"The {subject} {failure} (load test {n})",
                "description": f"While using the {subject} the application {failure}. Reproduced in load test run number {n} on the staging build."}

    def timed(method: str, path: str, headers: dict, **kwargs) -> float:
        started = time.perf_counter()
        response = client.request(method, bases[next(turn) % len(bases)] + path, headers=headers, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        response.raise_for_status()
        return elapsed

    def concurrently(n: int, action) -> list[float]:
        barrier = threading.Barrier(n)

        def run(_):
            barrier.wait()  # release every request at the same instant
            return action()

        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(run, range(n)))

    submit = lambda: timed("POST", f"/projects/{pid}/bugs", reporter, json=report_body())  # noqa: E731
    check = lambda: timed("POST", f"/projects/{pid}/duplicates/check", reporter, json=report_body())  # noqa: E731
    bug_id = client.post(f"{base}/projects/{pid}/bugs", headers=reporter, json=report_body()).json()["bug"]["id"]
    for _ in range(3):  # warm caches and connections
        check(), submit()

    results = {"corpus_size": corpus, "concurrent_reports": args.users, "backend_instances": len(bases)}
    print(f"Corpus: {corpus} bugs. Sequential measurements...")
    results["duplicate_check_sequential"] = summarise([check() for _ in range(30)])
    results["submit_sequential"] = summarise([submit() for _ in range(30)])
    results["recommendation_sequential"] = summarise([timed("GET", f"/bugs/{bug_id}/recommendations", manager) for _ in range(30)])
    print(f"{args.users} simultaneous reports...")
    results["submit_concurrent"] = summarise([t for _ in range(3) for t in concurrently(args.users, submit)])
    results["duplicate_check_concurrent"] = summarise([t for _ in range(3) for t in concurrently(args.users, check)])
    print("100 concurrent page loads...")
    pages = {"bug_list": lambda: timed("GET", f"/projects/{pid}/bugs", reporter),
             "bug_detail": lambda: timed("GET", f"/bugs/{bug_id}", reporter),
             "dashboard": lambda: timed("GET", f"/projects/{pid}/dashboard", reporter),
             "keyword_search": lambda: timed("GET", f"/projects/{pid}/bugs", reporter, params={"q": "checkout timeout"})}
    for name, action in pages.items():
        results[f"page_{name}_100_users"] = summarise(concurrently(100, action))

    base_median, loaded_median = results["submit_sequential"]["median_ms"], results["submit_concurrent"]["median_ms"]
    verdicts = {
        "NFR-02 duplicate check <= 1500 ms (p95, sequential)": results["duplicate_check_sequential"]["p95_ms"] <= 1500,
        "NFR-02 submission <= 1500 ms (p95, sequential)": results["submit_sequential"]["p95_ms"] <= 1500,
        "NFR-02 recommendation <= 3000 ms (p95)": results["recommendation_sequential"]["p95_ms"] <= 3000,
        f"NFR-02 submission <= 1500 ms with {args.users} simultaneous (p95)": results["submit_concurrent"]["p95_ms"] <= 1500,
        "NFR-01 pages <= 2000 ms with 100 users (p95)": all(results[f"page_{n}_100_users"]["p95_ms"] <= 2000 for n in pages),
        f"NFR-03 median within 10% of baseline at {args.users} simultaneous": loaded_median <= base_median * 1.10,
    }
    results["submit_median_change_under_load"] = f"{(loaded_median / base_median - 1):+.0%}"
    results["verdicts"] = verdicts
    results["measured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results["server"] = args.base

    for name, value in results.items():
        if isinstance(value, dict) and "median_ms" in value:
            print(f"  {name:<34} median {value['median_ms']:>8} ms   p95 {value['p95_ms']:>8} ms   max {value['max_ms']:>8} ms")
    print(f"  submission median under load vs baseline: {results['submit_median_change_under_load']}")
    for name, ok in verdicts.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    out = ROOT / "docs" / "evaluation"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"system_latency_{len(bases)}_instance{'s' if len(bases) > 1 else ''}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
