"""Results of the usability study (NFR-11): completion, times, SUS, and the verdict.

    python docs/usability/analyze.py docs/usability/timing_sheet.csv docs/usability/sus_responses.csv
    python docs/usability/analyze.py ... --allow-example   # try the script on the example rows

Writes docs/evaluation/usability.json next to the other evaluation results.
Standard library only, so it runs without the backend's environment.
"""
import csv
import json
import statistics
import sys
from pathlib import Path

LIMIT_SECONDS = 300           # NFR-11: first report within 5 minutes of registering
REQUIRED_PASSES = 4           # of 5 participants (README, "Pass criteria")
SUS_BENCHMARK = 68            # mean SUS across published studies (Bangor et al., 2008)
TASK_LIMITS = {1: 180, 2: 240, 3: 120, 4: 120, 5: 120}


def sus_score(answers: list[int]) -> float:
    if len(answers) != 10 or any(a not in (1, 2, 3, 4, 5) for a in answers):
        raise ValueError(f"SUS needs ten answers from 1 to 5, got {answers}")
    return 2.5 * sum((a - 1) if i % 2 == 0 else (5 - a) for i, a in enumerate(answers))


def load(path: str, allow_example: bool) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    examples = [r for r in rows if r["participant"].strip().upper() == "EXAMPLE"]
    if examples and not allow_example:
        sys.exit(f"{path} still contains EXAMPLE rows: delete them before analysing real sessions")
    return rows if allow_example else [r for r in rows if r not in examples]


def main(argv: list[str]) -> None:
    allow_example = "--allow-example" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if len(paths) != 2:
        sys.exit(__doc__)
    timing, sus_rows = load(paths[0], allow_example), load(paths[1], allow_example)

    people: dict[str, dict] = {}
    for row in timing:
        person = people.setdefault(row["participant"], {"group": row["group"], "vscode_years": row["vscode_years"], "tasks": {}})
        task = int(row["task"])
        seconds = float(row["seconds"]) - float(row["paused_seconds"] or 0)
        completion = float(row["completion"])
        if completion not in (0, 0.5, 1):
            sys.exit(f"completion must be 0, 0.5 or 1 (participant {row['participant']}, task {task})")
        person["tasks"][task] = {"seconds": seconds, "completion": completion, "errors": int(row["errors"] or 0),
                                 "over_limit": seconds > TASK_LIMITS.get(task, float("inf")), "notes": row["notes"]}

    for name, person in people.items():
        t1, t2 = person["tasks"].get(1), person["tasks"].get(2)
        # Unaided completion of both tasks is required: a repeated instruction counts as help.
        finished = bool(t1 and t2 and t1["completion"] == 1 and t2["completion"] == 1)
        person["first_report_seconds"] = t1["seconds"] + t2["seconds"] if t1 and t2 else None
        person["first_report_within_limit"] = finished and person["first_report_seconds"] <= LIMIT_SECONDS

    tasks = {}
    for task in sorted({t for p in people.values() for t in p["tasks"]}):
        done = [p["tasks"][task] for p in people.values() if task in p["tasks"]]
        tasks[task] = {"participants": len(done),
                       "completion_rate": round(statistics.mean(d["completion"] for d in done), 3),
                       "median_seconds": statistics.median(d["seconds"] for d in done),
                       "errors": sum(d["errors"] for d in done)}

    sus = {r["participant"]: sus_score([int(r[f"q{i}"]) for i in range(1, 11)]) for r in sus_rows}
    passes = sum(1 for p in people.values() if p["first_report_within_limit"])
    mean_completion = statistics.mean(t["completion"] for p in people.values() for t in p["tasks"].values()) if people else 0
    mean_sus = statistics.mean(sus.values()) if sus else None
    result = {
        "participants": len(people),
        "nfr_11": {"limit_seconds": LIMIT_SECONDS, "within_limit": passes, "required": REQUIRED_PASSES,
                   "met": len(people) >= 5 and passes >= REQUIRED_PASSES,
                   "first_report_seconds": {n: p["first_report_seconds"] for n, p in people.items()}},
        "tasks": tasks,
        "mean_completion": round(mean_completion, 3),
        "sus": {"scores": sus, "mean": mean_sus, "benchmark": SUS_BENCHMARK,
                "sd": statistics.stdev(sus.values()) if len(sus) > 1 else None},
        "participants_detail": people,
        "example_data": allow_example,
    }

    print(f"Participants: {len(people)}")
    print(f"{'Task':<6}{'n':>3}{'done':>7}{'median s':>10}{'errors':>8}")
    for task, t in tasks.items():
        print(f"{task:<6}{t['participants']:>3}{t['completion_rate']:>7.0%}{t['median_seconds']:>10.0f}{t['errors']:>8}")
    print("\nTime to first report (Task 1 + Task 2, pauses excluded):")
    for name, p in people.items():
        seconds = p["first_report_seconds"]
        print(f"  {name:<8} {'-' if seconds is None else f'{seconds:.0f} s':>7}  {'within 5 min' if p['first_report_within_limit'] else 'NOT within 5 min'}")
    print(f"\nNFR-11: {passes} of {len(people)} within {LIMIT_SECONDS} s (need {REQUIRED_PASSES} of 5): "
          f"{'MET' if result['nfr_11']['met'] else 'NOT MET'}")
    if mean_sus is not None:
        print(f"SUS: mean {mean_sus:.1f} (benchmark {SUS_BENCHMARK}); " + ", ".join(f"{n} {s:.1f}" for n, s in sus.items()))
    print(f"Mean task completion: {mean_completion:.0%} (target 80%)")

    if allow_example:
        print("\nExample data: nothing written.")
        return
    out = Path(__file__).resolve().parents[1] / "evaluation" / "usability.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
