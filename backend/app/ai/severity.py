"""Severity classification (FR-11).

Four classifiers judge each report: a linear SVM, a Random Forest and a Complement
Naive Bayes model over TF-IDF features, and a fine-tuned MiniLM sentence encoder
(all-MiniLM-L6-v2, the encoder family the duplicate detector uses) with a linear
output layer. A logistic regression learned on held-out data combines their scores,
and per-class offsets chosen on a second held-out part set the operating point:
macro F1 as high as possible without falling below the plain majority vote of the
three TF-IDF classifiers in accuracy or in recall of Critical bugs
(ml/train_severity.py). Bundles trained before the encoder existed are still served,
by the majority vote of their three classifiers.

When no trained model file is present the classifier falls back to keyword
rules instead of failing, so the system is usable before training (Section 3.5.4).
"""
import logging
import re
import threading
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
ENCODER_DIRNAME = "severity-encoder"
ENCODER_BASE = "sentence-transformers/all-MiniLM-L6-v2"
ENCODER_MAX_LEN = 256


def build_ensemble(max_features: int = 100_000, rf_features: int = 5_000, rf_trees: int = 200) -> Pipeline:
    """The three TF-IDF classifiers, used both for training and for inference.

    The Random Forest sees a chi-squared selection of the vocabulary to keep it
    tractable on a sparse matrix; SVM and Naive Bayes see all of it.
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


def member_scores(pipeline: Pipeline, texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Scores of the three TF-IDF classifiers side by side, and each one's predicted class index."""
    features = pipeline.named_steps["tfidf"].transform(texts)
    vote: VotingClassifier = pipeline.named_steps["vote"]
    scores = [estimator.decision_function(features) if name == "svm" else estimator.predict_proba(features)
              for name, estimator in zip(("svm", "rf", "nb"), vote.estimators_)]
    # Each classifier's own prediction is the highest of its scores, so the forest is evaluated once.
    predicted = np.stack([s.argmax(1) for s in scores], axis=1)
    return np.hstack(scores), predicted


