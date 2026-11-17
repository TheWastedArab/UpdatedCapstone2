# AI Bug Tracker for VS Code

Report, triage and track bugs without leaving the editor.

- **Duplicate check while you type**: similar existing bugs appear, with a match percentage, before you submit.
- **Automatic severity** on submission, which developers and managers can override.
- **Developer recommendations** with the reason for each; the Project Manager decides.
- Bug list with search and filters, status lifecycle, comments, attachments, audit trail, project dashboard and notifications.

## Setup

1. Start the backend (see the repository README).
2. Set **Bug Tracker: Server Url** in VS Code settings. The default is `http://127.0.0.1:8000`. Anything other than a server on your own machine must use `https`.
3. Open the **Bug Tracker** view in the activity bar and sign in or register.

## Commands

| Command | Shortcut |
| --- | --- |
| Bug Tracker: Report a Bug | `Ctrl+Alt+B` (`Cmd+Alt+B` on macOS) |
| Bug Tracker: Open Bug Tracker | |
| Bug Tracker: Open Project Dashboard | |
| Bug Tracker: Show Notifications | click the bell in the status bar |
| Bug Tracker: Select Project | |
| Bug Tracker: Sign Out | |

## Privacy

Only the text you type into a bug report and the files you explicitly attach are sent to the server. The extension never reads or uploads source files. The session token is kept in the operating system keychain through VS Code SecretStorage.

## Development

```bash
npm install
```

```bash
npm run compile
```

Press `F5` in VS Code to launch an Extension Development Host. The interface can also be opened in a browser against a running backend with `python -m scripts.webview_harness` (from `backend/`), then `http://127.0.0.1:8000/harness`.
