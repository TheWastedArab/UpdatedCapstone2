# Evaluation results

Measured results for the acceptance criteria of Sections 1.7 and 3.6 of the report.
Every number here comes from a JSON file in `docs/evaluation/`, produced by the scripts in
`backend/ml/` and `backend/scripts/`; the commands are in the README. Where a target was not
met, that is stated, with the measured value.

## Summary

| Criterion | Target | Measured | Met |
| --- | --- | --- | --- |
| Severity classification accuracy | ≥ 85% | 77.4% (four classes, Eclipse) | **No** |
| Duplicate detection Recall@5 | ≥ 80% | 64% Firefox, 68% Thunderbird, 48–55% Eclipse | **No** |
| Reassignment reduction vs random | ≥ 20% | 77% Eclipse, 62% Mozilla | Yes |
| Duplicate check and submission latency | ≤ 1.5 s | 34 ms and 79 ms (p95, one request at a time) | Yes |
| Recommendation latency | ≤ 3 s | 30 ms (p95) | Yes |
| Page load with 100 concurrent users | ≤ 2 s | ≤ 1.45 s (p95, one instance) | Yes |
| Search and filter over 50,000 bugs | ≤ 2 s | 0.28 s (worst p95, 20 users at once) | Yes |
| TLS 1.2 or later only (NFR-05) | TLS ≥ 1.2 | 1.0 and 1.1 refused, 1.2 and 1.3 accepted | Yes |
| Accessibility (NFR-12) | WCAG 2.1 AA | 0 axe violations on 8 screens × 3 themes; 48/48 controls reachable by keyboard | Partly |
| 50 simultaneous reports within 10% of baseline | ≤ +10% | see "System level" | Partly |
| Backend test coverage | ≥ 70% | 94% (57 tests, SQLite and PostgreSQL) | Yes |
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
File: `severity_eclipse.json`, log in `logs/severity.txt`.

The deployed model has four classifiers: the linear SVM, Random Forest and Complement Naive
Bayes over TF-IDF features, and all-MiniLM-L6-v2 fine-tuned for three epochs with a linear
output layer. They are fitted on 80% of the training part. On the other 20%, a logistic
regression learns how to combine their scores (on one half), and per-class offsets are
chosen (on the other half) to give the highest macro F1 that keeps accuracy and Critical
recall at least as high as the plain three-classifier vote's on that half.

| Metric (held-out 20%, 39,036 reports) | Deployed: four classifiers, learned combination | Three-classifier vote |
| --- | --- | --- |
| Accuracy | **0.774** | 0.769 |
| Accuracy of always answering Medium | 0.792 | 0.792 |
| Macro F1 / weighted F1 | **0.430** / 0.749 | 0.365 / 0.729 |
| Members alone: SVM / Random Forest / Naive Bayes / encoder | 0.757 / 0.795 / 0.664 / 0.712 | |
| Time per report, one at a time on the server | 35 ms | |

| Class | Precision | Recall | Recall of the three-classifier vote | Reports in test set |
| --- | --- | --- | --- | --- |
| Low | 0.46 | **0.18** | 0.08 | 2,549 |
| Medium | 0.84 | 0.92 | 0.93 | 30,931 |
| High | 0.34 | **0.18** | 0.10 | 3,716 |
| Critical | 0.33 | **0.38** | 0.34 | 1,840 |

The model trained on 17 November, the three-classifier vote fitted on the whole training
part, scored 0.767 accuracy and 0.373 macro F1 (Critical recall 0.35).

**The 85% target is not met, and the model does not beat the majority-class baseline on
accuracy.** 79% of Eclipse reports carry `normal`, which is the pre-selected value of the
Bugzilla form, so it is both the dominant class and a noisy label. A model tuned for
accuracy alone would answer Medium every time and never flag a critical bug. The deployed
model gives up 1.9 points of accuracy and in return finds 38% of critical reports and
roughly twice as many Low and High reports as the vote did. The learned combination improved every
class except Medium, whose recall fell by 1.3 points.

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
| **Deployed: CNN-LSTM + TF-IDF blend** | 0.266 | 0.560 | **13.3** | 0.164 | 0.324 | **49.5** |
| CNN-LSTM alone | 0.195 | 0.464 | 17.8 | 0.110 | 0.234 | 63.7 |
| TF-IDF + linear SVM | 0.279 | 0.562 | 16.1 | 0.185 | 0.350 | 61.4 |
| Most active developers first | 0.043 | 0.160 | 33.9 | 0.043 | 0.124 | 77.0 |
| Random | 0.009 | 0.045 | 57.6 | 0.004 | 0.021 | 130.7 |

