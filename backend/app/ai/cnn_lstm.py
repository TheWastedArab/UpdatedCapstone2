"""CNN-LSTM developer assignment model (FR-12), after Adhikari et al. [1].

A convolution picks up local cues (identifiers, error types); a bidirectional
LSTM reads the resulting sequence. The severity level and the duplicate flag
produced by the other two modules are concatenated to the text representation
before the output layer, which is the shared state described in Section 2.4.5.
"""
import io
import logging
import random
from collections import Counter

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .text import tokenize

log = logging.getLogger("bugtracker.cnn_lstm")

PAD, UNK = 0, 1
N_META = 6  # severity one-hot (4 levels + unknown) and the duplicate flag


def encode_meta(severity_rank: int | None, is_duplicate: bool) -> list[float]:
    meta = [0.0] * N_META
    meta[4 if severity_rank is None else max(0, min(3, severity_rank))] = 1.0
    meta[5] = 1.0 if is_duplicate else 0.0
    return meta


class Vocab:
    def __init__(self, itos: list[str]):
        self.itos = itos
        self.stoi = {tok: i for i, tok in enumerate(itos)}

    @classmethod
    def build(cls, texts: list[str], max_size: int = 20_000, min_freq: int = 2) -> "Vocab":
        counts = Counter(tok for text in texts for tok in tokenize(text))
        if len(texts) < 500:
            min_freq = 1
        words = [w for w, c in counts.most_common(max_size - 2) if c >= min_freq]
        return cls(["<pad>", "<unk>"] + words)

    def encode(self, text: str, max_len: int) -> list[int]:
        ids = [self.stoi.get(tok, UNK) for tok in tokenize(text)[:max_len]]
        return ids + [PAD] * (max_len - len(ids))


class CnnLstm(nn.Module):
    def __init__(self, vocab_size: int, n_classes: int, emb_dim: int = 100, filters: int = 128, hidden: int = 128,
                 dropout: float = 0.3):
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, emb_dim, padding_idx=PAD)
        self.conv = nn.Conv1d(emb_dim, filters, kernel_size=3, padding=1)
        self.pool = nn.MaxPool1d(2)
        self.lstm = nn.LSTM(filters, hidden, batch_first=True, bidirectional=True)
        self.dropout = nn.Dropout(dropout)
        self.out = nn.Linear(2 * hidden + N_META, n_classes)

    def forward(self, tokens: torch.Tensor, meta: torch.Tensor) -> torch.Tensor:
        x = self.embedding(tokens).transpose(1, 2)
        x = self.pool(F.relu(self.conv(x))).transpose(1, 2)
        x, _ = self.lstm(x)
        x = x.max(dim=1).values
        return self.out(self.dropout(torch.cat([x, meta], dim=1)))


def _topk_accuracy(logits: torch.Tensor, target: torch.Tensor, k: int) -> float:
    k = min(k, logits.shape[1])
    return (logits.topk(k, dim=1).indices == target[:, None]).any(dim=1).float().mean().item()


# The CNN-LSTM is blended with a TF-IDF + linear SVM ranking. In the rolling evaluation
# (Section 6.5.4) the SVM alone outranked the CNN-LSTM, and the blend outranked both:
# the two models make different mistakes. The weight is chosen per project on the
# validation split, from these values; 0 means the CNN-LSTM alone.
BLEND_WEIGHTS = (0.0, 0.25, 0.5, 0.75)
SVM_TEMPERATURE = 5.0  # turns SVM margins into a distribution comparable with the CNN-LSTM's softmax
SVM_FEATURES = 30_000


