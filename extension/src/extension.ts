import * as crypto from 'crypto';
import * as vscode from 'vscode';
import { Api, ApiError, Query } from './api';
import { BugTree } from './bugTree';

interface Route {
  view: string;
  id?: number;
}

interface Project {
  id: number;
  name: string;
  archived: boolean;
}

interface Notice {
  id: number;
  message: string;
  bug_id: number | null;
  type: string;
}

const PROJECT_KEY = 'bugTracker.projectId';

class Controller {
  private panel: vscode.WebviewPanel | undefined;
  private pendingRoute: Route | undefined;
  private lastUnread = 0;
  private timer: NodeJS.Timeout | undefined;
  private readonly tree: BugTree;
  private readonly treeView: vscode.TreeView<unknown>;
  private readonly statusBar: vscode.StatusBarItem;

  constructor(private readonly context: vscode.ExtensionContext, private readonly api: Api) {
    this.tree = new BugTree(api, () => this.projectId);
    this.treeView = vscode.window.createTreeView('bugTracker.bugs', { treeDataProvider: this.tree });
    this.statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    this.statusBar.command = 'bugTracker.openNotifications';
    context.subscriptions.push(this.treeView, this.statusBar, api.onDidSignOut(() => void this.sessionChanged()));
  }

  get projectId(): number | undefined {
    return this.context.globalState.get<number>(PROJECT_KEY);
  }

  async start(): Promise<void> {
    await this.sessionChanged();
    this.schedule();
    this.context.subscriptions.push(
      vscode.workspace.onDidChangeConfiguration((event) => {
        if (event.affectsConfiguration('bugTracker')) {
          this.schedule();
          void this.sessionChanged();
        }
      }),
      { dispose: () => this.timer && clearInterval(this.timer) },
    );
  }

  private schedule(): void {
    if (this.timer) {
      clearInterval(this.timer);
    }
    const seconds = Math.max(10, vscode.workspace.getConfiguration('bugTracker').get<number>('pollSeconds', 30));
    this.timer = setInterval(() => void this.poll(), seconds * 1000);
  }

  /** Re-evaluates sign-in state and the selected project, then refreshes every surface. */
  private async sessionChanged(): Promise<void> {
    let hasProject = false;
    if (this.api.signedIn) {
      try {
        const projects = await this.api.request<Project[]>('GET', '/projects');
        if (!projects.some((p) => p.id === this.projectId)) {
          await this.context.globalState.update(PROJECT_KEY, projects[0]?.id);
        }
        hasProject = this.projectId !== undefined;
      } catch {
        // Unreachable server: keep the last project; the panel reports the error.
        hasProject = this.projectId !== undefined;
      }
    }
    await vscode.commands.executeCommand('setContext', 'bugTracker.signedIn', this.api.signedIn);
    await vscode.commands.executeCommand('setContext', 'bugTracker.hasProject', this.api.signedIn && hasProject);
    await this.tree.refresh();
    this.treeView.description = this.tree.truncated ? 'latest 100' : undefined;
    await this.poll();
    this.post({ type: 'session', signedIn: this.api.signedIn, projectId: this.projectId });
  }

  /** Unread badge in the status bar and on the view; a toast for anything new (FR-17). */
  private async poll(): Promise<void> {
    if (!this.api.signedIn) {
      this.statusBar.hide();
      this.treeView.badge = undefined;
      this.lastUnread = 0;
      return;
    }
    try {
      const { unread } = await this.api.request<{ unread: number }>('GET', '/notifications/unread-count');
      this.statusBar.text = `$(bell) ${unread}`;
      this.statusBar.tooltip = unread === 1 ? '1 unread bug tracker notification' : `${unread} unread bug tracker notifications`;
      this.statusBar.accessibilityInformation = { label: this.statusBar.tooltip };
      this.statusBar.show();
      this.treeView.badge = unread > 0 ? { value: unread, tooltip: this.statusBar.tooltip } : undefined;
      if (unread > this.lastUnread) {
        const [newest] = await this.api.request<Notice[]>('GET', '/notifications', { query: { unread_only: true, limit: 1 } });
        if (newest) {
          const show = newest.type === 'critical_bug' ? vscode.window.showWarningMessage : vscode.window.showInformationMessage;
          void show(newest.message, 'Open').then((choice) => {
            if (choice) {
              this.open(newest.bug_id ? { view: 'bug', id: newest.bug_id } : { view: 'notifications' });
            }
          });
        }
        void this.tree.refresh();
        this.post({ type: 'unread', unread });
      }
      this.lastUnread = unread;
    } catch {
      // Transient network failure: try again on the next tick.
    }
  }

  open(route?: Route): void {
    if (this.panel) {
      this.panel.reveal();
      if (route) {
        this.post({ type: 'navigate', route });
      }
      return;
    }
    this.pendingRoute = route;
    const media = vscode.Uri.joinPath(this.context.extensionUri, 'media');
    this.panel = vscode.window.createWebviewPanel('bugTracker', 'Bug Tracker', vscode.ViewColumn.Active, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [media],
    });
    this.panel.iconPath = vscode.Uri.joinPath(media, 'bug.svg');
    const webview = this.panel.webview;
    const nonce = crypto.randomBytes(16).toString('base64');
    webview.html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}'; img-src ${webview.cspSource} data:;">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${webview.asWebviewUri(vscode.Uri.joinPath(media, 'style.css'))}">
