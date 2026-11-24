"""Fetch the date each Eclipse bug was marked FIXED, from the Zenodo activity logs.

    python -m ml.fetch_fix_dates JDT CDT PDE Platform

The slim project files written by ml/fetch_zenodo_eclipse.py keep the creation
date but not when a bug was fixed. The duplicate-routing evaluation in
ml/train_assignment.py needs that date: a duplicate may only be routed to the
fixer of its original if the original had been fixed before the duplicate was
filed. This script streams the same remote archive, reads each bug's activity
log, and keeps the last time its resolution was set to FIXED.
Output: data/raw/zenodo_eclipse/fix_dates/<PROJECT>.csv with bug_id, fixed_at, in a
subfolder so that loading the project folder does not read them as bug reports.
"""
import argparse
import ast
import io
import json
import sys
import time
import zipfile

import pandas as pd

from ml.fetch_zenodo_eclipse import OUT, URL, MemberStream, RemoteFile

FIX_DATES = OUT / "fix_dates"

LOG = "History/Activity Log"


def fixed_at(log: str) -> str | None:
    """The last time the resolution was set to FIXED, or None."""
    if not isinstance(log, str) or "FIXED" not in log:
        return None
    try:
        entries = json.loads(log)
    except ValueError:
        try:
            entries = ast.literal_eval(log)
        except (ValueError, SyntaxError):
            return None
    last = None
    for entry in entries or []:
        for change in entry.get("changes", []):
            if change.get("field_name") == "resolution" and str(change.get("added", "")).upper() == "FIXED":
                last = entry.get("when", last)
    return last


def fetch(remote: RemoteFile, member: zipfile.ZipInfo, target) -> None:
    started, rows, found, reported = time.time(), 0, [], 0.0
    stream = io.BufferedReader(MemberStream(remote, member), buffer_size=8 * 1024 * 1024)
    reader = pd.read_csv(stream, usecols=["ID", "Resolution", LOG], chunksize=2000, dtype=str,
                         encoding="utf-8", encoding_errors="replace")
    for chunk in reader:
        fixed = chunk[chunk["Resolution"].astype(str).str.upper() == "FIXED"]
        found += [(bug, fixed_at(log)) for bug, log in zip(fixed["ID"], fixed[LOG])]
        rows += len(chunk)
        if time.time() - reported > 60:
            reported = time.time()
            print(f"  {member.filename}: {rows} rows, {remote.transferred / 1e6:.0f} of {member.compress_size / 1e6:.0f} MB, "
                  f"{time.time() - started:.0f}s", flush=True)
    frame = pd.DataFrame(found, columns=["bug_id", "fixed_at"])
    frame.to_csv(target, index=False)
    print(f"Wrote {len(frame)} fixed bugs ({frame['fixed_at'].notna().sum()} with a fix date) to {target} "
          f"in {time.time() - started:.0f}s", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("projects", nargs="+", help="Project names, e.g. JDT CDT PDE Platform")
    args = parser.parse_args()
    remote = RemoteFile(URL)
    archive = zipfile.ZipFile(io.BufferedReader(remote, buffer_size=32 * 1024 * 1024))
    members = {i.filename.rsplit("/", 1)[-1].split("_dataset")[0].upper(): i for i in archive.infolist() if i.filename.endswith(".csv")}
    FIX_DATES.mkdir(parents=True, exist_ok=True)
    for project in args.projects:
        info = members.get(project.upper())
        if info is None:
            sys.exit(f"Unknown project {project}; available: {', '.join(members)}")
        target = FIX_DATES / f"{project.upper()}.csv"
        if target.exists():
            print(f"{target.name} already present, skipping")
            continue
        remote.transferred = 0
        fetch(remote, info, target)


if __name__ == "__main__":
    main()