class TextRanker:
    """TF-IDF + linear SVM over the same classes, kept as plain tensors so the bundle stays weights-only."""

    def __init__(self, terms: list[str], idf: torch.Tensor, coef: torch.Tensor, intercept: torch.Tensor):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vectoriser = TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), vocabulary={t: i for i, t in enumerate(terms)})
        self.vectoriser.idf_ = idf.numpy()
        self.coef, self.intercept = coef.float().numpy(), intercept.float().numpy()
        self.terms, self.idf = terms, idf

    @classmethod
    def fit(cls, texts: list[str], targets: list[int], n_classes: int) -> "TextRanker":
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.svm import LinearSVC
        vectoriser = TfidfVectorizer(sublinear_tf=True, ngram_range=(1, 2), min_df=2 if len(texts) >= 500 else 1, max_features=SVM_FEATURES)
        features = vectoriser.fit_transform(texts)
        svm = LinearSVC(C=0.5).fit(features, targets)
        coef = np.zeros((n_classes, features.shape[1]), dtype=np.float32)
        intercept = np.full(n_classes, -10.0, dtype=np.float32)  # a developer the SVM never saw ranks last
        if len(svm.classes_) == 2:  # scikit-learn stores one row for two classes
            coef[svm.classes_[1]], intercept[svm.classes_[1]] = svm.coef_[0], svm.intercept_[0]
            coef[svm.classes_[0]], intercept[svm.classes_[0]] = -svm.coef_[0], -svm.intercept_[0]
        else:
            coef[svm.classes_], intercept[svm.classes_] = svm.coef_, svm.intercept_
        terms = [None] * len(vectoriser.vocabulary_)
        for term, i in vectoriser.vocabulary_.items():
            terms[i] = term
        return cls(terms, torch.tensor(vectoriser.idf_, dtype=torch.float32), torch.tensor(coef).half(), torch.tensor(intercept))

    def probabilities(self, texts: list[str]) -> np.ndarray:
        scores = self.vectoriser.transform(texts) @ self.coef.T + self.intercept
        return F.softmax(torch.tensor(np.asarray(scores) * SVM_TEMPERATURE), dim=1).numpy()

    def state(self) -> dict:
        return {"terms": self.terms, "idf": self.idf, "coef": torch.tensor(self.coef).half(), "intercept": torch.tensor(self.intercept)}


def _mean_rank(probabilities: np.ndarray, target: np.ndarray) -> float:
    return float((probabilities > probabilities[np.arange(len(target)), target][:, None]).sum(axis=1).mean())


def train_model(
    texts: list[str], metas: list[list[float]], labels: list, *, epochs: int = 15, batch_size: int = 32,
    max_len: int = 200, lr: float = 1e-3, val_fraction: float = 0.1, device: str | None = None, seed: int = 42,
    val_data: tuple[list[str], list[list[float]], list] | None = None, svm_blend: bool = True,
) -> tuple[bytes, dict]:
    """Train and return (serialised bundle, metrics). Labels may be any hashable ids.

    With svm_blend, a TF-IDF + linear SVM ranker is trained alongside and blended in at
    inference with the weight that ranked the validation split best.
    """
    torch.manual_seed(seed)
    random.seed(seed)
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    classes = sorted(set(labels))
    class_index = {c: i for i, c in enumerate(classes)}
    vocab = Vocab.build(texts)

    def tensors(t, m, y):
        return (
            torch.tensor([vocab.encode(s, max_len) for s in t], dtype=torch.long),
            torch.tensor(m, dtype=torch.float32),
            torch.tensor([class_index.get(c, -1) for c in y], dtype=torch.long),
        )

    x, meta, y = tensors(texts, metas, labels)
    if val_data is not None:
        vx, vmeta, vy = tensors(*val_data)
        known = vy >= 0  # developers never seen in training cannot be predicted
        vx, vmeta, vy = vx[known], vmeta[known], vy[known]
        train_idx = torch.arange(len(y))
    else:
        order = torch.randperm(len(y))
        n_val = int(len(y) * val_fraction) if len(y) >= 50 else 0
        val_idx, train_idx = order[:n_val], order[n_val:]
        vx, vmeta, vy = x[val_idx], meta[val_idx], y[val_idx]

    model = CnnLstm(len(vocab.itos), len(classes)).to(device)
    optimiser = torch.optim.Adam(model.parameters(), lr=lr)
    best_state, best_score, history = None, -1.0, []

    def evaluate() -> dict:
        if len(vy) == 0:
            return {}
        model.eval()
        outputs = []
        with torch.no_grad():
            for i in range(0, len(vy), 512):
                outputs.append(model(vx[i:i + 512].to(device), vmeta[i:i + 512].to(device)).cpu())
        logits = torch.cat(outputs)
        return {f"top{k}": round(_topk_accuracy(logits, vy, k), 4) for k in (1, 3, 5)}

    for epoch in range(epochs):
        model.train()
        perm = train_idx[torch.randperm(len(train_idx))]
        total = 0.0
        for i in range(0, len(perm), batch_size):
            batch = perm[i:i + batch_size]
            optimiser.zero_grad()
            loss = F.cross_entropy(model(x[batch].to(device), meta[batch].to(device)), y[batch].to(device))
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimiser.step()
            total += loss.item() * len(batch)
        scores = evaluate()
        history.append({"epoch": epoch + 1, "loss": round(total / max(1, len(perm)), 4), **scores})
        score = scores.get("top1", -total)  # without a validation split, keep the lowest training loss
        if score > best_score or best_state is None:
            best_score = score
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    model.load_state_dict(best_state)
    final = evaluate()
    bundle = {"state": best_state, "vocab": vocab.itos, "classes": classes, "max_len": max_len}
    blend = {"weight": 0.0}
    if svm_blend:
        try:
            train_rows = [int(i) for i in train_idx]
            if val_data is not None:
                known_val = [i for i, c in enumerate(val_data[2]) if c in class_index]
                val_texts = [val_data[0][i] for i in known_val]
                extra = ([val_data[0][i] for i in known_val], [class_index[val_data[2][i]] for i in known_val])
            else:
                val_texts, extra = [texts[int(i)] for i in val_idx], ([], [])
            blend = _fit_blend(model, device, [texts[i] for i in train_rows], [int(y[i]) for i in train_rows], val_texts,
                               vx, vmeta, vy, list(texts) + extra[0], [int(t) for t in y] + extra[1], len(classes))
            bundle["svm"], bundle["blend_weight"] = blend.pop("ranker").state(), blend["weight"]
        except ValueError as exc:  # e.g. too little text for a vocabulary: the CNN-LSTM serves alone
            log.warning("Text ranker not trained (%s); using the CNN-LSTM alone", exc)
            blend = {"weight": 0.0}
    buffer = io.BytesIO()
    torch.save(bundle, buffer)
    metrics = {
        "samples": len(labels), "developers": len(classes), "vocabulary": len(vocab.itos),
        "validation_samples": int(len(vy)), "validation": final, "epochs": history, "device": device, "blend": blend,
    }
    return buffer.getvalue(), metrics


