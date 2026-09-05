/* ==========================================================================
   Switchboard — app.js
   Vanilla JS, no build, no external requests. One namespace: window.Switchboard.

   Sections:
     1. util        DOM helpers, formatting, toast, modal
     2. api         fetch wrapper + endpoint map (401 anywhere -> login)
     3. state       in-memory state, router, polling timers
     4. render/shell + login
     5. render/clouds
     6. render/usecases
     7. markdown    safe renderer (no raw HTML passthrough)
     8. highlight   tokeniser-based syntax colouring
     9. mock        ?mock=1 fixture backend exercising every state
   ========================================================================== */
(function () {
'use strict';

const SB = {};
window.Switchboard = SB;

const PARAMS = new URLSearchParams(location.search);
const MOCK = PARAMS.get('mock') === '1';
const KEEP_QUERY = location.search; // preserved across hash navigation

/* ==========================================================================
   1. util
   ========================================================================== */

function $(sel, root) { return (root || document).querySelector(sel); }
function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

/** h('div.cls#id', {attrs}, ...children) — children may be strings, nodes, arrays, null. */
function h(tag, attrs, ...children) {
  if (attrs && (typeof attrs === 'string' || attrs instanceof Node || Array.isArray(attrs))) {
    children.unshift(attrs); attrs = null;
  }
  const el = document.createElement(tag);
  if (attrs) {
    for (const k of Object.keys(attrs)) {
      const v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k === 'style') el.style.cssText = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'html') el.innerHTML = v;            // only ever fed pre-escaped markup
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}
function append(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
}
function icon(id, cls) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  if (cls) svg.setAttribute('class', cls);
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + id);
  svg.appendChild(use);
  return svg;
}
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

const USD = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2 });
function fmtUsd(n) { return typeof n === 'number' ? USD.format(n) : '—'; }
function fmtNum(n) { return typeof n === 'number' ? n.toLocaleString('en-US') : '—'; }
function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}
function fmtClock(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleTimeString('en-GB', { hour12: false });
}
function fmtRel(iso, now) {
  if (!iso) return '—';
  const d = new Date(iso); if (isNaN(d)) return String(iso);
  let s = Math.round(((now || Date.now()) - d.getTime()) / 1000);
  const future = s < 0; s = Math.abs(s);
  let out;
  if (s < 45) out = s + ' s';
  else if (s < 3600) out = Math.round(s / 60) + ' min';
  else if (s < 86400) out = Math.round(s / 3600) + ' h';
  else out = Math.round(s / 86400) + ' d';
  return future ? 'in ' + out : out + ' ago';
}
function fmtDur(startIso, endIso) {
  if (!startIso) return '';
  const a = new Date(startIso), b = endIso ? new Date(endIso) : new Date();
  if (isNaN(a) || isNaN(b)) return '';
  const s = Math.max(0, Math.round((b - a) / 1000));
  if (s < 60) return s + 's';
  return Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
}
function short(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

function toast(msg, isError) {
  const root = $('#toast-root');
  const t = h('div', { class: 'toast' + (isError ? ' is-error' : ''), role: 'status' }, msg);
  root.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 320); }, isError ? 7000 : 3500);
}

/** Modal with focus trap; resolves true on confirm, false on cancel/escape. */
function modal(opts) {
  return new Promise(resolve => {
    const root = $('#modal-root');
    const prev = document.activeElement;
    let done = false;
    function finish(v) {
      if (done) return; done = true;
      document.removeEventListener('keydown', onKey, true);
      clear(root);
      if (prev && prev.focus) prev.focus();
      resolve(v);
    }
    const cancelBtn = h('button', { class: 'btn', type: 'button', onclick: () => finish(false) }, opts.cancelLabel || 'Cancel');
    const okBtn = h('button', { class: 'btn ' + (opts.danger ? 'btn-danger' : 'btn-primary'), type: 'button', onclick: () => finish(true) }, opts.confirmLabel || 'Confirm');
    const dlg = h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'modal-title' },
      h('div', { class: 'modal-head' },
        h('div', { class: 'modal-title', id: 'modal-title' }, opts.title),
        opts.sub ? h('div', { class: 'modal-sub' }, opts.sub) : null),
      h('div', { class: 'modal-body' }, opts.body),
      h('div', { class: 'modal-foot' }, cancelBtn, opts.confirmLabel === null ? null : okBtn));
    const back = h('div', { class: 'modal-backdrop', onclick: e => { if (e.target === back) finish(false); } }, dlg);
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      if (e.key === 'Tab') {
        const f = $$('button, [href], input, textarea, select, [tabindex]:not([tabindex="-1"])', dlg).filter(x => !x.disabled);
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', onKey, true);
    root.appendChild(back);
    (opts.confirmLabel === null ? cancelBtn : okBtn).focus();
  });
}

/* ==========================================================================
   2. api
   ========================================================================== */

class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code || 'error'; }
}

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch (e) {
    throw new ApiError('Switchboard backend unreachable — ' + e.message, 0, 'network');
  }
  if (res.status === 401 && path !== '/api/auth/login') {
    onUnauthorised();
    throw new ApiError('Session expired — sign in again.', 401, 'unauthorised');
  }
  if (res.status === 204) return null;
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
  if (!res.ok) {
    const msg = (data && data.error) || (res.status + ' ' + res.statusText);
    throw new ApiError(msg, res.status, data && data.code);
  }
  return data;
}

const liveApi = {
  me:         () => request('GET', '/api/auth/me'),
  login:      (password) => request('POST', '/api/auth/login', { password }),
  logout:     () => request('POST', '/api/auth/logout'),
  providers:  () => request('GET', '/api/providers'),
  connect:    (id, creds) => request('POST', '/api/providers/' + id + '/connect', creds),
  disconnect: (id) => request('DELETE', '/api/providers/' + id),
  inventory:  (id, refresh) => request('GET', '/api/providers/' + id + '/inventory?refresh=' + (refresh ? 1 : 0)),
  usecases:   () => request('GET', '/api/usecases'),
  usecase:    (id) => request('GET', '/api/usecases/' + encodeURIComponent(id)),
  flip:       (id, action) => request('POST', '/api/usecases/' + encodeURIComponent(id) + '/' + action),
  refreshUsecase: (id) => request('POST', '/api/usecases/' + encodeURIComponent(id) + '/refresh'),
  codeTree:   (id) => request('GET', '/api/usecases/' + encodeURIComponent(id) + '/code'),
  codeFile:   (id, path) => request('GET', '/api/usecases/' + encodeURIComponent(id) + '/code?path=' + encodeURIComponent(path)),
  job:        (jobId) => request('GET', '/api/jobs/' + encodeURIComponent(jobId)),
  jobLog:     (jobId, since) => request('GET', '/api/jobs/' + encodeURIComponent(jobId) + '/log?since=' + (since || 0)),
};

let api = liveApi; // swapped for the mock in section 9 when ?mock=1

/* ==========================================================================
   3. state + router + polling
   ========================================================================== */

const state = {
  route: 'clouds',
  authed: null,                 // null = unknown (booting)
  providers: null, providersErr: null, providersLoading: false,
  inventory: null, invErr: null, invLoading: false,
  connect: null,                // {stage:'form'|'checking'|'done', report, shown, error, busy}
  openRegion: null,
  usecases: null, ucErr: null, ucLoading: false,
  expanded: null,               // use case id
  details: {},                  // id -> detail (+ _loading/_err)
  jobs: {},                     // jobId -> {job, lines, next}
  jobFor: {},                   // usecaseId -> jobId being tailed
  code: {},                     // usecaseId -> {open, tree, commit, active, file, loading, err}
  flipping: {},                 // usecaseId -> true while POST in flight
};
SB.state = state;

const timers = { usecases: null, jobs: {}, clock: null };

function stopTimer(name) { if (timers[name]) { clearInterval(timers[name]); timers[name] = null; } }
function stopJobTimers() { Object.keys(timers.jobs).forEach(id => { clearInterval(timers.jobs[id]); delete timers.jobs[id]; }); }

function onUnauthorised() {
  state.authed = false;
  stopTimer('usecases'); stopJobTimers();
  if (location.hash !== '#/login') location.hash = '#/login';
  else render();
}

function routeFromHash() {
  const m = /^#\/([a-z]+)/.exec(location.hash);
  return m ? m[1] : null;
}
function navigate(route) { location.hash = '#/' + route; }

async function boot() {
  const theme = PARAMS.get('theme');
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
  if (MOCK) { api = SB.mock.api; $('#mock-badge').hidden = false; }
  window.addEventListener('hashchange', onRoute);
  $('#signout').addEventListener('click', async () => {
    try { await api.logout(); } catch (e) { /* cookie may already be gone */ }
    onUnauthorised();
  });
  document.addEventListener('keydown', e => {
    if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    if (e.key === 'g') SB._g = Date.now();
    else if (SB._g && Date.now() - SB._g < 800) {
      if (e.key === 'c') navigate('clouds');
      if (e.key === 'u') navigate('usecases');
      SB._g = 0;
    }
  });
  try { await api.me(); state.authed = true; }
  catch (e) { state.authed = false; }
  if (MOCK && state.authed) SB.mock.scene(state);
  onRoute();
  if (MOCK && state.authed) SB.mock.sceneAfter(state);
  timers.clock = setInterval(tickClock, 15000);
}

function onRoute() {
  const r = routeFromHash();
  if (!state.authed) {
    if (r !== 'login') { location.hash = '#/login'; return; }
    state.route = 'login';
  } else {
    if (!r || r === 'login') { location.hash = '#/clouds'; return; }
    state.route = (r === 'usecases') ? 'usecases' : 'clouds';
  }
  stopTimer('usecases');
  if (state.route === 'usecases') {
    loadUsecases();
    timers.usecases = setInterval(() => loadUsecases(true), 15000);
  } else {
    stopJobTimers();
  }
  if (state.route === 'clouds') {
    if (!state.providers) loadProviders();
  }
  render();
  $('#view').focus({ preventScroll: true });
}

function tickClock() {
  // refresh relative timestamps without re-rendering everything
  $$('[data-rel]').forEach(el => { el.textContent = fmtRel(el.dataset.rel); });
}

/* ==========================================================================
   4. render/shell + login
   ========================================================================== */

function render() {
  const top = $('#topbar');
  top.hidden = state.route === 'login';
  $$('.nav-link').forEach(a => {
    a.href = KEEP_QUERY + a.getAttribute('href').replace(/^.*#/, '#');
    if (a.dataset.route === state.route) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  $('.wordmark').href = KEEP_QUERY + '#/clouds';
  const view = clear($('#view'));
  if (state.route === 'login') view.appendChild(renderLogin());
  else if (state.route === 'clouds') view.appendChild(renderClouds());
  else view.appendChild(renderUsecases());
}

function renderLogin() {
  const err = h('div', { class: 'form-error', role: 'alert', hidden: true });
  const input = h('input', { class: 'input', id: 'password', type: 'password', autocomplete: 'current-password', required: true, autofocus: true, placeholder: '••••••••••••' });
  const btn = h('button', { class: 'btn btn-primary', type: 'submit' }, 'Sign in');
  const form = h('form', { class: 'login', onsubmit: async e => {
    e.preventDefault();
    err.hidden = true; btn.classList.add('is-busy'); btn.disabled = true;
    try {
      await api.login(input.value);
      state.authed = true;
      loadProviders();
      navigate('clouds');
    } catch (ex) {
      err.textContent = ex.status === 401 ? 'That password was not accepted.' : ex.message;
      err.hidden = false; input.select();
    } finally { btn.classList.remove('is-busy'); btn.disabled = false; }
  } },
    h('div', { class: 'login-mark' }, icon('jack'), h('span', { class: 'wordmark-text' }, 'Switchboard')),
    h('p', { class: 'login-sub' }, 'Operator access. Plug in a cloud, flip a use case on.'),
    h('div', { class: 'field' }, h('label', { for: 'password' }, 'Password'), input),
    err,
    btn,
    h('p', { class: 'login-foot' }, MOCK ? 'mock mode — any non-empty password except "wrong"' : 'session lasts 12 h · lab network only'));
  setTimeout(() => input.focus(), 0);
  return h('div', { class: 'login-wrap' }, form);
}

/* ==========================================================================
   5. render/clouds
   ========================================================================== */

const PROVIDER_DEFS = [
  { id: 'aws',   name: 'Amazon Web Services', wired: true },
  { id: 'gcp',   name: 'Google Cloud',         wired: false },
  { id: 'azure', name: 'Microsoft Azure',      wired: false },
];

async function loadProviders() {
  state.providersLoading = true; state.providersErr = null;
  if (state.route === 'clouds') render();
  try {
    state.providers = await api.providers();
    const aws = providerById('aws');
    if (aws && aws.status === 'connected' && !state.inventory) loadInventory(false);
  } catch (e) { if (e.status !== 401) state.providersErr = e.message; }
  state.providersLoading = false;
  if (state.route === 'clouds') render();
}
function providerById(id) { return (state.providers || []).find(p => p.id === id) || null; }

async function loadInventory(refresh) {
  state.invLoading = true; state.invErr = null;
  if (state.route === 'clouds') render();
  try { state.inventory = await api.inventory('aws', refresh); }
  catch (e) { if (e.status !== 401) state.invErr = e.message; }
  state.invLoading = false;
  if (state.route === 'clouds') render();
}

function renderClouds() {
  const root = h('div');
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null, h('h1', { class: 'page-title' }, 'Clouds'), h('p', { class: 'page-sub' }, 'Plug a provider into the panel. Switchboard checks the line, then keeps an inventory of what is running.')),
    state.providersLoading ? h('span', { class: 'loading' }, 'Reading panel') : null));

  if (state.providersErr) {
    root.appendChild(h('div', { class: 'state-box is-error' },
      h('div', { class: 'title' }, 'Could not read the patch panel'),
      h('div', null, state.providersErr),
      h('button', { class: 'btn', type: 'button', onclick: () => loadProviders() }, 'Retry')));
    return root;
  }

  // Patch panel
  const patch = h('div', { class: 'patch', role: 'list' });
  for (const def of PROVIDER_DEFS) patch.appendChild(renderJack(def, providerById(def.id)));
  root.appendChild(patch);

  if (state.connect) root.appendChild(renderConnect());

  const aws = providerById('aws');
  if (aws && aws.status === 'connected' && !(state.connect && state.connect.stage !== 'done')) {
    root.appendChild(renderInventory(aws));
  } else if (!state.connect && state.providers) {
    root.appendChild(h('div', { class: 'section' },
      h('div', { class: 'state-box' },
        h('div', { class: 'title' }, 'No line connected'),
        h('div', null, 'Plug in AWS to validate credentials and pull an inventory across every enabled region.'),
        h('button', { class: 'btn btn-primary', type: 'button', onclick: () => openConnect() }, icon('plug'), 'Plug in AWS'))));
  } else if (!state.providers) {
    root.appendChild(h('div', { class: 'section' }, h('div', { class: 'skeleton', style: 'height:140px' })));
  }
  return root;
}

