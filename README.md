# AI-Powered Bug Tracking System

A bug tracker that lives inside Visual Studio Code. Reporters file bugs from the editor, and three
AI modules triage each report as it arrives:

- **Severity classification** – TF-IDF features into an ensemble of a linear SVM, a Random Forest and
  Naive Bayes (Low / Medium / High / Critical).
- **Duplicate detection** – two phases: BM25 keyword retrieval of 100 candidates, then re-ranking with
  Sentence-BERT (`all-MiniLM-L6-v2`, optionally fine-tuned on duplicate pairs). Shown live while the
  report is being typed.
- **Developer recommendation** – a CNN-LSTM over the report text plus severity and duplicate features,
  with a rule-based cold-start mode until a project has enough resolved history (FR-25 to FR-29).

Around them: projects and role-based access control, developer profiles, comments and attachments,
watchers, email / in-app / ntfy push notifications, a full audit trail and an OpenAPI 3.0 REST API.

| Part | Technology | Folder |
| --- | --- | --- |
| Client | VS Code extension, TypeScript, webview | `extension/` |
| Server | Python 3.11, FastAPI, SQLAlchemy 2, Alembic | `backend/app/` |
| Database | PostgreSQL 17 | `backend/alembic/` |
| AI | scikit-learn, sentence-transformers, PyTorch | `backend/app/ai/`, `backend/ml/` |
| Deployment | Docker Compose, Caddy (TLS), ntfy | `docker-compose.yml` |

Measured results against every acceptance criterion, including the ones not met, are in
[docs/EVALUATION.md](docs/EVALUATION.md). The API reference is [docs/openapi.json](docs/openapi.json)
and, while the server runs, http://localhost:8000/api/v1/docs.

## Repository layout

```
backend/
  app/            the API server: routers, services, models, AI modules
  alembic/        database migrations
  ml/             dataset preparation, training and evaluation scripts
  scripts/        seed data, smoke test, load tests
  tests/          pytest suite (57 tests, 95% coverage)
extension/        the VS Code extension
docs/             OpenAPI document, evaluation results and logs
data/raw/         datasets (not committed, see "Datasets")
models/           trained models (not committed, see "Training")
```

## Running the project locally

### 1. Install the prerequisites

- Python 3.11
- PostgreSQL 17 (on Windows: `winget install PostgreSQL.PostgreSQL.17`)
- Node.js 20 or newer
- Visual Studio Code 1.85 or newer
- Git

### 2. Create the database

Open `psql` as the `postgres` superuser (on Windows: `"C:\Program Files\PostgreSQL\17\bin\psql.exe" -U postgres`)
and run, choosing your own password:

```sql
CREATE ROLE bugtracker LOGIN PASSWORD 'choose-a-password';
CREATE DATABASE bugtracker OWNER bugtracker;
```

### 3. Set up the backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

For an NVIDIA GPU, install the CUDA build of PyTorch first (`pip install torch --index-url https://download.pytorch.org/whl/cu124`).
The CPU build is enough to run the server.

Copy `backend/.env.example` to `backend/.env` and set at least:

- `BT_DATABASE_URL` – with the password from step 2
- `BT_SECRET_KEY` – generate one with `python -c "import secrets; print(secrets.token_hex(32))"`

Then create the tables and some demo data:

```bash
alembic upgrade head
python -m scripts.seed
```

The seed creates the project "Demo Shop" and accounts such as `admin@demo.example.com`,
`manager@demo.example.com`, `reporter@demo.example.com` and `checkout.dev1@demo.example.com`, all
with the password `Demo!Pass123`.

### 4. Start the server

```bash
uvicorn app.main:app --port 8000
```

Check http://localhost:8000/api/v1/docs. In another terminal, `python -m scripts.smoke` runs an
end-to-end check against the running server. The first start downloads the Sentence-BERT model
(about 90 MB).

### 5. Install the extension

Either build and install the packaged extension:

```bash
cd extension
npm install
npm run package                  # produces ai-bug-tracker-1.0.0.vsix
code --install-extension ai-bug-tracker-1.0.0.vsix
```

or open the `extension` folder in VS Code and press **F5** to run it in a development window.

