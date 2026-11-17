/**
 * Integration test, run inside a real VS Code extension host:
 *
 *   npm run test:host        (needs the backend running and seeded, see the repository README)
 *
 * Checks that the extension activates, registers its commands, opens its
 * panel, and that the API client can sign in and read data through the same
 * code path the panel uses (token in SecretStorage, requests from the host).
 */
import * as assert from 'assert';
import * as vscode from 'vscode';
import { Api, ApiError } from '../api';

const COMMANDS = ['open', 'reportBug', 'refresh', 'selectProject', 'openNotifications', 'openDashboard', 'signOut'];
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

class MemorySecrets implements vscode.SecretStorage {
  private readonly values = new Map<string, string>();
  private readonly emitter = new vscode.EventEmitter<vscode.SecretStorageChangeEvent>();
  readonly onDidChange = this.emitter.event;
  async get(key: string) { return this.values.get(key); }
  async store(key: string, value: string) { this.values.set(key, value); }
  async delete(key: string) { this.values.delete(key); }
  async keys() { return [...this.values.keys()]; }
}

export async function run(): Promise<void> {
  const log = (line: string) => console.log(`[bug-tracker-test] ${line}`);
  const extension = vscode.extensions.getExtension('capstone-bugtracker.ai-bug-tracker');
  assert.ok(extension, 'extension is installed in the host');
  await extension.activate();
  assert.ok(extension.isActive, 'extension activates');
  log('activated');

  const registered = await vscode.commands.getCommands(true);
  for (const name of COMMANDS) {
    assert.ok(registered.includes(`bugTracker.${name}`), `command bugTracker.${name} is registered`);
  }
  log(`${COMMANDS.length} commands registered`);

  await vscode.commands.executeCommand('bugTracker.open');
  await sleep(1500);
  const tabs = vscode.window.tabGroups.all.flatMap((group) => group.tabs);
  assert.ok(tabs.some((tab) => tab.label === 'Bug Tracker' && tab.input instanceof vscode.TabInputWebview), 'the Bug Tracker panel opens as a webview');
  await vscode.commands.executeCommand('bugTracker.reportBug');
  await vscode.commands.executeCommand('bugTracker.openDashboard');
  await vscode.commands.executeCommand('bugTracker.refresh');
  log('panel opened and navigation commands ran');

  // The API client, in the extension host runtime, against the running backend.
  const api = new Api(new MemorySecrets());
  await api.init();
  assert.strictEqual(api.signedIn, false);
  await assert.rejects(() => api.request('GET', '/users/me'), (error: unknown) => error instanceof ApiError && error.status === 401);
  await assert.rejects(() => api.login('reporter@demo.example.com', 'wrong-password'), (error: unknown) => error instanceof ApiError && error.status === 401);
  const email = process.env.BUGTRACKER_TEST_EMAIL;
  const password = process.env.BUGTRACKER_TEST_PASSWORD;
  assert.ok(email && password, 'BUGTRACKER_TEST_EMAIL and BUGTRACKER_TEST_PASSWORD are set');
  const user = (await api.login(email, password)) as { email: string };
  assert.strictEqual(user.email, email);
  assert.strictEqual(api.signedIn, true);
  const projects = await api.request<Array<{ id: number; name: string }>>('GET', '/projects');
  assert.ok(projects.length > 0, 'the signed-in user sees at least one project');
  const page = await api.request<{ total: number }>('GET', `/projects/${projects[0].id}/bugs`, { query: { status: ['Open', 'In Progress'], page_size: 5 } });
  assert.ok(page.total > 0, 'bugs are listed with a repeated query parameter');
  const check = await api.request<{ available: boolean }>('POST', `/projects/${projects[0].id}/duplicates/check`, {
    body: { title: 'Checkout crash', description: 'The checkout page crashes with a NullPointerException when paying.' },
  });
  assert.strictEqual(check.available, true);
  let signedOut = false;
  api.onDidSignOut(() => (signedOut = true));
  await api.logout();
  assert.ok(signedOut && !api.signedIn, 'logout clears the token and notifies listeners');
  log(`API client: signed in as ${email}, ${projects.length} project(s), ${page.total} active bugs, signed out`);

  // Plain http to a remote host must be refused before any request is made (NFR-05).
  const configuration = vscode.workspace.getConfiguration('bugTracker');
  const original = configuration.get<string>('serverUrl');
  await configuration.update('serverUrl', 'http://tracker.example.com', vscode.ConfigurationTarget.Global);
  assert.throws(() => api.baseUrl(), /https/);
  await configuration.update('serverUrl', 'https://tracker.example.com', vscode.ConfigurationTarget.Global);
  assert.strictEqual(api.baseUrl(), 'https://tracker.example.com');
  await configuration.update('serverUrl', original, vscode.ConfigurationTarget.Global);
  log('plain http to a remote server is refused');
  log('ALL CHECKS PASSED');
}