function socketSvg(live) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 72 72'); svg.setAttribute('class', 'jack-socket'); svg.setAttribute('aria-hidden', 'true');
  const mk = (tag, attrs) => { const e = document.createElementNS(ns, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); svg.appendChild(e); return e; };
  mk('circle', { cx: 36, cy: 36, r: 30, class: 'ring' });
  mk('circle', { cx: 36, cy: 36, r: 18, class: 'bore' });
  if (live) {
    mk('path', { d: 'M36 36 L36 6', class: 'cable' });
    mk('circle', { cx: 36, cy: 36, r: 9, class: 'pin' });
  } else {
    mk('circle', { cx: 36, cy: 36, r: 6, class: 'pin' });
  }
  return svg;
}

function renderJack(def, p) {
  const connected = !!(p && p.status === 'connected');
  const cls = 'jack' + (connected ? ' is-live' : '') + (!def.wired ? ' is-dead' : '');
  const jack = h('div', { class: cls, role: 'listitem', 'aria-label': def.name + (connected ? ', connected' : def.wired ? ', unplugged' : ', not wired yet') });
  jack.appendChild(socketSvg(connected));
  const body = h('div');
  body.appendChild(h('div', { class: 'jack-name' }, def.name));
  if (!def.wired) {
    body.appendChild(h('div', { class: 'jack-status' }, h('span', { class: 'lamp unknown' }), 'Not wired yet'));
    body.appendChild(h('div', { class: 'jack-note' }, 'Provider module not built. The jack is here so the panel is honest about what it can and cannot connect.'));
    body.appendChild(h('div', { class: 'jack-actions' }, h('button', { class: 'btn btn-sm', type: 'button', disabled: true, 'aria-disabled': 'true' }, icon('plug'), 'Plug in')));
  } else if (connected) {
    body.appendChild(h('div', { class: 'jack-status' }, h('span', { class: 'lamp on' }), h('span', { class: 'state-word on' }, 'Line connected'),
      p.connected_at ? h('span', { class: 'mono', style: 'color:var(--text-faint);font-size:12px' }, 'since ', h('span', { dataset: { rel: p.connected_at } }, fmtRel(p.connected_at))) : null));
    const idn = p.identity || {};
    body.appendChild(h('div', { class: 'jack-id' }, h('div', null, 'account ', idn.account || '—', idn.alias ? ' (' + idn.alias + ')' : ''), h('div', { class: 'jack-arn', title: idn.arn || '' }, idn.arn || '')));
    body.appendChild(h('div', { class: 'jack-actions' },
      h('button', { class: 'btn btn-sm btn-danger', type: 'button', onclick: () => disconnectAws() }, 'Disconnect')));
  } else {
    body.appendChild(h('div', { class: 'jack-status' }, h('span', { class: 'lamp' }), h('span', { class: 'state-word off' }, 'Unplugged')));
    body.appendChild(h('div', { class: 'jack-note' }, 'Access key, secret and an optional session token. Checked live, stored encrypted.'));
    body.appendChild(h('div', { class: 'jack-actions' },
      h('button', { class: 'btn btn-sm btn-primary', type: 'button', onclick: () => openConnect() }, icon('plug'), 'Plug in')));
  }
  jack.appendChild(body);
  return jack;
}

function openConnect() {
  state.connect = { stage: 'form', report: null, shown: 0, error: null, busy: false };
  render();
  setTimeout(() => { const i = $('#ak'); if (i) i.focus(); }, 0);
}

async function disconnectAws() {
  const ok = await modal({
    title: 'Disconnect AWS?',
    sub: 'Switchboard forgets the stored credentials and the cached inventory. Nothing in the cloud is touched.',
    body: h('p', { style: 'color:var(--text-dim)' }, 'Use cases on this provider become unavailable until a line is plugged in again.'),
    confirmLabel: 'Disconnect', danger: true,
  });
  if (!ok) return;
  try {
    await api.disconnect('aws');
    state.inventory = null; state.openRegion = null; state.connect = null;
    toast('AWS line disconnected.');
    await loadProviders();
  } catch (e) { if (e.status !== 401) toast(e.message, true); }
}

function renderConnect() {
  const c = state.connect;
  const wrap = h('div', { class: 'connect panel' });
  if (c.stage === 'form') {
    const err = h('div', { class: 'form-error', role: 'alert', hidden: !c.error }, c.error || '');
    const f = {};
    const form = h('form', { onsubmit: e => { e.preventDefault(); submitConnect(f); } },
      h('div', { class: 'connect-title' }, 'Plug in Amazon Web Services'),
      h('div', { class: 'connect-sub' }, 'Paste the credentials from your SSO session. They are checked against STS, EC2, Pricing and the state bucket before anything is stored.'),
      h('div', { class: 'form-grid' },
        h('div', { class: 'field' }, h('label', { for: 'ak' }, 'Access key ID'), f.ak = h('input', { class: 'input', id: 'ak', autocomplete: 'off', spellcheck: 'false', required: true, placeholder: 'ASIA…' })),
        h('div', { class: 'field' }, h('label', { for: 'sk' }, 'Secret access key'), f.sk = h('input', { class: 'input', id: 'sk', type: 'password', autocomplete: 'off', required: true, placeholder: '••••••••' })),
        h('div', { class: 'field span-2' }, h('label', { for: 'st' }, 'Session token ', h('span', { class: 'hint' }, '(optional — required for SSO / temporary credentials)')), f.st = h('textarea', { class: 'input', id: 'st', rows: 3, autocomplete: 'off', spellcheck: 'false', placeholder: 'IQoJb3JpZ2luX2Vj…' })),
        h('div', { class: 'field span-2' }, h('label', { for: 'rg' }, 'Regions ', h('span', { class: 'hint' }, '(optional, comma separated; blank = every enabled region)')), f.rg = h('input', { class: 'input', id: 'rg', autocomplete: 'off', spellcheck: 'false', placeholder: 'eu-central-1, eu-west-1' }))),
      err,
      h('div', { class: 'form-actions' },
        f.btn = h('button', { class: 'btn btn-primary' + (c.busy ? ' is-busy' : ''), type: 'submit', disabled: c.busy }, icon('plug'), 'Plug in and check'),
        h('button', { class: 'btn btn-ghost', type: 'button', onclick: () => { state.connect = null; render(); } }, 'Cancel')));
    wrap.appendChild(h('div', { class: 'panel-body' }, form));
    return wrap;
  }
  // checking / done
  const rep = c.report;
  const list = h('ol', { class: 'checks', 'aria-live': 'polite' });
  const names = rep ? rep.checks.map(x => x.name) : PLACEHOLDER_CHECKS;
  names.forEach((name, i) => {
    const chk = rep ? rep.checks[i] : null;
    let cls = 'check';
    if (i < c.shown && chk) cls += ' is-done ' + (chk.ok ? 'ok' : 'bad');
    else if (i === c.shown && c.stage === 'checking') cls += ' is-running';
    list.appendChild(h('li', { class: cls },
      h('span', { class: 'check-mark' }, chk && i < c.shown ? icon(chk.ok ? 'check' : 'x') : null),
      h('span', { class: 'check-name' }, name),
      h('span', { class: 'check-detail' }, chk && i < c.shown ? (chk.detail || '') : '')));
  });
  const body = h('div', { class: 'panel-body' },
    h('div', { class: 'connect-title' }, 'Checking the line'),
    h('div', { class: 'connect-sub' }, rep && rep.identity && rep.identity.account ? ['account ', h('span', { class: 'mono' }, rep.identity.account)] : 'Running each check against AWS…'),
    list);
  if (c.stage === 'done' && rep) {
    const failed = rep.checks.filter(x => !x.ok);
    if (rep.ok) {
      body.appendChild(h('div', { class: 'check-summary ok' },
        h('div', null, h('strong', null, 'Line connected'), h('div', { class: 'sub' }, 'Credentials stored encrypted. Pulling the inventory now.')),
        h('span', { class: 'lamp on lg' })));
    } else {
      body.appendChild(h('div', { class: 'check-summary bad' },
        h('div', null, h('strong', null, failed.length === 1 ? 'One check failed — nothing was stored' : failed.length + ' checks failed — nothing was stored'),
          h('div', { class: 'sub' }, failed.map(x => x.name + (x.detail ? ': ' + x.detail : '')).join(' · '))),
        h('div', { style: 'display:flex;gap:8px' },
          h('button', { class: 'btn', type: 'button', onclick: () => { state.connect = { stage: 'form', report: null, shown: 0, error: null, busy: false }; render(); } }, 'Try again'),
          h('button', { class: 'btn btn-ghost', type: 'button', onclick: () => { state.connect = null; render(); } }, 'Close'))));
    }
  } else if (c.stage === 'error') {
    body.appendChild(h('div', { class: 'check-summary bad' },
      h('div', null, h('strong', null, 'Could not run the checks'), h('div', { class: 'sub' }, c.error)),
      h('button', { class: 'btn', type: 'button', onclick: () => { state.connect = { stage: 'form', report: null, shown: 0, error: null, busy: false }; render(); } }, 'Back')));
  }
  wrap.appendChild(body);
  return wrap;
}

const PLACEHOLDER_CHECKS = ['Credentials valid (STS)', 'Can list regions', 'Can describe EC2', 'Pricing API reachable', 'State bucket ready', 'Session token expiry'];

async function submitConnect(f) {
  const c = state.connect;
  const regions = f.rg.value.split(',').map(s => s.trim()).filter(Boolean);
  const creds = {
    access_key_id: f.ak.value.trim(),
    secret_access_key: f.sk.value,
    session_token: f.st.value.trim() || null,
    regions: regions.length ? regions : null,
  };
  c.busy = true; c.error = null; f.btn.classList.add('is-busy'); f.btn.disabled = true;
  // Show the checklist immediately, spinning on line 1, while the request is in flight.
  state.connect = { stage: 'checking', report: null, shown: 0, error: null, busy: true };
  render();
  let report;
  try { report = await api.connect('aws', creds); }
  catch (e) {
    if (e.status === 401) return;
    state.connect = { stage: 'error', report: null, shown: 0, error: e.message, busy: false };
    render(); return;
  }
  const cc = state.connect;
  if (!cc || cc.stage !== 'checking') return; // cancelled meanwhile
  cc.report = report;
  // Fill in one line at a time. Stop early on the first failure so the eye lands on it.
  for (let i = 0; i < report.checks.length; i++) {
    cc.shown = i; render();
    await sleep(i === 0 ? 300 : 380);
    if (state.connect !== cc) return;
    cc.shown = i + 1; render();
    if (!report.checks[i].ok && report.ok === false) {
      const rest = report.checks.slice(i + 1);
      // Non-required checks that still ran are shown after a beat; required failures end the sequence.
      if (rest.some(x => !x.ok)) { /* continue to reveal others */ } else { break; }
    }
    await sleep(90);
  }
  cc.shown = report.checks.length;
  cc.stage = 'done'; cc.busy = false;
  render();
  if (report.ok) {
    await sleep(900);
    if (state.connect !== cc) return;
    state.connect = null;
    await loadProviders();
    await loadInventory(true);
  }
}

function renderInventory(p) {
  const wrap = h('div', { class: 'section' });
  const inv = state.inventory;
  const head = h('div', { class: 'section-head' },
    h('div', { class: 'inv-toolbar' },
      h('span', { class: 'section-title' }, 'Inventory'),
      inv ? h('span', { class: 'inv-meta' },
        'generated ', h('span', { class: 'mono', title: inv.generated_at }, fmtTime(inv.generated_at)),
        h('span', { style: 'color:var(--text-faint)' }, '(', h('span', { dataset: { rel: inv.generated_at } }, fmtRel(inv.generated_at)), ')'),
        inv.stale ? h('span', { class: 'badge badge-stale', title: 'Served from cache older than 10 minutes' }, 'STALE') : null) : null,
      state.invLoading ? h('span', { class: 'loading' }, 'Scanning regions') : null),
    h('button', { class: 'btn btn-sm', type: 'button', disabled: state.invLoading, onclick: () => loadInventory(true) }, icon('refresh'), 'Refresh'));
  wrap.appendChild(head);

  if (state.invErr) {
    wrap.appendChild(h('div', { class: 'state-box is-error' },
      h('div', { class: 'title' }, 'Inventory failed'), h('div', null, state.invErr),
      h('button', { class: 'btn', type: 'button', onclick: () => loadInventory(true) }, 'Retry')));
    return wrap;
  }
  if (!inv) {
    wrap.appendChild(h('div', { class: 'totals' }, [1, 2, 3, 4, 5, 6].map(() => h('div', { class: 'total' }, h('div', { class: 'skeleton', style: 'height:12px;width:60%' }), h('div', { class: 'skeleton', style: 'height:26px;width:40%;margin-top:8px' })))));
    wrap.appendChild(h('div', { class: 'regions', style: 'margin-top:16px' }, (p.regions || new Array(8).fill('')).map(() => h('div', { class: 'skeleton', style: 'height:90px' }))));
    return wrap;
  }

  const t = inv.totals || {};
  wrap.appendChild(h('div', { class: 'totals' },
    total('Instances', t.instances, typeof t.running === 'number' ? (t.running === t.instances && t.instances > 0 ? 'all running' : t.running + ' running') : null),
    total('VPCs', t.vpcs),
    total('NAT gateways', t.nat_gateways),
    total('Elastic IPs', t.eips),
    total('Volumes', t.volumes_gb, 'GB'),
    total('Monthly', inv.cost ? fmtUsd(inv.cost.monthly_usd) : '—', inv.cost ? inv.cost.currency : null)));

  const grid = h('div', { class: 'grid-2', style: 'margin-top:20px' });
  const left = h('div');
  // region grid: union of scanned regions (inventory) and provider regions, so empty tiles are present
  const regionMap = new Map();
  (p.regions || []).forEach(r => regionMap.set(r, { region: r, instances: [], vpcs: [], nat_gateways: [], eips: [], volumes: [] }));
  (inv.regions || []).forEach(r => regionMap.set(r.region, r));
  const regions = Array.from(regionMap.values()).sort((a, b) => weight(b) - weight(a) || a.region.localeCompare(b.region));
  left.appendChild(h('div', { class: 'section-head' }, h('span', { class: 'section-title' }, regions.length + ' regions scanned')));
  const rg = h('div', { class: 'regions' });
  for (const r of regions) rg.appendChild(renderRegionTile(r));
  left.appendChild(rg);
  grid.appendChild(left);

  const right = h('div');
  right.appendChild(renderCost(inv));
  right.appendChild(renderGroups(inv));
  grid.appendChild(right);
  wrap.appendChild(grid);
  const open = regions.find(r => r.region === state.openRegion);
  if (open) wrap.appendChild(renderRegionDetail(open));
  return wrap;
}
function total(k, v, sub) {
  return h('div', { class: 'total' }, h('div', { class: 'total-k' }, k), h('div', { class: 'total-v' }, typeof v === 'number' ? fmtNum(v) : (v == null ? '—' : v), sub ? h('small', null, sub) : null));
}
function weight(r) { return (r.instances || []).length * 10 + (r.vpcs || []).filter(v => !v.default).length * 3 + (r.nat_gateways || []).length + (r.eips || []).length; }
function isEmptyRegion(r) {
  return !(r.instances || []).length && !(r.vpcs || []).some(v => !v.default) && !(r.nat_gateways || []).length && !(r.eips || []).length && !(r.volumes || []).length;
}

