# Evaluation results

Measured results for the acceptance criteria of Sections 1.7 and 3.6 of the report.
Every number here comes from a JSON file in `docs/evaluation/`, produced by the scripts in
`backend/ml/` and `backend/scripts/`; the commands are in the README. Where a target was not
met, that is stated, with the measured value.

## Summary

| Criterion | Target | Measured | Met |
| --- | --- | --- | --- |
| Severity classification accuracy | ≥ 85% | 77.4% (four classes, Eclipse) | **No** |
| Duplicate detection Recall@5 | ≥ 80% | 62% Firefox, 63% Thunderbird, 47–57% Eclipse (chronological) | **No** |
| Reassignment reduction vs random | ≥ 20% | 77% Eclipse, 62% Mozilla (simulated from the ranking, MNR); Top-3 0.46 and 0.27 | Yes |
| Duplicate check and submission latency | ≤ 1.5 s | 34 ms and 79 ms (p95, one request at a time); submission 272 ms with the trained models on a busier machine | Yes |
| Recommendation latency | ≤ 3 s | 30 ms (p95) | Yes |
| Page load with 100 concurrent users | ≤ 2 s | ≤ 1.45 s (p95, one instance) | Yes |
| Search and filter over 50,000 bugs | ≤ 2 s | 0.28 s (worst p95, 20 users at once) | Yes |
| TLS 1.2 or later only (NFR-05) | TLS ≥ 1.2 | 1.0 and 1.1 refused, 1.2 and 1.3 accepted | Yes |
| Accessibility (NFR-12) | WCAG 2.1 AA | 0 axe violations on 8 screens × 3 themes; 48/48 controls reachable by keyboard | Partly |
| Security scans (NFR-07) | no high or medium findings | ZAP: 0 high, 0 medium, 2 low (by design); dependencies: 0 known vulnerabilities after upgrades; Snyk: 0 open dependency issues (9 ignored with reasons), Snyk Code 18 → 3 reviewed false positives | Yes |
| 50 simultaneous reports within 10% of baseline | ≤ +10% | see "System level" | Partly |
| Backend test coverage | ≥ 70% | 94% (59 tests, SQLite and PostgreSQL) | Yes |
| REST API documented in OpenAPI 3.0 | all endpoints | 46 paths, `docs/openapi.json` | Yes |

Not measured: the five-participant usability study for NFR-11 (it needs people; the kit to run
it is in `docs/usability/`), the screen-reader test for NFR-12 (checklist in
`docs/accessibility/nvda_checklist.md`), and the 99% availability target of NFR-09 (it needs a
deployment observed over time).

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

Every classifier on its own, on the same 39,036 test reports (`severity_comparison_eclipse.json`,
written by `python -m ml.compare_severity --data ../data/raw/zenodo_eclipse`):

| Classifier | Accuracy | Macro F1 | Weighted F1 | Recall Low / Medium / High / Critical |
| --- | --- | --- | --- | --- |
| Majority class (always Medium) | 0.792 | 0.221 | 0.701 | 0.00 / 1.00 / 0.00 / 0.00 |
| Linear SVM | 0.757 | 0.391 | 0.732 | 0.16 / 0.91 / 0.14 / 0.33 |
| Random Forest | 0.795 | 0.251 | 0.709 | 0.02 / 1.00 / 0.01 / 0.03 |
| Naive Bayes | 0.664 | 0.350 | 0.679 | 0.14 / 0.78 / 0.20 / 0.40 |
| Fine-tuned encoder | 0.712 | 0.429 | 0.718 | 0.35 / 0.82 / 0.22 / 0.42 |
| Three-classifier vote (design) | 0.769 | 0.365 | 0.729 | 0.08 / 0.93 / 0.10 / 0.34 |
| **Deployed combination** | 0.774 | **0.430** | **0.749** | 0.18 / 0.92 / 0.18 / 0.38 |

Confusion matrix of the deployed model (rows true, columns predicted):

| | Low | Medium | High | Critical |
| --- | --- | --- | --- | --- |
| Low | 453 | 2,044 | 34 | 18 |
| Medium | 526 | 28,384 | 1,036 | 985 |
| High | 12 | 2,620 | 664 | 420 |
| Critical | 2 | 926 | 212 | 700 |

