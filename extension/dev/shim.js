/* Development only. Stands in for the VS Code extension host so the webview
   can be opened in an ordinary browser: it answers the same messages that
   src/extension.ts answers, calling the backend on the same origin.
   The token is kept in memory and is lost on reload, as nothing here is meant
   for real use. */
(function () {
  let token = null;
  let projectId;
  const API = '/api/v1';
  const send = (message) => window.postMessage(message, location.origin);

  async function request(method, path, query, body) {
    const url = new URL(API + path, location.origin);
    for (const [key, value] of Object.entries(query || {})) {
      if (value === null || value === undefined || value === '') continue;
      for (const item of Array.isArray(value) ? value : [value]) url.searchParams.append(key, String(item));
    }
    const headers = {};
    if (token) headers.Authorization = 'Bearer ' + token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json().catch(() => null);
    if (response.status === 401 && token) { token = null; send({ type: 'session', signedIn: false }); }
    if (!response.ok) {
      const detail = data && data.detail;
      throw typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((d) => (d.loc || []).slice(1).join('.') + ': ' + d.msg).join('; ') : 'Status ' + response.status;
    }
    return data;
  }

  async function handle(message) {
    const reply = (ok, data) => send({ type: 'response', id: message.id, ok, data });
    try {
      switch (message.type) {
        case 'ready': return send({ type: 'init', signedIn: token !== null, projectId, serverUrl: location.origin + ' (browser harness)' });
        case 'api': return reply(true, await request(message.method, message.path, message.query, message.body));
        case 'login': {
          const result = await request('POST', '/auth/login', undefined, { email: message.email, password: message.password });
          token = result.access_token;
          reply(true, result.user);
          return send({ type: 'session', signedIn: true, projectId });
        }
        case 'logout': token = null; reply(true, null); return send({ type: 'session', signedIn: false });
        case 'setProject': projectId = message.projectId; return reply(true, null);
        case 'confirm': return reply(true, window.confirm(message.text));
        case 'pickFiles': return reply(true, []);
        case 'upload': case 'download': return reply(false, 'File dialogs are only available inside VS Code.');
      }
    } catch (error) {
      reply(false, typeof error === 'string' ? error : String(error && error.message ? error.message : error));
    }
  }

  window.acquireVsCodeApi = () => ({ postMessage: (message) => { void handle(message); } });
})();