function renderRegionTile(r) {
  const empty = isEmptyRegion(r);
  const defaultOnly = empty && (r.vpcs || []).some(v => v.default);
  const isOpen = state.openRegion === r.region;
  const tile = h('button', { class: 'region' + (empty ? ' is-empty' : '') + (isOpen ? ' is-open' : ''), type: 'button', 'aria-expanded': String(isOpen), disabled: empty,
    onclick: () => { state.openRegion = isOpen ? null : r.region; render(); } },
    h('div', { class: 'region-name' }, r.region, (r.instances || []).some(i => i.state === 'running') ? h('span', { class: 'lamp on', title: 'running instances' }) : null),
    h('div', { class: 'region-counts' },
      cnt((r.instances || []).length, 'inst'), cnt((r.vpcs || []).length, 'vpc'), cnt((r.nat_gateways || []).length, 'nat'), cnt((r.eips || []).length, 'eip')),
    empty ? h('div', { class: 'region-empty-note' }, defaultOnly ? 'default VPC only' : 'nothing running') : null);
  return tile;
}
function cnt(n, k) { return h('div', { class: 'region-count' }, h('b', null, n), h('span', null, k)); }

function renderRegionDetail(r) {
  const p = h('div', { class: 'region-detail panel' });
  p.appendChild(h('div', { class: 'panel-head' }, h('span', { class: 'section-title' }, r.region),
    h('button', { class: 'btn btn-ghost btn-sm', type: 'button', onclick: () => { state.openRegion = null; render(); } }, 'Close')));
  const body = h('div', { class: 'panel-body' });
  const tbl = h('table', { class: 'tbl' });
  const rows = [];
  if ((r.instances || []).length) {
    rows.push(groupRow('Instances'));
    rows.push(h('tr', null, h('th', null, 'Name'), h('th', null, 'ID'), h('th', null, 'Type'), h('th', null, 'State'), h('th', null, 'Private IP'), h('th', null, 'Public IP'), h('th', null, 'Launched')));
    for (const i of r.instances) {
      const tags = Object.entries(i.tags || {}).filter(([k]) => k !== 'Name').map(([k, v]) => k + '=' + v).join('  ');
      rows.push(h('tr', null,
        h('td', null, i.name || h('span', { class: 'dim' }, 'unnamed'), tags ? h('div', { class: 'mono dim', style: 'font-size:11px;margin-top:2px' }, tags) : null),
        h('td', { class: 'mono' }, i.id), h('td', { class: 'mono' }, i.type),
        h('td', null, h('span', { class: 'state-word ' + (i.state === 'running' ? 'on' : i.state === 'stopped' ? 'off' : 'unknown') }, i.state)),
        h('td', { class: 'mono' }, i.private_ip || '—'), h('td', { class: 'mono' }, i.public_ip || h('span', { class: 'dim' }, '—')),
        h('td', { class: 'dim' }, h('span', { dataset: { rel: i.launched }, title: fmtTime(i.launched) }, fmtRel(i.launched)))));
    }
  }
  if ((r.vpcs || []).length) {
    rows.push(groupRow('VPCs'));
    rows.push(h('tr', null, h('th', null, 'Name'), h('th', null, 'ID'), h('th', null, 'CIDR'), h('th', null, '')));
    for (const v of r.vpcs) rows.push(h('tr', null, h('td', null, v.name || h('span', { class: 'dim' }, 'unnamed')), h('td', { class: 'mono' }, v.id), h('td', { class: 'mono' }, v.cidr), h('td', { class: 'dim' }, v.default ? 'default VPC' : '')));
  }
  if ((r.nat_gateways || []).length) {
    rows.push(groupRow('NAT gateways'));
    rows.push(h('tr', null, h('th', null, 'ID'), h('th', null, 'VPC'), h('th', null, 'State'), h('th', null, 'Public IP')));
    for (const n of r.nat_gateways) rows.push(h('tr', null, h('td', { class: 'mono' }, n.id), h('td', { class: 'mono' }, n.vpc), h('td', null, n.state), h('td', { class: 'mono' }, n.public_ip || '—')));
  }
  if ((r.eips || []).length) {
    rows.push(groupRow('Elastic IPs'));
    rows.push(h('tr', null, h('th', null, 'IP'), h('th', null, 'Attached'), h('th', null, 'Instance')));
    for (const e of r.eips) rows.push(h('tr', null, h('td', { class: 'mono' }, e.ip), h('td', null, e.attached ? 'yes' : h('span', { class: 'state-word bad' }, 'no — billed idle')), h('td', { class: 'mono' }, e.instance || '—')));
  }
  if ((r.volumes || []).length) {
    rows.push(groupRow('Volumes'));
    rows.push(h('tr', null, h('th', null, 'ID'), h('th', null, 'Type'), h('th', { class: 'num' }, 'Size'), h('th', null, 'Attached')));
    for (const v of r.volumes) rows.push(h('tr', null, h('td', { class: 'mono' }, v.id), h('td', { class: 'mono' }, v.type), h('td', { class: 'num' }, v.size_gb + ' GB'), h('td', null, v.attached ? 'yes' : 'no')));
  }
  tbl.appendChild(h('tbody', null, rows));
  body.appendChild(h('div', { style: 'overflow-x:auto' }, tbl));
  p.appendChild(body);
  return p;
}
function groupRow(t) { return h('tr', null, h('td', { class: 'tbl-group', colspan: 8 }, t)); }

function renderCost(inv) {
  const c = inv.cost;
  const p = h('div', { class: 'panel' });
  p.appendChild(h('div', { class: 'panel-head' }, h('span', { class: 'section-title' }, 'Estimated cost')));
  if (!c) { p.appendChild(h('div', { class: 'panel-body', style: 'color:var(--text-faint)' }, 'No cost estimate in this inventory.')); return p; }
  p.appendChild(h('div', { class: 'cost-total' }, h('span', { class: 'amount' }, fmtUsd(c.monthly_usd)), h('span', { class: 'per' }, c.currency + ' / month')));
  if (c.method) p.appendChild(h('div', { class: 'cost-method' }, c.method));
  const lines = c.lines || [];
  if (lines.length) {
    const tbl = h('table', { class: 'tbl' });
    tbl.appendChild(h('thead', null, h('tr', null, h('th', null, 'Item'), h('th', { class: 'num' }, 'Qty'), h('th', { class: 'num' }, 'Unit'), h('th', { class: 'num' }, 'Monthly'))));
    tbl.appendChild(h('tbody', null, lines.map(l => h('tr', null,
      h('td', null, l.item, l.region ? h('div', { class: 'mono dim', style: 'font-size:11px' }, l.region) : null),
      h('td', { class: 'num' }, fmtNum(l.qty), ' ', h('span', { class: 'dim' }, l.unit || '')),
      h('td', { class: 'num dim' }, typeof l.unit_usd === 'number' ? l.unit_usd.toFixed(l.unit_usd < 0.1 ? 4 : 3) : '—'),
      h('td', { class: 'num' }, fmtUsd(l.monthly_usd))))));
    p.appendChild(h('div', { style: 'overflow-x:auto' }, tbl));
  } else {
    p.appendChild(h('div', { class: 'panel-body', style: 'color:var(--text-faint)' }, 'Nothing billable found.'));
  }
  if (c.notes && c.notes.length) p.appendChild(h('div', { class: 'cost-notes' }, h('ul', null, c.notes.map(n => h('li', null, n)))));
  return p;
}

function renderGroups(inv) {
  const p = h('div', { class: 'panel', style: 'margin-top:16px' });
  p.appendChild(h('div', { class: 'panel-head' }, h('span', { class: 'section-title' }, 'Footprint by tag')));
  const groups = inv.groups || [];
  if (!groups.length) { p.appendChild(h('div', { class: 'panel-body', style: 'color:var(--text-faint)' }, 'No tagged resources. Use cases tag their footprint with Project=…')); return p; }
  p.appendChild(h('ul', { class: 'groups' }, groups.map(g => {
    const eq = g.key.indexOf('=');
    const k = eq > 0 ? g.key.slice(0, eq + 1) : '', v = eq > 0 ? g.key.slice(eq + 1) : g.key;
    return h('li', { class: 'group' },
      h('span', { class: 'group-key' }, h('span', { class: 'k' }, k), v),
      h('span', { class: 'group-n' }, fmtNum(g.instances), ' inst'),
      h('span', { class: 'group-cost' }, fmtUsd(g.monthly_usd)));
  })));
  return p;
}

/* ==========================================================================
   6. render/usecases
   ========================================================================== */

async function loadUsecases(quiet) {
  if (!quiet) { state.ucLoading = true; state.ucErr = null; if (state.route === 'usecases') render(); }
  try {
    const list = await api.usecases();
    state.usecases = list;
    state.ucErr = null;
    // If a use case is mid-transition and we are not yet tailing its job, pick it up.
    for (const uc of list) {
      if (/^turning_/.test(uc.state) && !state.jobFor[uc.id] && uc.last_run && uc.last_run.state === 'running' && uc.last_run.job_id) {
        trackJob(uc.id, uc.last_run.job_id);
      }
    }
  } catch (e) { if (e.status !== 401) state.ucErr = e.message; }
  state.ucLoading = false;
  if (state.route === 'usecases') render();
}

async function loadDetail(id, force) {
  const cur = state.details[id];
  if (cur && !force && !cur._err) return cur;
  state.details[id] = Object.assign({}, cur || {}, { _loading: true, _err: null });
  if (state.route === 'usecases') render();
  try {
    const d = await api.usecase(id);
    d._loading = false; d._err = null;
    state.details[id] = d;
    // tail the running job if there is one; otherwise show the last run's log once
    const running = (d.runs || []).find(r => r.state === 'running');
    if (running) trackJob(id, running.job_id);
    else if (!state.jobFor[id] && d.runs && d.runs[0] && d.runs[0].job_id) loadJobOnce(id, d.runs[0].job_id);
  } catch (e) {
    if (e.status === 401) return null;
    state.details[id] = Object.assign({}, cur || {}, { _loading: false, _err: e.message });
  }
  if (state.route === 'usecases') render();
  return state.details[id];
}

function ucById(id) { return (state.usecases || []).find(u => u.id === id) || null; }

/* ---- job tailing ---- */

function trackJob(ucId, jobId) {
  if (state.jobFor[ucId] === jobId && timers.jobs[jobId]) return;
  const old = state.jobFor[ucId];
  if (old && old !== jobId && timers.jobs[old]) { clearInterval(timers.jobs[old]); delete timers.jobs[old]; }
  state.jobFor[ucId] = jobId;
  if (!state.jobs[jobId]) state.jobs[jobId] = { job: null, lines: [], next: 0, rendered: 0, live: true };
  state.jobs[jobId].live = true;
  pollJob(ucId, jobId);
  timers.jobs[jobId] = setInterval(() => pollJob(ucId, jobId), 1500);
}

async function loadJobOnce(ucId, jobId) {
  state.jobFor[ucId] = jobId;
  const entry = state.jobs[jobId] || (state.jobs[jobId] = { job: null, lines: [], next: 0, rendered: 0, live: false });
  try {
    const [job, log] = await Promise.all([api.job(jobId), api.jobLog(jobId, 0)]);
    entry.job = job; entry.lines = log.lines || []; entry.next = log.next || entry.lines.length; entry.rendered = 0;
    if (job.state === 'running') { trackJob(ucId, jobId); return; }
  } catch (e) { entry.err = e.message; }
  if (state.route === 'usecases') render();
}

let pollBusy = {};
async function pollJob(ucId, jobId) {
  if (pollBusy[jobId]) return; pollBusy[jobId] = true;
  const entry = state.jobs[jobId];
  try {
    const [job, log] = await Promise.all([api.job(jobId), api.jobLog(jobId, entry.next)]);
    entry.job = job;
    if (log && log.lines && log.lines.length) { entry.lines.push(...log.lines); }
    if (log && typeof log.next === 'number') entry.next = log.next;
    entry.err = null;
    const card = $('[data-uc="' + CSS.escape(ucId) + '"]');
    if (card && state.expanded === ucId && !$('.term[data-job="' + CSS.escape(job.id) + '"]', card)) render();
    else if (card) patchJobView(card, entry);
    if (job.state !== 'running') {
      clearInterval(timers.jobs[jobId]); delete timers.jobs[jobId];
      entry.live = false;
      toast((ucById(ucId) ? ucById(ucId).name : ucId) + ': ' + (job.action === 'on' ? 'turn-on' : 'turn-off') + ' ' + job.state + '.', job.state === 'failed');
      await loadUsecases(true);
      await loadDetail(ucId, true);
    }
  } catch (e) {
    if (e.status === 401) return;
    entry.err = e.message;
    const card = $('[data-uc="' + CSS.escape(ucId) + '"]');
    if (card) patchJobView(card, entry);
  } finally { pollBusy[jobId] = false; }
}

