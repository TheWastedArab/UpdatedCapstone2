# Evaluation results

Measured results for the acceptance criteria of Sections 1.7 and 3.6 of the report.
Every number here comes from a JSON file in `docs/evaluation/`, produced by the scripts in
`backend/ml/` and `backend/scripts/`; the commands are in the README. Where a target was not
met, that is stated, with the measured value.

## Summary

| Criterion | Target | Measured | Met |
| --- | --- | --- | --- |
| Severity classification accuracy | ≥ 85% | 76.7% (four classes, Eclipse) | **No** |
| Duplicate detection Recall@5 | ≥ 80% | 64% Firefox, 68% Thunderbird, 48–55% Eclipse | **No** |
| Reassignment reduction vs random | ≥ 20% | 69% Eclipse, 51% Mozilla | Yes |
| Duplicate check and submission latency | ≤ 1.5 s | 34 ms and 79 ms (p95, one request at a time) | Yes |
| Recommendation latency | ≤ 3 s | 30 ms (p95) | Yes |
| Page load with 100 concurrent users | ≤ 2 s | ≤ 1.45 s (p95, one instance) | Yes |
| 50 simultaneous reports within 10% of baseline | ≤ +10% | see "System level" | Partly |
| Backend test coverage | ≥ 70% | 95% (57 tests, SQLite and PostgreSQL) | Yes |
| REST API documented in OpenAPI 3.0 | all endpoints | 46 paths, `docs/openapi.json` | Yes |

Not measured: the five-participant usability study for NFR-11 (it needs people), and the
99% availability target of NFR-09 (it needs a deployment observed over time).

## Datasets

| Dataset | Used for | Size | Source |
| --- | --- | --- | --- |
| Eclipse issue report dataset (Platform, JDT, CDT, PDE) | severity, duplicates, assignment | 225,772 reports with full descriptions | Zenodo, doi:10.5281/zenodo.14229936, CC BY-SA 4.0 |
| GitBugs (Firefox, Thunderbird) | duplicates | 24,824 and 15,192 reports with confirmed duplicate links | github.com/av9ash/gitbugs, CC BY 4.0 |
| Eclipse and Mozilla Defect Tracking Dataset (MSR 2013) | assignment on a second tracker | 394,878 Mozilla reports, titles and assignment history only | Lamkanfi, Perez and Demeyer, MSR 2013 |

Two differences from the plan in Section 1.10.1 of the report:

- The Meng et al. [13] duplicate benchmark was not available. GitBugs was used instead: it
  provides maintainer-confirmed duplicates for the same Mozilla products.
- The NLBSE issue-report dataset classifies issues as bug, feature or question. None of the
  three modules predicts that, so it was not used.

## Severity classification (Section 3.6.1)

Eclipse, 195,177 reports after removing `enhancement`; 80/20 stratified split; 10-fold
cross-validation on the training part chose the SVM regularisation (C = 0.1).
File: `severity_eclipse.json`.

| Metric | Value |
| --- | --- |
| Accuracy on the held-out 20% | 0.767 |
| Accuracy of always answering Medium | 0.792 |
| Macro F1 / weighted F1 | 0.373 / 0.731 |
| Members alone: SVM / Random Forest / Naive Bayes | 0.755 / 0.795 / 0.658 |
| Mean inference time per report | 0.17 ms |

| Class | Precision | Recall | Reports in test set |
| --- | --- | --- | --- |
| Low | 0.37 | 0.10 | 2,549 |
| Medium | 0.82 | 0.93 | 30,931 |
| High | 0.31 | 0.10 | 3,716 |
| Critical | 0.27 | 0.35 | 1,840 |

**The 85% target is not met, and the model does not beat the majority-class baseline on
accuracy.** 79% of Eclipse reports carry `normal`, which is the pre-selected value of the
Bugzilla form, so it is both the dominant class and a noisy label. A model tuned for
accuracy alone would answer Medium every time and never flag a critical bug. The deployed
ensemble uses class weighting, so it gives up about 2.5 points of accuracy and in return
finds 35% of critical reports.

Without the `normal` class (`severity_no_default_eclipse.json`, a three-class problem that
several of the papers in Chapter 2 use) accuracy is 0.616 against a baseline of 0.459, and
macro F1 is 0.592. That run is for comparison only; its model is not deployed because the
service needs all four levels.

What this means for the report: the 85–88% of Namdar et al. [14] was obtained on an
industrial dataset with curated labels and does not transfer to open Bugzilla data.

## Duplicate detection (Section 3.6.2)

Queries are reports from the newest 30% of duplicate groups; the corpus is every other
report of that project. The encoder was fine-tuned on 32,954 duplicate pairs from the older
70% of groups only (Eclipse, Firefox, Thunderbird), so it has never seen a test group.
Files: `duplicates_*_base_encoder.json`, `duplicates_*_finetuned.json`.

| Project | Corpus | Queries | BM25 only | + all-MiniLM-L6-v2 | + fine-tuned | MRR (fine-tuned) | Time per query |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Firefox | 24,824 | 709 | 0.487 | 0.616 | **0.645** | 0.544 | 9 ms |
| Thunderbird | 15,192 | 619 | 0.553 | 0.651 | **0.683** | 0.549 | 9 ms |
| Eclipse CDT | 22,371 | 520 | 0.444 | 0.502 | **0.550** | 0.444 | 9 ms |
| Eclipse JDT | 63,266 | 2,489 | 0.380 | 0.421 | **0.480** | 0.384 | 10 ms |
| Eclipse Platform | 122,496 | 4,876 | 0.384 | 0.449 | **0.497** | 0.406 | 13 ms |