In VS Code, click the bug icon in the activity bar, choose **Sign in or register**, and sign in with
one of the demo accounts. The server address is the setting `bugTracker.serverUrl`
(default `http://localhost:8000`); any address other than localhost must use https.

### 6. Run the tests

```bash
cd backend
pytest                           # in-memory SQLite, with coverage report
```

To run them against PostgreSQL, create a `bugtracker_test` database and set
`BT_TEST_DATABASE_URL` in `.env`. The extension's integration test runs with `npm run test:host`
in `extension/`.

## Datasets

Without datasets and trained models the system still works: severity falls back to keyword rules,
duplicate detection uses the base encoder, and assignment stays in cold-start mode. To reproduce
the evaluation, put the data under `data/raw/`:

| Dataset | Get it | Prepare it |
| --- | --- | --- |
| Eclipse issue reports (Zenodo, doi:10.5281/zenodo.14229936) | downloaded by the script | `python -m ml.fetch_zenodo_eclipse JDT CDT PDE Platform` |
| GitBugs (Firefox, Thunderbird) | `git clone https://github.com/av9ash/gitbugs ../data/raw/gitbugs/repo` | `python -m ml.prepare_local gitbugs ../data/raw/gitbugs/repo` |
| Eclipse and Mozilla Defect Tracking Dataset (MSR 2013) | `git clone https://github.com/ansymo/msr2013-bug_dataset` | `python -m ml.prepare_local msr2013 PATH/TO/msr2013-bug_dataset` |

All commands run from `backend/` with the virtual environment active. Each writes CSV files in one
common format to `data/raw/<dataset>/`.

## Training and evaluation

```bash
# Severity: trains the deployed model (models/severity.joblib) and writes the report
python -m ml.train_severity --data ../data/raw/zenodo_eclipse --tag eclipse

# Duplicates: fine-tune the encoder on the older 70% of duplicate groups, then evaluate on the rest
python -m ml.train_duplicates --data ../data/raw/zenodo_eclipse ../data/raw/gitbugs/firefox.csv ../data/raw/gitbugs/thunderbird.csv
python -m ml.eval_duplicates --data ../data/raw/gitbugs/firefox.csv --holdout 0.3 --model ../models/sbert-duplicates --tag firefox_finetuned

# Assignment: rolling chronological evaluation on Eclipse, generalisation check on Mozilla
python -m ml.train_assignment --data ../data/raw/zenodo_eclipse --ood ../data/raw/msr2013/mozilla.csv --tag eclipse_mozilla

# System latency and load (server must be running)
python -m scripts.seed --bugs 2000
python -m scripts.loadtest http://127.0.0.1:8000
```

Every script takes `--help`, and the training scripts take `--synthetic` to check the pipeline without data. Results go to `docs/evaluation/`. The server picks up
`models/severity.joblib` and `models/sbert-duplicates/` on its next start. The per-project
assignment model is trained by the server itself once a project has 100 verified resolutions
(`python -m app.cli retrain` forces a sweep).

## Administration

```bash
python -m app.cli create-admin you@example.org "Your Name"     # create or promote an Administrator
python -m app.cli backfill-embeddings                          # embed bugs created before a model change
python -m app.cli export-openapi ../docs/openapi.json          # regenerate the API document
```

Administrators can change the duplicate threshold, severity labels and cold-start threshold at
runtime from the extension or through `/api/v1/admin/settings`.

## Deployment with Docker

```bash
cp deploy.env.example deploy.env      # set DOMAIN, POSTGRES_PASSWORD and SECRET_KEY
docker compose --env-file deploy.env up -d --build
docker compose --env-file deploy.env up -d --scale backend=3
```

This runs PostgreSQL, the backend, a self-hosted ntfy server and Caddy, which obtains a TLS
certificate for `DOMAIN` and only accepts TLS 1.2 or newer. Trained models are read from `./models`.

## Continuous integration

`.github/workflows/ci.yml` runs ruff, the test suite on SQLite and on PostgreSQL, checks that
`docs/openapi.json` matches the code, builds the extension, and runs a Snyk scan when a
`SNYK_TOKEN` secret is set. `.coderabbit.yaml` configures automated review of pull requests.

## Licence

The extension is MIT licensed. The datasets keep their own licences (CC BY-SA 4.0 for the Eclipse
Zenodo data, CC BY 4.0 for GitBugs).