Why the combination: the Random Forest's accuracy comes from answering Medium almost every
time (Critical recall 0.03); the encoder alone matches the combination's macro F1 but is 6.2
points less accurate and less precise in every minority class. Most errors are into Medium;
only 2 Critical reports were called Low and 18 Low reports Critical.

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

What this means for the report: the 85–88% of Namdar et al. (2025) was obtained on an
industrial dataset with curated labels and does not transfer to open Bugzilla data.

## Duplicate detection (Section 3.6.2)

Duplicate groups are ordered by the id of their first report and the newest 30% held out;
the encoder was fine-tuned on 32,954 duplicate pairs from the older 70% of groups only
(Eclipse, Firefox, Thunderbird). Because the encoder was fine-tuned on the four Eclipse
projects together, the per-project held-out groups that fall in the training part of that
combined split (69 in JDT, 214 in Platform) are left out with `--fine-tuned-on`.

**The evaluation is chronological**, as the live check is: reports are ordered by filing
date (id breaks ties), a held-out report is a query only if a member of its group was filed
before it, it is searched only against earlier reports, and only those earlier members
count as correct. The first protocol searched the whole project, so a query could be
credited for a duplicate filed after it; those results are kept as
`duplicates_*_including_later.json` (run with `--include-later` and a tag ending in
`_including_later`, e.g. `--tag jdt_finetuned_including_later`, so the chronological file
is not overwritten).
Files: `duplicates_*_base_encoder.json`, `duplicates_*_finetuned.json`.

| Project | Corpus | Queries | BM25 only | + all-MiniLM-L6-v2 | + fine-tuned | MRR (fine-tuned) | First protocol, fine-tuned | Time per query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Firefox | 24,824 | 709 | 0.447 | 0.588 | **0.618** | 0.516 | 0.645 | 9 ms |
| Thunderbird | 15,192 | 619 | 0.490 | 0.588 | **0.632** | 0.512 | 0.683 | 8 ms |
| Eclipse CDT | 22,371 | 520 | 0.450 | 0.508 | **0.573** | 0.459 | 0.550 | 9 ms |
| Eclipse JDT | 63,266 | 2,389 | 0.368 | 0.402 | **0.469** | 0.379 | 0.480 | 10 ms |
| Eclipse Platform | 122,496 | 4,475 | 0.365 | 0.432 | **0.478** | 0.394 | 0.497 | 12 ms |

The three middle columns are Recall@5. Recall@1, Recall@10 and MAP are in the JSON files.

**The 80% target is not met.** What the measurements show:

- The two-phase design works as intended: semantic re-ranking adds 3 to 14 points over
  keyword matching, and fine-tuning on duplicate pairs adds a further 3 to 7.
- The first phase is the limit. On Eclipse the true duplicate is among the 100 BM25
  candidates for only 60–72% of queries, which no re-ranker can recover.
- Adding embedding-based retrieval to the first phase was tried (under the first protocol)
  and raised that ceiling to 84–93%, but Recall@5 rose by only 2–3 points, so the design
  was left as specified.
- The chronological protocol lowers Recall@5 by 1 to 5 points on four projects and raises
  it by 2 on CDT, where searching only earlier reports also removes competing later ones.
- Latency is far inside the 1.5 s budget even with 122,000 reports.

Before these changes the design exactly as first written (50 candidates, cosine only, base
encoder, raw text) scored 0.549 on Firefox and 0.577 on Thunderbird over all queries.
Removing pasted boilerplate, weighting the title, 100 candidates and blending the two scores
are all now part of `app/ai/duplicates.py`.

## Developer assignment (Section 3.6.3)

Rolling chronological evaluation over ten slices, as in DeepTriage (Mani et al., 2018): train on the 20,000
fixed bugs before each slice, test on the slice. File: `assignment_eclipse_mozilla.json`.

| | Eclipse (89,354 fixed bugs) | | Mozilla (69,549 fixed bugs) | |
| --- | --- | --- | --- | --- |
| Method | Top-1 / Top-3 / Top-5 | MNR | Top-1 / Top-3 / Top-5 | MNR |
| **Deployed: CNN-LSTM + TF-IDF blend** | 0.266 / **0.460** / 0.560 | **13.3** | 0.164 / 0.267 / 0.324 | **49.5** |
| CNN-LSTM alone | 0.195 / 0.368 / 0.464 | 17.8 | 0.110 / 0.189 / 0.234 | 63.7 |
| TF-IDF + linear SVM | 0.279 / 0.471 / 0.562 | 16.1 | 0.185 / 0.297 / 0.350 | 61.4 |
| Most active developers first | 0.043 / 0.095 / 0.160 | 33.9 | 0.043 / 0.086 / 0.124 | 77.0 |
| Random | 0.009 / 0.027 / 0.045 | 57.6 | 0.004 / 0.013 / 0.021 | 130.7 |