The three middle columns are Recall@5. Recall@1, Recall@10 and MAP are in the JSON files.

**The 80% target is not met.** What the measurements show:

- The two-phase design works as intended: semantic re-ranking adds 6 to 13 points over
  keyword matching, and fine-tuning on duplicate pairs adds a further 3 to 6.
- The first phase is the limit. On Eclipse the true duplicate is among the 100 BM25
  candidates for only 63–73% of queries, which no re-ranker can recover.
- Adding embedding-based retrieval to the first phase was tried and raised that ceiling to
  84–93%, but Recall@5 rose by only 2–3 points, so the design was left as specified.
- Latency is far inside the 1.5 s budget even with 122,000 reports.

Before these changes the design exactly as first written (50 candidates, cosine only, base
encoder, raw text) scored 0.549 on Firefox and 0.577 on Thunderbird over all queries.
Removing pasted boilerplate, weighting the title, 100 candidates and blending the two scores
are all now part of `app/ai/duplicates.py`.

## Developer assignment (Section 3.6.3)

Rolling chronological evaluation over ten slices, as in DeepTriage [12]: train on the 20,000
fixed bugs before each slice, test on the slice. File: `assignment_eclipse_mozilla.json`.

| | Eclipse (89,354 fixed bugs) | | | Mozilla (69,549 fixed bugs) | | |
| --- | --- | --- | --- | --- | --- | --- |
| Method | Top-1 | Top-5 | MNR | Top-1 | Top-5 | MNR |
| CNN-LSTM | 0.195 | 0.464 | 17.8 | 0.110 | 0.234 | 63.7 |
| TF-IDF + linear SVM | 0.279 | 0.562 | 16.1 | 0.185 | 0.350 | 61.4 |
| Most active developers first | 0.043 | 0.160 | 33.9 | 0.043 | 0.124 | 77.0 |
| Random | 0.009 | 0.045 | 57.6 | 0.004 | 0.021 | 130.7 |

Candidate developers per slice averaged 117 on Eclipse and 260 on Mozilla. MNR is the mean
number of developers tried before the one who fixed the bug.

- **The target is met**: the CNN-LSTM reduces MNR by 69% against random on Eclipse and 51%
  on Mozilla (47% and 17% against the stronger most-active-first baseline).
- **A simpler model does better.** TF-IDF with a linear SVM beats the CNN-LSTM on every
  metric on both trackers. The report's Table 6 puts SVM baselines at 60–70% precision and
  CNN-LSTM at 78–85%; neither figure is reproduced here, and the ordering is reversed.
- Coverage: 84% of Eclipse test bugs and 68% of Mozilla test bugs were fixed by a developer
  with at least ten fixes in the training window. The rest cannot be ranked by any method
  and are excluded from the figures above.
- The Mozilla data has titles only, which partly explains its lower scores.
- A single old/new split is misleading on this data. Over twenty years the team changes
  completely, and every method scores near zero. That is why the rolling protocol is used,
  and it matches how the deployed system retrains (FR-29).

## System level (Section 3.6.4)

PostgreSQL 17 and the backend on one Windows 11 machine (RTX 3060), about 1,800 bugs in the
project. Files: `system_latency_1_instance.json`, `system_latency_4_instances.json`,
`sustained_*.csv`.

| Measurement (median / p95, ms) | 1 instance | 4 instances |
| --- | --- | --- |
| Duplicate check, one at a time | 23 / 34 | 23 / 77 |
| Submission (duplicate + severity), one at a time | 68 / 79 | 72 / 88 |
| Recommendation | 26 / 30 | 33 / 79 |
| 50 reports submitted at the same instant | 1,038 / 1,568 | 555 / 928 |
| Bug list, 100 concurrent users | 606 / 882 | 306 / 485 |
| Dashboard, 100 concurrent users | 907 / 1,449 | 553 / 741 |
| Keyword search, 100 concurrent users | 354 / 550 | 425 / 539 |

- **NFR-01 and NFR-02 are met**, including under load with four instances. With one instance
  the 50-report burst has a p95 of 1.57 s, just over the 1.5 s budget.
- **NFR-03 depends on how it is read.** With 50 users working continuously (1–4 s between
  actions, 45 s, one instance) there were no failures in 1,150 requests and the median
  response was 16 ms, against 19 ms for a single user. With 50 reports released at exactly
  the same instant, the median is 8 to 15 times the single-request time, because the
  requests queue for the CPU; adding instances halves it.
- **NFR-08**: the four-instance figures come from four independent processes sharing only
  PostgreSQL, with requests spread round-robin, and no errors.
- The latency measurements used the base encoder and keyword-rule severity. The trained
  models add little: 0.17 ms per report for severity, and the same encoder size for duplicates.

Load testing found and fixed four defects that the unit tests could not: a race when two
requests stored the same embedding, every simultaneous submission rebuilding the search
index, and two ways a burst could exhaust the database connection pool.

## Security (NFR-04 to NFR-07)

Covered by automated tests in `backend/tests/`: bcrypt password storage, idle session expiry,
403 for non-members with no difference between "forbidden" and "does not exist", input
validation, SQL and markup stored inertly, upload type and content checks, path traversal in
file names. The webview inserts all user text as text nodes under a strict content security
policy. Not done: a third-party penetration test, and Snyk scanning (the CI job is present
and runs once a `SNYK_TOKEN` secret is added to the repository).
