"""Convert the locally available datasets into the canonical CSV schema.

    python -m ml.prepare_local msr2013 "C:/path/to/msr2013-bug_dataset-master"
    python -m ml.prepare_local gitbugs ../data/raw/gitbugs/repo

msr2013  Lamkanfi, Perez and Demeyer, "The Eclipse and Mozilla Defect Tracking
         Dataset" (MSR 2013). Titles, severity and the full assignment history
         of 47k Eclipse and 168k Mozilla reports. It has no description text and
         no duplicate links, so it serves severity (title only) and assignment.
         The assignment history gives the real number of reassignments per bug.
gitbugs  Patil, "GitBugs" (arXiv:2504.09651), CC BY 4.0. Summary, description
         and maintainer-confirmed duplicate links for Firefox and Thunderbird.
         It has no severity or assignee, so it serves duplicate detection.
"""
import json
import sys
import zipfile
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parents[2] / "data" / "raw"


def _history(path: Path) -> dict:
    return next(iter(json.loads(path.read_text(encoding="utf-8")).values()))


def msr2013(source: Path) -> None:
    target = OUT / "msr2013"
    target.mkdir(parents=True, exist_ok=True)
    for tracker in ("eclipse", "mozilla"):
        folder = source / "data" / "v02" / tracker
        reports = _history(folder / "reports.json")
        fields = {name: _history(folder / f"{name}.json") for name in ("short_desc", "severity", "assigned_to", "component", "product")}
        rows = []
        for bug_id, report in reports.items():
            last = {name: (history[bug_id][-1]["what"] if history.get(bug_id) else None) for name, history in fields.items()}
            # Consecutive identical values are edits by different people, not reassignments.
            owners = [step["what"] for step in fields["assigned_to"].get(bug_id, []) if step["what"]]
            distinct = [o for i, o in enumerate(owners) if i == 0 or o != owners[i - 1]]
            rows.append({
                "bug_id": int(bug_id), "title": last["short_desc"] or "", "description": "", "severity": last["severity"],
                "assignee": last["assigned_to"], "component": last["component"], "product": last["product"], "dup_id": None,
                "created": pd.Timestamp(report["opening"], unit="s", tz="UTC").isoformat(),
                "status": report.get("current_status"), "resolution": report.get("current_resolution"),
                "reassignments": max(0, len(distinct) - 1),
            })
        frame = pd.DataFrame(rows).sort_values("bug_id")
        frame.to_csv(target / f"{tracker}.csv", index=False)
        print(f"msr2013/{tracker}.csv: {len(frame)} reports, mean recorded reassignments {frame['reassignments'].mean():.2f}")


def _groups(pairs: pd.DataFrame, known: set[int]) -> dict[int, int]:
    """Union-find over duplicate pairs; returns bug -> smallest id of its group (in-corpus bugs only)."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for issue, others in zip(pairs.iloc[:, 0], pairs.iloc[:, 1].astype(str)):
        for other in others.replace('"', "").split(","):
            other = other.strip()
            if other.isdigit() and int(issue) in known and int(other) in known:
                a, b = find(int(issue)), find(int(other))
                if a != b:
                    parent[max(a, b)] = min(a, b)
    return {bug: find(bug) for bug in parent}


def gitbugs(source: Path) -> None:
    target = OUT / "gitbugs"
    target.mkdir(parents=True, exist_ok=True)
    for project, bugs_file, pairs_file in (("firefox", "firefox/Firefox_bugs.csv.zip", "firefox/Firefox_bugs-combined.csv"),
                                           ("thunderbird", "thunderbird/Thunderbird_bugs.csv", "thunderbird/Thunderbird_bugs-combined.csv")):
        path = source / bugs_file
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as archive, archive.open(archive.infolist()[0]) as handle:
                bugs = pd.read_csv(handle, low_memory=False)
        else:
            bugs = pd.read_csv(path, low_memory=False)
        bugs = bugs.dropna(subset=["Issue id"]).drop_duplicates("Issue id")
        roots = _groups(pd.read_csv(source / pairs_file), set(bugs["Issue id"].astype(int)))
        frame = pd.DataFrame({
            "bug_id": bugs["Issue id"].astype(int), "title": bugs["Summary"].fillna(""),
            "description": bugs["Description"].fillna("").astype(str).str.slice(0, 5000), "severity": None, "assignee": None,
            "component": None, "product": project, "created": bugs["Created"], "status": bugs["Status"], "resolution": bugs["Resolution"],
        })
        # dup_id points at the group's root; the root itself has none.
        frame["dup_id"] = frame["bug_id"].map(lambda b: roots[b] if b in roots and roots[b] != b else None)
        frame.to_csv(target / f"{project}.csv", index=False)
        print(f"gitbugs/{project}.csv: {len(frame)} reports, {frame['dup_id'].notna().sum()} with an in-corpus duplicate link, "
              f"{len(set(roots.values()))} groups")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("msr2013", "gitbugs"):
        sys.exit(__doc__)
    {"msr2013": msr2013, "gitbugs": gitbugs}[sys.argv[1]](Path(sys.argv[2]))
