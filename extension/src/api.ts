import * as vscode from 'vscode';

const TOKEN_KEY = 'bugTracker.sessionToken';
const API_PREFIX = '/api/v1';
const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) {
    super(message);
  }
}

export type Query = Record<string, string | number | boolean | Array<string | number> | null | undefined>;

/** Turns a FastAPI error body into one readable sentence. */
function describeError(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string') {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item: { loc?: unknown[]; msg?: string }) => {
        const field = (item.loc ?? []).filter((part) => part !== 'body' && part !== 'query').join('.');
        const message = (item.msg ?? 'Invalid value').replace(/^Value error, /, '');
        return field ? `${field}: ${message}` : message;
      })
      .join('; ');
  }
  return `The server answered with status ${status}`;
}

/**
 * REST client for the backend. The session token lives in VS Code SecretStorage
 * (the operating system keychain) and never reaches the webview.
 */
export class Api {
  private token: string | undefined;
  private readonly signedOut = new vscode.EventEmitter<void>();
  readonly onDidSignOut = this.signedOut.event;

  constructor(private readonly secrets: vscode.SecretStorage) {}

  async init(): Promise<void> {
    this.token = await this.secrets.get(TOKEN_KEY);
  }

  get signedIn(): boolean {
    return this.token !== undefined;
  }

  /** Configured server, refused unless it is https or runs on this machine (NFR-05). */
  baseUrl(): string {
    const configured = vscode.workspace.getConfiguration('bugTracker').get<string>('serverUrl', '').trim().replace(/\/+$/, '');
    let url: URL;
    try {
      url = new URL(configured);
    } catch {
      throw new ApiError(0, 'The setting "bugTracker.serverUrl" is not a valid URL.');
    }
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && LOCAL_HOSTS.has(url.hostname))) {
      throw new ApiError(0, 'The server URL must use https. Plain http is only allowed for a server on this machine.');
    }
    return configured;
  }

  private async setToken(token: string | undefined): Promise<void> {
    this.token = token;
    if (token === undefined) {
      await this.secrets.delete(TOKEN_KEY);
    } else {
      await this.secrets.store(TOKEN_KEY, token);
    }
  }

  private async send(method: string, path: string, init: { query?: Query; body?: unknown; form?: FormData } = {}): Promise<Response> {
    if (!path.startsWith('/') || path.includes('://')) {
      throw new ApiError(0, 'Invalid request path.');
    }
    const url = new URL(this.baseUrl() + API_PREFIX + path);
    for (const [key, value] of Object.entries(init.query ?? {})) {
      if (value === null || value === undefined || value === '') {
        continue;
      }
      for (const item of Array.isArray(value) ? value : [value]) {
        url.searchParams.append(key, String(item));
      }
    }
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (this.token) {
      headers.Authorization = `Bearer ${this.token}`;
    }
    let body: BodyInit | undefined;
    if (init.form) {
      body = init.form;
    } else if (init.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(init.body);
    }
    let response: Response;
    try {
      response = await fetch(url, { method, headers, body, signal: AbortSignal.timeout(20000) });
    } catch {
      throw new ApiError(0, `Cannot reach the bug tracker server at ${this.baseUrl()}.`);
    }
    if (response.status === 401 && this.token) {
      // Session expired (8 hours idle) or was ended elsewhere.
      await this.setToken(undefined);
      this.signedOut.fire();
    }
    return response;
  }

  async request<T = unknown>(method: string, path: string, init: { query?: Query; body?: unknown } = {}): Promise<T> {
    const response = await this.send(method, path, init);
    const data: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(response.status, describeError(response.status, data));
    }
    return data as T;
  }

  async login(email: string, password: string): Promise<unknown> {
    const result = await this.request<{ access_token: string; user: unknown }>('POST', '/auth/login', { body: { email, password } });
    await this.setToken(result.access_token);
    return result.user;
  }

  async logout(): Promise<void> {
    if (this.token) {
      await this.request('POST', '/auth/logout').catch(() => undefined);
      await this.setToken(undefined);
    }
    this.signedOut.fire();
  }

  async upload(bugId: number, file: vscode.Uri): Promise<unknown> {
    const bytes = await vscode.workspace.fs.readFile(file);
    const form = new FormData();
    form.append('file', new Blob([new Uint8Array(bytes)]), file.path.split('/').pop() ?? 'attachment');
    const response = await this.send('POST', `/bugs/${bugId}/attachments`, { form });
    const data: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(response.status, describeError(response.status, data));
    }
    return data;
  }

  async download(attachmentId: number): Promise<Uint8Array> {
    const response = await this.send('GET', `/attachments/${attachmentId}`);
    if (!response.ok) {
      throw new ApiError(response.status, describeError(response.status, await response.json().catch(() => null)));
    }
    return new Uint8Array(await response.arrayBuffer());
  }
}