<title>Bug Tracker</title>
</head>
<body>
<div id="app"></div>
<script nonce="${nonce}" src="${webview.asWebviewUri(vscode.Uri.joinPath(media, 'main.js'))}"></script>
</body>
</html>`;
    webview.onDidReceiveMessage((message) => void this.handle(message), undefined, this.context.subscriptions);
    this.panel.onDidDispose(() => (this.panel = undefined), undefined, this.context.subscriptions);
  }

  private post(message: unknown): void {
    void this.panel?.webview.postMessage(message);
  }

  /** Bridge between the webview and the backend. Each request is answered with the same id. */
  private async handle(message: { type: string; id?: number; [key: string]: unknown }): Promise<void> {
    const reply = (ok: boolean, data: unknown, status = 0) => this.post({ type: 'response', id: message.id, ok, data, status });
    try {
      switch (message.type) {
        case 'ready':
          this.post({ type: 'init', signedIn: this.api.signedIn, projectId: this.projectId, route: this.pendingRoute, serverUrl: this.safeBaseUrl() });
          this.pendingRoute = undefined;
          return;
        case 'api':
          return reply(true, await this.api.request(String(message.method), String(message.path), { query: message.query as Query, body: message.body }));
        case 'login': {
          const user = await this.api.login(String(message.email), String(message.password));
          await this.sessionChanged();
          return reply(true, user);
        }
        case 'logout':
          await this.api.logout();
          return reply(true, null);
        case 'setProject':
          await this.context.globalState.update(PROJECT_KEY, message.projectId);
          await this.sessionChanged();
          return reply(true, null);
        case 'changed':
          await this.tree.refresh();
          await this.poll();
          return;
        case 'pickFiles': {
          const files = await vscode.window.showOpenDialog({
            canSelectMany: true,
            openLabel: 'Attach',
            filters: { 'Screenshots and logs': ['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'txt', 'log', 'json', 'xml', 'csv', 'md'] },
          });
          return reply(true, (files ?? []).map((uri) => ({ uri: uri.toString(), name: uri.path.split('/').pop() })));
        }
        case 'upload': {
          const results = [];
          for (const uri of message.uris as string[]) {
            results.push(await this.api.upload(Number(message.bugId), vscode.Uri.parse(uri)));
          }
          return reply(true, results);
        }
        case 'download': {
          const bytes = await this.api.download(Number(message.attachmentId));
          const target = await vscode.window.showSaveDialog({ defaultUri: vscode.Uri.file(String(message.filename)), saveLabel: 'Save attachment' });
          if (target) {
            await vscode.workspace.fs.writeFile(target, bytes);
          }
          return reply(true, { saved: target !== undefined });
        }
        case 'confirm': {
          const choice = await vscode.window.showWarningMessage(String(message.text), { modal: true }, String(message.action));
          return reply(true, choice !== undefined);
        }
      }
    } catch (error) {
      if (error instanceof ApiError) {
        return reply(false, error.message, error.status);
      }
      return reply(false, error instanceof Error ? error.message : 'Unexpected error');
    }
  }

  private safeBaseUrl(): string {
    try {
      return this.api.baseUrl();
    } catch (error) {
      return error instanceof Error ? error.message : '';
    }
  }

  async selectProject(): Promise<void> {
    if (!this.api.signedIn) {
      return this.open();
    }
    try {
      const projects = await this.api.request<Project[]>('GET', '/projects');
      if (projects.length === 0) {
        void vscode.window.showInformationMessage('You are not a member of any project yet.');
        return;
      }
      const picked = await vscode.window.showQuickPick(
        projects.map((p) => ({ label: p.name, description: p.archived ? 'archived' : undefined, id: p.id })),
        { placeHolder: 'Select the project to show' },
      );
      if (picked) {
        await this.context.globalState.update(PROJECT_KEY, picked.id);
        await this.sessionChanged();
      }
    } catch (error) {
      void vscode.window.showErrorMessage(error instanceof Error ? error.message : 'Could not load projects');
    }
  }

  async refresh(): Promise<void> {
    await this.sessionChanged();
    this.post({ type: 'refresh' });
  }

  async signOut(): Promise<void> {
    await this.api.logout();
  }
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const api = new Api(context.secrets);
  await api.init();
  const controller = new Controller(context, api);
  context.subscriptions.push(
    vscode.commands.registerCommand('bugTracker.open', (route?: Route) => controller.open(route)),
    vscode.commands.registerCommand('bugTracker.reportBug', () => controller.open({ view: 'report' })),
    vscode.commands.registerCommand('bugTracker.openNotifications', () => controller.open({ view: 'notifications' })),
    vscode.commands.registerCommand('bugTracker.openDashboard', () => controller.open({ view: 'dashboard' })),
    vscode.commands.registerCommand('bugTracker.refresh', () => controller.refresh()),
    vscode.commands.registerCommand('bugTracker.selectProject', () => controller.selectProject()),
    vscode.commands.registerCommand('bugTracker.signOut', () => controller.signOut()),
  );
  await controller.start();
}

export function deactivate(): void {
  // Disposables registered on the extension context are released by VS Code.
}
