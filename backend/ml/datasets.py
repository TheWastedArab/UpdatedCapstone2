"""Loading bug report datasets into one canonical schema.

Public Eclipse and Mozilla dumps exist in several shapes (Bugzilla CSV exports,
JSON-lines dumps, the Lazar et al. and LogPAI collections), and they do not
agree on column names. Every loader here returns a DataFrame with:

    bug_id, title, description, severity, assignee, component, product, dup_id, created

Columns a source lacks are filled with None, and each script checks for the
columns it needs and stops with a clear message when they are missing.
"""
import json
import re
from pathlib import Path

import pandas as pd

CANONICAL = ["bug_id", "title", "description", "severity", "assignee", "component", "product", "dup_id", "created"]

ALIASES = {
    "bug_id": ["bug_id", "id", "issue_id", "bugid", "bug id", "issue id", "key", "number"],
    "title": ["title", "short_desc", "summary", "short_description", "short desc", "issue_title"],
    "description": ["description", "long_desc", "desc", "body", "thetext", "text", "comment", "first_comment", "issue_body"],
    "severity": ["severity", "bug_severity", "severity_level"],
    "assignee": ["assignee", "assigned_to", "owner", "assigned to", "assignee_email", "assigned_to_detail", "fixer", "developer"],
    "component": ["component", "components"],
    "product": ["product", "project"],
    "dup_id": ["dup_id", "duplicate_of", "dupe_of", "duplicated_issue", "duplicate", "dup", "master_id", "dup_list"],
    "created": ["created", "creation_ts", "created_time", "creation_time", "opened", "created_at", "reported"],
}

# Bugzilla severities onto the four levels of FR-11. "enhancement" is a feature
# request rather than a defect and is dropped.
BUGZILLA_TO_LEVEL = {
    "blocker": "Critical", "critical": "Critical", "major": "High", "normal": "Medium", "minor": "Low", "trivial": "Low",
    "s1": "Critical", "s2": "High", "s3": "Medium", "s4": "Low",  # Mozilla's newer scale
}
PLACEHOLDER_ASSIGNEES = re.compile(r"nobody|unassigned|inbox|triage|^$|-inbox@|platform-.*-inbox|general@|webmaster", re.I)


def _read(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        return pd.read_csv(path, sep="\t" if suffix == ".tsv" else ",", low_memory=False, on_bad_lines="skip")
    if suffix in (".jsonl", ".ndjson"):
        return pd.read_json(path, lines=True)
    if suffix == ".json":
        text = path.read_text(encoding="utf-8", errors="replace").lstrip()
        if text.startswith("["):
            return pd.DataFrame(json.loads(text))
        if text.startswith("{") and "\n{" in text:  # MongoDB export: one object per line
            return pd.read_json(path, lines=True)
        data = json.loads(text)
        records = next((v for v in data.values() if isinstance(v, list)), None) if isinstance(data, dict) else None
        return pd.DataFrame(records if records is not None else data)
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported dataset file type: {path.name}")


def _scalar(value):
    """Flatten the nested values some dumps use ({'$oid': ..}, [{'name': ..}], lists of ids)."""
    if isinstance(value, dict):
        for key in ("email", "name", "real_name", "$oid", "$numberInt", "text", "what"):
            if key in value:
                return _scalar(value[key])
        return None
    if isinstance(value, (list, tuple)):
        return _scalar(value[0]) if len(value) else None
    return value


def to_canonical(frame: pd.DataFrame) -> pd.DataFrame:
    lookup = {str(c).strip().lower(): c for c in frame.columns}
    out = pd.DataFrame(index=frame.index)
    for target in CANONICAL:
        source = next((lookup[a] for a in ALIASES[target] if a in lookup), None)
        out[target] = frame[source].map(_scalar) if source is not None else None
    out = out.dropna(subset=["bug_id"])
    out["bug_id"] = pd.to_numeric(out["bug_id"], errors="coerce")
    out = out.dropna(subset=["bug_id"]).astype({"bug_id": "int64"}).drop_duplicates("bug_id")
    out["title"] = out["title"].fillna("").astype(str).str.strip()
    out["description"] = out["description"].fillna("").astype(str).str.strip()
    out = out[(out["title"].str.len() > 0) | (out["description"].str.len() > 0)]
    # Optional columns, kept when the source has them.
    for extra, names in (("resolution", ["resolution", "current_resolution"]), ("reassignments", ["reassignments"])):
        source = next((lookup[n] for n in names if n in lookup), None)
        out[extra] = frame.loc[out.index, source] if source is not None else None
    out["dup_id"] = pd.to_numeric(out["dup_id"].astype(str).str.extract(r"(\d+)")[0], errors="coerce")
    out["created"] = pd.to_datetime(out["created"], errors="coerce", utc=True)
    for column in ("severity", "assignee", "component", "product"):
        out[column] = out[column].where(out[column].notna(), None)
    return out.reset_index(drop=True)


def load(path: str | Path) -> pd.DataFrame:
    """Load one file, or every dataset file in a directory, in the canonical schema."""
    path = Path(path)
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.suffix.lower() in (".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet"))
        if not files:
            raise FileNotFoundError(f"No dataset files in {path}")
        return pd.concat([to_canonical(_read(f)) for f in files], ignore_index=True).drop_duplicates("bug_id").reset_index(drop=True)
    return to_canonical(_read(path))


