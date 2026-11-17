"""Severity classification (FR-11): TF-IDF with an SVM / Random Forest / Naive Bayes vote.

When no trained model file is present the classifier falls back to keyword
rules instead of failing, so the system is usable before training (Section 3.5.4).
"""
import logging
import re
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier, VotingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.feature_selection import SelectKBest, chi2
from sklearn.naive_bayes import ComplementNB
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

log = logging.getLogger("bugtracker.severity")

MODEL_FILENAME = "severity.joblib"


def build_ensemble(max_features: int = 100_000, rf_features: int = 5_000, rf_trees: int = 200) -> Pipeline:
    """The ensemble used both for training and for inference.

    Hard (majority) voting, as described in Step 3 of Section 3.5.4. The Random
    Forest sees a chi-squared selection of the vocabulary to keep it tractable
    on a sparse matrix; SVM and Naive Bayes see all of it.
    """
    forest = Pipeline([
        ("select", SelectKBest(chi2, k=rf_features)),
        ("rf", RandomForestClassifier(n_estimators=rf_trees, class_weight="balanced_subsample", n_jobs=-1, random_state=42)),
    ])
    vote = VotingClassifier(
        estimators=[
            ("svm", LinearSVC(C=0.5, class_weight="balanced")),
            ("rf", forest),
            ("nb", ComplementNB(alpha=0.5)),
        ],
        voting="hard",
    )
    return Pipeline([
        ("tfidf", TfidfVectorizer(lowercase=True, ngram_range=(1, 2), sublinear_tf=True, min_df=2, max_df=0.9,
                                  max_features=max_features, strip_accents="unicode")),
        ("vote", vote),
    ])


def vote_share(pipeline: Pipeline, texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Predicted labels and the fraction of the three classifiers that agreed."""
    features = pipeline.named_steps["tfidf"].transform(texts)
    vote: VotingClassifier = pipeline.named_steps["vote"]
    # VotingClassifier fits on label-encoded targets, so sub-estimators predict indices.
    votes = np.stack([est.predict(features) for est in vote.estimators_], axis=1).astype(int)
    labels = vote.predict(features)
    predicted_idx = vote.le_.transform(labels)
    share = (votes == predicted_idx[:, None]).mean(axis=1)
    return labels, share


_RULES: list[tuple[str, re.Pattern]] = [
    ("Critical", re.compile(
        r"data loss|lost (all )?(my )?data|corrupt|security|vulnerab|exploit|injection|crash|segfault|"
        r"segmentation fault|kernel panic|deadlock|outage|cannot start|can't start|won't start|unusable|"
        r"blocker|production down|privilege escalation", re.I)),
    ("High", re.compile(
        r"exception|null ?pointer|stack ?trace|traceback|fail(s|ed|ure)?\b|broken|not working|doesn't work|"
        r"unable to|cannot|can't|timeout|timed out|hang(s|ing)?\b|freez|unresponsive|memory leak|regression|"
        r"error", re.I)),
    ("Low", re.compile(
        r"typo|cosmetic|misalign|alignment|spelling|grammar|wording|tooltip|colou?r|font|icon|padding|"
        r"margin|minor|trivial|nice to have|polish", re.I)),
]


def classify_by_rules(text: str) -> tuple[str, float]:
    for label, pattern in _RULES:
        if pattern.search(text):
            return label, 0.5
    return "Medium", 0.34


class SeverityClassifier:
    def __init__(self) -> None:
        self.pipeline: Pipeline | None = None
        self.meta: dict = {}

    def load(self, model_dir: Path) -> bool:
        path = Path(model_dir) / MODEL_FILENAME
        self.pipeline, self.meta = None, {}
        if not path.exists():
            log.warning("No trained severity model at %s; using keyword rules", path)
            return False
        try:
            # Only ever loaded from the server's own model directory, never from user input.
            bundle = joblib.load(path)
            self.pipeline, self.meta = bundle["pipeline"], bundle.get("meta", {})
            log.info("Severity model loaded (%s)", self.meta.get("trained_at", "unknown date"))
            return True
        except Exception:  # corrupt or incompatible file: degrade to rules, do not crash startup
            log.exception("Could not load severity model; using keyword rules")
            return False

    @property
    def mode(self) -> str:
        return "model" if self.pipeline is not None else "rules"

    def predict(self, title: str, description: str) -> tuple[str, float, str]:
        """Return (canonical label, confidence, source)."""
        text = f"{title}\n{description}"
        if self.pipeline is None:
            label, confidence = classify_by_rules(text)
            return label, confidence, "rules"
        labels, share = vote_share(self.pipeline, [text])
        return str(labels[0]), float(share[0]), "model"
