"""Synthetic bug reports for development, demos and pipeline checks.

These exist so that the training and evaluation scripts, the seed script and
the load test can be exercised without the real datasets. Numbers obtained on
this data say nothing about real-world accuracy and must never be reported as
results; the real figures come from the Eclipse, Mozilla and Meng et al. data.
"""
import random

import pandas as pd

AREAS = {
    "checkout": {
        "nouns": ["checkout page", "payment form", "order summary", "discount code field", "card validation", "invoice", "refund flow", "shipping calculator"],
        "tech": ["payment gateway", "PaymentService", "order total", "currency rounding", "CartController", "tax calculation"],
    },
    "auth": {
        "nouns": ["login form", "password reset", "session handling", "two factor prompt", "signup page", "account lockout", "OAuth callback", "remember me option"],
        "tech": ["AuthService", "session token", "JWT refresh", "SessionManager", "credential check", "SSO redirect"],
    },
    "search": {
        "nouns": ["search box", "result list", "filter panel", "autocomplete", "sort order", "pagination", "saved searches", "search index"],
        "tech": ["SearchIndexer", "query parser", "ranking function", "IndexWriter", "facet counts", "relevance score"],
    },
    "reports": {
        "nouns": ["PDF export", "CSV export", "weekly report", "chart rendering", "dashboard widget", "print layout", "scheduled report", "data table"],
        "tech": ["ReportBuilder", "export job", "chart library", "PdfRenderer", "aggregation query", "report cache"],
    },
    "editor": {
        "nouns": ["text editor", "syntax highlighting", "code completion", "undo history", "find and replace", "file tabs", "split view", "line numbers"],
        "tech": ["EditorBuffer", "language server", "tokenizer", "UndoManager", "render loop", "keybinding handler"],
    },
    "sync": {
        "nouns": ["file sync", "offline mode", "conflict resolution", "upload queue", "background refresh", "push notifications", "data import", "backup job"],
        "tech": ["SyncEngine", "retry queue", "websocket connection", "MergeResolver", "change feed", "replication lag"],
    },
}

# (bugzilla severity, weight, failure phrases)
FAILURES = [
    ("blocker", 3, ["makes the whole application unusable", "prevents anyone from starting the application", "blocks every user from working"]),
    ("critical", 8, ["crashes the application and loses unsaved data", "corrupts the stored data", "causes a crash with a NullPointerException", "exposes other users' private data"]),
    ("major", 22, ["throws an exception and the action fails", "stops working after the latest update", "fails with a timeout error", "hangs and never completes", "returns the wrong result"]),
    ("normal", 45, ["behaves inconsistently between runs", "is slower than expected", "shows stale information until refreshed", "does not remember the last setting", "ignores the selected option"]),
    ("minor", 15, ["is slightly misaligned on small screens", "shows a confusing label", "uses the wrong icon", "has an awkward tab order"]),
    ("trivial", 7, ["has a typo in the tooltip", "has a spelling mistake in the heading", "uses inconsistent capitalisation", "has a cosmetic colour mismatch"]),
]

TITLE_FORMS = ["{noun} {failure}", "The {noun} {failure}", "{tech}: {noun} {failure}", "After update the {noun} {failure}"]
DETAIL_FORMS = [
    "When I use the {noun}, it {failure}. I traced it to the {tech}. It happens on {env} every time I try.",
    "Steps: open the application, go to the {noun} and perform the usual action. The {noun} {failure}. Logs mention {tech}. Seen on {env}.",
    "Our team noticed that the {noun} {failure}. This started recently and seems related to {tech}. Environment: {env}.",
    "The {noun} {failure} whenever a large input is used. The {tech} looks suspicious. Reproduced on {env}.",
]
PARAPHRASE_FORMS = [
    "Something is wrong around the {noun}: it {failure_alt}. Possibly the {tech} is involved. Tested on {env}.",
    "I keep hitting a problem where the {noun} {failure_alt}. Could be {tech}. Using {env}.",
]
FAILURE_ALTERNATIVES = {
    "crashes the application and loses unsaved data": "kills the app and my unsaved work is gone",
    "throws an exception and the action fails": "raises an error so the operation does not go through",
    "fails with a timeout error": "times out before finishing",
    "hangs and never completes": "freezes and stays stuck forever",
    "is slower than expected": "takes far too long to respond",
    "has a typo in the tooltip": "contains a misspelled word in the hover text",
}
ENVIRONMENTS = ["Windows 11, Chrome 130", "macOS 15, Safari 18", "Ubuntu 24.04, Firefox 131", "Windows 10, Edge 129", "version 4.2.1 on Linux"]


def generate(n: int = 2000, developers_per_area: int = 3, duplicate_rate: float = 0.15, seed: int = 7) -> pd.DataFrame:
    """Return reports in the canonical schema used by ml/datasets.py."""
    rng = random.Random(seed)
    areas = list(AREAS)
    severities = [f[0] for f in FAILURES]
    weights = [f[1] for f in FAILURES]
    phrases = {f[0]: f[2] for f in FAILURES}
    rows: list[dict] = []
    for bug_id in range(1, n + 1):
        if rows and rng.random() < duplicate_rate:
            original = rng.choice(rows)
            while original["dup_id"] is not None:
                original = next(r for r in rows if r["bug_id"] == original["dup_id"])
            failure_alt = FAILURE_ALTERNATIVES.get(original["_failure"], original["_failure"])
            description = rng.choice(PARAPHRASE_FORMS).format(noun=original["_noun"], failure_alt=failure_alt, tech=original["_tech"], env=rng.choice(ENVIRONMENTS))
            rows.append({**original, "bug_id": bug_id, "title": f"{original['_noun'].capitalize()} problem: {failure_alt}", "description": description, "dup_id": original["bug_id"]})
            continue
        area = rng.choice(areas)
        severity = rng.choices(severities, weights)[0]
        noun, tech, failure = rng.choice(AREAS[area]["nouns"]), rng.choice(AREAS[area]["tech"]), rng.choice(phrases[severity])
        # Each area has a small team; the first member takes most of the work, as in real projects.
        developer = rng.choices(range(developers_per_area), weights=[5, 3, 2][:developers_per_area] + [1] * max(0, developers_per_area - 3))[0]
        if rng.random() < 0.1:  # some noise: work that lands outside the usual team
            area_for_owner = rng.choice(areas)
        else:
            area_for_owner = area
        rows.append({
            "bug_id": bug_id,
            "title": rng.choice(TITLE_FORMS).format(noun=noun, failure=failure, tech=tech)[:200],
            "description": rng.choice(DETAIL_FORMS).format(noun=noun, failure=failure, tech=tech, env=rng.choice(ENVIRONMENTS)),
            "severity": severity,
            "assignee": f"{area_for_owner}.dev{developer + 1}@example.org",
            "component": area,
            "product": "Synthetic",
            "dup_id": None,
            "_noun": noun, "_tech": tech, "_failure": failure,
        })
    frame = pd.DataFrame(rows).drop(columns=["_noun", "_tech", "_failure"])
    frame["created"] = pd.date_range("2024-01-01", periods=n, freq="h")
    return frame


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Write a synthetic sample in the canonical CSV schema")
    parser.add_argument("--rows", type=int, default=2000)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[2] / "data" / "synthetic_sample.csv")
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    generate(args.rows).to_csv(args.out, index=False)
    print(f"Wrote {args.rows} synthetic reports to {args.out}")