Candidate developers per slice averaged 117 on Eclipse and 260 on Mozilla. MNR is the mean
number of developers tried before the one who fixed the bug. The extension shows three
recommendations, so Top-3 is the figure closest to what a triager sees.

**All reassignment figures are simulated from the ranking, not observed.** MNR assumes the
bug is offered to candidates one at a time in the recommended order. For context only: in
the Mozilla history the assignee field of a fixed bug changed 1.39 times on average (every
bug at least once, because Mozilla bugs start with a default owner). That is not comparable
with MNR, and no observed reduction is claimed.

- **The deployed model** blends the CNN-LSTM's probabilities with those of a TF-IDF and
  linear SVM ranker trained on the same window (`app/ai/cnn_lstm.py`). The blend weight is
  chosen per training run from {0, 0.25, 0.5, 0.75} by mean rank on a validation split taken
  from the end of the window; it chose 0.5 or 0.75 in every slice.
- **The target is met**: the blend reduces MNR by 77% against random on Eclipse and 62% on
  Mozilla (61% and 36% against most-active-first). The CNN-LSTM alone reached 69% and 51%.
- **The two models are complementary.** Neither alone reaches the blend's mean rank: on
  Eclipse the SVM has the better top-1 (0.279 against 0.266), but it places the remaining
  developers worse, so the blend's MNR is 2.8 lower than the SVM's. On Mozilla the gap is 11.9.
- Table 2.3 of the report puts SVM baselines at 60–70% precision and CNN-LSTM at 78–85%;
  neither figure is reproduced here, and on its own the CNN-LSTM is the weaker model.
- Coverage: 84% of Eclipse test bugs and 68% of Mozilla test bugs were fixed by a developer
  with at least ten fixes in the training window. The rest cannot be ranked by any method
  and are excluded from the figures above.
- The Mozilla data has titles only, which partly explains its lower scores.
- A single old/new split is misleading on this data. Over twenty years the team changes
  completely, and every method scores near zero. That is why the rolling protocol is used,
  and it matches how the deployed system retrains (FR-29).

## Integration of the modules (Section 6.5.5 of the report)

Does feeding severity and duplicate detection into assignment help? Two runs of the same
rolling evaluation on Eclipse: with the inputs (`assignment_eclipse_ablation.json`) and
without them (`--no-meta`, `assignment_eclipse_no_meta.json`). Each run also ranks the
reports closed as duplicates in each test slice whose original had already been fixed when
the duplicate was filed (1,029 with a real assignee; 972 by a rankable developer), by the
model alone and with the recommender's duplicate bonus (0.40 x relative probability + 0.25
for the original's fixer). The fix date comes from each bug's activity log
(`ml/fetch_fix_dates.py`, saved in `data/raw/zenodo_eclipse/fix_dates/`).

| Comparison | Top-1 | Top-3 | Top-5 | MNR |
| --- | --- | --- | --- | --- |
| All fixed bugs: deployed model, with severity and duplicate inputs | 0.266 | 0.460 | 0.560 | 13.3 |
| All fixed bugs: deployed model, text only | 0.265 | 0.458 | 0.556 | 13.3 |
| All fixed bugs: CNN-LSTM, with inputs | 0.195 | 0.368 | 0.464 | 17.8 |
| All fixed bugs: CNN-LSTM, text only | 0.194 | 0.364 | 0.459 | 18.0 |
| Duplicate reports: model alone | 0.314 | 0.526 | 0.614 | 9.6 |
| Duplicate reports: model + duplicate bonus | **0.429** | **0.816** | **0.860** | **4.4** |

- The duplicate link is where integration pays: in 67% of the 1,203 Eclipse duplicates whose
  original was already fixed when they were filed, the duplicate was handled by whoever had
  fixed the original.
- A first version checked only that the original was created earlier, not fixed earlier, so it
  also counted originals fixed after the duplicate was filed (Top-3 0.51 to 0.77 on 4,166
  reports). The figures above replace it.
