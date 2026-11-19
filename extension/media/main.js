/* Webview user interface of the Bug Tracker extension.

   The page never talks to the network itself: every request goes through the
   extension host (see src/extension.ts), which holds the session token.
   All content is inserted as text nodes, never as HTML, so nothing a user
   types into a bug report can be interpreted as markup (NFR-07). */
(function () {
  'use strict';

  const vscode = acquireVsCodeApi();
  const root = document.getElementById('app');
  const DEBOUNCE_MS = 600; // wait after the last keystroke before checking for duplicates
  const MIN_DESCRIPTION = 20;

  // ---- bridge to the extension host -----------------------------------------
  let sequence = 0;
  const pending = new Map();

  function call(type, payload) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      pending.set(id, { resolve, reject });
      vscode.postMessage(Object.assign({ type, id }, payload));
    });
  }
  const api = (method, path, options) => call('api', Object.assign({ method, path }, options));
  const get = (path, query) => api('GET', path, { query });
  const post = (path, body) => api('POST', path, { body });

  // ---- state -------------------------------------------------------------------
  const state = {
    signedIn: false, user: null, projects: [], projectId: null, meta: { severity_labels: [], statuses: [] },
    route: { view: 'bugs' }, unread: 0, serverUrl: '',
  };
  let filters = { q: '', status: [], severity: '', assignee_id: '', created_from: '', created_to: '', page: 1 };
  let draft = emptyDraft();
  let authMode = { form: 'signin', email: '', code: '', note: '' };
  let adminTab = 'profiles';
  let renderToken = 0;

  function emptyDraft() {
    return { title: '', description: '', steps_to_reproduce: '', environment: '', component: '', files: [] };
  }
  const project = () => state.projects.find((p) => p.id === state.projectId) || null;
  const myRole = () => (project() ? project().my_role : null);
  const RANK = { reporter: 1, developer: 2, project_manager: 3 };
  const atLeast = (role, minimum) => (RANK[role] || 0) >= RANK[minimum];
  const isAdmin = () => state.user && state.user.role === 'administrator';
  const canCreateProjects = () => state.user && (state.user.role === 'administrator' || state.user.role === 'project_manager');

  // ---- DOM helpers -------------------------------------------------------------
  function h(tag, props) {
    const element = document.createElementNS(tag === 'svg' || h.svg ? 'http://www.w3.org/2000/svg' : 'http://www.w3.org/1999/xhtml', tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key.startsWith('on')) element.addEventListener(key.slice(2), value);
      else if (key === 'value' && tag !== 'option') element.value = value;
      else if (key === 'checked' || key === 'disabled' || key === 'selected' || key === 'open') element[key] = Boolean(value);
      else element.setAttribute(key === 'className' ? 'class' : key, value === true ? '' : value);
    }
    append(element, Array.prototype.slice.call(arguments, 2));
    return element;
  }
  function append(parent, children) {
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      if (Array.isArray(child)) append(parent, child);
      else parent.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  function svg(tag, props) {
    h.svg = true;
    try { return h.apply(null, arguments); } finally { h.svg = false; }
  }
  let fieldCounter = 0;
  /** A labelled form control; the hint and error are tied to it for screen readers. */
  function field(label, control, options) {
    const id = control.id || 'f' + ++fieldCounter;
    control.id = id;
    const hint = options && options.hint ? h('div', { class: 'hint', id: id + '-hint' }, options.hint) : null;
    const error = h('div', { class: 'err', id: id + '-err', role: 'alert' });
    control.setAttribute('aria-describedby', (hint ? id + '-hint ' : '') + id + '-err');
    control.showError = (message) => {
      error.textContent = message || '';
      control.setAttribute('aria-invalid', message ? 'true' : 'false');
    };
    return h('div', { class: 'field' }, h('label', { for: id }, label), control, hint, error);
  }
  const button = (text, onclick, options) =>
    h('button', Object.assign({ type: 'button', class: 'btn' + (options && options.secondary ? ' secondary' : ''), onclick }, options && options.attrs), text);
  const linkButton = (text, onclick) => h('button', { type: 'button', class: 'link', onclick }, text);
  const select = (options, value, props) =>
    h('select', props || {}, options.map((o) => h('option', { value: o.value, selected: String(o.value) === String(value) }, o.label)));
  const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—');
  const fmtDay = (iso) => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  const who = (user) => (user ? user.display_name : '—');
  const roleName = (role) => ({ reporter: 'Reporter', developer: 'Developer', project_manager: 'Project Manager', administrator: 'Administrator' }[role] || role);

  function severityBadge(severity) {
    const labels = state.meta.severity_labels;
    const index = labels.indexOf(severity);
    const rank = index < 0 ? 'none' : labels.length === 1 ? 0 : Math.round((index / (labels.length - 1)) * 3);
    return h('span', { class: 'badge sev-' + rank }, severity);
  }

  function toast(message, kind) {
    let region = document.getElementById('toast');
    if (!region) {
      region = h('div', { id: 'toast', role: 'status', 'aria-live': 'polite' });
      document.body.appendChild(region);
    }
    const note = h('div', { class: 'notice ' + (kind || '') }, message);
    region.replaceChildren(note);
    setTimeout(() => note.remove(), kind === 'error' ? 9000 : 5000);
  }
  /** Runs an action, reporting failure to the user instead of failing silently. */
  async function attempt(action, success) {
    try {
      const result = await action();
      if (success) toast(success);
      return result;
    } catch (error) {
      toast(typeof error === 'string' ? error : (error && error.message) || 'Something went wrong', 'error');
      return undefined;
    }
  }

  // ---- shell -------------------------------------------------------------------
  function navigate(route) {
    state.route = route;
    render();
  }

  function shell(content) {
    const items = [['bugs', 'Bugs'], ['report', 'Report bug'], ['dashboard', 'Dashboard'], ['notifications', 'Notifications'], ['team', 'Projects & team']];
    if (isAdmin()) items.push(['admin', 'Administration']);
    items.push(['account', 'Account']);
    const current = state.route.view === 'bug' ? 'bugs' : state.route.view;
    const projectSelect = state.projects.length
      ? [h('label', { for: 'project-select', class: 'sr-only' }, 'Project'),
         select(state.projects.map((p) => ({ value: p.id, label: p.name + (p.archived ? ' (archived)' : '') })), state.projectId, {
           id: 'project-select',
           onchange: async (event) => {
             state.projectId = Number(event.target.value);
             filters.page = 1;
             await call('setProject', { projectId: state.projectId });
             navigate({ view: state.route.view === 'bug' ? 'bugs' : state.route.view });
           },
         })]
      : null;
    return [
      h('header', { class: 'top' },
        h('span', { class: 'brand' }, 'Bug Tracker'), projectSelect,
        h('nav', { 'aria-label': 'Main' }, items.map(([view, label]) =>
          h('button', { type: 'button', 'aria-current': current === view ? 'page' : null, onclick: () => navigate({ view }) },
            label, view === 'notifications' && state.unread ? h('span', { class: 'count', 'aria-label': state.unread + ' unread' }, state.unread) : null))),
        button('Sign out', async () => { await call('logout'); }, { secondary: true })),
      h('main', {}, content),
    ];
  }

  async function render() {
    const token = ++renderToken;
    const view = state.signedIn ? (views[state.route.view] || views.bugs) : views.auth;
    let content;
    try {
      content = await view(state.route);
    } catch (error) {
      content = [h('h1', { tabindex: '-1' }, 'Something went wrong'),
        h('div', { class: 'notice error', role: 'alert' }, typeof error === 'string' ? error : (error && error.message) || 'Unexpected error'),
        button('Try again', render)];
    }
    if (token !== renderToken) return; // a newer navigation finished first
    root.replaceChildren.apply(root, state.signedIn ? shell(content) : [h('main', {}, content)]);
    const heading = root.querySelector('h1');
    if (heading) heading.focus(); // announce the new view and reset the keyboard position
  }

  async function boot() {
    if (state.signedIn) {
      try {
        const [user, projects, meta, unread] = await Promise.all([get('/users/me'), get('/projects'), get('/meta'), get('/notifications/unread-count')]);
        Object.assign(state, { user, projects, meta, unread: unread.unread });
        if (!projects.some((p) => p.id === state.projectId)) state.projectId = projects.length ? projects[0].id : null;
      } catch (error) {
        if (!state.signedIn) return render();
        root.replaceChildren(h('main', {}, h('h1', { tabindex: '-1' }, 'Bug Tracker'),
          h('div', { class: 'notice error', role: 'alert' }, String(error)), h('p', { class: 'muted' }, 'Server: ' + state.serverUrl), button('Retry', boot)));
        return;
      }
    }
    render();
  }

  // The confidence is the share of the classifiers that agree: thirds for the earlier three-classifier vote,
  // quarters for the current four classifiers. Only a full agreement fits both, and it needs no count.
  const agreement = (share) => {
    if (!share) return '';
    if (share > 0.999) return ' (all classifiers agreed)';
    const size = [3, 4].find((n) => Math.abs(share * n - Math.round(share * n)) < 0.01);
    return size ? ' (' + Math.round(share * size) + ' of ' + size + ' classifiers agreed)' : '';
  };
  const needProject = () => [h('h1', { tabindex: '-1' }, 'No project yet'),
    h('p', {}, canCreateProjects() ? 'Create a project to start tracking bugs.' : 'You are not a member of any project yet. Ask a Project Manager to add you by your email address.'),
    canCreateProjects() ? button('Create a project', () => navigate({ view: 'team' })) : null];

  // ---- views -------------------------------------------------------------------
  const views = {};

  views.auth = function () {
    const email = h('input', { type: 'email', autocomplete: 'username', required: true, value: authMode.email });
    const note = authMode.note ? h('div', { class: 'notice', role: 'status' }, authMode.note) : null;
    const switcher = (form, text) => linkButton(text, () => { authMode = { form, email: email.value, code: '', note: '' }; render(); });
    if (authMode.form === 'register') {
      const name = h('input', { type: 'text', autocomplete: 'name', required: true, maxlength: 100 });
      const password = h('input', { type: 'password', autocomplete: 'new-password', required: true });
      return [h('h1', { tabindex: '-1' }, 'Create an account'),
        h('form', { onsubmit: async (event) => {
          event.preventDefault();
          const result = await attempt(() => post('/auth/register', { display_name: name.value, email: email.value, password: password.value }));
          if (result) {
            authMode = { form: 'verify', email: email.value, code: result.verification_code || '',
              note: result.verification_code ? 'Development mode: the verification code has been filled in for you.' : result.message };
            render();
          }
        } },
          field('Display name', name), field('Email address', email),
          field('Password', password, { hint: 'At least 8 characters with an uppercase letter, a number and a special character.' }),
          h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Create account'), switcher('signin', 'I already have an account'))),
        h('p', { class: 'muted' }, 'Server: ' + state.serverUrl)];
    }
    if (authMode.form === 'verify') {
      const code = h('input', { type: 'text', class: 'narrow', autocomplete: 'one-time-code', required: true, value: authMode.code });
      return [h('h1', { tabindex: '-1' }, 'Verify your email'), note,
        h('form', { onsubmit: async (event) => {
          event.preventDefault();
          if (await attempt(() => post('/auth/verify-email', { email: email.value, code: code.value }))) {
            authMode = { form: 'signin', email: email.value, code: '', note: 'Email verified. You can now sign in.' };
            render();
          }
        } },
          field('Email address', email), field('Verification code', code, { hint: 'The 8-character code sent to your email address.' }),
          h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Verify'),
            linkButton('Send a new code', () => attempt(async () => {
              const result = await post('/auth/resend-verification', { email: email.value });
              if (result.verification_code) code.value = result.verification_code;
              return result;
            }, 'If the account exists, a new code has been sent.')),
            switcher('signin', 'Back to sign in')))];
    }
    const password = h('input', { type: 'password', autocomplete: 'current-password', required: true });
    return [h('h1', { tabindex: '-1' }, 'Sign in to Bug Tracker'), note,
      h('form', { onsubmit: async (event) => {
        event.preventDefault();
        await attempt(() => call('login', { email: email.value, password: password.value }));
      } },
        field('Email address', email), field('Password', password),
        h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Sign in'),
          switcher('register', 'Create an account'), switcher('verify', 'Enter a verification code'))),
      h('p', { class: 'muted' }, 'Server: ' + state.serverUrl)];
  };

  views.bugs = async function () {
    if (!project()) return needProject();
    const [page, members] = await Promise.all([
      get('/projects/' + state.projectId + '/bugs', Object.assign({}, filters, { page_size: 25 })),
      get('/projects/' + state.projectId + '/members'),
    ]);
    const search = h('input', { type: 'search', value: filters.q, placeholder: 'Keyword or #id' });
    const statusBoxes = state.meta.statuses.map((s) => h('input', { type: 'checkbox', value: s, checked: filters.status.includes(s) }));
    const severity = select([{ value: '', label: 'Any' }].concat(state.meta.severity_labels.concat(['Unclassified']).map((s) => ({ value: s, label: s }))), filters.severity);
    const assignee = select([{ value: '', label: 'Anyone' }, { value: 0, label: 'Unassigned' }].concat(members.map((m) => ({ value: m.user_id, label: m.display_name }))), filters.assignee_id);
    const from = h('input', { type: 'date', class: 'narrow', value: filters.created_from });
    const to = h('input', { type: 'date', class: 'narrow', value: filters.created_to });
    const apply = (event) => {
      event.preventDefault();
      filters = { q: search.value.trim(), status: statusBoxes.filter((b) => b.checked).map((b) => b.value), severity: severity.value,
        assignee_id: assignee.value, created_from: from.value, created_to: to.value, page: 1 };
      render();
    };
    const pages = Math.max(1, Math.ceil(page.total / page.page_size));
    const turn = (delta) => { filters.page = page.page + delta; render(); };
    return [
      h('div', { class: 'row spread' }, h('h1', { tabindex: '-1' }, 'Bugs in ' + project().name),
        button('Report a bug', () => navigate({ view: 'report' }), { attrs: { disabled: project().archived } })),
      project().archived ? h('div', { class: 'notice warn' }, 'This project is archived. Its bugs can be viewed but no new reports are accepted.') : null,
      h('form', { onsubmit: apply, 'aria-label': 'Search and filter' },
        h('div', { class: 'row end' }, field('Search', search), field('Severity', severity), field('Assignee', assignee),
          field('Reported from', from), field('Reported to', to)),
        h('fieldset', {}, h('legend', {}, 'Status'), state.meta.statuses.map((s, i) => h('label', { class: 'check' }, statusBoxes[i], s))),
        h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Apply filters'),
          button('Clear', () => { filters = { q: '', status: [], severity: '', assignee_id: '', created_from: '', created_to: '', page: 1 }; render(); }, { secondary: true }))),
      h('p', { role: 'status' }, page.total === 1 ? '1 bug found.' : page.total + ' bugs found.'),
      page.items.length ? h('div', { class: 'wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['#', 'Title', 'Severity', 'Status', 'Assignee', 'Reported'].map((c) => h('th', { scope: 'col' }, c)))),
        h('tbody', {}, page.items.map((bug) => h('tr', {},
          h('td', { class: 'num' }, bug.id),
          h('td', {}, linkButton(bug.title, () => navigate({ view: 'bug', id: bug.id })),
            bug.duplicate_of_id ? h('span', { class: 'badge tag' }, 'duplicate of #' + bug.duplicate_of_id)
              : bug.probable_duplicate_of_id ? h('span', { class: 'badge tag' }, 'probable duplicate') : null),
          h('td', {}, severityBadge(bug.severity)), h('td', {}, bug.status), h('td', {}, who(bug.assignee)), h('td', {}, fmtDate(bug.created_at))))))) : null,
      h('div', { class: 'row' },
        button('Previous', () => turn(-1), { secondary: true, attrs: { disabled: page.page <= 1 } }),
        h('span', {}, 'Page ' + page.page + ' of ' + pages),
        button('Next', () => turn(1), { secondary: true, attrs: { disabled: page.page >= pages } })),
    ];
  };

  views.report = async function () {
    if (!project()) return needProject();
    if (project().archived) return [h('h1', { tabindex: '-1' }, 'Report a bug'), h('div', { class: 'notice warn' }, 'This project is archived and does not accept new bug reports.')];
    const components = await get('/projects/' + state.projectId + '/components').catch(() => []);
    const title = h('input', { type: 'text', maxlength: 200, required: true, value: draft.title });
    const description = h('textarea', { rows: 7, required: true, maxlength: 20000 }, draft.description);
    const steps = h('textarea', { rows: 4, maxlength: 10000 }, draft.steps_to_reproduce);
    const environment = h('input', { type: 'text', maxlength: 500, value: draft.environment });
    const component = h('input', { type: 'text', maxlength: 100, value: draft.component, list: 'component-list' });
    const counter = h('span', {});
    const similar = h('section', { 'aria-live': 'polite', 'aria-label': 'Similar existing bugs' });
    const fileList = h('ul', { class: 'plain' });
    let timer = null;
    let checkNumber = 0;

    const drawFiles = () => fileList.replaceChildren.apply(fileList, draft.files.map((file, index) =>
      h('li', {}, file.name + ' ', linkButton('Remove', () => { draft.files.splice(index, 1); drawFiles(); }))));

    const drawSimilar = (result) => {
      if (!result) return similar.replaceChildren();
      if (!result.available) return similar.replaceChildren(h('div', { class: 'notice warn' }, result.notice));
      if (!result.candidates.length) return similar.replaceChildren(h('p', { class: 'muted' }, 'No similar bugs found so far.'));
      similar.replaceChildren(h('div', { class: 'card' },
        h('h3', {}, 'Similar bugs already reported'),
        h('p', { class: 'muted' }, 'Open one to check whether your problem is already known before you submit.'),
        h('ul', { class: 'plain' }, result.candidates.map((candidate) => {
          const detail = h('div', {});
          return h('li', {},
            h('div', { class: 'row' }, h('strong', {}, candidate.match_percent + '% match'),
              h('span', {}, '#' + candidate.bug_id + ' ' + candidate.title), severityBadge(candidate.severity), h('span', { class: 'muted' }, candidate.status),
              candidate.probable_duplicate ? h('span', { class: 'badge tag' }, 'probable duplicate') : null,
              linkButton('View', async () => {
                if (detail.firstChild) return detail.replaceChildren();
                const bug = await attempt(() => get('/bugs/' + candidate.bug_id));
                if (bug) detail.replaceChildren(h('pre', { class: 'text' }, bug.description),
                  linkButton('Open bug #' + bug.id + ' (your draft is kept)', () => navigate({ view: 'bug', id: bug.id })));
              })), detail);
        }))));
    };

    const check = async () => {
      const number = ++checkNumber;
      const result = await post('/projects/' + state.projectId + '/duplicates/check', { title: draft.title, description: draft.description }).catch(() => null);
      if (number === checkNumber) drawSimilar(result); // ignore answers that arrive out of order
    };
    const changed = () => {
      Object.assign(draft, { title: title.value, description: description.value, steps_to_reproduce: steps.value, environment: environment.value, component: component.value });
      const length = description.value.trim().length;
      counter.textContent = length < MIN_DESCRIPTION ? (MIN_DESCRIPTION - length) + ' more characters needed' : length + ' characters';
      clearTimeout(timer);
      if (length >= MIN_DESCRIPTION) timer = setTimeout(check, DEBOUNCE_MS);
      else { checkNumber++; drawSimilar(null); }
    };
    title.addEventListener('input', changed);
    description.addEventListener('input', changed);
    [steps, environment, component].forEach((control) => control.addEventListener('input', () => {
      Object.assign(draft, { steps_to_reproduce: steps.value, environment: environment.value, component: component.value });
    }));

    const submit = async (event) => {
      event.preventDefault();
      title.showError(title.value.trim().length < 5 ? 'Enter a title of at least 5 characters.' : '');
      description.showError(description.value.trim().length < MIN_DESCRIPTION ? 'Describe the problem in at least 20 characters.' : '');
      const invalid = [title, description].find((control) => control.getAttribute('aria-invalid') === 'true');
      if (invalid) return invalid.focus();
      const submitButton = event.target.querySelector('button[type="submit"]');
      submitButton.disabled = true;
      const created = await attempt(() => post('/projects/' + state.projectId + '/bugs', {
        title: title.value, description: description.value, steps_to_reproduce: steps.value, environment: environment.value, component: component.value || null }));
      if (!created) { submitButton.disabled = false; return; }
      const files = draft.files;
      draft = emptyDraft();
      if (files.length) await attempt(() => call('upload', { bugId: created.bug.id, uris: files.map((f) => f.uri) }));
      vscode.postMessage({ type: 'changed' });
      navigate({ view: 'bug', id: created.bug.id, flash: created.notice || 'Bug #' + created.bug.id + ' submitted. Severity: ' + created.bug.severity + '.', flashKind: created.notice ? 'warn' : '' });
    };

    const content = [h('h1', { tabindex: '-1' }, 'Report a bug in ' + project().name),
      h('form', { onsubmit: submit, novalidate: true },
        field('Title', title, { hint: 'A one-line summary of the problem.' }),
        field('Description', description, { hint: h('span', {}, 'What happened and what you expected, at least 20 characters. ', counter) }),
        similar,
        field('Steps to reproduce', steps, { hint: 'Optional. One step per line.' }),
        field('Environment', environment, { hint: 'Optional. Operating system, browser or application version.' }),
        field('Component', component, { hint: 'Optional. The part of the product that is affected.' }),
        h('datalist', { id: 'component-list' }, components.map((name) => h('option', { value: name }))),
        h('fieldset', {}, h('legend', {}, 'Attachments'),
          h('p', { class: 'muted' }, 'Optional screenshots or log files, up to 10 MB each. Source code files are not accepted.'), fileList,
          button('Add files…', async () => {
            const picked = await attempt(() => call('pickFiles'));
            if (picked) { draft.files = draft.files.concat(picked); drawFiles(); }
          }, { secondary: true })),
        h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Submit bug report'),
          button('Discard draft', () => { draft = emptyDraft(); render(); }, { secondary: true })))];
    drawFiles();
    changed();
    return content;
  };

  views.bug = async function (route) {
    const id = route.id;
    const bug = await get('/bugs/' + id);
    const role = bug.my_role;
    const developer = atLeast(role, 'developer');
    const manager = atLeast(role, 'project_manager');
    const [comments, attachments, audit, members, recommendations] = await Promise.all([
      get('/bugs/' + id + '/comments'), get('/bugs/' + id + '/attachments'), get('/bugs/' + id + '/audit'),
      manager ? get('/projects/' + bug.project_id + '/members') : [],
      developer ? get('/bugs/' + id + '/recommendations').catch(() => null) : null,
    ]);
    const reload = (message) => { vscode.postMessage({ type: 'changed' }); navigate({ view: 'bug', id, flash: message }); };
    const act = async (action, message) => { if ((await attempt(action)) !== undefined) reload(message); };
    const canEdit = developer || (state.user && bug.reporter && bug.reporter.id === state.user.id);

    const assignTo = async (userId, name, override) => {
      const done = await post('/bugs/' + id + '/assign', { assignee_id: userId, override_exclusion: Boolean(override) }).then(() => true, async (error) => {
        if (!override && /override/.test(String(error)) && await call('confirm', { text: String(error), action: 'Assign anyway' })) {
          return assignTo(userId, name, true);
        }
        if (!/override/.test(String(error)) || override) toast(String(error), 'error');
        return false;
      });
      if (done === true) reload(userId ? 'Assigned to ' + name + '.' : 'Unassigned.');
      return false;
    };

    // --- duplicate information
    const originalId = bug.duplicate_of_id || bug.probable_duplicate_of_id;
    const duplicateBox = originalId ? h('div', { class: 'notice' + (bug.duplicate_of_id ? '' : ' warn') },
      bug.duplicate_of_id ? 'Confirmed duplicate of ' : 'Probable duplicate of ', linkButton('#' + originalId, () => navigate({ view: 'bug', id: originalId })),
      bug.duplicate_score ? ' (' + Math.round(bug.duplicate_score * 100) + '% similar at submission). ' : '. ',
      developer && !bug.duplicate_of_id ? button('Confirm duplicate', () => act(() => post('/bugs/' + id + '/duplicate', { duplicate_of_id: originalId }), 'Marked as a duplicate.'), { secondary: true }) : null,
      developer ? button('Not a duplicate', () => act(() => post('/bugs/' + id + '/duplicate', { duplicate_of_id: null }), 'Duplicate link cleared.'), { secondary: true }) : null) : null;

    // --- severity override
    const severitySelect = select(state.meta.severity_labels.map((s) => ({ value: s, label: s })), bug.severity, { id: 'severity-override' });
    const severitySource = { model: 'assigned by the classifier', rules: 'assigned by keyword rules (no trained model loaded)', manual: 'set manually', none: 'not classified' }[bug.severity_source];

    // --- assignment
    let assignment = null;
    if (developer && recommendations) {
      const cold = recommendations.cold_start;
      const modeLabel = { model: 'Model-based recommendations', rule_based: 'Rule-based suggestions', manual: 'Manual assignment', unavailable: 'Recommendations unavailable' }[recommendations.mode];
      const manual = manager ? select([{ value: '', label: 'Choose a project member…' }].concat(members.map((m) => ({ value: m.user_id, label: m.display_name + ' (' + roleName(m.role) + ')' }))), '', { id: 'manual-assignee' }) : null;
      assignment = h('section', { 'aria-labelledby': 'assign-heading' },
        h('h2', { id: 'assign-heading' }, 'Assignment'),
        h('p', {}, 'Currently assigned to: ', h('strong', {}, bug.assignee ? bug.assignee.display_name : 'nobody')),
        h('div', { class: 'notice' + (recommendations.mode === 'model' ? '' : ' warn') }, h('strong', {}, modeLabel + '. '), recommendations.message),
        recommendations.recommendations.map((rec, index) => h('div', { class: 'card' },
          h('div', { class: 'row spread' },
            h('h3', {}, (index + 1) + '. ' + rec.display_name + ' ', rec.limited_history ? h('span', { class: 'badge tag' }, 'limited history') : null,
              recommendations.mode === 'rule_based' ? h('span', { class: 'badge tag' }, 'rule-based') : null),
            manager ? button('Assign to ' + rec.display_name, () => assignTo(rec.user_id, rec.display_name)) : null),
          h('p', { class: 'muted' }, 'Why this developer:'), h('ul', { class: 'reasons' }, rec.reasons.map((reason) => h('li', {}, reason))))),
        recommendations.excluded.length ? h('div', {}, h('h3', {}, 'Not recommended'),
          h('ul', {}, recommendations.excluded.map((entry) => h('li', {}, entry.display_name + ': ' + entry.reason + ' ',
            manager ? linkButton('Assign anyway', () => assignTo(entry.user_id, entry.display_name, true)) : null)))) : null,
        manager ? h('div', { class: 'row end' }, field('Assign manually', manual),
          h('div', { class: 'field' }, button('Assign', () => manual.value ? assignTo(Number(manual.value), manual.selectedOptions[0].textContent) : manual.focus(), { secondary: true })),
          bug.assignee ? h('div', { class: 'field' }, button('Unassign', () => assignTo(null), { secondary: true })) : null) : null,
        cold.cold_start ? null : h('p', { class: 'muted' }, 'Model last trained ' + fmtDate(cold.last_trained_at) + ' on ' + cold.verified_count + ' verified bugs. Answered in ' + Math.round(recommendations.elapsed_ms) + ' ms.'));
    }

    // --- edit form (hidden until requested)
    const editArea = h('div', {});
    const openEdit = () => {
      const title = h('input', { type: 'text', maxlength: 200, value: bug.title });
      const description = h('textarea', { rows: 6 }, bug.description);
      const steps = h('textarea', { rows: 3 }, bug.steps_to_reproduce);
      const environment = h('input', { type: 'text', value: bug.environment });
      const component = h('input', { type: 'text', value: bug.component || '' });
      editArea.replaceChildren(h('form', { class: 'card', onsubmit: (event) => {
        event.preventDefault();
        act(() => api('PATCH', '/bugs/' + id, { body: { title: title.value, description: description.value, steps_to_reproduce: steps.value, environment: environment.value, component: component.value } }), 'Changes saved.');
      } }, h('h3', {}, 'Edit report'), field('Title', title), field('Description', description), field('Steps to reproduce', steps),
        field('Environment', environment), field('Component', component),
        h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Save changes'), button('Cancel', () => editArea.replaceChildren(), { secondary: true }))));
      title.focus();
    };

    const commentBox = h('textarea', { rows: 3, maxlength: 10000, required: true });
    const transitionLabel = (status) => (bug.status === 'Resolved' && status === 'In Progress' ? 'Fix not accepted: back to In Progress' : 'Move to ' + status);

    return [
      draft.description || draft.title ? h('div', { class: 'notice' }, 'You have an unfinished bug report. ', linkButton('Return to your draft', () => navigate({ view: 'report' }))) : null,
      route.flash ? h('div', { class: 'notice ' + (route.flashKind || ''), role: 'status' }, route.flash) : null,
      h('h1', { tabindex: '-1' }, '#' + bug.id + ' ' + bug.title),
      h('div', { class: 'row' }, h('span', { class: 'badge tag' }, bug.status), severityBadge(bug.severity),
        bug.allowed_transitions.map((status) => button(transitionLabel(status), () => act(() => post('/bugs/' + id + '/status', { status }), 'Status changed to ' + status + '.'), { secondary: status === 'In Progress' && bug.status === 'Resolved' })),
        button(bug.watching ? 'Stop watching' : 'Watch', () => act(() => api(bug.watching ? 'DELETE' : 'PUT', '/bugs/' + id + '/watch'), bug.watching ? 'You will no longer be notified about comments.' : 'You will be notified about comments.'), { secondary: true }),
        canEdit ? button('Edit', openEdit, { secondary: true }) : null),
      duplicateBox, editArea,
      h('dl', { class: 'meta' },
        h('dt', {}, 'Severity'), h('dd', {}, bug.severity + ', ' + severitySource + (bug.severity_source === 'model' ? agreement(bug.severity_confidence) : '')),
        h('dt', {}, 'Reported by'), h('dd', {}, who(bug.reporter) + ' on ' + fmtDate(bug.created_at)),
        h('dt', {}, 'Assigned to'), h('dd', {}, bug.assignee ? bug.assignee.display_name : 'Unassigned'),
        h('dt', {}, 'Component'), h('dd', {}, bug.component || '—'),
        h('dt', {}, 'Environment'), h('dd', {}, bug.environment || '—')),
      developer ? h('div', { class: 'row end' }, field('Override severity', severitySelect),
        h('div', { class: 'field' }, button('Apply', () => act(() => post('/bugs/' + id + '/severity', { severity: severitySelect.value }), 'Severity set to ' + severitySelect.value + '.'), { secondary: true }))) : null,
      h('h2', {}, 'Description'), h('pre', { class: 'text' }, bug.description),
      bug.steps_to_reproduce ? [h('h2', {}, 'Steps to reproduce'), h('pre', { class: 'text' }, bug.steps_to_reproduce)] : null,
      assignment,
      h('h2', {}, 'Attachments'),
      attachments.length ? h('ul', {}, attachments.map((file) => h('li', {},
        linkButton(file.filename, () => attempt(() => call('download', { attachmentId: file.id, filename: file.filename }))),
        h('span', { class: 'muted' }, ' ' + (file.size / 1024).toFixed(1) + ' KB, added ' + fmtDate(file.created_at))))) : h('p', { class: 'muted' }, 'No attachments.'),
      button('Add attachment…', async () => {
        const picked = await attempt(() => call('pickFiles'));
        if (picked && picked.length) act(() => call('upload', { bugId: id, uris: picked.map((f) => f.uri) }), 'Attachment added.');
      }, { secondary: true }),
      h('h2', {}, 'Comments'),
      comments.length ? comments.map((comment) => h('div', { class: 'comment' },
        h('div', {}, h('strong', {}, who(comment.author)), h('span', { class: 'muted' }, ' · ' + fmtDate(comment.created_at))), h('pre', { class: 'text' }, comment.body)))
        : h('p', { class: 'muted' }, 'No comments yet.'),
      h('form', { onsubmit: (event) => { event.preventDefault(); act(() => post('/bugs/' + id + '/comments', { body: commentBox.value }), 'Comment added.'); } },
        field('Add a comment', commentBox), h('button', { type: 'submit', class: 'btn' }, 'Comment')),
      h('h2', {}, 'History'),
      h('div', { class: 'wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['When', 'Who', 'What', 'Change'].map((c) => h('th', { scope: 'col' }, c)))),
        h('tbody', {}, audit.map((entry) => h('tr', {},
          h('td', {}, fmtDate(entry.created_at)), h('td', {}, entry.actor ? entry.actor.display_name : 'System'),
          h('td', {}, entry.action.replace(/_/g, ' ') + (entry.field ? ' (' + entry.field + ')' : '')),
          h('td', {}, [entry.old_value, entry.new_value].filter((v) => v !== null).map((v) => (v.length > 120 ? v.slice(0, 120) + '…' : v)).join(' → '))))))),
    ];
  };

  function trendChart(trend) {
    const width = 720, height = 220, left = 34, bottom = 28, top = 14;
    const max = Math.max(1, ...trend.map((w) => Math.max(w.reported, w.resolved)));
    const slot = (width - left - 8) / trend.length;
    const bar = Math.min(26, slot / 2 - 4);
    const y = (value) => top + (height - top - bottom) * (1 - value / max);
    const summary = trend.map((w) => fmtDay(w.week_start) + ': ' + w.reported + ' reported, ' + w.resolved + ' resolved').join('; ');
    return svg('svg', { class: 'chart', viewBox: '0 0 ' + width + ' ' + height, role: 'img', 'aria-label': 'Bugs reported and resolved per week. ' + summary },
      [0, 0.5, 1].map((fraction) => [
        svg('line', { class: 'axis', x1: left, x2: width - 8, y1: y(max * fraction), y2: y(max * fraction) }),
        svg('text', { x: left - 6, y: y(max * fraction) + 4, 'text-anchor': 'end' }, Math.round(max * fraction))]),
      trend.map((week, index) => {
        const x = left + slot * index + slot / 2;
        return [
          svg('rect', { class: 'a', x: x - bar - 1, y: y(week.reported), width: bar, height: y(0) - y(week.reported) }),
          svg('rect', { class: 'b', x: x + 1, y: y(week.resolved), width: bar, height: y(0) - y(week.resolved) }),
          svg('text', { x: x - bar / 2 - 1, y: y(week.reported) - 3, 'text-anchor': 'middle' }, week.reported),
          svg('text', { x: x + bar / 2 + 1, y: y(week.resolved) - 3, 'text-anchor': 'middle' }, week.resolved),
          svg('text', { x, y: height - 8, 'text-anchor': 'middle' }, fmtDay(week.week_start))];
      }));
  }

  views.dashboard = async function () {
    if (!project()) return needProject();
    const board = await get('/projects/' + state.projectId + '/dashboard');
    const cold = board.cold_start;
    const tile = (label, value) => h('div', { class: 'tile' }, h('div', { class: 'value' }, value), h('div', { class: 'muted' }, label));
    const modelBox = cold.cold_start
      ? h('div', { class: 'notice warn' }, h('strong', {}, 'Assignment model: cold start. '),
          cold.state === 'training' ? 'The model is being trained for the first time.'
            : cold.verified_count + ' of ' + cold.threshold + ' verified bugs with an assignee recorded. ' + cold.remaining + ' more are needed before model-based recommendations become available. Until then assignment is manual or rule-based, and every assignment is recorded as training data.',
          cold.last_error ? h('p', {}, 'Last training attempt failed: ' + cold.last_error) : null)
      : h('div', { class: 'notice' }, h('strong', {}, 'Assignment model: active. '), 'Trained ' + fmtDate(cold.last_trained_at) + ' on ' + cold.verified_count + ' verified bugs.');
    return [h('h1', { tabindex: '-1' }, 'Dashboard: ' + project().name),
      h('p', { class: 'muted' }, 'Updated ' + fmtDate(board.generated_at) + '. ', linkButton('Refresh', render)),
      h('div', { class: 'tiles' }, tile('Open', board.counts['Open']), tile('In progress', board.counts['In Progress']), tile('Resolved', board.counts['Resolved']),
        tile('Verified', board.counts['Verified']), tile('Closed', board.counts['Closed']), tile(state.meta.severity_labels.slice(-1)[0] + ', unresolved', board.critical_open)),
      modelBox,
      h('h2', {}, 'Reported and resolved per week, last eight weeks'),
      h('div', { class: 'legend' }, h('span', {}, h('span', { class: 'swatch a' }), 'Reported'), h('span', {}, h('span', { class: 'swatch b' }), 'Resolved')),
      trendChart(board.trend),
      h('details', {}, h('summary', {}, 'Show as a table'), h('table', {},
        h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Week starting'), h('th', { scope: 'col', class: 'num' }, 'Reported'), h('th', { scope: 'col', class: 'num' }, 'Resolved'))),
        h('tbody', {}, board.trend.map((w) => h('tr', {}, h('td', {}, fmtDay(w.week_start)), h('td', { class: 'num' }, w.reported), h('td', { class: 'num' }, w.resolved)))))),
      h('h2', {}, 'Average time to resolve, by severity'),
      h('table', {}, h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, 'Severity'), h('th', { scope: 'col', class: 'num' }, 'Average'))),
        h('tbody', {}, Object.entries(board.avg_resolution_hours).map(([label, hours]) => h('tr', {}, h('td', {}, severityBadge(label)),
          h('td', { class: 'num' }, hours === null ? 'No resolved bugs' : hours >= 48 ? (hours / 24).toFixed(1) + ' days' : hours + ' hours')))))];
  };

  views.notifications = async function () {
    const list = await get('/notifications', { limit: 50 });
    state.unread = list.filter((n) => !n.read).length;
    return [h('div', { class: 'row spread' }, h('h1', { tabindex: '-1' }, 'Notifications'),
      button('Mark all as read', async () => { await attempt(() => post('/notifications/read-all')); state.unread = 0; vscode.postMessage({ type: 'changed' }); render(); }, { secondary: true, attrs: { disabled: !state.unread } })),
      list.length ? h('ul', { class: 'plain' }, list.map((note) => h('li', { class: 'comment' },
        h('div', { class: note.read ? '' : 'unread' }, note.read ? null : h('span', { class: 'badge tag' }, 'new'), ' ',
          linkButton(note.message, async () => {
            if (!note.read) { await attempt(() => post('/notifications/' + note.id + '/read')); state.unread = Math.max(0, state.unread - 1); vscode.postMessage({ type: 'changed' }); }
            navigate(note.bug_id ? { view: 'bug', id: note.bug_id } : { view: 'notifications' });
          })),
        h('div', { class: 'muted' }, fmtDate(note.created_at))))) : h('p', { class: 'muted' }, 'Nothing yet. You are notified when a bug is assigned to you, when a bug you follow changes, and when a critical bug is reported.')];
  };

  views.team = async function () {
    const current = project();
    const manager = current && atLeast(current.my_role, 'project_manager');
    const refreshProjects = async (selectId) => { state.projects = await get('/projects'); if (selectId) { state.projectId = selectId; await call('setProject', { projectId: selectId }); } render(); };
    const sections = [h('h1', { tabindex: '-1' }, 'Projects and team')];
    if (current) {
      const members = await get('/projects/' + current.id + '/members');
      sections.push(h('h2', {}, 'Members of ' + current.name), h('div', { class: 'wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Name', 'Email', 'Project role', manager ? 'Actions' : null].filter(Boolean).map((c) => h('th', { scope: 'col' }, c)))),
        h('tbody', {}, members.map((member) => h('tr', {}, h('td', {}, member.display_name), h('td', {}, member.email),
          h('td', {}, manager ? select(['reporter', 'developer', 'project_manager'].map((r) => ({ value: r, label: roleName(r) })), member.role, {
            'aria-label': 'Role of ' + member.display_name,
            onchange: async (event) => { await attempt(() => api('PATCH', '/projects/' + current.id + '/members/' + member.user_id, { body: { role: event.target.value } }), 'Role updated.'); render(); },
          }) : roleName(member.role)),
          manager ? h('td', {}, linkButton('Remove', async () => {
            if (await call('confirm', { text: 'Remove ' + member.display_name + ' from ' + current.name + '? Their open bugs become unassigned.', action: 'Remove' })) {
              await attempt(() => api('DELETE', '/projects/' + current.id + '/members/' + member.user_id), 'Member removed.'); render();
            }
          })) : null))))));
      if (manager) {
        const email = h('input', { type: 'email', required: true });
        const role = select(['reporter', 'developer', 'project_manager'].map((r) => ({ value: r, label: roleName(r) })), 'reporter');
        const name = h('input', { type: 'text', value: current.name, maxlength: 120, required: true });
        const description = h('input', { type: 'text', value: current.description, maxlength: 2000 });
        sections.push(
          h('form', { class: 'card', onsubmit: async (event) => { event.preventDefault(); if (await attempt(() => post('/projects/' + current.id + '/members', { email: email.value, role: role.value }), 'Member added.')) render(); } },
            h('h3', {}, 'Add a member'), h('div', { class: 'row end' }, field('Email address of a registered user', email), field('Project role', role),
              h('div', { class: 'field' }, h('button', { type: 'submit', class: 'btn' }, 'Add')))),
          h('form', { class: 'card', onsubmit: async (event) => { event.preventDefault(); if (await attempt(() => api('PATCH', '/projects/' + current.id, { body: { name: name.value, description: description.value } }), 'Project updated.')) refreshProjects(); } },
            h('h3', {}, 'Project settings'), field('Name', name), field('Description', description),
            h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Save'),
              button(current.archived ? 'Restore project' : 'Archive project', async () => {
                if (current.archived || await call('confirm', { text: 'Archive ' + current.name + '? It stays viewable but accepts no new bug reports.', action: 'Archive' })) {
                  await attempt(() => api('PATCH', '/projects/' + current.id, { body: { archived: !current.archived } }), current.archived ? 'Project restored.' : 'Project archived.');
                  refreshProjects();
                }
              }, { secondary: true }))));
      }
    } else {
      sections.push(h('p', {}, 'You are not a member of any project yet.'));
    }
    if (canCreateProjects()) {
      const name = h('input', { type: 'text', maxlength: 120, required: true });
      const description = h('input', { type: 'text', maxlength: 2000 });
      sections.push(h('form', { class: 'card', onsubmit: async (event) => {
        event.preventDefault();
        const created = await attempt(() => post('/projects', { name: name.value, description: description.value }), 'Project created.');
        if (created) refreshProjects(created.id);
      } }, h('h3', {}, 'Create a project'), field('Name', name), field('Description', description), h('button', { type: 'submit', class: 'btn' }, 'Create project')));
    } else if (!current) {
      sections.push(h('p', { class: 'muted' }, 'Ask a Project Manager to add you by your email address.'));
    }
    return sections;
  };

  views.admin = async function (route) {
    if (!isAdmin()) return [h('h1', { tabindex: '-1' }, 'Administration'), h('div', { class: 'notice error' }, 'Only Administrators can open this page.')];
    const tabs = [['profiles', 'Developer profiles'], ['users', 'Users'], ['models', 'Assignment models'], ['settings', 'Settings']];
    const head = [h('h1', { tabindex: '-1' }, 'Administration'),
      h('div', { class: 'row', role: 'group', 'aria-label': 'Administration sections' }, tabs.map(([key, label]) =>
        button(label, () => { adminTab = key; navigate({ view: 'admin' }); }, { secondary: adminTab !== key, attrs: { 'aria-pressed': String(adminTab === key) } })))];
    const list = (text) => text.split(',').map((s) => s.trim()).filter(Boolean);

    if (adminTab === 'profiles' && route.id) {
      const [profile, trail] = await Promise.all([get('/admin/profiles/' + route.id), get('/admin/profiles/' + route.id + '/audit')]);
      const skills = h('input', { type: 'text', value: profile.skills.join(', ') });
      const components = h('input', { type: 'text', value: profile.components.join(', ') });
      const availability = select(['Available', 'Busy', 'Unavailable'].map((a) => ({ value: a, label: a })), profile.availability);
      const capacity = h('input', { type: 'number', class: 'narrow', min: 1, max: 100, value: profile.max_capacity });
      return head.concat([
        h('p', {}, linkButton('← All profiles', () => navigate({ view: 'admin' }))),
        h('h2', {}, profile.user.display_name + (profile.active ? '' : ' (deactivated)')), h('p', { class: 'muted' }, profile.user.email),
        h('form', { class: 'card', onsubmit: async (event) => {
          event.preventDefault();
          if (await attempt(() => api('PATCH', '/admin/profiles/' + profile.id, { body: { skills: list(skills.value), components: list(components.value), availability: availability.value, max_capacity: Number(capacity.value) } }), 'Profile saved.')) render();
        } }, h('h3', {}, 'Declared by the Administrator'),
          field('Skills and technologies', skills, { hint: 'Separate with commas, for example: Python, PostgreSQL, OAuth.' }),
          field('Associated components', components, { hint: 'Separate with commas.' }),
          h('div', { class: 'row end' }, field('Availability', availability), field('Maximum concurrent bugs', capacity)),
          h('div', { class: 'row' }, h('button', { type: 'submit', class: 'btn' }, 'Save profile'),
            button(profile.active ? 'Deactivate profile' : 'Reactivate profile', async () => {
              if (!profile.active || await call('confirm', { text: 'Deactivate the profile of ' + profile.user.display_name + '? They will no longer be recommended.', action: 'Deactivate' })) {
                await attempt(() => post('/admin/profiles/' + profile.id + (profile.active ? '/deactivate' : '/activate')), profile.active ? 'Profile deactivated.' : 'Profile reactivated.');
                render();
              }
            }, { secondary: true }))),
        h('h3', {}, 'Recorded by the system (read-only)'),
        h('dl', { class: 'meta' }, h('dt', {}, 'Open bugs now'), h('dd', {}, profile.open_bugs + ' of ' + profile.max_capacity),
          h('dt', {}, 'Verified fixes'), h('dd', {}, profile.resolved_count)),
        profile.history.length ? h('div', { class: 'wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, ['Bug', 'Component', 'Severity', 'Keywords', 'Time to resolve', 'Verified'].map((c) => h('th', { scope: 'col' }, c)))),
          h('tbody', {}, profile.history.slice().reverse().map((entry) => h('tr', {}, h('td', {}, entry.bug_id ? '#' + entry.bug_id : '—'), h('td', {}, entry.component || '—'),
            h('td', {}, severityBadge(entry.severity)), h('td', {}, entry.keywords.join(', ')), h('td', {}, (entry.resolution_seconds / 3600).toFixed(1) + ' h'), h('td', {}, fmtDate(entry.created_at))))))) : h('p', { class: 'muted' }, 'No verified fixes recorded yet.'),
        h('h3', {}, 'Profile audit trail'),
        h('table', {}, h('thead', {}, h('tr', {}, ['When', 'Who', 'What', 'Change'].map((c) => h('th', { scope: 'col' }, c)))),
          h('tbody', {}, trail.map((entry) => h('tr', {}, h('td', {}, fmtDate(entry.created_at)), h('td', {}, entry.actor ? entry.actor.display_name : 'System'),
            h('td', {}, entry.action.replace(/_/g, ' ') + (entry.field ? ' (' + entry.field + ')' : '')), h('td', {}, [entry.old_value, entry.new_value].filter((v) => v !== null).join(' → ')))))),
      ]);
    }
    if (adminTab === 'profiles') {
      const [profiles, users] = await Promise.all([get('/admin/profiles'), get('/admin/users')]);
      const without = users.filter((u) => !profiles.some((p) => p.user.id === u.id));
      const chooser = select([{ value: '', label: 'Choose a user…' }].concat(without.map((u) => ({ value: u.id, label: u.display_name + ' (' + u.email + ')' }))), '');
      return head.concat([
        h('p', { class: 'muted' }, 'A profile is created automatically when a user becomes a Developer on a project. Skills, components, availability and capacity are declared here; the history is written by the system when a bug is verified.'),
        h('div', { class: 'wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, ['Developer', 'Availability', 'Open / capacity', 'Verified fixes', 'Skills', 'Components'].map((c) => h('th', { scope: 'col' }, c)))),
          h('tbody', {}, profiles.map((profile) => h('tr', {},
            h('td', {}, linkButton(profile.user.display_name, () => navigate({ view: 'admin', id: profile.id })), profile.active ? null : h('span', { class: 'badge tag' }, 'deactivated')),
            h('td', {}, profile.availability), h('td', { class: 'num' }, profile.open_bugs + ' / ' + profile.max_capacity), h('td', { class: 'num' }, profile.resolved_count),
            h('td', {}, profile.skills.join(', ') || '—'), h('td', {}, profile.components.join(', ') || '—')))))),
        without.length ? h('div', { class: 'row end' }, field('Create a profile for', chooser), h('div', { class: 'field' }, button('Create profile', async () => {
          if (!chooser.value) return chooser.focus();
          const created = await attempt(() => post('/admin/profiles', { user_id: Number(chooser.value) }), 'Profile created.');
          if (created) navigate({ view: 'admin', id: created.id });
        }, { secondary: true }))) : null]);
    }
    if (adminTab === 'users') {
      const users = await get('/admin/users');
      const update = async (user, body) => { await attempt(() => api('PATCH', '/admin/users/' + user.id, { body }), 'User updated.'); render(); };
      return head.concat([h('div', { class: 'wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Name', 'Email', 'System role', 'Verified', 'Active'].map((c) => h('th', { scope: 'col' }, c)))),
        h('tbody', {}, users.map((user) => h('tr', {}, h('td', {}, user.display_name), h('td', {}, user.email),
          h('td', {}, select(['reporter', 'developer', 'project_manager', 'administrator'].map((r) => ({ value: r, label: roleName(r) })), user.role, { 'aria-label': 'System role of ' + user.display_name, onchange: (event) => update(user, { role: event.target.value }) })),
          h('td', {}, user.email_verified ? 'Yes' : 'No'),
          h('td', {}, h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: user.is_active, onchange: (event) => update(user, { is_active: event.target.checked }) }), h('span', { class: 'sr-only' }, user.display_name + ' is '), 'active'))))))),
        h('p', { class: 'muted' }, 'The Project Manager system role allows a user to create projects. Roles inside a project are set under Projects & team.')]);
    }
    if (adminTab === 'models') {
      const states = await get('/admin/model-states');
      return head.concat([h('div', { class: 'wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, ['Project', 'State', 'Verified bugs', 'Last trained', 'Validation top-1 / top-3', 'Actions'].map((c) => h('th', { scope: 'col' }, c)))),
        h('tbody', {}, states.map((entry) => h('tr', {}, h('td', {}, entry.project_name),
          h('td', {}, entry.state === 'active' ? 'Active' : entry.state === 'training' ? 'Training' : 'Cold start (' + entry.remaining + ' more needed)', entry.last_error ? h('div', { class: 'err' }, entry.last_error) : null),
          h('td', { class: 'num' }, entry.verified_count + ' / ' + entry.threshold), h('td', {}, fmtDate(entry.last_trained_at)),
          h('td', {}, entry.metrics && entry.metrics.validation && entry.metrics.validation.top1 !== undefined ? Math.round(entry.metrics.validation.top1 * 100) + '% / ' + Math.round(entry.metrics.validation.top3 * 100) + '% on ' + entry.metrics.validation_samples + ' held-out bugs' : '—'),
          h('td', {}, button('Retrain now', async () => { await attempt(() => post('/admin/projects/' + entry.project_id + '/retrain'), 'Training started in the background.'); }, { secondary: true, attrs: { disabled: entry.remaining > 0 } }))))))),
        h('p', { class: 'muted' }, 'Models retrain automatically every 30 days or after 200 further verified bugs, whichever comes first.')]);
    }
    const settings = await get('/admin/settings');
    const threshold = h('input', { type: 'number', class: 'narrow', min: 0, max: 1, step: 0.01, value: settings.duplicate_threshold });
    const labels = h('input', { type: 'text', value: settings.severity_labels.join(', ') });
    return head.concat([h('form', { class: 'card', onsubmit: async (event) => {
      event.preventDefault();
      if (await attempt(() => api('PUT', '/admin/settings', { body: { duplicate_threshold: Number(threshold.value), severity_labels: list(labels.value) } }), 'Settings saved.')) { state.meta = await get('/meta'); render(); }
    } }, h('h3', {}, 'Triage settings'),
      field('Duplicate warning threshold', threshold, { hint: 'Similarity from 0 to 1 at or above which a report is flagged as a probable duplicate.' }),
      field('Severity labels', labels, { hint: 'From least to most severe, separated by commas. The last label triggers the urgent alert.' }),
      h('button', { type: 'submit', class: 'btn' }, 'Save settings')),
      h('p', { class: 'muted' }, 'Changes apply immediately, without restarting the server.')]);
  };

  views.account = async function () {
    const [user, profile] = await Promise.all([get('/users/me'), get('/users/me/developer-profile')]);
    state.user = user;
    const name = h('input', { type: 'text', value: user.display_name, maxlength: 100, required: true });
    const email = h('input', { type: 'email', value: user.email, required: true });
    const prefLabels = { assignment: 'A bug is assigned to me', status_change: 'The status of a bug I reported, own or watch changes', comment: 'Someone comments on a bug I am watching', critical_bug: 'A bug of the highest severity is reported in one of my projects' };
    const prefs = Object.keys(prefLabels).map((key) => h('input', { type: 'checkbox', value: key, checked: user.notification_prefs[key] !== false }));
    const current = h('input', { type: 'password', autocomplete: 'current-password', required: true });
    const next = h('input', { type: 'password', autocomplete: 'new-password', required: true });
    const code = h('input', { type: 'text', class: 'narrow', required: true });
    return [h('h1', { tabindex: '-1' }, 'Account'),
      h('p', { class: 'muted' }, 'System role: ' + roleName(user.role)),
      h('form', { class: 'card', onsubmit: async (event) => {
        event.preventDefault();
        const body = { display_name: name.value, notification_prefs: Object.fromEntries(prefs.map((box) => [box.value, box.checked])) };
        if (email.value.trim().toLowerCase() !== user.email) body.email = email.value;
        if (await attempt(() => api('PATCH', '/users/me', { body }), body.email ? 'Saved. Enter the code sent to your new address to confirm it.' : 'Saved.')) render();
      } }, h('h3', {}, 'Profile'), field('Display name', name), field('Email address', email, { hint: 'A change takes effect after you confirm the new address.' }),
        h('fieldset', {}, h('legend', {}, 'Email me when'), Object.keys(prefLabels).map((key, index) => h('div', {}, h('label', { class: 'check' }, prefs[index], prefLabels[key])))),
        h('button', { type: 'submit', class: 'btn' }, 'Save')),
      user.pending_email ? h('form', { class: 'card', onsubmit: async (event) => {
        event.preventDefault();
        if (await attempt(() => post('/users/me/confirm-email', { email: user.pending_email, code: code.value }), 'Email address changed.')) render();
      } }, h('h3', {}, 'Confirm ' + user.pending_email), h('div', { class: 'row end' }, field('Verification code', code), h('div', { class: 'field' }, h('button', { type: 'submit', class: 'btn' }, 'Confirm')))) : null,
      h('form', { class: 'card', onsubmit: async (event) => {
        event.preventDefault();
        if (await attempt(() => post('/users/me/password', { current_password: current.value, new_password: next.value }), 'Password changed. Other sessions were signed out.')) render();
      } }, h('h3', {}, 'Change password'), field('Current password', current),
        field('New password', next, { hint: 'At least 8 characters with an uppercase letter, a number and a special character.' }), h('button', { type: 'submit', class: 'btn' }, 'Change password')),
      profile ? h('div', { class: 'card' }, h('h3', {}, 'My developer profile'),
        h('dl', { class: 'meta' }, h('dt', {}, 'Availability'), h('dd', {}, profile.availability), h('dt', {}, 'Open bugs'), h('dd', {}, profile.open_bugs + ' of ' + profile.max_capacity),
          h('dt', {}, 'Verified fixes'), h('dd', {}, profile.resolved_count), h('dt', {}, 'Skills'), h('dd', {}, profile.skills.join(', ') || '—'), h('dt', {}, 'Components'), h('dd', {}, profile.components.join(', ') || '—')),
        h('p', { class: 'muted' }, 'Declared skills, components and availability are maintained by an Administrator.')) : null];
  };

  // ---- messages from the extension host ------------------------------------------
  window.addEventListener('message', (event) => {
    const message = event.data;
    switch (message.type) {
      case 'response': {
        const waiting = pending.get(message.id);
        if (!waiting) return;
        pending.delete(message.id);
        return message.ok ? waiting.resolve(message.data) : waiting.reject(message.data);
      }
      case 'init':
        Object.assign(state, { signedIn: message.signedIn, projectId: message.projectId === undefined ? null : message.projectId, serverUrl: message.serverUrl });
        if (message.route) state.route = message.route;
        return boot();
      case 'session': {
        const was = state.signedIn;
        state.signedIn = message.signedIn;
        if (message.projectId !== undefined) state.projectId = message.projectId;
        if (was !== message.signedIn) {
          if (!message.signedIn) { state.user = null; authMode = { form: 'signin', email: '', code: '', note: was ? 'You have been signed out.' : '' }; }
          state.route = { view: 'bugs' };
          return boot();
        }
        return undefined;
      }
      case 'navigate':
        return navigate(message.route);
      case 'unread':
        state.unread = message.unread;
        return undefined; // shown on the next render; the host raises the toast
      case 'refresh':
        return boot();
    }
    return undefined;
  });

  vscode.postMessage({ type: 'ready' });
})();