def text_of(frame: pd.DataFrame) -> pd.Series:
    return (frame["title"] + "\n" + frame["description"]).str.slice(0, 5000)


def require(frame: pd.DataFrame, column: str, purpose: str) -> None:
    if frame[column].notna().sum() == 0:
        raise SystemExit(f"This dataset has no usable '{column}' column, which {purpose} needs. "
                         f"Columns are matched by these names: {', '.join(ALIASES[column])}.")


def with_levels(frame: pd.DataFrame, drop_default: bool = False) -> pd.DataFrame:
    """Rows with a severity mapped to Low / Medium / High / Critical.

    drop_default removes Bugzilla's 'normal', which is the pre-selected value
    of the form and is therefore a noisy label; several of the papers reviewed
    in Chapter 2 exclude it. Both settings are reported, never only the better one.
    """
    require(frame, "severity", "severity classification")
    labelled = frame.assign(_raw=frame["severity"].astype(str).str.strip().str.lower())
    if drop_default:
        labelled = labelled[labelled["_raw"] != "normal"]
    labelled = labelled.assign(level=labelled["_raw"].map(BUGZILLA_TO_LEVEL)).dropna(subset=["level"])
    return labelled.drop(columns="_raw").reset_index(drop=True)


def with_assignees(frame: pd.DataFrame, min_bugs: int = 20) -> pd.DataFrame:
    """Rows with a real developer who fixed at least min_bugs reports, in chronological order."""
    require(frame, "assignee", "developer assignment")
    assigned = frame[frame["assignee"].notna()].copy()
    if "resolution" in assigned and assigned["resolution"].notna().any():
        # Only bugs that were actually fixed tell us who the right developer was.
        assigned = assigned[assigned["resolution"].astype(str).str.upper() == "FIXED"]
    assigned["assignee"] = assigned["assignee"].astype(str).str.strip().str.lower()
    assigned = assigned[~assigned["assignee"].str.contains(PLACEHOLDER_ASSIGNEES)]
    counts = assigned["assignee"].value_counts()
    assigned = assigned[assigned["assignee"].isin(counts[counts >= min_bugs].index)]
    order = "created" if assigned["created"].notna().any() else "bug_id"
    return assigned.sort_values(order).reset_index(drop=True)


def duplicate_groups(frame: pd.DataFrame) -> dict[int, int]:
    """Map every report that has an in-corpus duplicate to the root of its duplicate group.

    Uses union-find, so chains (A -> B -> C) and mutual links (A <-> B) both
    end up as one group. The root is the oldest report (smallest id).
    """
    require(frame, "dup_id", "duplicate detection")
    known = set(frame["bug_id"])
    parent: dict[int, int] = {}

    def find(bug: int) -> int:
        while parent.setdefault(bug, bug) != bug:
            parent[bug] = parent[parent[bug]]
            bug = parent[bug]
        return bug

    for bug, dup in zip(frame["bug_id"], frame["dup_id"]):
        if pd.notna(dup) and int(dup) in known and int(dup) != int(bug):
            a, b = find(int(bug)), find(int(dup))
            if a != b:
                parent[max(a, b)] = min(a, b)
    return {bug: find(bug) for bug in parent}
