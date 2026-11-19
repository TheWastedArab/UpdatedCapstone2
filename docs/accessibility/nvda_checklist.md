# Screen-reader test checklist (NFR-12)

The automated audit (`extension/scripts/a11y-audit.js`, results in
`docs/evaluation/accessibility.json`) found no WCAG 2.1 A or AA violations and showed that every
control is reachable by keyboard. Automated rules cannot tell whether the pages *make sense*
when heard instead of seen. This checklist is that manual test, done with NVDA on Windows.

## Setup

- Windows 10 or 11, **NVDA 2024.1 or later** (free, nvaccess.org), default settings,
  speech viewer on (NVDA menu → Tools → Speech viewer) so what is announced can be copied.
- VS Code (record the version) with the extension installed, the backend running and seeded
  (`python -m scripts.seed`). Turn on VS Code's screen-reader mode:
  `"editor.accessibilitySupport": "on"`.
- Test in the default Dark Modern theme, then repeat sections B and D in a High Contrast theme.
- Do not use the mouse at any point. Useful NVDA keys: `Insert+Space` toggles browse/focus
  mode, `H` / `Shift+H` next/previous heading, `Insert+F7` elements list, `Insert+Tab` says the
  focused element, `Tab` / `Shift+Tab` move between controls.

## How to record

For each item write **Pass**, **Fail** or **N/A** in the Result column, and for every Fail paste
what NVDA said from the speech viewer into Notes. An item passes only if it works by
keyboard alone *and* what is announced is enough to act on without seeing the screen.
WCAG references are the success criteria the item checks.

## A. Opening the extension and signing in

| # | Check | WCAG | Result | Notes |
|---|---|---|---|---|
| A1 | `Ctrl+Shift+P` → "Open Bug Tracker" opens the panel; NVDA announces the panel and its heading "Sign in to Bug Tracker". | 2.4.2, 4.1.2 | | |
| A2 | Tab reaches Email address, Password and the Sign in button, and each is announced with its label and type ("Email address, edit"). | 1.3.1, 4.1.2 | | |
| A3 | Submitting with an empty email announces the error message, and the field is reported as invalid. | 3.3.1, 4.1.3 | | |
| A4 | A wrong password announces the server's message ("Invalid email or password") without moving focus away. | 3.3.1, 4.1.3 | | |
| A5 | "Create an account" link is reachable and the registration form's heading is announced after it is activated. | 2.4.3 | | |
| A6 | On the verify screen, the hint "The 8-character code sent to your email address" is read together with the code field. | 3.3.2 | | |

## B. Bug list and search (FR-07)

| # | Check | WCAG | Result | Notes |
|---|---|---|---|---|
| B1 | After sign-in, NVDA announces the heading "Bugs in Demo Shop". | 2.4.6 | | |
| B2 | The main navigation is announced as a navigation landmark ("Main"), and the current page is announced as "current page". | 1.3.1, 4.1.2 | | |
| B3 | The unread-notifications badge is read as "N unread", not just a number. | 1.1.1 | | |
| B4 | The search form is found as "Search and filter"; every filter (status, severity, assignee, dates) has a spoken label. | 1.3.1, 3.3.2 | | |
| B5 | After a search, "N bugs found." is announced automatically without moving focus. | 4.1.3 | | |
| B6 | The date fields and their calendar buttons can be operated by keyboard and their value is read back. | 2.1.1 | | |
| B7 | In browse mode, each bug in the list is read with its id, title, status and severity in a sensible order. | 1.3.2 | | |
| B8 | Opening a bug moves focus to its title heading ("#12 …"), so the user knows the page changed. | 2.4.3 | | |

## C. Reporting a bug (FR-05, FR-10, FR-11)

