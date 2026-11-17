"""Train and evaluate the CNN-LSTM developer assignment model (Section 3.6.3).

    python -m ml.train_assignment --data ../data/raw/zenodo_eclipse
    python -m ml.train_assignment --data ../data/raw/zenodo_eclipse --ood ../data/raw/msr2013/mozilla.csv
    python -m ml.train_assignment --synthetic

Protocol: rolling chronological evaluation, as in DeepTriage [12]. Fixed bugs
are ordered by creation date and cut into eleven equal slices. For each of the
last ten slices the model is trained on the bugs that precede it and tested on
that slice, and the ten results are averaged. A single old/new split is not
used because development teams turn over: on twenty years of Eclipse history
the developers of the newest fifth are almost all different people from those
of the first four fifths, and any model scores near zero. The rolling protocol
is also how the deployed system behaves, since it retrains as bugs are verified
(FR-29). Training uses a window of the most recent bugs for the same reason.

Reported per slice and on average: Top-1/3/5 accuracy and the simulated Mean
Number of Reassignments (MNR). For each test bug the model ranks the developers
it knows; the bug is offered to them in that order and one reassignment is
counted for each developer tried before the one who actually fixed it. Random
assignment gives (N - 1) / 2 for N developers. Two further baselines are
measured on the same bugs: always trying the busiest developers first, and a
TF-IDF + linear SVM ranking. Bugs fixed by someone the model has never seen
cannot be ranked by any method and are reported as coverage, not hidden.

--ood repeats the same protocol, unchanged, on a second tracker (Mozilla) to
check that the architecture and settings chosen on Eclipse carry over.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.svm import LinearSVC

from app.ai import cnn_lstm
from ml import datasets, synthetic

ROOT = Path(__file__).resolve().parents[2]
TARGET_MNR_REDUCTION = 0.20
SEVERITY_RANK = {"Low": 0, "Medium": 1, "High": 2, "Critical": 3}
FOLDS = 10


def prepare(frame: pd.DataFrame, max_rows: int = 0):
    """Fixed bugs with a real assignee, oldest first, as (texts, metas, developers)."""
    frame = datasets.with_assignees(frame, min_bugs=1)
    if max_rows and len(frame) > max_rows:
        frame = frame.tail(max_rows)
    raw = frame["severity"].astype(str).str.strip().str.lower().map(datasets.BUGZILLA_TO_LEVEL)
    known = set(frame["bug_id"])
    metas = [cnn_lstm.encode_meta(SEVERITY_RANK.get(level), bool(dup == dup and dup in known))
             for level, dup in zip(raw, frame["dup_id"])]
    recorded = pd.to_numeric(frame["reassignments"], errors="coerce")
    history = None
    if recorded.notna().any():
        history = {"mean_recorded_reassignments": round(float(recorded.mean()), 3), "share_reassigned": round(float((recorded > 0).mean()), 3)}
        print(f"  recorded history: {history['mean_recorded_reassignments']} reassignments per fixed bug, "
              f"{history['share_reassigned']:.0%} reassigned at least once")
    return datasets.text_of(frame).tolist(), metas, frame["assignee"].tolist(), history


def ranks_of(scores: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Zero-based position of the true developer when candidates are sorted by score."""
    return (scores > scores[np.arange(len(target)), target][:, None]).sum(axis=1)


def summarise(ranks: np.ndarray) -> dict:
    return {**{f"top{k}": round(float((ranks < k).mean()), 4) for k in (1, 3, 5)}, "mnr": round(float(ranks.mean()), 3)}


