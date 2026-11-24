"""Evaluate two-phase duplicate detection (Section 3.6.2).

    python -m ml.eval_duplicates --data ../data/raw/gitbugs/firefox.csv --holdout 0.3
    python -m ml.eval_duplicates --data ../data/raw/gitbugs/firefox.csv --holdout 0.3 --model ../models/sbert-duplicates
    python -m ml.eval_duplicates --synthetic

Each report that has an earlier duplicate is used as a query. By default the
evaluation is chronological, as the live check is: a query is searched only
against reports created before it, and only those earlier members of its
duplicate group count as correct answers. --include-later reproduces the
first protocol, which searched every other report, including later ones. Reports Recall@1/5/10, Mean Reciprocal Rank and Mean Average
Precision for BM25 alone and for BM25 followed by Sentence-BERT re-ranking,
with the mean time per query. Text preparation, retrieval and score fusion are
the functions the backend itself uses (app.ai.duplicates).

--holdout F evaluates only on the newest fraction F of duplicate groups (ordered by
the id of the group's first report). The fine-tuning script (ml.train_duplicates)
trains on the remaining older groups with the same split, so a fine-tuned encoder
is always tested on groups it has never seen. When the encoder was fine-tuned on a
larger collection than the one evaluated (the four Eclipse projects together,
evaluated one project at a time), the newest groups of that collection and of one
project are not the same; --fine-tuned-on PATH then also drops every held-out group
that falls in the training part of PATH. Pass it for the base encoder too, so both
are measured on the same queries. The BM25 statistics (document frequencies) are
computed once over the whole corpus; they use no labels.
"""
import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.ai.duplicates import BM25Index, Embedder, embed_text, fuse, index_text
from app.config import get_settings
from ml import datasets, synthetic

ROOT = Path(__file__).resolve().parents[2]
TARGET_RECALL_AT_5 = 0.80
KS = (1, 5, 10)


def split_groups(roots: dict[int, int], holdout: float) -> tuple[set[int], set[int]]:
    """Group roots split by age: (older groups for training, newest groups for testing)."""
    ordered = sorted(set(roots.values()))
    cut = len(ordered) - int(round(len(ordered) * holdout))
    return set(ordered[:cut]), set(ordered[cut:])