/** In-place DOM update for a running job: step states and appended log lines. */
function patchJobView(card, entry) {
  const job = entry.job;
  if (!job) return;
  const list = $('[data-action="' + job.action + '"]', card);
  if (list) {
    list.classList.toggle('is-active', job.state === 'running');
    $$('.step', list).forEach((li, i) => {
      const s = job.steps && job.steps[i];
      li.className = 'step' + (s ? ' ' + s.state : '');
      const t = $('.step-t', li);
      if (t) t.textContent = s && s.started ? fmtDur(s.started, s.ended) : '';
    });
  }
  const status = $('[data-job-status]', card);
  if (status) { status.textContent = job.state; status.className = 'state-word ' + jobStateClass(job.state); }
  const dur = $('[data-job-dur]', card);
  if (dur) dur.textContent = jobDurText(job);
  const term = $('.term[data-job]', card);
  if (term) {
    if (term.dataset.job !== job.id) return; // panel belongs to another job; full render will fix
    const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
    const cur = $('.cursor', term); if (cur) cur.remove();
    const empty = $('.term-empty', term); if (empty && entry.lines.length) empty.remove();
    for (let i = entry.rendered; i < entry.lines.length; i++) term.appendChild(logLine(entry.lines[i], i + 1));
    entry.rendered = entry.lines.length;
    if (job.state === 'running') term.appendChild(h('span', { class: 'cursor' }));
    if (entry.err) term.appendChild(h('div', { class: 'l-err' }, '! log poll failed: ' + entry.err));
    if (atBottom) term.scrollTop = term.scrollHeight;
  }
}
function jobDurText(job) {
  if (!job.started) return '';
  return job.state === 'running' ? fmtDur(job.started) + ' elapsed' : 'in ' + fmtDur(job.started, job.ended);
}
function jobStateClass(s) { return s === 'running' ? 'busy' : s === 'succeeded' ? 'on' : s === 'failed' ? 'bad' : 'unknown'; }
function logLine(text, n) {
  let cls = '';
  if (/^(==>|---|\[step|\[\d+\/\d+\]|#{2,})/.test(text)) cls = 'l-step';
  else if (/\b(error|failed|fatal|traceback|denied)\b/i.test(text)) cls = 'l-err';
  else if (/\b(ok|succeeded|done|complete|enrolled|authenticated)\b/i.test(text)) cls = 'l-ok';
  return h('div', { class: cls }, h('span', { class: 'ln' }, n), text);
}

/* ---- page ---- */

function renderUsecases() {
  const root = h('div');
  root.appendChild(h('div', { class: 'page-head' },
    h('div', null, h('h1', { class: 'page-title' }, 'Use cases'), h('p', { class: 'page-sub' }, 'Each switch runs a declared procedure against the connected cloud. Nothing happens without a confirmation.')),
    h('div', { style: 'display:flex;gap:10px;align-items:center' },
      state.ucLoading ? h('span', { class: 'loading' }, 'Loading') : null,
      h('button', { class: 'btn btn-sm', type: 'button', onclick: () => loadUsecases() }, icon('refresh'), 'Refresh'))));

  if (state.ucErr && !state.usecases) {
    root.appendChild(h('div', { class: 'state-box is-error' }, h('div', { class: 'title' }, 'Could not load use cases'), h('div', null, state.ucErr),
      h('button', { class: 'btn', type: 'button', onclick: () => loadUsecases() }, 'Retry')));
    return root;
  }
  if (!state.usecases) {
    root.appendChild(h('div', { class: 'cards' }, [1, 2].map(() => h('div', { class: 'skeleton', style: 'height:96px' }))));
    return root;
  }
  if (!state.usecases.length) {
    root.appendChild(h('div', { class: 'state-box' }, h('div', { class: 'title' }, 'No use cases registered'),
      h('div', null, 'Add a manifest at ', h('code', null, 'usecases/<id>/usecase.yaml'), ' and restart Switchboard. Each one gets a switch here.')));
    return root;
  }
  if (state.ucErr) root.appendChild(h('div', { class: 'form-error', style: 'margin-bottom:12px' }, 'Last refresh failed: ' + state.ucErr));
  const cards = h('div', { class: 'cards' });
  for (const uc of state.usecases) cards.appendChild(renderCard(uc));
  root.appendChild(cards);
  return root;
}

const STATE_INFO = {
  on:          { lamp: 'on',      word: 'on',      cls: 'on' },
  off:         { lamp: '',        word: 'off',     cls: 'off' },
  turning_on:  { lamp: 'busy',    word: 'turning on',  cls: 'busy' },
  turning_off: { lamp: 'busy',    word: 'turning off', cls: 'busy' },
  error:       { lamp: 'bad',     word: 'error',   cls: 'bad' },
  unknown:     { lamp: 'unknown', word: 'unknown', cls: 'unknown' },
};

function providerName(id) { const d = PROVIDER_DEFS.find(p => p.id === id); return d ? d.name : id; }

function renderCard(uc) {
  const info = STATE_INFO[uc.state] || STATE_INFO.unknown;
  const busy = /^turning_/.test(uc.state) || !!state.flipping[uc.id];
  const open = state.expanded === uc.id;
  const card = h('div', { class: 'card' + (uc.state === 'on' || busy ? ' is-live' : '') + (uc.state === 'error' ? ' is-error' : '') + (open ? ' is-open' : ''), dataset: { uc: uc.id } });

  const disabledReason = !uc.provider_connected
    ? (PROVIDER_DEFS.find(p => p.id === uc.provider && p.wired)
        ? providerName(uc.provider) + ' line is unplugged — plug it in on the Clouds page to operate this use case.'
        : providerName(uc.provider) + ' is not wired yet — this use case cannot be operated from Switchboard.')
    : null;

  const checked = uc.state === 'on' ? 'true' : uc.state === 'off' ? 'false' : 'mixed';
  const toggle = h('button', {
    class: 'toggle' + (busy ? ' is-busy' : '') + (uc.state === 'error' ? ' is-error is-mid' : '') + (uc.state === 'unknown' ? ' is-mid' : ''),
    type: 'button', role: 'switch', 'aria-checked': checked,
    'aria-label': (uc.state === 'on' ? 'Turn off ' : uc.state === 'off' ? 'Turn on ' : 'Change state of ') + uc.name,
    disabled: busy || !!disabledReason,
    title: disabledReason || (busy ? 'A job is running' : ''),
    onclick: () => requestFlip(uc),
  }, h('span', { class: 'knob' }));

  const sw = h('div', { class: 'switch' }, toggle,
    h('span', { class: 'switch-state' }, h('span', { class: 'lamp ' + info.lamp }), h('span', { class: 'state-word ' + info.cls }, info.word)));

  const title = h('div', null,
    h('div', { class: 'card-title' }, uc.name,
      h('span', { class: 'chip', title: uc.provider_connected ? 'provider connected' : 'provider not connected' }, h('span', { class: 'lamp ' + (uc.provider_connected ? 'on' : '') }), uc.provider)),
    h('div', { class: 'card-summary' }, uc.summary || ''));

  const lr = uc.last_run;
  const meta = h('div', { class: 'card-meta' },
    h('div', null, h('div', { class: 'meta-k' }, 'Resources'), h('div', { class: 'meta-v' }, typeof uc.resources === 'number' ? fmtNum(uc.resources) : '—')),
    h('div', null, h('div', { class: 'meta-k' }, 'Last run'), h('div', { class: 'meta-v' }, lr
      ? [h('span', { class: lr.state === 'succeeded' ? 'ok' : lr.state === 'failed' ? 'bad' : '' }, lr.action + ' ' + lr.state), ' · ', h('span', { dataset: { rel: lr.ended || lr.started }, style: 'color:var(--text-dim)' }, fmtRel(lr.ended || lr.started))]
      : h('span', { style: 'color:var(--text-faint)' }, 'never'))));

  const expand = h('button', { class: 'btn btn-ghost card-expand', type: 'button', 'aria-expanded': String(open), 'aria-label': (open ? 'Collapse ' : 'Expand ') + uc.name,
    onclick: () => toggleExpand(uc.id) }, icon('chev'));

  const head = h('div', { class: 'card-head' }, sw, title, meta, expand);
  if (disabledReason) head.appendChild(h('div', { class: 'card-warn' }, disabledReason, ' ', uc.provider === 'aws' ? h('a', { href: KEEP_QUERY + '#/clouds' }, 'Open Clouds') : null));
  card.appendChild(head);
  if (open) card.appendChild(renderDetail(uc));
  return card;
}

function toggleExpand(id) {
  if (state.expanded === id) {
    state.expanded = null;
    render();
    return;
  }
  state.expanded = id;
  render();
  loadDetail(id);
}

function renderDetail(uc) {
  const d = state.details[uc.id];
  const wrap = h('div', { class: 'card-detail' });
  if (!d || d._loading && !d.procedure) { wrap.appendChild(h('span', { class: 'loading' }, 'Loading manifest')); return wrap; }
  if (d._err && !d.procedure) {
    wrap.appendChild(h('div', { class: 'state-box is-error' }, h('div', { class: 'title' }, 'Could not load this use case'), h('div', null, d._err),
      h('button', { class: 'btn', type: 'button', onclick: () => loadDetail(uc.id, true) }, 'Retry')));
    return wrap;
  }

  const grid = h('div', { class: 'detail-grid' });
  // left: description + status probe
  const left = h('div');
  left.appendChild(h('div', { class: 'detail-block' }, h('div', { class: 'section-title', style: 'margin-bottom:8px' }, 'Description'),
    h('div', { class: 'md', html: SB.markdown.render(d.description || '_No description in the manifest._') })));
  if (d.source) left.appendChild(h('div', { class: 'detail-block' }, h('div', { class: 'section-title', style: 'margin-bottom:6px' }, 'Source'),
    h('div', { class: 'mono', style: 'font-size:12px;color:var(--text-dim);word-break:break-all' }, d.source.git || '', d.source.ref ? ' @ ' + d.source.ref : '', d.source.commit ? ' · ' + String(d.source.commit).slice(0, 10) : ' · not checked out')));
  left.appendChild(renderProbe(d));
  grid.appendChild(left);

  // right: procedure
  const right = h('div');
  right.appendChild(h('div', { class: 'section-title', style: 'margin-bottom:8px' }, 'Procedure'));
  const jobId = state.jobFor[uc.id];
  const entry = jobId ? state.jobs[jobId] : null;
  const job = entry && entry.job;
  right.appendChild(h('div', { class: 'procedure' },
    renderStepList('on', (d.procedure && d.procedure.on) || [], job),
    renderStepList('off', (d.procedure && d.procedure.off) || [], job)));
  grid.appendChild(right);
  wrap.appendChild(grid);

  // job panel
  wrap.appendChild(renderJobPanel(uc, d, entry));

  // code drawer
  const cs = state.code[uc.id] || {};
  wrap.appendChild(h('div', { style: 'display:flex;align-items:center;gap:10px;margin-top:20px' },
    h('button', { class: 'btn btn-sm', type: 'button', 'aria-expanded': String(!!cs.open), onclick: () => toggleCode(uc) }, icon('code'), cs.open ? 'Hide code' : 'Browse code'),
    d.source && d.source.commit ? h('span', { class: 'mono', style: 'font-size:12px;color:var(--text-faint)' }, 'checkout ', String(d.source.commit).slice(0, 10)) : null));
  if (cs.open) wrap.appendChild(renderDrawer(uc, cs));
  return wrap;
}

function renderStepList(action, steps, job) {
  const active = job && job.action === action;
  const col = h('div');
  const st = STATE_INFO[action === 'on' ? 'turning_on' : 'turning_off'];
  col.appendChild(h('div', { class: 'proc-col-title' }, h('span', { class: 'state-word' }, action === 'on' ? 'Turn on' : 'Turn off'),
    h('span', { style: 'color:var(--text-faint);font-size:12px' }, steps.length + (steps.length === 1 ? ' step' : ' steps'))));
  if (!steps.length) { col.appendChild(h('div', { style: 'color:var(--text-faint);font-size:13px' }, 'No steps declared.')); return col; }
  const ol = h('ol', { class: 'steps' + (active && job.state === 'running' ? ' is-active' : ''), dataset: { action } });
  steps.forEach((s, i) => {
    const js = active && job.steps && job.steps[i];
    ol.appendChild(h('li', { class: 'step' + (js ? ' ' + js.state : '') },
      h('span', { class: 'step-n', 'aria-hidden': 'true' }),
      h('div', null, h('div', { class: 'step-name' }, s.name), h('div', { class: 'step-run' }, s.run)),
      h('span', { class: 'step-t' }, js && js.started ? fmtDur(js.started, js.ended) : '')));
  });
  col.appendChild(ol);
  void st;
  return col;
}

function renderJobPanel(uc, d, entry) {
  const block = h('div', { class: 'detail-block' });
  const job = entry && entry.job;
  const headL = h('div', { class: 'job-title' }, h('span', { class: 'section-title' }, 'Log'));
  if (job) {
    headL.appendChild(h('span', null, job.action === 'on' ? 'turn on' : 'turn off'));
    headL.appendChild(h('span', { class: 'id' }, job.id));
    headL.appendChild(h('span', { class: 'state-word ' + jobStateClass(job.state), 'data-job-status': '' }, job.state));
    headL.appendChild(h('span', { 'data-job-dur': '' }, jobDurText(job)));
    if (job.started) headL.appendChild(h('span', { class: 'id' }, 'started ' + fmtTime(job.started)));
  }
  const runs = (d.runs || []);
  const headR = h('div', { style: 'display:flex;gap:8px;align-items:center' });
  if (runs.length > 1) {
    const sel = h('select', { class: 'input', style: 'width:auto;padding:4px 8px;font-size:12px', 'aria-label': 'Show a previous run', onchange: e => { const v = e.target.value; if (v) loadJobOnce(uc.id, v); } },
      runs.slice(0, 20).map(r => h('option', { value: r.job_id, selected: job && job.id === r.job_id }, r.action + ' · ' + r.state + ' · ' + fmtTime(r.started))));
    headR.appendChild(sel);
  }
  block.appendChild(h('div', { class: 'job-head' }, headL, headR));
  const term = h('div', { class: 'term', role: 'log', 'aria-live': 'polite', dataset: { job: job ? job.id : '' }, tabindex: '0' });
  if (!entry) term.appendChild(h('span', { class: 'term-empty' }, 'No runs yet. Flip the switch to start one; the log tails here.'));
  else if (entry.err && !entry.job) term.appendChild(h('div', { class: 'l-err' }, '! ' + entry.err));
  else {
    if (!entry.lines.length) term.appendChild(h('span', { class: 'term-empty' }, job && job.state === 'running' ? 'Waiting for output' : 'No output recorded.'));
    entry.lines.forEach((l, i) => term.appendChild(logLine(l, i + 1)));
    entry.rendered = entry.lines.length;
    if (job && job.state === 'running') term.appendChild(h('span', { class: 'cursor' }));
    if (entry.err) term.appendChild(h('div', { class: 'l-err' }, '! log poll failed: ' + entry.err));
  }
  block.appendChild(term);
  setTimeout(() => { term.scrollTop = term.scrollHeight; }, 0);
  return block;
}

function renderProbe(d) {
  const block = h('div', { class: 'detail-block' });
  block.appendChild(h('div', { class: 'section-title', style: 'margin-bottom:8px' }, 'Status probe'));
  const s = d.status;
  if (!s || typeof s !== 'object') { block.appendChild(h('div', { style: 'color:var(--text-faint);font-size:13px' }, 'No status probe output. It runs on demand and every interval while the use case is on.')); return block; }
  const items = [];
  function walk(prefix, v) {
    if (v && typeof v === 'object' && !Array.isArray(v)) { for (const k of Object.keys(v)) walk(prefix ? prefix + '.' + k : k, v[k]); }
    else if (Array.isArray(v)) items.push([prefix, v.map(x => typeof x === 'object' ? JSON.stringify(x) : String(x)).join(', ')]);
    else items.push([prefix, String(v)]);
  }
  walk('', s);
  if (items.length > 24) { block.appendChild(h('pre', { class: 'probe-raw' }, JSON.stringify(s, null, 2))); return block; }
  block.appendChild(h('div', { class: 'probe' }, items.map(([k, v]) => {
    const good = /authenticated|healthy|running|ok|true|enrolled/i.test(v), poor = /fail|error|down|false|unauth|missing/i.test(v);
    return h('div', { class: 'probe-item' }, h('span', { class: 'k' }, k), h('span', { class: 'v' + (good ? ' good' : poor ? ' poor' : '') }, v));
  })));
  return block;
}

/* ---- flipping ---- */

async function requestFlip(uc) {
  if (state.flipping[uc.id]) return;
  let d = state.details[uc.id];
  if (!d || !d.procedure) { d = await loadDetail(uc.id); }
  if (!d || !d.procedure) { toast('Could not load the procedure for ' + uc.name + '.', true); return; }

  let action = uc.state === 'on' ? 'off' : uc.state === 'off' ? 'on' : null;
  const body = h('div');
  let picker = null;
  if (!action) {
    action = 'off';
    picker = h('div', { style: 'display:flex;gap:18px;margin-bottom:12px' },
      h('label', { style: 'display:flex;gap:6px;align-items:center' }, h('input', { type: 'radio', name: 'act', value: 'off', checked: true, onchange: () => rebuild('off') }), 'Turn off (destroy)'),
      h('label', { style: 'display:flex;gap:6px;align-items:center' }, h('input', { type: 'radio', name: 'act', value: 'on', onchange: () => rebuild('on') }), 'Turn on (rebuild)'));
    body.appendChild(h('p', { style: 'color:var(--text-dim);margin-bottom:10px' }, 'The state is ', h('b', null, uc.state), '. Choose which procedure to run.'));
    body.appendChild(picker);
  }
  const listHost = h('div');
  body.appendChild(listHost);
  function rebuild(a) {
    action = a;
    clear(listHost);
    const steps = d.procedure[a] || [];
    listHost.appendChild(h('p', { style: 'color:var(--text-dim);margin-bottom:8px' }, steps.length + ' step' + (steps.length === 1 ? '' : 's') + ' will run in order on ', h('b', null, providerName(uc.provider)), '. The job stops at the first failure.'));
    listHost.appendChild(h('ol', { class: 'steps' }, steps.map(s => h('li', { class: 'step' }, h('span', { class: 'step-n', 'aria-hidden': 'true' }), h('div', null, h('div', { class: 'step-name' }, s.name), h('div', { class: 'step-run' }, s.run))))));
  }
  rebuild(action);
  const ok = await modal({
    title: (action === 'on' ? 'Turn on ' : 'Turn off ') + uc.name + '?',
    sub: action === 'off' ? 'This destroys the infrastructure the use case created.' : 'This creates infrastructure and may take several minutes.',
    body, confirmLabel: action === 'on' ? 'Turn on' : 'Turn off', danger: action === 'off',
  });
  if (!ok) return;
  state.flipping[uc.id] = true; render();
  try {
    const res = await api.flip(uc.id, action);
    const live = ucById(uc.id); if (live) live.state = action === 'on' ? 'turning_on' : 'turning_off';
    state.expanded = uc.id;
    delete state.flipping[uc.id];
    render();
    if (res && res.job_id) trackJob(uc.id, res.job_id);
    loadUsecases(true);
  } catch (e) {
    delete state.flipping[uc.id];
    if (e.status === 401) return;
    toast(e.status === 409 ? 'A job is already running for ' + uc.name + '.' : e.message, true);
    loadUsecases(true);
  }
}

/* ---- code drawer ---- */

const EXT_LANG = { tf: 'hcl', hcl: 'hcl', tfvars: 'hcl', py: 'python', yaml: 'yaml', yml: 'yaml', sh: 'sh', bash: 'sh', json: 'json', md: 'markdown', markdown: 'markdown' };
function langFor(path, hint) {
  if (hint && SB.highlight.languages[hint]) return hint;
  const m = /\.([a-z0-9]+)$/i.exec(path || '');
  return (m && EXT_LANG[m[1].toLowerCase()]) || 'text';
}

async function toggleCode(uc) {
  const cs = state.code[uc.id] || (state.code[uc.id] = { open: false, tree: null, commit: null, active: null, file: null, loading: false, err: null });
  cs.open = !cs.open;
  render();
  if (cs.open && !cs.tree) {
    cs.loading = true; cs.err = null; render();
    try {
      const t = await api.codeTree(uc.id);
      cs.tree = t.files || []; cs.commit = t.commit || null;
      cs.loading = false; render();
      const pref = cs.tree.find(f => /(^|\/)usecase\.ya?ml$/.test(f.path)) || cs.tree.find(f => /main\.tf$/.test(f.path)) || cs.tree[0];
      if (pref) openFile(uc, pref.path);
    } catch (e) { if (e.status === 401) return; cs.loading = false; cs.err = e.message; render(); }
  }
}

async function openFile(uc, path) {
  const cs = state.code[uc.id];
  cs.active = path; cs.file = null; cs.fileErr = null; cs.fileLoading = true; render();
  try { cs.file = await api.codeFile(uc.id, path); }
  catch (e) { if (e.status === 401) return; cs.fileErr = e.message; }
  cs.fileLoading = false; render();
}

function renderDrawer(uc, cs) {
  const d = h('div', { class: 'drawer' });
  d.appendChild(h('div', { class: 'drawer-head' },
    h('span', { class: 'mono' }, cs.active || 'checkout'),
    h('span', { class: 'mono' }, cs.commit ? 'commit ' + String(cs.commit).slice(0, 10) : '', cs.file && cs.file.language ? ' · ' + cs.file.language : '')));
  const body = h('div', { class: 'drawer-body' });
  const tree = h('div', { class: 'tree', role: 'tree', 'aria-label': 'Files' });
  if (cs.loading) tree.appendChild(h('div', { style: 'padding:14px' }, h('span', { class: 'loading' }, 'Reading checkout')));
  else if (cs.err) tree.appendChild(h('div', { style: 'padding:14px;color:var(--bad);font-size:13px' }, cs.err));
  else if (!cs.tree || !cs.tree.length) tree.appendChild(h('div', { style: 'padding:14px;color:var(--text-faint);font-size:13px' }, 'Checkout is empty. It is cloned on the first run.'));
  else {
    const dirs = new Map();
    for (const f of cs.tree) {
      const i = f.path.lastIndexOf('/');
      const dir = i > 0 ? f.path.slice(0, i) : '';
      if (!dirs.has(dir)) dirs.set(dir, []);
      dirs.get(dir).push(f);
    }
    const keys = Array.from(dirs.keys()).sort((a, b) => (a === '' ? -1 : b === '' ? 1 : a.localeCompare(b)));
    for (const k of keys) {
      if (k) tree.appendChild(h('div', { class: 'tree-dir' }, icon('folder'), k + '/'));
      for (const f of dirs.get(k).sort((a, b) => a.path.localeCompare(b.path))) {
        const name = f.path.slice(k ? k.length + 1 : 0);
        tree.appendChild(h('button', { class: 'tree-file' + (cs.active === f.path ? ' is-active' : ''), type: 'button', role: 'treeitem', 'aria-selected': String(cs.active === f.path), style: k ? '' : 'padding-left:14px', onclick: () => openFile(uc, f.path) },
          icon('file'), name, h('span', { class: 'sz' }, fmtSize(f.size))));
      }
    }
  }
  body.appendChild(tree);
  const pane = h('div', { class: 'code-pane' });
  if (cs.fileLoading) pane.appendChild(h('div', { class: 'code-empty' }, h('span', { class: 'loading' }, 'Opening ' + cs.active)));
  else if (cs.fileErr) pane.appendChild(h('div', { class: 'code-empty', style: 'color:var(--bad)' }, cs.fileErr));
  else if (cs.file) {
    const lang = langFor(cs.file.path, cs.file.language);
    const lines = SB.highlight.highlight(cs.file.content || '', lang).split('\n');
    const pre = h('pre');
    pre.innerHTML = lines.map((l, i) => '<span class="ln">' + (i + 1) + '</span>' + l).join('\n');
    pane.appendChild(pre);
  } else pane.appendChild(h('div', { class: 'code-empty' }, 'Select a file.'));
  body.appendChild(pane);
  d.appendChild(body);
  return d;
}
function fmtSize(n) { if (typeof n !== 'number') return ''; return n < 1024 ? n + ' B' : (n / 1024).toFixed(1) + ' K'; }

/* ==========================================================================
   7. markdown — small, safe renderer. All text is HTML-escaped; raw HTML in
      the source is shown as text, never passed through. Links only allow
      http(s), mailto and relative targets.
   ========================================================================== */

SB.markdown = (function () {
  function safeUrl(u) {
    u = String(u || '').trim();
    if (/^(https?:|mailto:)/i.test(u)) return u;
    if (/^[#./]/.test(u) || !/^[a-z][a-z0-9+.-]*:/i.test(u)) return u; // relative
    return '#';
  }
  function inline(text) {
    // split out code spans first so no formatting applies inside them
    const parts = text.split(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/);
    // the regex above yields [text, ticks, code, text, ticks, code, ...]
    let out = '';
    for (let i = 0; i < parts.length; i += 3) {
      out += fmt(parts[i] || '');
      if (i + 2 < parts.length) out += '<code>' + escapeHtml(parts[i + 2]) + '</code>';
    }
    return out;
  }
  function fmt(s) {
    s = escapeHtml(s);
    // links [text](url "title")
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (m, t, u) => '<a href="' + escapeHtml(safeUrl(u.replace(/&amp;/g, '&'))) + '" rel="noopener">' + t + '</a>');
    // autolinks <https://…>
    s = s.replace(/&lt;(https?:\/\/[^\s&]+)&gt;/g, (m, u) => '<a href="' + u + '" rel="noopener">' + u + '</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/__([^_]+)__/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>').replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    s = s.replace(/ {2,}\n/g, '<br>\n');
    return s;
  }
  function render(src) {
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    return blocks(lines, 0, lines.length);
  }
  function blocks(lines, from, to) {
    let out = '', i = from;
    while (i < to) {
      const line = lines[i];
      let m;
      if (/^\s*$/.test(line)) { i++; continue; }
      if ((m = /^\s*(```+|~~~+)\s*([\w+-]*)\s*$/.exec(line))) {
        const fence = m[1], lang = m[2].toLowerCase();
        let j = i + 1; const buf = [];
        while (j < to && !new RegExp('^\\s*' + fence.replace(/[~]/g, '\\~') + '\\s*$').test(lines[j])) buf.push(lines[j++]);
        const code = buf.join('\n');
        const known = SB.highlight.languages[lang] ? lang : (EXT_LANG[lang] || null);
        out += '<pre><code' + (lang ? ' class="lang-' + escapeHtml(lang) + '"' : '') + '>' + (known ? SB.highlight.highlight(code, known) : escapeHtml(code)) + '</code></pre>\n';
        i = j + 1; continue;
      }
      if ((m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) { const n = m[1].length; out += '<h' + n + '>' + inline(m[2]) + '</h' + n + '>\n'; i++; continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out += '<hr>\n'; i++; continue; }
      if (/^\s*>/.test(line)) {
        const buf = []; let j = i;
        while (j < to && /^\s*>/.test(lines[j])) buf.push(lines[j++].replace(/^\s*>\s?/, ''));
        out += '<blockquote>' + blocks(buf, 0, buf.length) + '</blockquote>\n'; i = j; continue;
      }
      if ((m = /^(\s*)([-*+]|\d+[.)])\s+/.exec(line))) {
        const r = list(lines, i, to, m[1].length); out += r.html; i = r.next; continue;
      }
      // paragraph: until blank line or a block start
      const buf = [line]; let j = i + 1;
      while (j < to && !/^\s*$/.test(lines[j]) && !/^(\s*(```|~~~)|#{1,6}\s|\s*>|\s*([-*+]|\d+[.)])\s+)/.test(lines[j])) buf.push(lines[j++]);
      out += '<p>' + inline(buf.join('\n')) + '</p>\n'; i = j;
    }
    return out;
  }
  function list(lines, i, to, indent) {
    const first = /^(\s*)([-*+]|\d+[.)])\s+/.exec(lines[i]);
    const ordered = /\d/.test(first[2]);
    const tag = ordered ? 'ol' : 'ul';
    let html = '<' + tag + (ordered && parseInt(first[2], 10) !== 1 ? ' start="' + parseInt(first[2], 10) + '"' : '') + '>\n';
    while (i < to) {
      const m = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
      if (!m || m[1].length !== indent) break;
      const item = [m[3]]; let j = i + 1; const nested = [];
      while (j < to) {
        const l = lines[j];
        if (/^\s*$/.test(l)) { if (j + 1 < to && /^\s{2,}/.test(lines[j + 1])) { j++; continue; } break; }
        const mm = /^(\s*)([-*+]|\d+[.)])\s+/.exec(l);
        if (mm && mm[1].length <= indent) break;
        if (mm && mm[1].length > indent) { nested.push(j); j++; continue; }
        if (/^\s{2,}/.test(l) || nested.length === 0) { if (nested.length) nested.push(j); else item.push(l.trim()); j++; continue; }
        break;
      }
      html += '<li>' + inline(item.join(' '));
      if (nested.length) {
        const sub = nested.map(k => lines[k]);
        const r = list(sub, 0, sub.length, /^(\s*)/.exec(sub[0])[1].length);
        html += '\n' + r.html;
      }
      html += '</li>\n';
      i = j;
    }
    html += '</' + tag + '>\n';
    return { html, next: i };
  }
  return { render, inline };
})();

/* ==========================================================================
   8. highlight — tokeniser-based syntax colouring. Each language is an
      ordered list of sticky regexes; first match wins; unmatched characters
      are plain text. Output is escaped HTML with <span class="tok-*">.
   ========================================================================== */

SB.highlight = (function () {
  const kw = words => new RegExp('\\b(?:' + words.split(' ').join('|') + ')\\b', 'y');
  const NUM = /\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b/y;
  const DQ = /"(?:\\.|[^"\\\n])*"/y;
  const SQ = /'(?:\\.|[^'\\\n])*'/y;

  const languages = {
    hcl: [
      ['comment', /\/\*[\s\S]*?\*\/|(?:\/\/|#)[^\n]*/y],
      ['string', /<<-?([A-Z_]+)\n[\s\S]*?\n\s*\1/y],
      ['string', /"(?:\\.|\$\{[^}]*\}|[^"\\\n])*"/y],
      ['number', NUM],
      ['keyword', kw('resource data variable output module provider terraform locals backend required_providers required_version for_each count depends_on lifecycle dynamic content true false null var local each path')],
      ['attr', /[A-Za-z_][\w-]*(?=\s*=(?!=))/y],
      ['punct', /[{}\[\]()=,.:?]|[<>!]=?|&&|\|\||[-+*\/%]/y],
    ],
    python: [
      ['comment', /#[^\n]*/y],
      ['string', /[rbuf]{0,2}(?:"""[\s\S]*?"""|'''[\s\S]*?''')/iy],
      ['string', /[rbuf]{0,2}(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')/iy],
      ['attr', /@[\w.]+/y],
      ['number', /\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?j?)\b/y],
      ['keyword', kw('def class return if elif else for while in not and or is None True False import from as with try except finally raise pass break continue lambda yield global nonlocal assert del async await print')],
      ['punct', /[{}\[\]()=,.:;]|[<>!]=?|->|[-+*\/%@|&^~]=?/y],
    ],
    yaml: [
      ['comment', /#[^\n]*/y],
      ['attr', /(?:[A-Za-z_][\w.\/-]*|"[^"\n]*"|'[^'\n]*')(?=\s*:(?:\s|$))/y],
      ['string', DQ], ['string', SQ],
      ['keyword', /\b(?:true|false|null|yes|no|on|off|~)\b/y],
      ['number', /\b\d+(?:\.\d+)?\b/y],
      ['punct', /^(?:---|\.\.\.)$|[:\-\[\]{},|>&*!?]/my],
    ],
    sh: [
      ['comment', /#[^\n]*/y],
      ['string', /"(?:\\.|[^"\\])*"/y], ['string', SQ],
      ['string', /<<-?['"]?([A-Z_]+)['"]?\n[\s\S]*?\n\1/y],
      ['attr', /\$\{[^}]*\}|\$[\w@#?$!*-]+/y],
      ['keyword', kw('if then else elif fi for while until do done case esac in function select return exit export local readonly set unset shift source echo printf test cd trap')],
      ['number', NUM],
      ['punct', /[|&;<>(){}\[\]=]|\$\(|\)/y],
    ],
    json: [
      ['attr', /"(?:\\.|[^"\\\n])*"(?=\s*:)/y],
      ['string', DQ],
      ['number', /-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/y],
      ['keyword', kw('true false null')],
      ['punct', /[{}\[\]:,]/y],
    ],
    markdown: [
      ['string', /```[\s\S]*?```/y],
      ['heading', /#{1,6} [^\n]*/y, 'bol'],
      ['string', /`[^`\n]+`/y],
      ['bold', /\*\*[^*\n]+\*\*/y],
      ['keyword', /\[[^\]\n]*\]\([^)\n]*\)/y],
      ['comment', />[^\n]*/y, 'bol'],
      ['punct', /(?:[-*+]|\d+\.) /y, 'bol'],
      ['punct', /-{3,}|\*{3,}/y, 'bol'],
    ],
    text: [],
  };

  function tokenize(src, rules) {
    const out = [];
    let i = 0, buf = '';
    const n = src.length;
    while (i < n) {
      let hit = null;
      for (const r of rules) {
        if (r[2] === 'bol' && i > 0 && src[i - 1] !== '\n') continue;
        const re = r[1]; re.lastIndex = i;
        const m = re.exec(src);
        if (m && m.index === i && m[0].length) { hit = [r[0], m[0]]; break; }
      }
      if (hit) {
        if (buf) { out.push(['text', buf]); buf = ''; }
        out.push(hit); i += hit[1].length;
      } else { buf += src[i]; i++; }
    }
    if (buf) out.push(['text', buf]);
    return out;
  }
  function highlight(src, lang) {
    const rules = languages[lang] || languages.text;
    if (!rules.length) return escapeHtml(src);
    return tokenize(src, rules).map(([t, s]) => {
      const e = escapeHtml(s);
      if (t === 'text') return e;
      // keep spans single-line so line splitting for gutters still works
      return e.split('\n').map(part => part ? '<span class="tok-' + t + '">' + part + '</span>' : '').join('\n');
    }).join('');
  }
  return { highlight, tokenize, languages };
})();

/* ==========================================================================
   9. mock — ?mock=1 swaps the api for an in-memory backend that exercises
      every state. Extra switches: &authed=1 (skip login), &connected=1
      (AWS already plugged in). An access key containing "FAIL" yields a
      failing ConnectionReport; the password "wrong" yields 401.
   ========================================================================== */

SB.mock = (function () {
  const iso = (msAgo) => new Date(Date.now() - msAgo).toISOString();
  const MIN = 60000, H = 3600000, D = 86400000;
  let authed = PARAMS.get('authed') === '1';
  let connected = PARAMS.get('connected') === '1';
  const jobs = {};
  let jobSeq = 100;

  const identity = { account: '257394018842', arn: 'arn:aws:sts::257394018842:assumed-role/AWSReservedSSO_AdministratorAccess_9f1c2d/nils', alias: null };
  const REGIONS = ['eu-central-1', 'eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-north-1', 'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'ca-central-1', 'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1', 'ap-northeast-2', 'ap-south-1', 'sa-east-1', 'ap-northeast-3'];

  function inventory(fresh) {
    const lab = { Project: 'zpa-pse-lab', ManagedBy: 'opentofu' };
    const region = (r, extra) => Object.assign({ region: r, instances: [], vpcs: [], nat_gateways: [], eips: [], volumes: [] }, extra || {});
    return {
      generated_at: fresh ? iso(0) : iso(14 * MIN + 12000),
      stale: !fresh,
      regions: [
        region('eu-central-1', {
          instances: [
            { id: 'i-0a1b2c3d4e5f60718', name: 'zpa-lab-pse', type: 'm5.large', state: 'running', private_ip: '10.91.10.5', public_ip: '63.188.16.52', launched: iso(3 * D + 2 * H), tags: Object.assign({ Name: 'zpa-lab-pse', Role: 'pse' }, lab) },
            { id: 'i-0b2c3d4e5f607182a', name: 'zpa-lab-connector-a', type: 't3.medium', state: 'running', private_ip: '10.90.1.21', public_ip: null, launched: iso(3 * D + 2 * H), tags: Object.assign({ Name: 'zpa-lab-connector-a', Role: 'app-connector' }, lab) },
            { id: 'i-0c3d4e5f6071829b3', name: 'zpa-lab-connector-b', type: 't3.medium', state: 'running', private_ip: '10.90.1.22', public_ip: null, launched: iso(3 * D + 2 * H), tags: Object.assign({ Name: 'zpa-lab-connector-b', Role: 'app-connector' }, lab) },
            { id: 'i-0d4e5f607182a9b4c', name: 'zpa-lab-nginx', type: 't3.micro', state: 'running', private_ip: '10.90.2.10', public_ip: null, launched: iso(3 * D + H), tags: Object.assign({ Name: 'zpa-lab-nginx', Role: 'app-server' }, lab) },
            { id: 'i-0e5f607182a9b4c5d', name: 'zpa-lab-win-client', type: 't3.medium', state: 'running', private_ip: '10.90.3.15', public_ip: '3.72.118.204', launched: iso(3 * D + H), tags: Object.assign({ Name: 'zpa-lab-win-client', Role: 'client' }, lab) },
          ],
          vpcs: [
            { id: 'vpc-0f1e2d3c4b5a69788', name: 'zpa-lab-pse-vpc', cidr: '10.91.0.0/16', default: false },
            { id: 'vpc-0a9b8c7d6e5f41323', name: 'zpa-lab-app-vpc', cidr: '10.90.0.0/16', default: false },
            { id: 'vpc-3b2a1c0d', name: null, cidr: '172.31.0.0/16', default: true },
          ],
          nat_gateways: [{ id: 'nat-0123456789abcdef0', vpc: 'vpc-0a9b8c7d6e5f41323', state: 'available', public_ip: '18.196.44.9' }],
          eips: [
            { ip: '63.188.16.52', attached: true, instance: 'i-0a1b2c3d4e5f60718' },
            { ip: '18.196.44.9', attached: true, instance: null },
            { ip: '3.72.118.204', attached: true, instance: 'i-0e5f607182a9b4c5d' },
          ],
          volumes: [
            { id: 'vol-0aa1', size_gb: 80, type: 'gp3', attached: true }, { id: 'vol-0aa2', size_gb: 30, type: 'gp3', attached: true },
            { id: 'vol-0aa3', size_gb: 30, type: 'gp3', attached: true }, { id: 'vol-0aa4', size_gb: 8, type: 'gp3', attached: true },
            { id: 'vol-0aa5', size_gb: 50, type: 'gp3', attached: true },
          ],
        }),
        region('eu-west-1', { vpcs: [{ id: 'vpc-1a2b3c4d', name: null, cidr: '172.31.0.0/16', default: true }], eips: [{ ip: '54.170.12.88', attached: false, instance: null }] }),
        region('us-east-1', { vpcs: [{ id: 'vpc-5e6f7a8b', name: null, cidr: '172.31.0.0/16', default: true }], volumes: [{ id: 'vol-0ff9', size_gb: 100, type: 'gp2', attached: false }] }),
        ...REGIONS.filter(r => !['eu-central-1', 'eu-west-1', 'us-east-1'].includes(r)).map(r => region(r, ['eu-west-2', 'us-west-2', 'ap-southeast-1'].includes(r) ? { vpcs: [{ id: 'vpc-' + r.replace(/-/g, '').slice(0, 8), name: null, cidr: '172.31.0.0/16', default: true }] } : {})),
      ],
      totals: { instances: 5, running: 5, vpcs: 6, nat_gateways: 1, eips: 4, volumes_gb: 298 },
      groups: [
        { key: 'Project=zpa-pse-lab', instances: 5, monthly_usd: 284.86 },
        { key: 'untagged', instances: 0, monthly_usd: 11.65 },
      ],
      cost: {
        monthly_usd: 296.51, currency: 'USD', method: 'on-demand list price × 730h',
        lines: [
          { item: 'm5.large Linux', region: 'eu-central-1', qty: 730, unit: 'hr', unit_usd: 0.115, monthly_usd: 83.95 },
          { item: 't3.medium Linux ×2', region: 'eu-central-1', qty: 1460, unit: 'hr', unit_usd: 0.048, monthly_usd: 70.08 },
          { item: 't3.medium Windows', region: 'eu-central-1', qty: 730, unit: 'hr', unit_usd: 0.0744, monthly_usd: 54.31 },
          { item: 't3.micro Linux', region: 'eu-central-1', qty: 730, unit: 'hr', unit_usd: 0.012, monthly_usd: 8.76 },
          { item: 'NAT gateway', region: 'eu-central-1', qty: 730, unit: 'hr', unit_usd: 0.052, monthly_usd: 37.96 },
          { item: 'EBS gp3', region: 'eu-central-1', qty: 198, unit: 'GB-mo', unit_usd: 0.0952, monthly_usd: 18.85 },
          { item: 'Public IPv4 ×3', region: 'eu-central-1', qty: 2190, unit: 'hr', unit_usd: 0.005, monthly_usd: 10.95 },
          { item: 'EBS gp2 (unattached)', region: 'us-east-1', qty: 100, unit: 'GB-mo', unit_usd: 0.08, monthly_usd: 8.00 },
          { item: 'Elastic IP (idle)', region: 'eu-west-1', qty: 730, unit: 'hr', unit_usd: 0.005, monthly_usd: 3.65 },
        ],
        notes: ['Unattached elastic IPs are billed', 'NAT data processing not included', 'Windows price includes the licence surcharge'],
      },
    };
  }
  let inv = inventory(false);

  const PSE_DESC = `## What it builds

A **ZPA Private Service Edge** in its own VPC (\`10.91.0.0/16\`) so client traffic terminates
inside the lab instead of on a public broker, plus a segmented application VPC (\`10.90.0.0/16\`)
with two App Connectors, an nginx test server and a Windows client.

### Components

1. \`zpa-lab-pse\` — m5.large, elastic IP, enrols against the PSE group
2. \`zpa-lab-connector-a\` / \`-b\` — t3.medium, private subnet behind the NAT
3. \`zpa-lab-nginx\` — t3.micro, the protected application
4. \`zpa-lab-win-client\` — t3.medium, Zscaler Client Connector installed via user-data

Provisioning keys are created by \`scripts/zpa_create.py\` through OneAPI and seeded into SSM
Parameter Store; the instances pull them at boot. State lives in S3 with \`use_lockfile\`.

> Turning this off destroys every instance and both VPCs. The ZPA objects (segments, groups)
> are left in place so the next turn-on is fast.

See the [lab repository](https://github.com/nilsujma-dev/zs-zpa-private-service-edge-lab) for the runbook.`;

  const PROC_PSE = {
    on: [
      { name: 'Create ZPA groups and keys', run: 'python3 scripts/zpa_create.py' },
      { name: 'Create PRIV connector group', run: 'python3 scripts/zpa_create_priv.py' },
      { name: 'Seed provisioning keys into SSM', run: 'python3 scripts/put_keys_ssm.py' },
      { name: 'Apply infrastructure', run: 'tofu -chdir=terraform apply -auto-approve -input=false' },
      { name: 'Wait for enrolment', run: 'python3 scripts/wait_enrolled.py --timeout 900' },
    ],
    off: [{ name: 'Destroy infrastructure', run: 'tofu -chdir=terraform destroy -auto-approve -input=false' }],
  };
  const PROC_BRANCH = {
    on: [
      { name: 'Create connector group', run: 'python3 scripts/zpa_group.py --create' },
      { name: 'Seed provisioning key', run: 'python3 scripts/put_key_ssm.py' },
      { name: 'Apply infrastructure', run: 'tofu -chdir=terraform apply -auto-approve -input=false' },
      { name: 'Wait for enrolment', run: 'python3 scripts/wait_enrolled.py --timeout 600' },
    ],
    off: [
      { name: 'Destroy infrastructure', run: 'tofu -chdir=terraform destroy -auto-approve -input=false' },
      { name: 'Remove connector group', run: 'python3 scripts/zpa_group.py --delete' },
    ],
  };
  const PROC_CC = {
    on: [
      { name: 'Register Cloud Connector', run: 'python3 scripts/cc_register.py' },
      { name: 'Apply infrastructure', run: 'tofu -chdir=terraform apply -auto-approve -input=false' },
      { name: 'Smoke test egress', run: 'bash scripts/smoke.sh' },
    ],
    off: [{ name: 'Destroy infrastructure', run: 'tofu -chdir=terraform destroy -auto-approve -input=false' }],
  };
  const PROC_OT = {
    on: [
      { name: 'Render relay config', run: 'python3 scripts/render.py' },
      { name: 'Apply infrastructure', run: 'tofu -chdir=terraform apply -auto-approve -input=false' },
      { name: 'Verify Modbus reachability', run: 'python3 scripts/probe_modbus.py --host 10.1.75.10' },
    ],
    off: [{ name: 'Destroy infrastructure', run: 'tofu -chdir=terraform destroy -auto-approve -input=false' }],
  };

  const usecases = {
    'zpa-private-service-edge': {
      id: 'zpa-private-service-edge', name: 'ZPA Private Service Edge lab', provider: 'aws',
      summary: 'A Private Service Edge in an isolated VPC, plus a segmented client/server VPC.',
      state: 'on', resources: 5, description: PSE_DESC, procedure: PROC_PSE,
      source: { git: 'https://github.com/nilsujma-dev/zs-zpa-private-service-edge-lab.git', ref: 'main', commit: '8c1f4e2a9b3d7c6e5f4a3b2c1d0e9f8a7b6c5d4e' },
      status: { pse: { enrolled: true, status: 'ZPN_STATUS_AUTHENTICATED', version: '25.62.1' }, connector_a: { status: 'ZPN_STATUS_AUTHENTICATED' }, connector_b: { status: 'ZPN_STATUS_AUTHENTICATED' }, client: { app_segment: 'zpa-lab-nginx', reachable: true }, checked_at: iso(40000) },
      runs: [], tags: { Project: 'zpa-pse-lab' },
    },
    'zpa-branch-connector-pair': {
      id: 'zpa-branch-connector-pair', name: 'ZPA branch connector pair', provider: 'aws',
      summary: 'Two App Connectors in a second region fronting a simulated branch subnet.',
      state: 'turning_on', resources: 2, procedure: PROC_BRANCH,
      description: 'Stands up **two App Connectors** in `eu-west-1` with a `/24` branch subnet and a route back to the lab through a peering connection.\n\n- Connector group: `branch-eu-west-1`\n- Instances: 2 × t3.medium\n- Peering: `10.92.0.0/16` ↔ `10.90.0.0/16`',
      source: { git: 'https://github.com/nilsujma-dev/zs-zpa-branch-pair.git', ref: 'main', commit: '1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b' },
      status: null, runs: [], tags: { Project: 'zpa-branch' },
    },
    'zia-cloud-connector-sandbox': {
      id: 'zia-cloud-connector-sandbox', name: 'ZIA Cloud Connector sandbox', provider: 'aws',
      summary: 'A single Cloud Connector forwarding a workload subnet to ZIA for egress policy demos.',
      state: 'off', resources: 0, procedure: PROC_CC,
      description: 'A minimal **Cloud Connector** deployment: one connector, one workload instance, one route table. Useful for showing URL filtering and DLP on server egress.\n\n```yaml\nconnector:\n  size: t3.medium\n  ha: false\n```',
      source: { git: 'https://github.com/nilsujma-dev/zs-zia-cc-sandbox.git', ref: 'main', commit: null },
      status: null, runs: [], tags: { Project: 'zia-cc-sandbox' },
    },
    'ot-edge-relay': {
      id: 'ot-edge-relay', name: 'OT edge relay', provider: 'aws',
      summary: 'A relay instance that bridges the OT cell (10.1.75.0/24) into a cloud-hosted historian.',
      state: 'error', resources: 3, procedure: PROC_OT,
      description: 'Bridges the EBC OT cell into a cloud historian over a ZPA-published Modbus/TCP path.\n\nThe verification step talks to the **real LOGO! PLC** at `10.1.75.10`; if the cell is offline the turn-on fails on purpose rather than leaving a half-configured relay.',
      source: { git: 'https://github.com/nilsujma-dev/zs-ot-edge-relay.git', ref: 'main', commit: 'deadbeefcafe0123456789abcdef0123456789ab' },
      status: null, runs: [], tags: { Project: 'ot-relay' },
    },
    'gke-workload-segmentation': {
      id: 'gke-workload-segmentation', name: 'GKE workload segmentation', provider: 'gcp',
      summary: 'Zscaler Workload Segmentation on a small GKE cluster.',
      state: 'unknown', resources: null, procedure: { on: [{ name: 'Apply infrastructure', run: 'tofu -chdir=terraform apply -auto-approve -input=false' }], off: [{ name: 'Destroy infrastructure', run: 'tofu -chdir=terraform destroy -auto-approve -input=false' }] },
      description: 'Placeholder until the GCP provider module exists.\n\nRenderer self-test — raw HTML must appear as text: <script>alert("never executed")</script> and [a javascript link](javascript:alert(1)) must be neutralised.',
      source: { git: 'https://github.com/nilsujma-dev/zs-gke-segmentation.git', ref: 'main', commit: null },
      status: null, runs: [], tags: {},
    },
  };

  /* ---- job engine ---- */
  function mkJob(ucId, action, opts) {
    const uc = usecases[ucId];
    const id = 'job_' + (++jobSeq) + '_' + Math.random().toString(36).slice(2, 6);
    const steps = (uc.procedure[action] || []).map(s => ({ name: s.name, state: 'pending', started: null, ended: null, exit_code: null }));
    const job = { id, usecase: ucId, action, state: 'running', steps, started: opts && opts.started || iso(0), ended: null, _lines: [], _cursor: 0, _tick: 0, _failAt: opts && opts.failAt };
    jobs[id] = job;
    uc.runs.unshift({ job_id: id, action, state: 'running', started: job.started, ended: null });
    uc.state = action === 'on' ? 'turning_on' : 'turning_off';
    job._lines.push('==> ' + (action === 'on' ? 'turn on' : 'turn off') + ' ' + uc.name);
    job._lines.push('git fetch origin ' + uc.source.ref + ' && git reset --hard origin/' + uc.source.ref);
    job._lines.push('HEAD is now at ' + String(uc.source.commit || '0000000').slice(0, 7) + ' ' + uc.source.ref);
    job._lines.push('tofu -chdir=terraform init -input=false -backend-config=bucket=zs-lab-tfstate-257394018842 -backend-config=key=usecases/' + ucId + '/terraform.tfstate');
    job._lines.push('Initializing the backend... Successfully configured the backend "s3"!');
    return job;
  }
  const STEP_LINES = {
    'Create ZPA groups and keys': ['POST /zpa/mgmtconfig/v1/admin/customers/…/segmentGroup 200', 'segment group zpa-lab (id 216196257331370452) exists, reusing', 'POST /zpa/mgmtconfig/v1/admin/customers/…/serverGroup 201', 'app segment zpa-lab-nginx -> 10.90.2.10:80,443', 'provisioning key pse-zpa-lab: created (expires never)', 'ok'],
    'Create PRIV connector group': ['connector group PRIV-zpa-lab in eu-central-1 (lat 50.11, lon 8.68)', 'provisioning key priv-zpa-lab: created', 'ok'],
    'Seed provisioning keys into SSM': ['put-parameter /zpa-lab/pse/key (SecureString) version 3', 'put-parameter /zpa-lab/connector/key (SecureString) version 3', 'ok'],
    'Create connector group': ['connector group branch-eu-west-1 (lat 53.35, lon -6.26)', 'ok'],
    'Seed provisioning key': ['put-parameter /zpa-branch/connector/key (SecureString) version 1', 'ok'],
    'Register Cloud Connector': ['ZIA: provisioning URL zscloud.net/…/cc-sandbox', 'ok'],
    'Render relay config': ['rendered relay.conf (14 lines)', 'ok'],
    'Apply infrastructure': ['tofu -chdir=terraform apply -auto-approve -input=false', 'Acquiring state lock. This may take a few moments...', 'aws_vpc.pse: Creating...', 'aws_vpc.app: Creating...', 'aws_vpc.pse: Creation complete after 2s [id=vpc-0f1e2d3c4b5a69788]', 'aws_vpc.app: Creation complete after 2s [id=vpc-0a9b8c7d6e5f41323]', 'aws_subnet.pse_public: Creation complete after 1s', 'aws_internet_gateway.pse: Creation complete after 1s', 'aws_eip.pse: Creation complete after 1s [id=eipalloc-0c2d]', 'aws_nat_gateway.app: Creating...', 'aws_nat_gateway.app: Still creating... [10s elapsed]', 'aws_nat_gateway.app: Still creating... [20s elapsed]', 'aws_nat_gateway.app: Creation complete after 94s [id=nat-0123456789abcdef0]', 'aws_instance.connector[0]: Creating...', 'aws_instance.connector[1]: Creating...', 'aws_instance.pse: Creating...', 'aws_instance.pse: Creation complete after 33s [id=i-0a1b2c3d4e5f60718]', 'aws_instance.connector[0]: Creation complete after 34s', 'aws_instance.connector[1]: Creation complete after 35s', 'Apply complete! Resources: 5 added, 0 changed, 0 destroyed.', 'Releasing state lock. This may take a few moments...', 'Outputs:', 'pse_public_ip = "63.188.16.52"'],
    'Wait for enrolment': ['polling ZPA for enrolment (timeout 900s)', 'pse: ZPN_STATUS_DISCONNECTED (booting)', 'connector-a: not seen yet', 'connector-b: not seen yet', 'pse: ZPN_STATUS_DISCONNECTED', 'connector-a: ZPN_STATUS_DISCONNECTED', 'pse: ZPN_STATUS_AUTHENTICATED', 'connector-b: ZPN_STATUS_DISCONNECTED', 'connector-a: ZPN_STATUS_AUTHENTICATED', 'connector-b: ZPN_STATUS_AUTHENTICATED', 'all components enrolled after 186s', 'ok'],
    'Smoke test egress': ['curl -s https://ip.zscaler.com from workload: routed via Cloud Connector', 'ok'],
    'Verify Modbus reachability': ['connecting 10.1.75.10:502 via relay', 'read holding register 40001 -> 1', 'ok'],
    'Destroy infrastructure': ['tofu -chdir=terraform destroy -auto-approve -input=false', 'Acquiring state lock. This may take a few moments...', 'aws_instance.pse: Destroying... [id=i-0a1b2c3d4e5f60718]', 'aws_instance.connector[0]: Destroying...', 'aws_instance.connector[1]: Destroying...', 'aws_instance.pse: Still destroying... [10s elapsed]', 'aws_instance.pse: Destruction complete after 41s', 'aws_nat_gateway.app: Destroying...', 'aws_nat_gateway.app: Still destroying... [30s elapsed]', 'aws_nat_gateway.app: Destruction complete after 62s', 'aws_vpc.app: Destruction complete after 1s', 'aws_vpc.pse: Destruction complete after 1s', 'Destroy complete! Resources: 5 destroyed.', 'Releasing state lock. This may take a few moments...'],
    'Remove connector group': ['DELETE connector group branch-eu-west-1 204', 'ok'],
  };
  function tickJob(job) {
    if (job.state !== 'running') return;
    const i = job.steps.findIndex(s => s.state === 'running' || s.state === 'pending');
    if (i < 0) return finishJob(job, 'succeeded');
    const st = job.steps[i];
    if (st.state === 'pending') {
      st.state = 'running'; st.started = iso(0); job._cursor = 0;
      job._lines.push('[' + (i + 1) + '/' + job.steps.length + '] ' + st.name);
      return;
    }
    const pool = STEP_LINES[st.name] || ['running ' + st.name, 'ok'];
    if (job._failAt === i && job._cursor >= Math.min(3, pool.length - 1)) {
      job._lines.push('Error: ' + (st.name === 'Verify Modbus reachability' ? 'connect 10.1.75.10:502: no route to host (cell offline?)' : 'step failed'));
      st.state = 'failed'; st.ended = iso(0); st.exit_code = 1;
      for (let k = i + 1; k < job.steps.length; k++) job.steps[k].state = 'skipped';
      return finishJob(job, 'failed');
    }
    if (job._cursor < pool.length) { job._lines.push(pool[job._cursor++]); return; }
    st.state = 'succeeded'; st.ended = iso(0); st.exit_code = 0;
  }
  function finishJob(job, result) {
    job.state = result; job.ended = job._endedAt || iso(0);
    job._lines.push(result === 'succeeded' ? '==> ' + job.action + ' succeeded in ' + fmtDur(job.started, job.ended) : '==> ' + job.action + ' failed');
    const uc = usecases[job.usecase];
    const run = uc.runs.find(r => r.job_id === job.id); if (run) { run.state = result; run.ended = job.ended; }
    if (result === 'failed') uc.state = 'error';
    else { uc.state = job.action === 'on' ? 'on' : 'off'; uc.resources = job.action === 'on' ? (uc.id === 'zpa-private-service-edge' ? 5 : uc.id === 'zpa-branch-connector-pair' ? 4 : 3) : 0; }
  }
  // completed history for the fixtures
  function backfill(ucId, action, result, endedAgo, failAt) {
    const job = mkJob(ucId, action, { started: iso(endedAgo + 4 * MIN), failAt });
    job._endedAt = iso(endedAgo);
    for (let n = 0; n < 200 && job.state === 'running'; n++) tickJob(job);
    job.ended = iso(endedAgo);
    const uc = usecases[ucId]; const run = uc.runs.find(r => r.job_id === job.id); run.ended = job.ended;
    job.steps.forEach((s, i) => { if (s.started) { s.started = iso(endedAgo + (job.steps.length - i) * 40000); s.ended = iso(endedAgo + (job.steps.length - i - 1) * 40000 + 2000); } });
    return job;
  }
  backfill('zpa-private-service-edge', 'off', 'succeeded', 9 * D);
  backfill('zpa-private-service-edge', 'on', 'succeeded', 3 * D + 2 * H);
  usecases['zpa-private-service-edge'].state = 'on'; usecases['zpa-private-service-edge'].resources = 5;
  backfill('zia-cloud-connector-sandbox', 'off', 'succeeded', 12 * D);
  usecases['zia-cloud-connector-sandbox'].state = 'off'; usecases['zia-cloud-connector-sandbox'].resources = 0;
  backfill('ot-edge-relay', 'on', 'failed', 1 * H + 7 * MIN, 2);
  usecases['ot-edge-relay'].state = 'error'; usecases['ot-edge-relay'].resources = 3;
  // the live one: a job that is running right now
  const liveJob = mkJob('zpa-branch-connector-pair', 'on', { started: iso(48000) });
  for (let n = 0; n < 9; n++) tickJob(liveJob);
  liveJob.steps.forEach((st, i) => { if (st.started) { st.started = iso(46000 - i * 19000); if (st.ended) st.ended = iso(46000 - i * 19000 - 17000); } });
  setInterval(() => { Object.values(jobs).forEach(j => { if (j.state === 'running') tickJob(j); }); }, 1100);

  /* ---- code fixtures ---- */
  const FILES = {
    'usecase.yaml': `id: zpa-private-service-edge
name: ZPA Private Service Edge lab
provider: aws
summary: A Private Service Edge in an isolated VPC, plus a segmented client/server VPC.
source:
  git: https://github.com/nilsujma-dev/zs-zpa-private-service-edge-lab.git
  ref: main
terraform:
  dir: terraform
  state_key: usecases/zpa-private-service-edge/terraform.tfstate
env:
  AWS_DEFAULT_REGION: eu-central-1   # every step inherits this
secrets:
  - zscaler_oneapi
on:
  - name: Create ZPA groups and keys
    run: python3 scripts/zpa_create.py
  - name: Create PRIV connector group
    run: python3 scripts/zpa_create_priv.py
  - name: Seed provisioning keys into SSM
    run: python3 scripts/put_keys_ssm.py
  - name: Apply infrastructure
    run: tofu -chdir=terraform apply -auto-approve -input=false
  - name: Wait for enrolment
    run: python3 scripts/wait_enrolled.py --timeout 900
off:
  - name: Destroy infrastructure
    run: tofu -chdir=terraform destroy -auto-approve -input=false
status:
  run: python3 scripts/status.py --json
  interval_s: 60
tags:
  Project: zpa-pse-lab
`,
    'terraform/main.tf': `terraform {
  required_version = ">= 1.8"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
  backend "s3" {
    # bucket, key and region are injected by Switchboard via -backend-config
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "zpa-pse-lab", ManagedBy = "opentofu" }
  }
}

locals {
  pse_cidr = "10.91.0.0/16"
  app_cidr = "10.90.0.0/16"
  name     = "zpa-lab"
}

/* Isolated VPC for the Private Service Edge */
resource "aws_vpc" "pse" {
  cidr_block           = local.pse_cidr
  enable_dns_hostnames = true
  tags                 = { Name = "\${local.name}-pse-vpc" }
}

resource "aws_vpc" "app" {
  cidr_block           = local.app_cidr
  enable_dns_hostnames = true
  tags                 = { Name = "\${local.name}-app-vpc" }
}

resource "aws_eip" "pse" {
  domain = "vpc"
  tags   = { Name = "\${local.name}-pse" }
}

resource "aws_instance" "pse" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = "m5.large"
  subnet_id              = aws_subnet.pse_public.id
  vpc_security_group_ids = [aws_security_group.pse.id]
  iam_instance_profile   = aws_iam_instance_profile.zpa.name
  user_data              = templatefile("\${path.module}/userdata/pse.sh.tftpl", { ssm_key = "/zpa-lab/pse/key" })
  root_block_device { volume_size = 80, volume_type = "gp3" }
  tags = { Name = "\${local.name}-pse", Role = "pse" }
}

resource "aws_instance" "connector" {
  count                  = 2
  ami                    = data.aws_ami.al2023.id
  instance_type          = "t3.medium"
  subnet_id              = aws_subnet.app_private.id
  vpc_security_group_ids = [aws_security_group.connector.id]
  iam_instance_profile   = aws_iam_instance_profile.zpa.name
  user_data              = templatefile("\${path.module}/userdata/connector.sh.tftpl", { ssm_key = "/zpa-lab/connector/key" })
  tags = { Name = "\${local.name}-connector-\${count.index == 0 ? "a" : "b"}", Role = "app-connector" }
  depends_on = [aws_nat_gateway.app]
}
`,
    'terraform/variables.tf': `variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "allowed_rdp_cidr" {
  description = "Who may reach the Windows client on 3389. Lab egress only."
  type        = string
  default     = "0.0.0.0/0" // narrowed by the ZPA policy, not here
}

variable "windows_ami_name" {
  type    = string
  default = "Windows_Server-2022-English-Full-Base-*"
}
`,
    'terraform/outputs.tf': `output "pse_public_ip" {
  value = aws_eip.pse.public_ip
}

output "connector_private_ips" {
  value = aws_instance.connector[*].private_ip
}

output "nginx_private_ip" {
  value = aws_instance.nginx.private_ip
}
`,
    'scripts/zpa_create.py': `#!/usr/bin/env python3
"""Create the ZPA objects the lab needs, idempotently.

Uses OneAPI with the client credentials mounted at ~/.zscaler_api_key.
Every call is safe to repeat: existing objects are looked up by name.
"""
import json
import os
import sys
from pathlib import Path

from oneapi import Client  # thin wrapper, see oneapi.py

SEGMENT_GROUP = "zpa-lab"
APP_SEGMENT = {"name": "zpa-lab-nginx", "domains": ["10.90.2.10"], "ports": [80, 443]}
PSE_GROUP = {"name": "PSE-zpa-lab", "latitude": 50.11, "longitude": 8.68, "city": "Frankfurt"}


def load_creds() -> dict:
    path = Path(os.environ.get("ZS_API_KEY_FILE", Path.home() / ".zscaler_api_key"))
    if not path.exists():
        sys.exit(f"missing {path}")
    return json.loads(path.read_text())


def ensure(client: Client, kind: str, spec: dict) -> dict:
    """Return the object named spec['name'], creating it if absent."""
    existing = client.find(kind, name=spec["name"])
    if existing:
        print(f"{kind} {spec['name']} (id {existing['id']}) exists, reusing")
        return existing
    created = client.create(kind, spec)
    print(f"{kind} {spec['name']}: created")
    return created


def main() -> int:
    creds = load_creds()
    client = Client(creds["client_id"], creds["client_secret"], customer_id=os.environ["ZPA_CUSTOMER_ID"])

    group = ensure(client, "segmentGroup", {"name": SEGMENT_GROUP, "enabled": True})
    ensure(client, "application", {**APP_SEGMENT, "segmentGroupId": group["id"]})
    pse = ensure(client, "serviceEdgeGroup", PSE_GROUP)

    key = client.provisioning_key("SERVICE_EDGE_GRP", pse["id"], name="pse-zpa-lab", max_usage=3)
    Path("out").mkdir(exist_ok=True)
    Path("out/keys.json").write_text(json.dumps({"pse": key}, indent=2))
    print("provisioning key pse-zpa-lab: created (expires never)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
`,
    'scripts/wait_enrolled.py': `#!/usr/bin/env python3
"""Poll ZPA until every component reports ZPN_STATUS_AUTHENTICATED."""
import argparse
import time

from oneapi import Client, creds_from_env

WANT = {"pse": "serviceEdge", "connector-a": "connector", "connector-b": "connector"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--interval", type=int, default=15)
    args = ap.parse_args()

    client = Client(*creds_from_env())
    deadline = time.monotonic() + args.timeout
    print(f"polling ZPA for enrolment (timeout {args.timeout}s)")
    while time.monotonic() < deadline:
        seen = {}
        for name, kind in WANT.items():
            obj = client.find(kind, name=f"zpa-lab-{name}")
            seen[name] = obj["currentVersion"] if obj else None
            print(f"{name}: {obj['status'] if obj else 'not seen yet'}")
        if all(s == "ZPN_STATUS_AUTHENTICATED" for s in seen.values()):
            print("all components enrolled")
            return 0
        time.sleep(args.interval)
    print("timed out waiting for enrolment")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
`,
    'scripts/status.py': `#!/usr/bin/env python3
"""Status probe: prints one JSON object on stdout; Switchboard shows it as-is."""
import json
import sys
from datetime import datetime, timezone

from oneapi import Client, creds_from_env

if __name__ == "__main__":
    client = Client(*creds_from_env())
    out = {}
    for name in ("pse", "connector_a", "connector_b"):
        obj = client.find("connector" if "connector" in name else "serviceEdge", name=f"zpa-lab-{name.replace('_', '-')}")
        out[name] = {"status": obj["status"] if obj else "MISSING"}
    out["checked_at"] = datetime.now(timezone.utc).isoformat()
    json.dump(out, sys.stdout)
`,
    'terraform/userdata/pse.sh.tftpl': `#!/bin/bash
set -euo pipefail

# Pull the provisioning key from SSM and enrol the Private Service Edge.
KEY=$(aws ssm get-parameter --name "\${ssm_key}" --with-decryption --query Parameter.Value --output text)
if [ -z "$KEY" ]; then
  echo "no provisioning key at \${ssm_key}" >&2
  exit 1
fi

rpm --import https://yum.private.zscaler.com/gpg
cat > /etc/yum.repos.d/zscaler.repo <<'REPO'
[zscaler]
name=Zscaler Private Access Repository
baseurl=https://yum.private.zscaler.com/yum/el9
enabled=1
gpgcheck=1
REPO

yum install -y zpa-service-edge
echo "$KEY" > /opt/zscaler/var/service-edge/provision_key
chmod 600 /opt/zscaler/var/service-edge/provision_key
systemctl enable --now zpa-service-edge
echo "enrolment started at $(date -u +%FT%TZ)"
`,
    'scripts/oneapi_config.json': `{
  "issuer": "https://nilsujma.zslogin.net/oauth2/v1/token",
  "audience": "https://api.zscaler.com",
  "scopes": ["zpa.admin"],
  "retry": { "attempts": 4, "backoff_s": 1.5 },
  "timeout_s": 30
}
`,
    'README.md': `# zs-zpa-private-service-edge-lab

Infrastructure for the **ZPA Private Service Edge** lab, driven by Switchboard.

## Layout

- \`terraform/\` — two VPCs, five instances, one NAT
- \`scripts/\` — OneAPI helpers, enrolment wait, status probe
- \`tools/cost.py\` — the cost estimate Switchboard is checked against

## Running by hand

\`\`\`sh
tofu -chdir=terraform init -backend-config=bucket=zs-lab-tfstate-<account>
tofu -chdir=terraform apply
\`\`\`

> State is in S3 with \`use_lockfile = true\`; never run with local state.
`,
  };
  const FILE_LANG = { yaml: 'yaml', tf: 'hcl', py: 'python', tftpl: 'sh', json: 'json', md: 'markdown' };
  function codeTree(ucId) {
    const paths = ucId === 'zpa-private-service-edge' ? Object.keys(FILES) : Object.keys(FILES).filter(p => !/wait_enrolled|status|zpa_create|userdata/.test(p));
    return { commit: usecases[ucId].source.commit, files: paths.map(p => ({ path: p, size: FILES[p].length })) };
  }
  function codeFile(ucId, path) {
    if (!FILES[path] || path.includes('..')) throw new ApiError('No such file in the checkout.', 404, 'not_found');
    let content = FILES[path];
    if (path === 'usecase.yaml' && ucId !== 'zpa-private-service-edge') content = content.replace(/zpa-private-service-edge/g, ucId).replace('ZPA Private Service Edge lab', usecases[ucId].name);
    return { path, language: FILE_LANG[path.split('.').pop()] || 'text', content };
  }

  function report(fail, temporary) {
    const checks = [
      { name: 'Credentials valid (STS)', ok: true, detail: 'assumed-role/AWSReservedSSO_AdministratorAccess_9f1c2d/nils' },
      { name: 'Can list regions', ok: true, detail: '17 enabled' },
      { name: 'Can describe EC2 in eu-central-1', ok: !fail, detail: fail ? 'UnauthorizedOperation: not authorized to perform ec2:DescribeInstances' : '' },
      { name: 'Pricing API reachable', ok: true, detail: 'us-east-1' },
      { name: 'State bucket ready', ok: !fail, detail: fail ? 'skipped — an earlier required check failed' : 'zs-lab-tfstate-257394018842' },
      { name: 'Session token expiry', ok: true, detail: temporary ? 'temporary credentials — expires when the SSO session does' : 'long-lived key — consider SSO credentials' },
    ];
    return { ok: !fail, identity: { account: identity.account, arn: identity.arn }, checks };
  }

  /* ---- scenes: ?scene=checklist&shown=3&fail=1  ?page=usecases&expand=<id>&code=1  ?region=eu-central-1 ---- */
  function scene(state) {
    const page = PARAMS.get('page');
    if (page === 'usecases' || page === 'clouds') location.hash = '#/' + page;
    if (PARAMS.get('scene') === 'checklist') {
      const rep = report(PARAMS.get('fail') === '1', true);
      const shown = Math.min(rep.checks.length, parseInt(PARAMS.get('shown') || '3', 10));
      state.connect = { stage: shown >= rep.checks.length ? 'done' : 'checking', report: rep, shown, error: null, busy: true };
    }
    if (PARAMS.get('region')) state.openRegion = PARAMS.get('region');
    if (PARAMS.get('expand')) state.expanded = PARAMS.get('expand');
  }
  async function sceneAfter(state) {
    const id = PARAMS.get('expand');
    if (id && usecases[id]) {
      await loadDetail(id);
      if (PARAMS.get('code') === '1') await toggleCode(usecases[id]);
      if (PARAMS.get('confirm') === '1' && ucById(id)) requestFlip(ucById(id));
    }
  }

  /* ---- api surface ---- */
  const need = async () => { await sleep(120 + Math.random() * 180); if (!authed) { onUnauthorised(); throw new ApiError('Not signed in.', 401, 'unauthorised'); } };
  const listItem = uc => {
    const lr = uc.runs[0] ? { job_id: uc.runs[0].job_id, action: uc.runs[0].action, state: uc.runs[0].state, ended: uc.runs[0].ended, started: uc.runs[0].started } : null;
    const prov = uc.provider === 'aws' ? connected : false;
    return { id: uc.id, name: uc.name, provider: uc.provider, summary: uc.summary, state: uc.state, resources: uc.resources, last_run: lr, provider_connected: prov };
  };
  const api = {
    me: async () => { await sleep(80); if (!authed) throw new ApiError('Not signed in.', 401, 'unauthorised'); return { authenticated: true }; },
    login: async (pw) => { await sleep(500); if (!pw || pw === 'wrong') throw new ApiError('Wrong password.', 401, 'bad_password'); authed = true; return null; },
    logout: async () => { authed = false; return null; },
    providers: async () => { await need(); return [{ id: 'aws', name: 'Amazon Web Services', status: connected ? 'connected' : 'disconnected', identity: connected ? identity : null, regions: connected ? REGIONS : [], connected_at: connected ? (SB.mock._connectedAt || (SB.mock._connectedAt = iso(2 * H))) : null }]; },
    connect: async (id, creds) => {
      await need(); await sleep(700);
      const fail = /fail/i.test(creds.access_key_id || '');
      const rep = report(fail, !!creds.session_token);
      if (!fail) { connected = true; SB.mock._connectedAt = iso(0); inv = inventory(false); }
      return rep;
    },
    disconnect: async () => { await need(); connected = false; return null; },
    inventory: async (id, refresh) => { await need(); if (!connected) throw new ApiError('AWS is not connected.', 409, 'not_connected'); if (refresh) { await sleep(1400); inv = inventory(true); } return inv; },
    usecases: async () => { await need(); return Object.values(usecases).map(listItem); },
    usecase: async (id) => { await need(); const uc = usecases[id]; if (!uc) throw new ApiError('No such use case.', 404, 'not_found'); return Object.assign(listItem(uc), { description: uc.description, procedure: uc.procedure, source: uc.source, status: uc.status, runs: uc.runs.slice(0, 20) }); },
    flip: async (id, action) => {
      await need(); const uc = usecases[id]; if (!uc) throw new ApiError('No such use case.', 404, 'not_found');
      if (uc.runs.some(r => r.state === 'running')) throw new ApiError('A job is already running for this use case.', 409, 'busy');
      if (uc.provider !== 'aws' || !connected) throw new ApiError('Provider is not connected.', 409, 'provider_disconnected');
      await sleep(300);
      const job = mkJob(id, action, {});
      return { job_id: job.id };
    },
    refreshUsecase: async (id) => { await need(); return listItem(usecases[id]); },
    codeTree: async (id) => { await need(); await sleep(400); return codeTree(id); },
    codeFile: async (id, path) => { await need(); await sleep(200); return codeFile(id, path); },
    job: async (jobId) => { await need(); const j = jobs[jobId]; if (!j) throw new ApiError('No such job.', 404, 'not_found'); return { id: j.id, usecase: j.usecase, action: j.action, state: j.state, steps: j.steps.map(s => Object.assign({}, s)), started: j.started, ended: j.ended }; },
    jobLog: async (jobId, since) => { await need(); const j = jobs[jobId]; if (!j) throw new ApiError('No such job.', 404, 'not_found'); since = since || 0; return { lines: j._lines.slice(since), next: j._lines.length }; },
  };
  return { api, usecases, jobs, scene, sceneAfter, report };
})();

/* ==========================================================================
   boot
   ========================================================================== */

Object.assign(SB, { render, navigate, api: () => api, MOCK });
document.addEventListener('DOMContentLoaded', boot);
})();