def run_fold(train, test, min_bugs: int, epochs: int, batch_size: int, device: str) -> tuple[dict, bytes]:
    texts, metas, labels = train
    counts = pd.Series(labels).value_counts()
    keep_devs = set(counts[counts >= min_bugs].index)
    rows = [i for i, label in enumerate(labels) if label in keep_devs]
    texts, metas, labels = [texts[i] for i in rows], [metas[i] for i in rows], [labels[i] for i in rows]
    cut = int(len(texts) * 0.9)  # newest tenth of the training window selects the epoch; the test slice never does
    blob, _ = cnn_lstm.train_model(texts[:cut], metas[:cut], labels[:cut], epochs=epochs, batch_size=batch_size, device=device,
                                   val_data=(texts[cut:], metas[cut:], labels[cut:]))
    model = cnn_lstm.AssignmentModel(blob)
    index = {dev: i for i, dev in enumerate(model.classes)}
    covered = [i for i, label in enumerate(test[2]) if label in index]
    result = {"train_bugs": len(texts), "developers": len(model.classes), "test_bugs": len(test[2]),
              "coverage": round(len(covered) / max(1, len(test[2])), 4)}
    if not covered:
        return result, blob
    t_texts, t_metas = [test[0][i] for i in covered], [test[1][i] for i in covered]
    target = np.array([index[test[2][i]] for i in covered])
    started = time.perf_counter()
    probabilities = np.concatenate([model.predict_proba(t_texts[i:i + 256], t_metas[i:i + 256]) for i in range(0, len(covered), 256)])
    result["mean_inference_ms"] = round((time.perf_counter() - started) / len(covered) * 1000, 3)
    result["cnn_lstm"] = summarise(ranks_of(probabilities, target))
    # Baselines, on exactly the same bugs and the same candidate developers.
    n = len(model.classes)
    result["random"] = {"mnr": round((n - 1) / 2, 3), **{f"top{k}": round(min(1.0, k / n), 4) for k in (1, 3, 5)}}
    frequency = np.array([counts[dev] for dev in model.classes], dtype=float)
    result["most_active_first"] = summarise(ranks_of(np.tile(frequency, (len(target), 1)), target))
    vectoriser = TfidfVectorizer(sublinear_tf=True, min_df=2, max_features=100_000, ngram_range=(1, 2))
    known = [i for i, label in enumerate(labels) if label in index]
    svm = LinearSVC(C=0.5).fit(vectoriser.fit_transform([texts[i] for i in known]), [index[labels[i]] for i in known])
    if len(svm.classes_) < n:  # a developer present only in the model-selection tail
        full = np.full((len(target), n), -np.inf)
        full[:, svm.classes_] = svm.decision_function(vectoriser.transform(t_texts)).reshape(len(target), -1)
        result["tfidf_svm"] = summarise(ranks_of(full, target))
        return result, blob
    result["tfidf_svm"] = summarise(ranks_of(svm.decision_function(vectoriser.transform(t_texts)), target))
    return result, blob


def rolling(texts, metas, labels, *, window: int, min_bugs: int, epochs: int, batch_size: int, device: str) -> tuple[dict, bytes]:
    edges = np.linspace(0, len(texts), FOLDS + 2).astype(int)
    folds, blob = [], b""
    for k in range(1, FOLDS + 1):
        start, stop = edges[k], edges[k + 1]
        first = max(0, start - window)
        fold, blob = run_fold((texts[first:start], metas[first:start], labels[first:start]),
                              (texts[start:stop], metas[start:stop], labels[start:stop]), min_bugs, epochs, batch_size, device)
        folds.append(fold)
        shown = fold.get("cnn_lstm", {})
        print(f"  slice {k:>2}: train {fold['train_bugs']:>6}, {fold['developers']:>4} developers, coverage {fold['coverage']:.0%}, "
              f"top1 {shown.get('top1')}, top5 {shown.get('top5')}, MNR {shown.get('mnr')} (random {fold.get('random', {}).get('mnr')})", flush=True)
    scored = [f for f in folds if "cnn_lstm" in f]
    weights = np.array([f["test_bugs"] * f["coverage"] for f in scored])

    def average(method: str, key: str) -> float:
        return round(float(np.average([f[method][key] for f in scored], weights=weights)), 4)

    methods = {m: {key: average(m, key) for key in ("top1", "top3", "top5", "mnr")} for m in ("cnn_lstm", "tfidf_svm", "most_active_first", "random")}
    reduction = 1 - methods["cnn_lstm"]["mnr"] / methods["random"]["mnr"]
    summary = {
        "methods": methods, "mnr_reduction_vs_random": round(reduction, 4),
        "mnr_reduction_vs_most_active_first": round(1 - methods["cnn_lstm"]["mnr"] / methods["most_active_first"]["mnr"], 4),
        "mean_coverage": round(float(np.mean([f["coverage"] for f in folds])), 4),
        "mean_developers": round(float(np.mean([f["developers"] for f in folds])), 1),
        "target_mnr_reduction": TARGET_MNR_REDUCTION, "target_met": reduction >= TARGET_MNR_REDUCTION, "slices": folds,
    }
    return summary, blob


