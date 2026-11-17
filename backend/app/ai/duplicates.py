"""Two-phase duplicate detection (FR-10): BM25 retrieval, then Sentence-BERT re-ranking.

Phase 1 narrows the project's corpus to a small candidate set in milliseconds.
Phase 2 scores those candidates by cosine similarity of sentence embeddings.
Embeddings of stored bugs are cached in the database, so a check embeds only
the text being typed. The search is always scoped to one project (NFR-06).
"""
import logging
import re
import threading
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Bug, BugEmbedding
from .text import tokenize

log = logging.getLogger("bugtracker.duplicates")

SYNC_REBUILD_LIMIT = 5000  # larger corpora are re-indexed in the background


class BM25Index:
    """Okapi BM25 over a fixed corpus, stored as a pre-weighted sparse matrix."""

    def __init__(self, ids: list[int], texts: list[str], k1: float = 1.5, b: float = 0.75):
        self.ids = np.asarray(ids, dtype=np.int64)
        self.matrix = None
        if not texts:
            return
        self.vectorizer = CountVectorizer(tokenizer=tokenize, lowercase=False, token_pattern=None)
        try:
            tf = self.vectorizer.fit_transform(texts).astype(np.float32).tocoo()
        except ValueError:  # corpus with no usable tokens
            return
        n_docs, n_terms = tf.shape
        doc_len = np.bincount(tf.row, weights=tf.data, minlength=n_docs)
        avg_len = doc_len.mean() or 1.0
        df = np.bincount(tf.col, minlength=n_terms)
        idf = np.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
        weights = idf[tf.col] * tf.data * (k1 + 1) / (tf.data + k1 * (1 - b + b * doc_len[tf.row] / avg_len))
        self.matrix = csr_matrix((weights, (tf.row, tf.col)), shape=tf.shape).tocsc()

    def __len__(self) -> int:
        return len(self.ids)

    def top(self, text: str, k: int, exclude_id: int | None = None) -> list[tuple[int, float]]:
        if self.matrix is None:
            return []
        terms = np.unique(self.vectorizer.transform([text]).indices)
        if terms.size == 0:
            return []
        scores = np.asarray(self.matrix[:, terms].sum(axis=1)).ravel()
        if exclude_id is not None:
            scores[self.ids == exclude_id] = 0.0
        k = min(k, int((scores > 0).sum()))
        if k == 0:
            return []
        best = np.argpartition(-scores, k - 1)[:k]
        best = best[np.argsort(-scores[best])]
        return [(int(self.ids[i]), float(scores[i])) for i in best]


class Embedder:
    """Sentence-BERT wrapper. Loaded once at start-up, never per request (NFR-02)."""

    def __init__(self) -> None:
        self.model = None
        self.name = ""

    def load(self, name: str) -> bool:
        try:
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(name)
            self.name = name
            self.encode(["warm up"])
            return True
        except Exception:
            log.exception("Sentence-BERT unavailable; duplicate re-ranking will be lexical")
            self.model = None
            return False

    @property
    def ready(self) -> bool:
        return self.model is not None

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False, batch_size=64),
            dtype=np.float32,
        )


@dataclass
class DuplicateResult:
    candidates: list[tuple[int, float]]  # (bug id, similarity 0..1), best first
    method: str
    query_vector: np.ndarray | None = None


TITLE_WEIGHT = 3          # the title counts this many times in the keyword stage
EMBED_CHARS = 2000        # the encoder reads roughly the first 256 word pieces anyway
# Text that browsers and bug forms paste into every report and that says nothing about the defect.
_BOILERPLATE = re.compile(r"User[- ]Agent:.*|Build ID:.*|https?://\S+|Mozilla/5\.0 \([^)]*\)[^\n]*", re.I)


def clean_description(description: str) -> str:
    return _BOILERPLATE.sub(" ", description or "")


def index_text(title: str, description: str) -> str:
    """Text for BM25: the summary line is the most reliable signal, so it is weighted up."""
    return ((title or "") + "\n") * TITLE_WEIGHT + clean_description(description)


def embed_text(title: str, description: str) -> str:
    """Text for the sentence encoder: title first, then the cleaned description."""
    return f"{title}\n{clean_description(description)[:EMBED_CHARS]}".strip()


def fuse(similarity: np.ndarray, bm25: np.ndarray, weight: float) -> np.ndarray:
    """Ranking score: semantic similarity blended with the normalised keyword score.

    The two stages make different mistakes, and on the Firefox and Thunderbird
    data blending them ranks the true duplicate higher than either alone.
    """
    top = float(bm25.max()) if bm25.size else 0.0
    if top <= 0 or weight >= 1:
        return similarity
    return weight * similarity + (1 - weight) * bm25 / top


def upsert_embedding(db: Session, bug_id: int, model: str, vector: np.ndarray) -> None:
    """Insert or replace atomically: concurrent requests may embed the same bug at once."""
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    values = {"bug_id": bug_id, "model": model, "vector": vector.tobytes()}
    db.execute(insert(BugEmbedding).values(**values).on_conflict_do_update(
        index_elements=["bug_id"], set_={"model": model, "vector": values["vector"]}))