- Not measured: the live bonus also goes to the assignee of an original that is still open.
  The archive does not record who was assigned at each point in time.
- As model inputs, severity and the duplicate flag add little. The flag is never set on a
  fixed Eclipse bug (no fixed bug is itself closed as a duplicate), so the model cannot use it.
- Upper bound: the recorded severity is used, not the classifier's prediction. The workload
  penalty cannot be evaluated offline (past workloads were not recorded).

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
  is what matters. One instance: a single submission +42 ms at the median (130 → 172 ms) and +51 ms at p95 (221 → 272 ms),
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
The screen-reader test is written out as a 38-item NVDA checklist mapped to WCAG success
criteria (`docs/accessibility/nvda_checklist.md`). Preparing it found one defect, now fixed:
the bug page computed "N of 3 classifiers agreed" from the confidence, so a 3-of-4 agreement
of the four-classifier model was shown as "2 of 3".

## Security (NFR-04 to NFR-07)

Covered by automated tests in `backend/tests/`: bcrypt password storage, idle session expiry,
403 for non-members with no difference between "forbidden" and "does not exist", input
validation, SQL and markup stored inertly, upload type and content checks, path traversal in
file names. The webview inserts all user text as text nodes under a strict content security
policy.

**Dynamic scan (OWASP ZAP 2.17.0).** Automation-framework run against the API with trained
models and PostgreSQL, all 59 operations imported from `docs/openapi.json`, passive and
active scan with the default policy, signed in as an Administrator (logout, password change
and deactivating the scanning account excluded). File: `zap_scan.json`. Result: no high or
medium alerts. Two low alerts, both by design: "XSS weakness (persistent in JSON response)",
because the API returns submitted text unchanged as `application/json` with `nosniff` and
`no-store`, and the only client renders it as text nodes; and a Unix timestamp, which is the
version tag of the duplicate model. Reading the scan's responses found one real issue that
ZAP does not flag, now fixed: `GET /ai/status` showed every signed-in user the training-set
path on the server (`..\data\raw\zenodo_eclipse`); it now gives only the dataset name.

**Dependency audit.** `pip-audit` over the 108 installed backend packages and `npm audit` over
the extension (`dependency_audit.json`). The backend's only finding was setuptools 65.5.0
(5 advisories), the version bundled with the Python 3.12 image; the Dockerfile now upgrades it.
The extension ships no runtime dependencies; three moderate advisories in the build-time
packager (`@vscode/vsce` via `glob` and `minimatch`) were removed by moving to vsce 4.1.1 with
patched versions pinned through `overrides`. vsce 4 needs Node.js 22, which CI now uses.
After the changes both audits report no known vulnerabilities.

**Snyk (CI, on every pull request).** Three steps in the `snyk` job: Snyk Open Source over the
installed backend dependencies and over the extension's lockfile, and Snyk Code over the source.
Open Source: of the 71 backend dependencies, setuptools 79 (pulled in by torch) had two
advisories, fixed by requiring setuptools 84.1 or later; nine advisories in transformers have no
fixed release. Each of the nine needs a crafted model, checkpoint or remote code to be loaded,
which this service never does (own `models/` directory or the official MiniLM only, no
`trust_remote_code`, `torch.load(weights_only=True)`), so they are ignored with reasons in
`backend/.snyk` until May 2027. The extension had none. Snyk Code first reported 18 issues
(2 high, 5 medium, 11 low), none exploitable:
the two high ones were the attachment download path, built from a name the server generates
(`uuid4().hex`), and the name of the SecretStorage slot for the session token, read as a
hardcoded secret. Changes: downloads now refuse a stored name that is not 32 hex characters, the
development shim and audit script post messages to their own origin instead of `*`, and the
fixed passwords of the test fixtures and of the development tools (`docs/usability`,
`extension/scripts`), which only sign in to a test or seeded demo database, are excluded in the
root `.snyk`. Three findings remain, all reviewed as false positives: the SecretStorage slot name
(`extension/src/api.ts`, high), the output path of `export-openapi`, which the operator passes on
the command line (`backend/app/cli.py`, low), and the webview's message listener, which in VS Code
only receives messages from the extension host (`extension/media/main.js`, low). Snyk Code has
no per-finding ignore, so its step prints them but does not fail the job; the two dependency
scans stay blocking and report 0 open issues.

Not done: a third-party penetration test.