Candidate developers per slice averaged 117 on Eclipse and 260 on Mozilla. MNR is the mean
number of developers tried before the one who fixed the bug.

- **The deployed model** blends the CNN-LSTM's probabilities with those of a TF-IDF and
  linear SVM ranker trained on the same window (`app/ai/cnn_lstm.py`). The blend weight is
  chosen per training run from {0, 0.25, 0.5, 0.75} by mean rank on a validation split taken
  from the end of the window; it chose 0.5 or 0.75 in every slice.
- **The target is met**: the blend reduces MNR by 77% against random on Eclipse and 62% on
  Mozilla (61% and 36% against most-active-first). The CNN-LSTM alone reached 69% and 51%.
- **The two models are complementary.** Neither alone reaches the blend's mean rank: on
  Eclipse the SVM has the better top-1 (0.279 against 0.266), but it places the remaining
  developers worse, so the blend's MNR is 2.8 lower than the SVM's. On Mozilla the gap is 11.9.
- The report's Table 6 puts SVM baselines at 60–70% precision and CNN-LSTM at 78–85%;
  neither figure is reproduced here, and on its own the CNN-LSTM is the weaker model.
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
- The latency measurements above used the base encoder and keyword-rule severity. The
  cost of the trained models was measured afterwards, back to back against that
  configuration on fresh 2,000-bug databases (`system_latency_models_ab.json`). The machine
  was busier then, so both configurations were slower than in the table; the difference
  is what matters. One instance: a single submission +42 ms at the median (130 → 172 ms),
  the 50-report burst +0.07 s at the median and +0.84 s at p95. Four instances: no
  measurable difference in the burst (p95 1.82 s → 1.66 s, within run-to-run noise).
  Pages are unaffected. A severity prediction costs 35 ms for one report, most of it the
  Random Forest; concurrent reports are therefore scored together in one batch
  (`app/ai/severity.py`, `Coalescer`), without it, a first run on the (larger) development
  database gave a burst p95 of 4.6 s.

Load testing found and fixed four defects that the unit tests could not: a race when two
requests stored the same embedding, every simultaneous submission rebuilding the search
index, and two ways a burst could exhaust the database connection pool.

## Search at volume (FR-07)

`backend/scripts/search_volume.py` runs against a project seeded with 50,000 bugs
(`scripts.seed --bugs 50000`, PostgreSQL 17) and times six searches (keyword, rare term, keyword with filters, date range, filters only, first
page unfiltered), 30 times by one user and once by 20 users at the same instant. File:
`search_50k.json`. One user: p95 between 14 and 44 ms. Twenty at once: p95 between 144 and
278 ms. The target of 2 s is met with a wide margin.

## Transport security (NFR-05)

The repository's Caddyfile was run with Caddy 2.11.9 in front of the backend and probed with
OpenSSL 3.5.5 (`tls_check.json`). TLS 1.0 and 1.1 are refused with a protocol-version alert,
TLS 1.2 and 1.3 are accepted with AES-GCM suites, and plain HTTP is redirected to HTTPS
(308). The certificate came from Caddy's internal authority because no public domain was
available; a Let's Encrypt certificate needs one.

## Accessibility (NFR-12)

`extension/scripts/a11y-audit.js` runs axe-core 4 (WCAG 2.1 A and AA rules) with Playwright
on the eight webview screens, under the light, Dark Modern and High Contrast theme
variables VS Code supplies, and then presses Tab through the bug list in each theme. File:
`accessibility.json`. Result: no violations on any screen in any theme. Axe could not compute
colour contrast automatically on three screens (report form, dashboard, bug detail), where
text sits over a chart or a layered background, and left it for manual review. All 48 interactive controls on it are reached by Tab and all show a visible
focus outline. The audit found one defect, now fixed: the date field's calendar button took
focus without an outline. Not done: a test with a screen reader, and the usability study.

## Security (NFR-04 to NFR-07)

Covered by automated tests in `backend/tests/`: bcrypt password storage, idle session expiry,
403 for non-members with no difference between "forbidden" and "does not exist", input
validation, SQL and markup stored inertly, upload type and content checks, path traversal in
file names. The webview inserts all user text as text nodes under a strict content security
policy. Not done: a third-party penetration test, and Snyk scanning (the CI job is present
and runs once a `SNYK_TOKEN` secret is added to the repository).