class DuplicateDetector:
    def __init__(self) -> None:
        self.embedder = Embedder()
        self._indexes: dict[int, tuple[tuple, BM25Index]] = {}
        self._lock = threading.Lock()
        self._rebuilding: set[int] = set()
        self._build_locks: dict[int, threading.Lock] = {}

    # ---- phase 1: BM25 index, derived from the database and cached per project ----
    @staticmethod
    def _version(db: Session, project_id: int) -> tuple:
        row = db.execute(select(func.count(Bug.id), func.max(Bug.id)).where(Bug.project_id == project_id)).one()
        return (row[0], row[1])

    @staticmethod
    def _build(db: Session, project_id: int) -> BM25Index:
        rows = db.execute(select(Bug.id, Bug.title, Bug.description).where(Bug.project_id == project_id)).all()
        return BM25Index([r[0] for r in rows], [index_text(r[1], r[2]) for r in rows])

    def _background_rebuild(self, project_id: int, version: tuple) -> None:
        from ..database import SessionLocal

        try:
            with SessionLocal() as db:
                index = self._build(db, project_id)
            with self._lock:
                self._indexes[project_id] = (version, index)
        except Exception:
            log.exception("Background re-index failed for project %s", project_id)
        finally:
            self._rebuilding.discard(project_id)

    def index_for(self, db: Session, project_id: int) -> BM25Index:
        version = self._version(db, project_id)
        cached = self._indexes.get(project_id)
        if cached and cached[0] == version:
            return cached[1]
        lock = self._build_locks.setdefault(project_id, threading.Lock())
        if cached is None:
            with lock:  # first use: everyone waits for the one build
                cached = self._indexes.get(project_id)
                if cached is None or cached[0] != version:
                    self._indexes[project_id] = cached = (version, self._build(db, project_id))
                return cached[1]
        if len(cached[1]) > SYNC_REBUILD_LIMIT:
            # Large corpus: serve the slightly stale index now and refresh it off the request path.
            if project_id not in self._rebuilding:
                self._rebuilding.add(project_id)
                threading.Thread(target=self._background_rebuild, args=(project_id, version), daemon=True).start()
            return cached[1]
        # Small corpus: rebuild in line, but only one request at a time does so. Under a burst of
        # submissions the others use the index as it was a moment ago instead of all rebuilding it.
        if not lock.acquire(blocking=False):
            return cached[1]
        try:
            index = self._build(db, project_id)
            self._indexes[project_id] = (version, index)
            return index
        finally:
            lock.release()

    def invalidate(self, project_id: int) -> None:
        with self._lock:
            self._indexes.pop(project_id, None)

    # ---- phase 2: semantic re-ranking ----
    def _stored_vectors(self, db: Session, bug_ids: list[int]) -> dict[int, np.ndarray]:
        rows = db.execute(
            select(BugEmbedding.bug_id, BugEmbedding.vector).where(
                BugEmbedding.bug_id.in_(bug_ids), BugEmbedding.model == self.embedder.name
            )
        ).all()
        vectors = {bug_id: np.frombuffer(blob, dtype=np.float32) for bug_id, blob in rows}
        missing = [i for i in bug_ids if i not in vectors]
        if missing:
            bugs = db.execute(select(Bug.id, Bug.title, Bug.description).where(Bug.id.in_(missing))).all()
            if bugs:
                encoded = self.embedder.encode([embed_text(b[1], b[2]) for b in bugs])
                for (bug_id, _, _), vec in zip(bugs, encoded):
                    vectors[bug_id] = vec
                    upsert_embedding(db, bug_id, self.embedder.name, vec)
                db.commit()
        return vectors

    def store_embedding(self, db: Session, bug: Bug, vector: np.ndarray | None = None) -> None:
        if not self.embedder.ready:
            return
        if vector is None:
            vector = self.embedder.encode([embed_text(bug.title, bug.description)])[0]
        upsert_embedding(db, bug.id, self.embedder.name, vector)

    @staticmethod
    def _lexical_similarity(query: str, texts: list[str]) -> np.ndarray:
        try:
            matrix = TfidfVectorizer(tokenizer=tokenize, lowercase=False, token_pattern=None, sublinear_tf=True).fit_transform([query] + texts)
        except ValueError:
            return np.zeros(len(texts))
        return np.asarray((matrix[1:] @ matrix[0].T).todense()).ravel()

    def check(
        self, db: Session, project_id: int, title: str, description: str, *, candidates: int = 100, top_k: int = 3,
        exclude_id: int | None = None, fusion: float = 0.7,
    ) -> DuplicateResult:
        """Return the top_k most similar bugs as (id, similarity), best first.

        Candidates are ordered by the fused score; the similarity reported for
        each is the plain semantic (or lexical) similarity, which is what the
        match percentage and the duplicate threshold are defined on.
        """
        method = "bm25+sbert" if self.embedder.ready else "bm25+lexical"
        shortlist = self.index_for(db, project_id).top(index_text(title, description), candidates, exclude_id)
        query = embed_text(title, description)
        query_vector = self.embedder.encode([query])[0] if self.embedder.ready else None
        if not shortlist:
            return DuplicateResult([], method, query_vector)
        bm25 = dict(shortlist)
        ids = list(bm25)
        if query_vector is not None:
            vectors = self._stored_vectors(db, ids)
            ids = [i for i in ids if i in vectors]
            scores = np.stack([vectors[i] for i in ids]) @ query_vector if ids else np.zeros(0)
        else:
            rows = dict((r[0], embed_text(r[1], r[2])) for r in db.execute(select(Bug.id, Bug.title, Bug.description).where(Bug.id.in_(ids))))
            ids = [i for i in ids if i in rows]
            scores = self._lexical_similarity(query, [rows[i] for i in ids])
        order = np.argsort(-fuse(scores, np.array([bm25[i] for i in ids]), fusion))[:top_k]
        ranked = [(ids[i], float(np.clip(scores[i], 0.0, 1.0))) for i in order]
        return DuplicateResult(ranked, method, query_vector)