def rank_metrics(rankings: list[list[int]], relevant: list[set[int]]) -> dict:
    recall = {k: 0 for k in KS}
    reciprocal, precision = [], []
    for ranked, wanted in zip(rankings, relevant):
        hits = [i for i, bug in enumerate(ranked) if bug in wanted]
        for k in KS:
            recall[k] += any(i < k for i in hits)
        reciprocal.append(1 / (hits[0] + 1) if hits else 0.0)
        # Average precision over the retrieved list, normalised by the number of relevant reports.
        precision.append(sum((n + 1) / (i + 1) for n, i in enumerate(hits)) / len(wanted) if wanted else 0.0)
    n = max(1, len(rankings))
    return {**{f"recall@{k}": round(recall[k] / n, 4) for k in KS}, "mrr": round(float(np.mean(reciprocal)), 4),
            "map": round(float(np.mean(precision)), 4)}


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, help="Dataset file or directory with duplicate links")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--holdout", type=float, default=0.0, help="Evaluate only on this newest fraction of duplicate groups")
    parser.add_argument("--model", default=settings.sbert_model, help="Sentence encoder name or path")
    parser.add_argument("--max-queries", type=int, default=5000)
    parser.add_argument("--candidates", type=int, default=settings.duplicate_candidates)
    parser.add_argument("--fusion", type=float, default=settings.duplicate_fusion)
    parser.add_argument("--report", type=Path, default=ROOT / "docs" / "evaluation")
    parser.add_argument("--tag", default="", help="Suffix for the report file name, e.g. the dataset name")
    parser.add_argument("--fine-tuned-on", type=Path,
                        help="Data the encoder was fine-tuned on (same --holdout); its training groups are left out of the queries")
    parser.add_argument("--include-later", action="store_true",
                        help="Search every other report, including later ones (the first protocol); default: earlier reports only")
    args = parser.parse_args()
    if not args.synthetic and not args.data:
        parser.error("give --data PATH or --synthetic")

    frame = synthetic.generate(3000) if args.synthetic else datasets.load(args.data)
    roots = datasets.duplicate_groups(frame)
    groups: dict[int, set[int]] = defaultdict(set)
    for bug, root in roots.items():
        groups[root].update((bug, root))
    _, test_roots = split_groups(roots, args.holdout) if args.holdout else (set(), set(groups))
    seen_in_training = 0
    if args.fine_tuned_on:
        trained_roots, _ = split_groups(datasets.duplicate_groups(datasets.load(args.fine_tuned_on)), args.holdout)
        seen_in_training = len(test_roots & trained_roots)
        test_roots -= trained_roots
        print(f"{seen_in_training} held-out groups were in the fine-tuning data of {args.fine_tuned_on} and are left out")
    # A query is a report filed after its group's first report: the situation the live check faces.
    queries = sorted(bug for bug, root in roots.items() if bug != root and root in test_roots)
    rng = np.random.default_rng(42)
    if len(queries) > args.max_queries:
        queries = sorted(rng.choice(queries, args.max_queries, replace=False).tolist())
    ids = frame["bug_id"].tolist()
    titles, descriptions = frame["title"].tolist(), frame["description"].tolist()
    position = {bug: i for i, bug in enumerate(ids)}
    # Time order: creation date, with the bug id to break ties. If any date is missing the
    # bug id orders the whole corpus, so a report without a date is never put first.
    created = frame["created"] if "created" in frame and frame["created"].notna().all() else None
    if created is not None:
        order = np.lexsort((frame["bug_id"].to_numpy(), created.to_numpy()))
    else:
        order = np.argsort(frame["bug_id"].to_numpy(), kind="stable")
    when = np.empty(len(ids), dtype=np.int64)
    when[order] = np.arange(len(ids))
    chronological = not args.include_later
    if chronological:  # a query needs at least one group member filed before it
        queries = [q for q in queries if any(when[position[m]] < when[position[q]] for m in groups[roots[q]] if m != q)]
    print(f"Corpus {len(ids)} reports, {sum(len(g) - 1 for g in groups.values())} duplicates in {len(groups)} groups, "
          f"{len(queries)} queries" + (f" from the newest {args.holdout:.0%} of groups" if args.holdout else ""))
    if not queries:
        raise SystemExit("No duplicate links whose original is also in the corpus were found.")

    started = time.perf_counter()
    index_texts = [index_text(t, d) for t, d in zip(titles, descriptions)]
    embed_texts = [embed_text(t, d) for t, d in zip(titles, descriptions)]
    index = BM25Index(ids, index_texts)
    print(f"BM25 index built in {time.perf_counter() - started:.1f}s")
    embedder = Embedder()
    semantic = embedder.load(str(args.model))
    vectors = None
    if semantic:
        started = time.perf_counter()
        vectors = np.asarray(embedder.model.encode(embed_texts, normalize_embeddings=True, batch_size=256, show_progress_bar=False), dtype=np.float32)
        print(f"Corpus embedded in {time.perf_counter() - started:.1f}s")

    print(f"{len(queries)} queries, " + ("chronological: earlier reports only" if chronological else "searching all other reports"))
    relevant, bm25_rankings, two_phase_rankings = [], [], []
    bm25_ms, rerank_ms = [], []
    for bug in queries:
        here = position[bug]
        earlier = when < when[here] if chronological else None
        relevant.append({m for m in groups[roots[bug]] - {bug} if not chronological or earlier[position[m]]})
        started = time.perf_counter()
        shortlist = index.top(index_texts[here], args.candidates, exclude_id=bug, allowed=earlier)
        bm25_ms.append((time.perf_counter() - started) * 1000)
        candidates = [i for i, _ in shortlist]
        bm25_rankings.append(candidates)
        if semantic and candidates:
            started = time.perf_counter()
            query_vector = embedder.encode([embed_texts[here]])[0]  # embedded per query, as the live service does
            similarity = vectors[[position[i] for i in candidates]] @ query_vector
            order = np.argsort(-fuse(similarity, np.array([s for _, s in shortlist]), args.fusion))
            two_phase_rankings.append([candidates[i] for i in order])
            rerank_ms.append((time.perf_counter() - started) * 1000)
        elif semantic:  # every candidate was filtered out: nothing to re-rank
            two_phase_rankings.append([])
            rerank_ms.append(0.0)

    results = {"bm25_only": {**rank_metrics(bm25_rankings, relevant), "mean_ms_per_query": round(float(np.mean(bm25_ms)), 2)}}
    if semantic:
        results["bm25_plus_sbert"] = {**rank_metrics(two_phase_rankings, relevant),
                                      "mean_ms_per_query": round(float(np.mean(bm25_ms) + np.mean(rerank_ms)), 2)}
    # The share of queries whose duplicate is in the BM25 shortlist caps what re-ranking can achieve.
    ceiling = sum(bool(set(r) & w) for r, w in zip(bm25_rankings, relevant)) / len(queries)
    final = results.get("bm25_plus_sbert", results["bm25_only"])
    meta = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": "synthetic (pipeline check only)" if args.synthetic else str(args.data),
        "corpus_size": len(ids), "duplicates": sum(len(g) - 1 for g in groups.values()), "groups": len(groups), "queries": len(queries),
        "holdout_fraction_of_groups": args.holdout,
        "fine_tuned_on": str(args.fine_tuned_on) if args.fine_tuned_on else None,
        "held_out_groups_left_out_as_seen_in_fine_tuning": seen_in_training,
        "protocol": "chronological: each query searches only reports created before it, and only those count as correct"
                    if chronological else "all other reports searched, including later ones", "phase1_candidates": args.candidates, "phase1_ceiling": round(ceiling, 4),
        "fusion_weight": args.fusion, "sbert_model": str(args.model) if semantic else None, "results": results,
        "target_recall@5": TARGET_RECALL_AT_5, "target_met": final["recall@5"] >= TARGET_RECALL_AT_5,
    }
    for name, values in results.items():
        print(f"{name:<18}" + "  ".join(f"{k}={v}" for k, v in values.items()))
    print(f"Phase-1 ceiling (duplicate within top {args.candidates}): {ceiling:.4f}; target Recall@5 {TARGET_RECALL_AT_5}: "
          f"{'met' if meta['target_met'] else 'NOT met'}")
    args.report.mkdir(parents=True, exist_ok=True)
    name = f"duplicates{'_synthetic' if args.synthetic else ''}{'_' + args.tag if args.tag else ''}.json"
    (args.report / name).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if args.synthetic:
        print("Synthetic run: a pipeline check, not a result.")


if __name__ == "__main__":
    main()
