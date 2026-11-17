"""Shared text utilities for the three AI modules."""
import re
from collections import Counter

from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS

_TOKEN = re.compile(r"[a-z][a-z0-9_+#.]*[a-z0-9+#]|[a-z]")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
STOPWORDS = frozenset(ENGLISH_STOP_WORDS) | {
    "bug", "issue", "problem", "does", "doesn", "don", "isn", "like", "just", "using", "use", "used", "new",
    "try", "tried", "trying", "expected", "actual", "steps", "reproduce", "result", "results", "following",
}


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens; camelCase identifiers are split so they match prose."""
    return _TOKEN.findall(_CAMEL.sub(" ", text).lower())


def content_tokens(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in STOPWORDS and len(t) > 2]


def extract_keywords(text: str, limit: int = 10) -> list[str]:
    """Most frequent content words, ties broken by first appearance (FR-22)."""
    tokens = content_tokens(text)
    counts = Counter(tokens)
    first = {}
    for i, tok in enumerate(tokens):
        first.setdefault(tok, i)
    return sorted(counts, key=lambda t: (-counts[t], first[t]))[:limit]


CANONICAL_SEVERITIES = ["Low", "Medium", "High", "Critical"]


def map_canonical(label: str, labels: list[str]) -> str:
    """Map a canonical four-level label onto the configured label set (NFR-14)."""
    if label in labels or not labels:
        return label
    rank = CANONICAL_SEVERITIES.index(label) if label in CANONICAL_SEVERITIES else 1
    return labels[round(rank / 3 * (len(labels) - 1))]


def severity_rank(label: str | None, labels: list[str]) -> int | None:
    """Position of a label on a 0-3 urgency scale, or None when unclassified."""
    if not label or label not in labels:
        return None
    if len(labels) == 1:
        return 0
    return round(labels.index(label) / (len(labels) - 1) * 3)
