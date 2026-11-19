# Usability study kit (NFR-11)

NFR-11 says a new user must be able to file their first bug report **within 5 minutes of
registering**, without reading any outside documentation. The Capstone 1 plan (Section 3.6)
asks for a task-completion study with **five participants** (developers and bug reporters)
that measures the time to the first submission. This folder holds what is needed to run it.

| File | What it is |
|---|---|
| `README.md` | This test plan: setup, procedure, measures and pass criteria |
| `consent.md` | Information sheet and consent form, one copy per participant |
| `tasks.md` | The five task cards, read aloud or handed to the participant one at a time |
| `sus.md` | The System Usability Scale questionnaire (10 items), filled in after the tasks |
| `timing_sheet.csv` | One row per participant and task, filled in by the facilitator |
| `sus_responses.csv` | One row per participant with the ten SUS answers |
| `add_participant.py` | Facilitator helper: files the Task 3 bug, adds a new participant to the project, changes a status for Task 5 |
| `analyze.py` | Reads the two CSV files and prints the results and the NFR-11 verdict |

## Participants

Five people who have not used this system before: at least two who write code
(developer role) and at least two who would mainly report bugs (tester, student, support).
Everyone must have used VS Code at least once. Record only a participant code (P1–P5), the
group (developer or reporter) and years of VS Code use; no names in the data files.

## Setup (once, before the sessions)

1. Start the backend with `docker compose up` (or `uvicorn app.main:app` from `backend/`)
   and seed it with `python scripts/seed.py`. In development mode the email verification
   code is filled in for the user, so no mail server is needed.
2. Install the extension from the `.vsix` (`npm run package` in `extension/`) into a clean
   VS Code profile: `code --profile "Usability" --install-extension ai-bug-tracker-1.0.0.vsix`.
3. Check that the seeded project "Demo Shop" exists and that the Project Manager account can sign in.
4. Prepare a screen recorder (optional, only with consent) and a stopwatch.

Before each session run `python docs/usability/add_participant.py --setup`, which files the
open "login" bug that Task 3 asks for (it does nothing if an open copy already exists). The
script signs in as the seeded project manager; set `BT_URL` if the server is not on
`http://127.0.0.1:8000`.

After each session, sign the participant out (Command Palette → "Bug Tracker: Sign Out")
and check that the panel shows the sign-in screen. The extension keeps the session between
VS Code restarts, so without this the next participant would start already signed in and
would never see "Create an account".

## Procedure (about 25 minutes per participant)

1. **Welcome (2 min).** Read the information sheet, collect the signed consent form.
   Say: "We are testing the software, not you. Please think aloud. I cannot help you with
   the tasks, but you may give up on any task at any time."
2. **Background (1 min).** Note group and VS Code experience on the timing sheet.
3. **Tasks (about 15 min).** Hand over the task cards one at a time (`tasks.md`).
   Do not answer questions about the interface; you may only repeat the task text, and
   each repetition counts as assistance (completion 0.5 for that task).
   - **Before handing over Task 1**, start
     `python docs/usability/add_participant.py p1@study.example.com` in a terminal the
     participant cannot see (use `p2@study.example.com` for P2, and so on). It waits for that account to be created and adds it to the
     "Demo Shop" project as a Reporter within a second, standing in for the project manager
     who would invite a new user. By the time the participant has verified the email and
     signed in, the project is there; nothing is said to them about it.
   - **Tasks 1 and 2 are timed as one continuous run.** Start the stopwatch when the
     participant has read the Task 1 card and says "ready". When Task 1's success condition
     is met, take a lap time and hand over the Task 2 card at once, without stopping the
     stopwatch. Stop it when the report is submitted, the participant gives up, or the
     limit is reached. The clock is never paused, so Task 1 + Task 2 is the real time
     from starting registration to the first report, including reading the second card.
   - Tasks 3 to 5 are timed separately: start when the participant says "ready", stop at
     the success condition, when they give up, or at the time limit.
   - Before Task 5 run `python docs/usability/add_participant.py --status`, which moves the
     Task 3 bug (now watched by the participant) to In Progress, so a notification is waiting.
4. **SUS (3 min).** The participant fills in `sus.md` on paper or screen right after Task 5.
5. **Debrief (4 min).** Ask: "What was the hardest part?", "Was anything surprising?",
   "Would you use this instead of a web bug tracker?" Write short notes.

## Measures

| Measure | How | Recorded in |
|---|---|---|
| **Time to first report** (NFR-11) | Task 1 lap + Task 2 lap of the one continuous run (no pauses) | `timing_sheet.csv` |
| Task completion | 1 = done unaided, 0.5 = done after any assistance (including a repeated instruction), 0 = gave up or over the limit | `timing_sheet.csv` |
| Errors | Wrong screen opened, wrong field filled, error message shown | `timing_sheet.csv` |
| Satisfaction | SUS score 0–100 | `sus_responses.csv` |
| Comments | Think-aloud and debrief notes | `notes` column |

## Pass criteria

- **NFR-11 is met** when **at least 4 of the 5** participants file their first report within
  5 minutes of starting registration (Task 1 + Task 2 ≤ 300 s), with both tasks completed
  unaided (completion 1). `seconds` in `timing_sheet.csv` is the elapsed stopwatch time of
  the task, with nothing subtracted.
  With five participants the result is an indication, not a statistical proof; the report
  should say so.
- Secondary: mean task completion ≥ 80%, and a mean SUS score ≥ 68 (the commonly cited
  average across published studies, Bangor et al. 2008).

## Analysis

```
python docs/usability/analyze.py docs/usability/timing_sheet.csv docs/usability/sus_responses.csv
```

The script prints per-task completion and median time, each participant's time to first
report, the NFR-11 verdict, and the SUS scores, and writes `docs/evaluation/usability.json`.
The two CSV files ship with example rows marked `EXAMPLE`; delete them before the first
session (the script refuses to report while they are present).