def report(name: str, summary: dict) -> None:
    print(f"{name}: average over {FOLDS} slices, {summary['mean_developers']:.0f} developers on average, coverage {summary['mean_coverage']:.0%}")
    for method, values in summary["methods"].items():
        print(f"  {method:<18} top1 {values['top1']:.4f}  top3 {values['top3']:.4f}  top5 {values['top5']:.4f}  MNR {values['mnr']:.2f}")
    print(f"  MNR reduction vs random {summary['mnr_reduction_vs_random']:.1%} (target {TARGET_MNR_REDUCTION:.0%}: "
          f"{'met' if summary['target_met'] else 'NOT met'}); vs most-active-first {summary['mnr_reduction_vs_most_active_first']:.1%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, help="Training dataset (Eclipse)")
    parser.add_argument("--ood", type=Path, help="Second tracker for the generalisation check (Mozilla)")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--min-bugs", type=int, default=10, help="A developer needs this many fixes in the training window to be a candidate")
    parser.add_argument("--window", type=int, default=20000, help="Most recent bugs used for training before each slice")
    parser.add_argument("--max-rows", type=int, default=0, help="Use only the most recent N fixed bugs (0 = all)")
    parser.add_argument("--epochs", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--out", type=Path, default=ROOT / "models")
    parser.add_argument("--report", type=Path, default=ROOT / "docs" / "evaluation")
    parser.add_argument("--tag", default="", help="Suffix for the report file name, e.g. the dataset name")
    args = parser.parse_args()
    if not args.synthetic and not args.data:
        parser.error("give --data PATH or --synthetic")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    settings = dict(window=args.window, min_bugs=args.min_bugs, epochs=args.epochs, batch_size=args.batch_size, device=device)

    frame = synthetic.generate(6000) if args.synthetic else datasets.load(args.data)
    texts, metas, labels, history = prepare(frame, args.max_rows)
    print(f"{len(texts)} fixed bugs with an assignee, {len(set(labels))} developers in total; device {device}")
    summary, blob = rolling(texts, metas, labels, **settings)
    report("Result", summary)
    meta = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": "synthetic (pipeline check only)" if args.synthetic else str(args.data),
        "protocol": f"rolling chronological, {FOLDS} slices, training window {args.window} bugs, developers with >= {args.min_bugs} fixes in the window",
        "fixed_bugs": len(texts), "recorded_history": history, "epochs": args.epochs, **summary,
    }
    if args.ood:
        o_texts, o_metas, o_labels, o_history = prepare(datasets.load(args.ood), args.max_rows)
        print(f"Generalisation check on {args.ood.name}: {len(o_texts)} fixed bugs, {len(set(o_labels))} developers")
        o_summary, _ = rolling(o_texts, o_metas, o_labels, **settings)
        report("Second tracker", o_summary)
        meta["second_tracker"] = {"dataset": str(args.ood), "fixed_bugs": len(o_texts), "recorded_history": o_history, **o_summary}

    args.report.mkdir(parents=True, exist_ok=True)
    name = f"assignment{'_synthetic' if args.synthetic else ''}{'_' + args.tag if args.tag else ''}.json"
    (args.report / name).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if args.synthetic:
        print("Synthetic run: model NOT saved. A pipeline check, not a result.")
        return
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "assignment_benchmark.pt").write_bytes(blob)
    print(f"Model of the last slice saved to {args.out / 'assignment_benchmark.pt'}; report in {args.report}")


if __name__ == "__main__":
    main()