def vote_share(pipeline: Pipeline, texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Majority-vote labels and the fraction of the three classifiers that agreed."""
    features = pipeline.named_steps["tfidf"].transform(texts)
    vote: VotingClassifier = pipeline.named_steps["vote"]
    # VotingClassifier fits on label-encoded targets, so sub-estimators predict indices.
    votes = np.stack([est.predict(features) for est in vote.estimators_], axis=1).astype(int)
    labels = vote.predict(features)
    predicted_idx = vote.le_.transform(labels)
    share = (votes == predicted_idx[:, None]).mean(axis=1)
    return labels, share


class Coalescer:
    """Runs a batch function once for every group of calls that arrive together.

    The server handles requests in many threads. A severity prediction costs about the same
    for fifty reports as for one (the forest walks each tree once for the whole batch, and
    the encoder fills the processor with one pass), but fifty predictions run side by side
    contend for the interpreter and the cores. So one batch runs at a time, and it takes
    every report that is waiting at that moment.
    """

    def __init__(self, function):
        self.function = function
        self._queue: list[dict] = []
        self._queue_lock = threading.Lock()
        self._run_lock = threading.Lock()

    def __call__(self, value):
        item = {"value": value, "done": threading.Event()}
        with self._queue_lock:
            self._queue.append(item)
        with self._run_lock:
            if not item["done"].is_set():
                with self._queue_lock:
                    batch, self._queue = self._queue, []
                try:
                    for waiting, result in zip(batch, self.function([w["value"] for w in batch])):
                        waiting["result"] = result
                except Exception as exc:  # every report in the batch sees the failure and degrades the same way
                    for waiting in batch:
                        waiting["error"] = exc
                for waiting in batch:
                    waiting["done"].set()
        if "error" in item:
            raise item["error"]
        return item["result"]


class EncoderClassifier:
    """MiniLM with mean pooling and a linear layer over the severity classes."""

    def __init__(self, n_classes: int, base: str = ENCODER_BASE):
        import torch.nn as nn
        from transformers import AutoModel, AutoTokenizer

        class Net(nn.Module):
            def __init__(self):
                super().__init__()
                self.encoder = AutoModel.from_pretrained(base)
                self.dropout = nn.Dropout(0.1)
                self.head = nn.Linear(self.encoder.config.hidden_size, n_classes)

            def forward(self, ids, mask):
                hidden = self.encoder(input_ids=ids, attention_mask=mask).last_hidden_state
                pooled = (hidden * mask.unsqueeze(-1)).sum(1) / mask.sum(1, keepdim=True)
                return self.head(self.dropout(pooled))

        self.tokenizer = AutoTokenizer.from_pretrained(base)
        self.net = Net()
        self.device = "cpu"

    def to(self, device: str) -> "EncoderClassifier":
        self.net.to(device)
        self.device = device
        return self

    def batch(self, texts: list[str]):
        encoded = self.tokenizer(texts, truncation=True, max_length=ENCODER_MAX_LEN, padding=True, return_tensors="pt")
        return encoded["input_ids"].to(self.device), encoded["attention_mask"].to(self.device)

    def logits(self, texts: list[str], batch_size: int = 128) -> np.ndarray:
        import torch
        self.net.eval()
        out = []
        with torch.no_grad(), torch.autocast(self.device.split(":")[0], enabled=self.device.startswith("cuda")):
            for i in range(0, len(texts), batch_size):
                out.append(self.net(*self.batch(texts[i:i + batch_size])).float().cpu().numpy())
        return np.concatenate(out) if out else np.zeros((0, self.net.head.out_features))

    def save(self, directory: Path) -> None:
        import torch
        directory.mkdir(parents=True, exist_ok=True)
        self.net.encoder.save_pretrained(directory)
        self.tokenizer.save_pretrained(directory)
        torch.save({"head": self.net.head.state_dict()}, directory / "head.pt")

    @classmethod
    def load(cls, directory: Path, n_classes: int) -> "EncoderClassifier":
        import torch
        model = cls(n_classes, base=str(directory))
        model.net.head.load_state_dict(torch.load(directory / "head.pt", map_location="cpu", weights_only=True)["head"])
        return model


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
        self.stack = None
        self.bias: np.ndarray | None = None
        self.encoder: EncoderClassifier | None = None
        self.meta: dict = {}
        self._combined = Coalescer(self._predict_batch)

    def load(self, model_dir: Path) -> bool:
        path = Path(model_dir) / MODEL_FILENAME
        self.pipeline, self.stack, self.bias, self.encoder, self.meta = None, None, None, None, {}
        if not path.exists():
            log.warning("No trained severity model at %s; using keyword rules", path)
            return False
        try:
            # Only ever loaded from the server's own model directory, never from user input.
            bundle = joblib.load(path)
            self.pipeline, self.meta = bundle["pipeline"], bundle.get("meta", {})
            # One report at a time: a thread pool per prediction costs more than the forest itself.
            self.pipeline.named_steps["vote"].estimators_[1].named_steps["rf"].n_jobs = 1
            if bundle.get("stack") is not None:
                encoder_dir = Path(model_dir) / bundle.get("encoder_dir", ENCODER_DIRNAME)
                self.encoder = EncoderClassifier.load(encoder_dir, len(bundle["stack"].classes_))
                # The same rule as the duplicate detector's encoder: a GPU when there is one, else the CPU.
                import torch
                self.encoder.to("cuda" if torch.cuda.is_available() else "cpu")
                self.stack, self.bias = bundle["stack"], np.asarray(bundle["bias"], dtype=float)
            log.info("Severity model loaded (%s, %s)", self.meta.get("trained_at", "unknown date"),
                     "four classifiers, learned combination" if self.stack is not None else "three-classifier vote")
            return True
        except Exception:  # corrupt or incompatible file: degrade to rules, do not crash startup
            log.exception("Could not load severity model; using keyword rules")
            self.pipeline, self.stack, self.bias, self.encoder = None, None, None, None
            return False

    @property
    def mode(self) -> str:
        return "model" if self.pipeline is not None else "rules"

    def predict(self, title: str, description: str) -> tuple[str, float, str]:
        """Return (canonical label, confidence, source).

        The confidence is the share of the classifiers that agree with the final label.
        """
        text = f"{title}\n{description}"
        if self.pipeline is None:
            label, confidence = classify_by_rules(text)
            return label, confidence, "rules"
        if self.stack is None:
            labels, share = vote_share(self.pipeline, [text])
            return str(labels[0]), float(share[0]), "model"
        label, confidence = self._combined(text)
        return label, confidence, "model"

    def _predict_batch(self, texts: list[str]) -> list[tuple[str, float]]:
        """The four classifiers and their learned combination, for several reports at once."""
        scores, member_votes = member_scores(self.pipeline, texts)
        encoder_logits = self.encoder.logits(texts)
        combined = self.stack.predict_log_proba(np.hstack([encoder_logits, scores])) + self.bias
        indices = combined.argmax(1)
        votes = np.hstack([member_votes, encoder_logits.argmax(1)[:, None]])
        return [(str(self.stack.classes_[i]), float((row == i).mean())) for i, row in zip(indices, votes)]
