"""Fine-tune the sentence encoder on confirmed duplicate pairs (Sections 1.10.1 and 2.2.3).

    python -m ml.train_duplicates --data ../data/raw/zenodo_eclipse ../data/raw/gitbugs/firefox.csv ../data/raw/gitbugs/thunderbird.csv

The published all-MiniLM-L6-v2 encoder was trained on general text. Here it is
trained further, Siamese-style, on pairs of bug reports that maintainers marked
as duplicates of one another: each pair is pulled together and the other
reports in the same batch act as non-duplicates (multiple-negatives ranking
loss). This is the "labelled duplicate pairs" training the literature review
identifies as what makes the semantic stage work.

Only the older (1 - holdout) share of each dataset's duplicate groups is used.
The newest groups are never seen in training and are what ml.eval_duplicates
--holdout measures. The result is written to models/sbert-duplicates, which
the backend loads in place of the base encoder when it exists.
"""
import argparse
import json
import random
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import torch
import torch.nn.functional as F
from sentence_transformers import SentenceTransformer

from app.ai.duplicates import embed_text
from app.ai.engine import FINE_TUNED_DIR
from app.config import get_settings
from ml import datasets
from ml.eval_duplicates import split_groups

ROOT = Path(__file__).resolve().parents[2]
MAX_PAIRS_PER_GROUP = 6


def training_pairs(path: Path, holdout: float, rng: random.Random) -> tuple[list[tuple[str, str]], dict]:
    frame = datasets.load(path)
    roots = datasets.duplicate_groups(frame)
    train_roots, test_roots = split_groups(roots, holdout)
    text = {bug: embed_text(title, description) for bug, title, description in zip(frame["bug_id"], frame["title"], frame["description"])}
    groups: dict[int, list[int]] = defaultdict(list)
    for bug, root in roots.items():
        if root in train_roots:
            groups[root].append(bug)
    pairs = []
    for members in groups.values():
        members = sorted(set(members))
        combos = [(a, b) for i, a in enumerate(members) for b in members[i + 1:]]
        for a, b in rng.sample(combos, min(len(combos), MAX_PAIRS_PER_GROUP)):
            pairs.append((text[a], text[b]))
    return pairs, {"dataset": str(path), "reports": len(frame), "train_groups": len(groups), "held_out_groups": len(test_roots), "pairs": len(pairs)}


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, nargs="+", required=True, help="Dataset files or directories with duplicate links")
    parser.add_argument("--holdout", type=float, default=0.3, help="Newest share of duplicate groups kept out of training")
    parser.add_argument("--base", default=settings.sbert_model)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--out", type=Path, default=ROOT / "models" / FINE_TUNED_DIR)
    args = parser.parse_args()

    rng = random.Random(42)
    pairs, sources = [], []
    for path in args.data:
        found, info = training_pairs(path, args.holdout, rng)
        pairs.extend(found)
        sources.append(info)
        print(f"{path}: {info['pairs']} pairs from {info['train_groups']} groups ({info['held_out_groups']} groups held out)")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(args.base, device=device)
    steps = len(pairs) // args.batch_size * args.epochs
    warmup = max(1, int(0.1 * steps))
    optimiser = torch.optim.AdamW(model.parameters(), lr=2e-5, weight_decay=0.01)
    schedule = torch.optim.lr_scheduler.LambdaLR(optimiser, lambda step: min(1.0, (step + 1) / warmup) * max(0.0, 1 - step / steps))
    scaler = torch.amp.GradScaler(enabled=device == "cuda")
    print(f"Fine-tuning {args.base} on {len(pairs)} pairs, {steps} steps, device {device}")

    def embed(texts: list[str]) -> torch.Tensor:
        features = {key: value.to(device) for key, value in model.tokenize(texts).items() if hasattr(value, "to")}
        return F.normalize(model(features)["sentence_embedding"], dim=1)

    started, step = time.time(), 0
    model.train()
    for epoch in range(args.epochs):
        rng.shuffle(pairs)
        for start in range(0, len(pairs) - args.batch_size + 1, args.batch_size):
            batch = pairs[start:start + args.batch_size]
            with torch.autocast(device_type=device, enabled=device == "cuda"):
                anchors, positives = embed([a for a, _ in batch]), embed([b for _, b in batch])
                # Multiple-negatives ranking loss: each report must pick out its own duplicate
                # from all the others in the batch, in both directions.
                logits = anchors @ positives.T * 20.0
                target = torch.arange(len(batch), device=device)
                loss = (F.cross_entropy(logits, target) + F.cross_entropy(logits.T, target)) / 2
            optimiser.zero_grad()
            scaler.scale(loss).backward()
            scaler.unscale_(optimiser)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(optimiser)
            scaler.update()
            schedule.step()
            step += 1
            if step % 50 == 0:
                print(f"  step {step}/{steps}  loss {loss.item():.4f}", flush=True)
    model.eval()
    args.out.mkdir(parents=True, exist_ok=True)
    model.save(str(args.out))
    meta = {"trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "base_model": args.base, "pairs": len(pairs),
            "epochs": args.epochs, "batch_size": args.batch_size, "holdout_fraction_of_groups": args.holdout,
            "loss": "multiple-negatives ranking (symmetric), scale 20, lr 2e-5", "seconds": round(time.time() - started), "sources": sources}
    (args.out / "training.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"Saved to {args.out} in {meta['seconds']}s. Evaluate with: python -m ml.eval_duplicates --data <file> --holdout {args.holdout} --model {args.out}")


if __name__ == "__main__":
    main()
