"""Train and evaluate the severity classifier (Section 3.6.1).

    python -m ml.train_severity --data ../data/raw/zenodo_eclipse --tag eclipse
    python -m ml.train_severity --data ../data/raw/zenodo_eclipse --no-encoder --tag eclipse_vote   # Capstone 1 design only
    python -m ml.train_severity --data ../data/raw/zenodo_eclipse --drop-default                    # without Bugzilla's 'normal'
    python -m ml.train_severity --synthetic                                                         # pipeline check only

Method: 80/20 stratified split, seed 42; the 20% test part is used once, at the end.
10-fold cross-validation on the training part selects the SVM regularisation strength.
The training part is split again, 80/20 with seed 7:
  - on the larger part the three TF-IDF classifiers (SVM, Random Forest, Naive Bayes)
    are fitted and MiniLM is fine-tuned (three epochs, a GPU is used when present);
  - the held-out part chooses the encoder's epoch, then one half of it fits the logistic
    regression that combines the four classifiers and the other half sets per-class
    offsets: macro F1 as high as possible without accuracy or Critical recall falling
    below the three-classifier majority vote on those reports.
The test report gives the final model, the majority vote and each classifier alone:
accuracy, macro and weighted precision / recall / F1, and the confusion matrix. The model
is written to models/severity.joblib and models/severity-encoder/, which the backend
loads at start-up.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, precision_recall_fscore_support, recall_score
from sklearn.base import clone
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split

from app.ai.severity import ENCODER_DIRNAME, MODEL_FILENAME, EncoderClassifier, build_ensemble, member_scores
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


def train_encoder(x_fit, y_fit, x_hold, y_hold, classes, args):
    """Fine-tune MiniLM on the fitting part; keep the epoch with the best held-out macro F1."""
    import copy

    import torch
    import torch.nn as nn
    torch.manual_seed(0)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    encoder = EncoderClassifier(len(classes)).to(device)
    index = {c: i for i, c in enumerate(classes)}
    target = np.array([index[y] for y in y_fit])
    counts = np.bincount(target, minlength=len(classes))
    weights = torch.tensor((counts.sum() / counts) ** 0.5, dtype=torch.float32, device=device)
    loss_fn = nn.CrossEntropyLoss(weight=weights / weights.mean())  # softened class weights for the rare levels
    optimiser = torch.optim.AdamW(encoder.net.parameters(), lr=5e-5, weight_decay=0.01)
    steps_per_epoch = len(x_fit) // args.encoder_batch_size
    schedule = torch.optim.lr_scheduler.OneCycleLR(optimiser, max_lr=5e-5, total_steps=args.encoder_epochs * steps_per_epoch,
                                                   pct_start=0.06, anneal_strategy="linear")
    scaler = torch.amp.GradScaler(enabled=device == "cuda")
    best, history = (-1.0, None, -1), []
    for epoch in range(args.encoder_epochs):
        encoder.net.train()
        order = np.random.default_rng(epoch).permutation(len(x_fit))
        for step in range(steps_per_epoch):
            rows = order[step * args.encoder_batch_size:(step + 1) * args.encoder_batch_size]
            with torch.autocast(device, enabled=device == "cuda"):
                loss = loss_fn(encoder.net(*encoder.batch([x_fit[i] for i in rows])), torch.tensor(target[rows], device=device))
            optimiser.zero_grad()
            scaler.scale(loss).backward()
            scaler.step(optimiser)
            scaler.update()
            schedule.step()
        predicted = np.array(classes)[encoder.logits(x_hold).argmax(1)]
        score = float(f1_score(y_hold, predicted, average="macro"))
        history.append({"epoch": epoch + 1, "held_out_macro_f1": round(score, 4),
                        "held_out_accuracy": round(float(accuracy_score(y_hold, predicted)), 4)})
        print(f"  encoder epoch {epoch + 1}: held-out macro F1 {score:.4f}", flush=True)
        if score > best[0]:
            best = (score, copy.deepcopy(encoder.net.state_dict()), epoch + 1)
    encoder.net.load_state_dict(best[1])
    info = {"base": "sentence-transformers/all-MiniLM-L6-v2", "epochs": history, "selected_epoch": best[2], "device": device}
    return encoder, info


def combine(model, encoder, x_hold, y_hold, classes, trials: int = 6000) -> dict:
    """Learn the combination on one half of the held-out data and the operating point on the other.

    Operating point: per-class offsets added to the combined log-probabilities, chosen to
    maximise macro F1 subject to neither accuracy nor Critical recall falling below the
    three-classifier majority vote on the same reports.
    """
    scores, _ = member_scores(model, x_hold)
    features = np.hstack([encoder.logits(x_hold), scores])
    y_hold = np.asarray(y_hold)
    fa, fb, ya, yb, _, rows_b = train_test_split(features, y_hold, np.arange(len(y_hold)), test_size=0.5, stratify=y_hold, random_state=3)
    stack = LogisticRegression(max_iter=3000).fit(fa, ya)
    assert list(stack.classes_) == classes
    log_p = stack.predict_log_proba(fb)
    vote = model.predict([x_hold[i] for i in rows_b])
    floor_acc = float(accuracy_score(yb, vote))
    floor_crit = float(recall_score(yb, vote, labels=["Critical"], average=None)[0])
    rng, grid, medium = np.random.default_rng(0), np.linspace(-1, 3, 17), classes.index("Medium")
    best = (-1.0, np.zeros(len(classes)), 0.0, 0.0)
    for _ in range(trials):
        bias = rng.choice(grid, size=len(classes))
        bias[medium] = 0.0
        predicted = np.array(classes)[(log_p + bias).argmax(1)]
        acc = accuracy_score(yb, predicted)
        crit = recall_score(yb, predicted, labels=["Critical"], average=None)[0]
        if acc < floor_acc or crit < floor_crit:
            continue
        f = f1_score(yb, predicted, average="macro")
        if f > best[0]:
            best = (f, bias.copy(), acc, crit)
    return {"stack": stack, "bias": best[1], "held_out_macro_f1": round(float(best[0]), 4),
            "held_out_accuracy": round(float(best[2]), 4), "held_out_critical_recall": round(float(best[3]), 4),
            "floor_accuracy": round(floor_acc, 4), "floor_critical_recall": round(floor_crit, 4)}


def operating_point(combination: dict | None, classes: list[str]) -> dict | None:
    if combination is None:
        return None
    shown = {k: v for k, v in combination.items() if k not in ("stack", "bias")}
    return shown | {"bias": dict(zip(classes, np.round(combination["bias"], 2).tolist()))}


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
    parser.add_argument("--no-encoder", dest="encoder", action="store_false",
                        help="Train only the three TF-IDF classifiers and their majority vote (the Capstone 1 design)")
    parser.add_argument("--encoder-epochs", type=int, default=3)
    parser.add_argument("--encoder-batch-size", type=int, default=64)
    args = parser.parse_args()
    if not args.synthetic and not args.data:
        parser.error("give --data PATH or --synthetic")

    frame = synthetic.generate(4000) if args.synthetic else datasets.load(args.data)
    frame = datasets.with_levels(frame, drop_default=args.drop_default)
    if args.drop_default:
        args.encoder = False  # a comparison run of the TF-IDF classifiers; Medium is absent, so there is no operating point to set
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

    # Model fitting uses 80% of the training part; the other 20% is held out to choose the
    # encoder's epoch, learn how to combine the four classifiers and set the operating point.
    x_fit, x_hold, y_fit, y_hold = train_test_split(x_train, y_train, test_size=0.2, stratify=y_train, random_state=7)
    started = time.perf_counter()
    model = build_ensemble()
    model.set_params(vote__svm__C=best_c)
    model.fit(x_fit, y_fit)
    fit_seconds = time.perf_counter() - started
    classes = list(model.named_steps["vote"].le_.classes_)  # alphabetical; the encoder and the combiner use the same order

    vote_test = model.predict(x_test)
    features = model.named_steps["tfidf"].transform(x_test)
    vote = model.named_steps["vote"]
    members = {name: round(float(accuracy_score(y_test, vote.le_.inverse_transform(est.predict(features).astype(int)))), 4)
               for name, est in zip(("svm", "random_forest", "naive_bayes"), vote.estimators_)}
    report_vote = evaluate(y_test, vote_test, labels)
    combination, encoder_info = None, None
    if args.encoder:
        encoder, encoder_info = train_encoder(x_fit, y_fit, x_hold, y_hold, classes, args)
        combination = combine(model, encoder, x_hold, y_hold, classes)
        started = time.perf_counter()
        scores, _ = member_scores(model, x_test)
        logits = encoder.logits(x_test)
        combined = combination["stack"].predict_log_proba(np.hstack([logits, scores])) + combination["bias"]
        predicted = np.array(classes)[combined.argmax(1)]
        latency_ms = (time.perf_counter() - started) / len(x_test) * 1000
        members["encoder"] = round(float(accuracy_score(y_test, np.array(classes)[logits.argmax(1)])), 4)
        report = evaluate(y_test, predicted, labels)
    else:
        started = time.perf_counter()
        model.predict(x_test)
        latency_ms = (time.perf_counter() - started) / len(x_test) * 1000
        report = report_vote

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": "synthetic (pipeline check only)" if args.synthetic else str(args.data),
        "drop_default_severity": args.drop_default, "rows": len(texts), "class_distribution": distribution,
        "split": "80/20 stratified, seed 42", "cv_folds": args.folds, "cv_accuracy_by_svm_C": {str(k): v for k, v in cv_scores.items()},
        "selected_svm_C": best_c, "majority_class_baseline": round(majority, 4), "member_accuracy": members,
        "model": "four classifiers combined by logistic regression" if args.encoder else "majority vote of three TF-IDF classifiers",
        "three_classifier_vote": report_vote, "encoder": encoder_info,
        "operating_point": operating_point(combination, classes),
        "fit_seconds": round(fit_seconds, 1), "mean_inference_ms_per_report": round(latency_ms, 3),
        "target_accuracy": TARGET_ACCURACY, "target_met": report["accuracy"] >= TARGET_ACCURACY, "test": report,
    }
    print(f"\nHeld-out accuracy {report['accuracy']:.4f} (majority-class baseline {majority:.4f}, target {TARGET_ACCURACY})")
    print(f"macro F1 {report['macro']['f1']:.4f}, weighted F1 {report['weighted']['f1']:.4f}; members {members}")
    if args.encoder:
        print(f"Three-classifier vote for comparison: accuracy {report_vote['accuracy']:.4f}, macro F1 {report_vote['macro']['f1']:.4f}")
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
    if args.encoder:
        encoder.save(args.out / ENCODER_DIRNAME)
    joblib.dump({"pipeline": model, "stack": combination["stack"] if combination else None,
                 "bias": combination["bias"].tolist() if combination else None, "encoder_dir": ENCODER_DIRNAME, "meta": {k: meta[k] for k in ("trained_at", "dataset", "rows", "drop_default_severity")} | {"test_accuracy": report["accuracy"]}},
                args.out / MODEL_FILENAME, compress=3)
    print(f"\nModel saved to {args.out / MODEL_FILENAME}; report in {args.report}")


if __name__ == "__main__":
    main()
