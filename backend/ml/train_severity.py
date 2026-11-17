"""Train and evaluate the severity classifier (Section 3.6.1).

    python -m ml.train_severity --data ../data/raw/eclipse.csv
    python -m ml.train_severity --data ../data/raw/eclipse.csv --drop-default   # without Bugzilla's 'normal'
    python -m ml.train_severity --synthetic                                      # pipeline check only

Method: 80/20 stratified split; 10-fold cross-validation on the training
portion selects the SVM regularisation strength (the TF-IDF + SVM member is
what is cross-validated); the ensemble with that setting is fitted
on all training data and evaluated once on the held-out 20%. Reports accuracy,
macro and weighted precision / recall / F1, and the confusion matrix. The model
is written to models/severity.joblib, which the backend loads at start-up.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.metrics import accuracy_score, confusion_matrix, precision_recall_fscore_support
from sklearn.base import clone
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split

from app.ai.severity import MODEL_FILENAME, build_ensemble
from app.ai.text import CANONICAL_SEVERITIES
from ml import datasets, synthetic

ROOT = Path(__file__).resolve().parents[2]
TARGET_ACCURACY = 0.85


def evaluate(y_true, y_pred, labels: list[str]) -> dict:
    report = {"accuracy": round(float(accuracy_score(y_true, y_pred)), 4)}
    for average in ("macro", "weighted"):
        p, r, f, _ = precision_recall_fscore_support(y_true, y_pred, labels=labels, average=average, zero_division=0)
        report[average] = {"precision": round(float(p), 4), "recall": round(float(r), 4), "f1": round(float(f), 4)}
    p, r, f, support = precision_recall_fscore_support(y_true, y_pred, labels=labels, zero_division=0)
    report["per_class"] = {label: {"precision": round(float(p[i]), 4), "recall": round(float(r[i]), 4), "f1": round(float(f[i]), 4),
                                   "support": int(support[i])} for i, label in enumerate(labels)}
    report["confusion_matrix"] = {"labels": labels, "rows_true_cols_predicted": confusion_matrix(y_true, y_pred, labels=labels).tolist()}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, help="Dataset file or directory (Eclipse)")
    parser.add_argument("--synthetic", action="store_true", help="Use generated data to check the pipeline; results are not evidence")
    parser.add_argument("--drop-default", action="store_true", help="Exclude Bugzilla's default 'normal' severity")
    parser.add_argument("--max-rows", type=int, default=0, help="Subsample for a quicker run (0 = all)")
    parser.add_argument("--folds", type=int, default=10)
    parser.add_argument("--out", type=Path, default=ROOT / "models")
    parser.add_argument("--report", type=Path, default=ROOT / "docs" / "evaluation")
    parser.add_argument("--tag", default="", help="Suffix for the report file name, e.g. the dataset name")
    args = parser.parse_args()
    if not args.synthetic and not args.data:
        parser.error("give --data PATH or --synthetic")

    frame = synthetic.generate(4000) if args.synthetic else datasets.load(args.data)
    frame = datasets.with_levels(frame, drop_default=args.drop_default)
    if args.max_rows and len(frame) > args.max_rows:
        frame = frame.sample(args.max_rows, random_state=42)
    texts, levels = datasets.text_of(frame).tolist(), frame["level"].tolist()
    labels = [l for l in CANONICAL_SEVERITIES if l in set(levels)]
    distribution = {label: levels.count(label) for label in labels}
    print(f"{len(texts)} labelled reports: {distribution}")

    x_train, x_test, y_train, y_test = train_test_split(texts, levels, test_size=0.2, stratify=levels, random_state=42)
    majority = max(distribution.values()) / len(levels)

    # Hyperparameter selection by cross-validation on the training portion only.
    folds = StratifiedKFold(n_splits=args.folds, shuffle=True, random_state=42)
    cv_scores = {}
    for c in (0.1, 0.5, 1.0):
        # The parameter being chosen belongs to the SVM, so the SVM is what is cross-validated.
        candidate = Pipeline([("tfidf", clone(build_ensemble().named_steps["tfidf"])), ("svm", LinearSVC(C=c, class_weight="balanced"))])
        scores = cross_val_score(candidate, x_train, y_train, cv=folds, scoring="accuracy", n_jobs=1)
        cv_scores[c] = {"mean": round(float(scores.mean()), 4), "std": round(float(scores.std()), 4)}
        print(f"  {args.folds}-fold CV, SVM C={c}: {scores.mean():.4f} +/- {scores.std():.4f}")
    best_c = max(cv_scores, key=lambda c: cv_scores[c]["mean"])

    started = time.perf_counter()
    model = build_ensemble()
    model.set_params(vote__svm__C=best_c)
    model.fit(x_train, y_train)
    fit_seconds = time.perf_counter() - started

    started = time.perf_counter()
    predicted = model.predict(x_test)
    latency_ms = (time.perf_counter() - started) / len(x_test) * 1000
    report = evaluate(y_test, predicted, labels)
    # How much each member contributes: the ensemble should beat or match its parts.
    features = model.named_steps["tfidf"].transform(x_test)
    vote = model.named_steps["vote"]
    members = {name: round(float(accuracy_score(y_test, vote.le_.inverse_transform(est.predict(features).astype(int)))), 4)
               for name, est in zip(("svm", "random_forest", "naive_bayes"), vote.estimators_)}

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": "synthetic (pipeline check only)" if args.synthetic else str(args.data),
        "drop_default_severity": args.drop_default, "rows": len(texts), "class_distribution": distribution,
        "split": "80/20 stratified, seed 42", "cv_folds": args.folds, "cv_accuracy_by_svm_C": {str(k): v for k, v in cv_scores.items()},
        "selected_svm_C": best_c, "majority_class_baseline": round(majority, 4), "member_accuracy": members,
        "fit_seconds": round(fit_seconds, 1), "mean_inference_ms_per_report": round(latency_ms, 3),
        "target_accuracy": TARGET_ACCURACY, "target_met": report["accuracy"] >= TARGET_ACCURACY, "test": report,
    }
    print(f"\nHeld-out accuracy {report['accuracy']:.4f} (majority-class baseline {majority:.4f}, target {TARGET_ACCURACY})")
    print(f"macro F1 {report['macro']['f1']:.4f}, weighted F1 {report['weighted']['f1']:.4f}; members {members}")
    print("Confusion matrix (rows = true, columns = predicted):", labels)
    for label, row in zip(labels, report["confusion_matrix"]["rows_true_cols_predicted"]):
        print(f"  {label:<9}{np.array(row)}")

    suffix = "_synthetic" if args.synthetic else ("_no_default" if args.drop_default else "")
    args.report.mkdir(parents=True, exist_ok=True)
    (args.report / f"severity{suffix}{'_' + args.tag if args.tag else ''}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if args.synthetic:
        print("\nSynthetic run: model NOT saved. These numbers are a pipeline check, not a result.")
        return
    if args.drop_default:
        # Without 'normal' the model cannot predict Medium, so this is a comparison run only.
        print("\n--drop-default run: report written, model NOT saved (the service needs all four levels).")
        return
    args.out.mkdir(parents=True, exist_ok=True)
    joblib.dump({"pipeline": model, "meta": {k: meta[k] for k in ("trained_at", "dataset", "rows", "drop_default_severity")} | {"test_accuracy": report["accuracy"]}},
                args.out / MODEL_FILENAME, compress=3)
    print(f"\nModel saved to {args.out / MODEL_FILENAME}; report in {args.report}")


if __name__ == "__main__":
    main()