| # | Check | WCAG | Result | Notes |
|---|---|---|---|---|
| C1 | "Report a Bug" opens the form and focus lands on its heading. | 2.4.3 | | |
| C2 | Title, Description, Steps to reproduce, Component and the attachment picker are each announced with their label; required fields are announced as required. | 1.3.1, 3.3.2 | | |
| C3 | While typing a title, similar existing bugs are announced politely ("Similar existing bugs" region) without interrupting typing. | 4.1.3 | | |
| C4 | Each suggested duplicate can be reached and opened by keyboard. | 2.1.1 | | |
| C5 | Submitting with an empty title announces the error and moves focus to the first invalid field. | 3.3.1 | | |
| C6 | After submission the new bug's page is announced, including the severity the classifier assigned and its confidence ("3 of 4 classifiers agreed"). | 4.1.3 | | |

## D. Bug detail, comments and watching (FR-06, FR-09, FR-12, FR-14)

| # | Check | WCAG | Result | Notes |
|---|---|---|---|---|
| D1 | Headings (Description, Steps to reproduce, Attachments, Comments, History, Assignment) are listed by `Insert+F7` and `H` moves between them. | 1.3.1, 2.4.6 | | |
| D2 | The status selector announces its current value, and a change is confirmed by a spoken status message. | 4.1.2, 4.1.3 | | |
| D3 | Adding a comment: the comment box has a label, and after posting, the new comment can be read in the list. | 3.3.2 | | |
| D4 | The Watch toggle announces whether it is on or off, and the change is announced. | 4.1.2 | | |
| D5 | Recommended developers (Project Manager account) are read with their rank and reason, and "Assign" buttons say whom they assign. | 2.4.6, 2.5.3 | | |
| D6 | An attachment link announces the file name; an image attachment has a text alternative. | 1.1.1 | | |
| D7 | The history list reads who changed what and when, in order. | 1.3.2 | | |

## E. Notifications, dashboard, team and administration

| # | Check | WCAG | Result | Notes |
|---|---|---|---|---|
| E1 | Notifications page: each item is read with its text and whether it is unread; "Mark all as read" is reachable and its effect is announced. | 4.1.2, 4.1.3 | | |
| E2 | Dashboard: the weekly chart is announced as an image with a text summary of the numbers ("Bugs reported and resolved per week. …"). | 1.1.1 | | |
| E3 | Dashboard tables (time to resolve by severity) are read as tables with column headers (`Ctrl+Alt+arrows`). | 1.3.1 | | |
| E4 | Projects and team: each member's role selector is announced as "Role of <name>". | 2.4.6, 4.1.2 | | |
| E5 | Administration (Administrator account): the section buttons announce which one is pressed, and the user role selectors are announced as "System role of <name>". | 4.1.2 | | |
| E6 | Toast messages ("Member added.", "Role updated.") are spoken once, without stealing focus. | 4.1.3 | | |

## F. General

| # | Check | WCAG | Result | Notes |
|---|---|---|---|---|
| F1 | Focus is never lost to the top of the document after an action (after save, delete, refresh). | 2.4.3 | | |
| F2 | No keyboard trap: `Ctrl+Shift+E` or `Ctrl+1` always leaves the panel. | 2.1.2 | | |
| F3 | Session expiry: after 30 minutes idle, the sign-in screen is announced, not a silent blank page. | 2.2.1, 4.1.3 | | |
| F4 | The "Bugs" tree view in the side bar is readable with arrow keys; each item reads its title and status. | 4.1.2 | | |
| F5 | Error from an unreachable server ("Retry") is announced as an alert, and Retry is reachable. | 4.1.3 | | |

## Summary

| Section | Pass | Fail | N/A |
|---|---|---|---|
| A. Sign-in | | | |
| B. Bug list | | | |
| C. Report | | | |
| D. Detail | | | |
| E. Other pages | | | |
| F. General | | | |

Tester: ____________ NVDA version: ________ VS Code version: ________ Date: __________

Each Fail becomes a bug report in the tracker itself with severity High if it blocks a task,
otherwise Medium. NFR-12 can be reported as met when A–F have no Fail that blocks a task.
