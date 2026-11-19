"""AI Triage Engine: owns the models, loaded once at application start (Section 3.5.4)."""
import logging
from pathlib import PureWindowsPath

from ..config import get_settings
from .duplicates import DuplicateDetector
from .severity import SeverityClassifier

log = logging.getLogger("bugtracker.ai")
FINE_TUNED_DIR = "sbert-duplicates"


class AIEngine:
    def __init__(self) -> None:
        self.severity = SeverityClassifier()
        self.duplicates = DuplicateDetector()
        self.loaded = False

    def load(self) -> None:
        settings = get_settings()
        if not settings.ai_enabled:
            log.warning("AI is disabled by configuration")
            return
        self.severity.load(settings.model_dir)
        if settings.semantic_enabled:
            tuned = settings.model_dir / FINE_TUNED_DIR
            if (tuned / "config.json").exists() and self.duplicates.embedder.load(str(tuned)):
                # Stored embeddings are keyed by this name, so a retrained encoder invalidates them.
                stamp = int((tuned / "config.json").stat().st_mtime)
                self.duplicates.embedder.name = f"{FINE_TUNED_DIR}@{stamp}"
            else:
                self.duplicates.embedder.load(settings.sbert_model)
        self.loaded = True

    @property
    def available(self) -> bool:
        return get_settings().ai_enabled and self.loaded

    def status(self) -> dict:
        if not self.available:
            return {"enabled": False, "severity": "unavailable", "duplicates": "unavailable", "assignment": "unavailable", "detail": {}}
        return {
            "enabled": True,
            "severity": self.severity.mode,
            "duplicates": "bm25+sbert" if self.duplicates.embedder.ready else "bm25+lexical",
            "assignment": "per-project (see project dashboard)",
            "detail": {"severity_model": public_meta(self.severity.meta), "sbert_model": self.duplicates.embedder.name or None},
        }


def public_meta(meta: dict) -> dict:
    """Model metadata as any signed-in user may see it: the training set by name, not by its path on the server."""
    if isinstance(meta.get("dataset"), str):
        # PureWindowsPath splits on both separators, so paths recorded on either system reduce to their last part.
        meta = meta | {"dataset": PureWindowsPath(meta["dataset"]).name or meta["dataset"]}
    return meta


engine = AIEngine()
