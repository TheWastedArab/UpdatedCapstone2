"""Compare the severity ensemble with the baseline and with each classifier alone (Section 6.5).

    python -m ml.compare_severity --data ../data/raw/zenodo_eclipse

Loads the trained model from models/ (ml.train_severity) and rebuilds the same 80/20
stratified test split (seed 42), so every row of the comparison is measured on the same
39,036 reports that no part of the model was fitted, tuned or combined on. For each of
the majority-class baseline, the three TF-IDF classifiers, the fine-tuned encoder, the
three-classifier vote (the Capstone 1 design) and the deployed combination it reports
accuracy, macro and per-class precision / recall / F1 and the confusion matrix.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.model_selection import train_test_split

from app.ai.severity import ENCODER_DIRNAME, MODEL_FILENAME, EncoderClassifier, member_scores
from app.ai.text import CANONICAL_SEVERITIES
from ml import datasets
from ml.train_severity import evaluate

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--models", type=Path, default=ROOT / "models")
    parser.add_argument("--report", type=Path, default=ROOT / "docs" / "evaluation")
    args = parser.parse_args()

    frame = datasets.with_levels(datasets.load(args.data))
    texts, levels = datasets.text_of(frame).tolist(), frame["level"].tolist()
    labels = [label for label in CANONICAL_SEVERITIES if label in set(levels)]
    _, x_test, _, y_test = train_test_split(texts, levels, test_size=0.2, stratify=levels, random_state=42)
    print(f"{len(x_test)} test reports")

    bundle = joblib.load(args.models / MODEL_FILENAME)  # the project's own model file
    pipeline, stack, bias = bundle["pipeline"], bundle["stack"], np.asarray(bundle["bias"], dtype=float)
    classes = list(pipeline.named_steps["vote"].le_.classes_)
    encoder = EncoderClassifier.load(args.models / bundle.get("encoder_dir", ENCODER_DIRNAME), len(classes))
    try:
        import torch
        encoder.to("cuda" if torch.cuda.is_available() else "cpu")
    except ImportError:
        pass

    names = np.array(classes)
    scores, member_predicted = member_scores(pipeline, x_test)
    logits = encoder.logits(x_test)
    combined = stack.predict_log_proba(np.hstack([logits, scores])) + bias
    majority = max(set(levels), key=levels.count)
    predictions = {
        "majority_class_baseline": np.array([majority] * len(y_test)),
        "svm": names[member_predicted[:, 0]],
        "random_forest": names[member_predicted[:, 1]],
        "naive_bayes": names[member_predicted[:, 2]],
        "encoder": names[logits.argmax(1)],
        "three_classifier_vote": pipeline.predict(x_test),
        "deployed_combination": names[combined.argmax(1)],
    }
    results = {name: evaluate(y_test, predicted, labels) for name, predicted in predictions.items()}
    print(f"{'classifier':<24}{'accuracy':>9}{'macro F1':>9}" + "".join(f"{'R ' + label:>11}" for label in labels))
    for name, r in results.items():
        print(f"{name:<24}{r['accuracy']:>9.4f}{r['macro']['f1']:>9.4f}"
              + "".join(f"{r['per_class'][label]['recall']:>11.4f}" for label in labels))
    meta = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": str(args.data), "model_trained_at": bundle.get("meta", {}).get("trained_at"),
        "split": "80/20 stratified, seed 42 (the test set of ml.train_severity)", "test_reports": len(y_test),
        "results": results,
    }
    args.report.mkdir(parents=True, exist_ok=True)
    (args.report / "severity_comparison_eclipse.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Report written to {args.report / 'severity_comparison_eclipse.json'}")


if __name__ == "__main__":
    main()