def _fit_blend(model, device, train_texts, train_targets, val_texts, vx, vmeta, vy, all_texts, all_targets, n_classes) -> dict:
    """Choose the blend weight on the validation split, then refit the text ranker on all the data.

    The CNN-LSTM was trained on train_texts only and its epoch chosen on the validation
    split, so the validation split is also where the two models are compared. With no
    validation data (very small projects) the middle weight is used.
    """
    weight, by_weight = BLEND_WEIGHTS[2], {}
    if len(vy) > 0:
        ranker = TextRanker.fit(train_texts, train_targets, n_classes)
        model.eval()
        with torch.no_grad():
            cnn = torch.cat([F.softmax(model(vx[i:i + 512].to(device), vmeta[i:i + 512].to(device)), dim=1).cpu()
                             for i in range(0, len(vy), 512)]).numpy()
        svm = ranker.probabilities(val_texts)
        target = vy.numpy()
        by_weight = {w: round(_mean_rank((1 - w) * cnn + w * svm, target), 3) for w in BLEND_WEIGHTS}
        weight = min(by_weight, key=by_weight.get)
    ranker = TextRanker.fit(all_texts, all_targets, n_classes)
    return {"ranker": ranker, "weight": weight, "validation_mean_rank_by_weight": {str(k): v for k, v in by_weight.items()}}


class AssignmentModel:
    """A loaded bundle, ready for inference on CPU."""

    def __init__(self, blob: bytes):
        bundle = torch.load(io.BytesIO(blob), map_location="cpu", weights_only=True)
        self.vocab = Vocab(bundle["vocab"])
        self.classes = bundle["classes"]
        self.max_len = bundle["max_len"]
        self.model = CnnLstm(len(self.vocab.itos), len(self.classes))
        self.model.load_state_dict(bundle["state"])
        self.model.eval()
        # Bundles trained before the text ranker existed have no "svm" entry and serve the CNN-LSTM alone.
        svm = bundle.get("svm")
        self.ranker = TextRanker(svm["terms"], svm["idf"], svm["coef"], svm["intercept"]) if svm else None
        self.blend_weight = float(bundle.get("blend_weight", 0.0)) if svm else 0.0

    def predict_proba(self, texts: list[str], metas: list[list[float]], cnn_only: bool = False) -> np.ndarray:
        tokens = torch.tensor([self.vocab.encode(t, self.max_len) for t in texts], dtype=torch.long)
        with torch.no_grad():
            logits = self.model(tokens, torch.tensor(metas, dtype=torch.float32))
        probabilities = F.softmax(logits, dim=1).numpy()
        if cnn_only or self.ranker is None or self.blend_weight == 0.0:
            return probabilities
        return (1 - self.blend_weight) * probabilities + self.blend_weight * self.ranker.probabilities(texts)

    def probabilities(self, text: str, severity_rank: int | None, is_duplicate: bool) -> dict:
        probs = self.predict_proba([text], [encode_meta(severity_rank, is_duplicate)])[0]
        return {cls: float(p) for cls, p in zip(self.classes, probs)}
