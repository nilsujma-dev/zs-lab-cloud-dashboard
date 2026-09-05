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

/** Modal with focus trap; resolves true on confirm, false on cancel/escape.
 *  opts.setup(ctl) runs after mount; ctl.enableConfirm(bool) gates the confirm button
 *  (used while a plan is still loading), ctl.close(v) ends the modal programmatically. */
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
    const okBtn = h('button', { class: 'btn ' + (opts.danger ? 'btn-danger' : 'btn-primary'), type: 'button', disabled: !!opts.confirmDisabled, onclick: () => finish(true) }, opts.confirmLabel || 'Confirm');
    const dlg = h('div', { class: 'modal' + (opts.wide ? ' modal-wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'modal-title' },
      h('div', { class: 'modal-head' },
        h('div', { class: 'modal-title', id: 'modal-title' }, opts.title),
        opts.sub ? h('div', { class: 'modal-sub' }, opts.sub) : null),
      h('div', { class: 'modal-body' }, opts.body),
      h('div', { class: 'modal-foot' }, cancelBtn, opts.confirmLabel === null ? null : okBtn));
    const back = h('div', { class: 'modal-backdrop', onclick: e => { if (e.target === back) finish(false); } }, dlg);
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      if (e.key === 'Tab') {
        const f = $$('button, a[href], input, textarea, select, summary, [tabindex]:not([tabindex="-1"])', dlg).filter(x => !x.disabled);
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', onKey, true);
    root.appendChild(back);
    (opts.confirmLabel === null || okBtn.disabled ? cancelBtn : okBtn).focus();
    if (opts.setup) opts.setup({
      enableConfirm(on) { okBtn.disabled = !on; okBtn.setAttribute('aria-disabled', String(!on)); },
      close: finish, okBtn, cancelBtn, dialog: dlg,
    });
  });
}

/** Copy-on-click control for an id. Falls back to a selection range when the clipboard API is unavailable. */
function copyable(text, opts) {
  opts = opts || {};
  const btn = h('button', { class: 'copy' + (opts.cls ? ' ' + opts.cls : ''), type: 'button', title: 'Copy ' + text, 'aria-label': 'Copy ' + text, dataset: opts.fk ? { fk: opts.fk } : null,
    onclick: async e => {
      e.stopPropagation();
      let ok = false;
      try { await navigator.clipboard.writeText(text); ok = true; }
      catch (err) {
        try {
          const ta = h('textarea', { style: 'position:fixed;opacity:0', 'aria-hidden': 'true' }, text);
          document.body.appendChild(ta); ta.select(); ok = document.execCommand('copy'); ta.remove();
        } catch (err2) { ok = false; }
      }
      btn.classList.add('is-copied'); setTimeout(() => btn.classList.remove('is-copied'), 1200);
      toast(ok ? 'Copied ' + text : 'Could not copy — select it by hand.', !ok);
    } },
    h('span', { class: 'copy-text' }, opts.label || text), icon('copy', 'copy-ic'));
  return btn;
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
  providerForm: (id) => request('GET', '/api/providers/' + encodeURIComponent(id) + '/form'),
  connect:    (id, creds) => request('POST', '/api/providers/' + encodeURIComponent(id) + '/connect', creds),
  disconnect: (id) => request('DELETE', '/api/providers/' + encodeURIComponent(id)),
  inventory:  (id, refresh) => request('GET', '/api/providers/' + encodeURIComponent(id) + '/inventory?refresh=' + (refresh ? 1 : 0)),
  usecases:   () => request('GET', '/api/usecases'),
  usecase:    (id) => request('GET', '/api/usecases/' + encodeURIComponent(id)),
  outline:    (id, action) => request('GET', '/api/usecases/' + encodeURIComponent(id) + '/outline?action=' + encodeURIComponent(action)),
  topology:   (id, refresh) => request('GET', '/api/usecases/' + encodeURIComponent(id) + '/topology' + (refresh ? '?refresh=1' : '')),
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
  inventories: {},              // providerId -> {data, err, loading}
  connect: null,                // {provider, mode:'connect'|'rotate', stage:'loading'|'form'|'checking'|'done'|'error', fields, report, shown, error, busy}
  openRegion: null,             // {provider, region, focus?:{kind,id}} — mirrored in the URL hash
  drawer: { sections: {}, inst: {}, project: null, focusKey: null },
  drawerReturn: null,           // hash to go back to when the drawer was opened from elsewhere (the topology)
  drawerReturnFocus: null,      // topology node id that opened the drawer; refocused on close
  topo: {},                     // usecaseId -> {loading, data, err, width, sel}
  usecases: null, ucErr: null, ucLoading: false,
  expanded: null,               // use case id
  details: {},                  // id -> detail (+ _loading/_err)
  outlines: {},                 // id -> {on:{loading,data,err}, off:{…}}
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
  const m = /^#\/([a-z]+)(?:\/([a-z0-9-]+)(?:\/([a-z0-9-]+))?)?/.exec(location.hash);
  return m ? m[1] : null;
}
/** Deep-link params the drawer understands: ?inst= ?subnet= ?vpc= ?nat= ?eip= ?sg= ?vol= -> section to open. */
const DRAWER_FOCUS = { inst: 'instances', subnet: 'vpcs', vpc: 'vpcs', nat: 'nat_gateways', eip: 'eips', sg: 'security_groups', vol: 'volumes' };
/** #/clouds/<provider>/<region>[?inst=…] -> {provider, region, focus?} | null */
function regionFromHash() {
  const m = /^#\/clouds\/([a-z0-9-]+)\/([a-z0-9-]+)(?:\?([^#]*))?/.exec(location.hash);
  if (!m) return null;
  const out = { provider: m[1], region: m[2] };
  if (m[3]) {
    const q = new URLSearchParams(m[3]);
    for (const k of Object.keys(DRAWER_FOCUS)) { const v = q.get(k); if (v) { out.focus = { kind: k, id: v }; break; } }
  }
  return out;
}
function navigate(route) { location.hash = '#/' + route; }
function openRegionDrawer(providerId, region) { location.hash = '#/clouds/' + providerId + '/' + region; }
/** Open the drawer on one resource, remembering where to return on close. */
function openResourceDrawer(providerId, region, kind, id, opts) {
  opts = opts || {};
  state.drawerReturn = opts.returnTo || null;
  state.drawerReturnFocus = opts.returnFocus || null;
  location.hash = '#/clouds/' + providerId + '/' + region + (kind && id ? '?' + kind + '=' + encodeURIComponent(id) : '');
}
function closeRegionDrawer() { if (state.openRegion) location.hash = state.drawerReturn || '#/clouds'; }
/** Scroll to, expand and flash one resource in the open drawer. */
function applyDrawerFocus(focus) {
  const sec = DRAWER_FOCUS[focus.kind]; if (!sec) return;
  state.drawer.sections[sec] = true;
  if (focus.kind === 'inst') state.drawer.inst[focus.id] = true;
  state.drawer.project = null; // a filter must not hide the target
  state.drawer.flash = focus.id;
}

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
  if (!state.providers && !state.providersLoading) loadProviders();
  // region drawer follows the hash so it is deep-linkable and survives refresh
  const wasOpen = state.openRegion;
  const next = state.route === 'clouds' ? regionFromHash() : null;
  const key = o => o ? o.provider + '/' + o.region : '';
  const changedRegion = key(wasOpen) !== key(next);
  const changed = JSON.stringify(wasOpen) !== JSON.stringify(next);
  state.openRegion = next;
  if (changedRegion && wasOpen) { state.drawer = { sections: {}, inst: {}, project: null, focusKey: null }; } // switching regions starts fresh; a first open keeps deep-link state
  if (next && next.focus && changed) applyDrawerFocus(next.focus);
  const returnFocus = !next && wasOpen ? state.drawerReturnFocus : null;
  if (!next) { state.drawerReturn = null; state.drawerReturnFocus = null; }
  render();
  if (next) {
    if (changedRegion) focusDrawer();
  } else if (wasOpen) {
    // closing: whatever opened the drawer regains focus — a region tile, or a node in a topology drawing
    const node = returnFocus ? $('[data-node="' + CSS.escape(returnFocus) + '"]') : null;
    if (returnFocus && state.expanded && state.topo[state.expanded]) { const t = state.topo[state.expanded]; t.refocus = returnFocus; t.refocusUntil = Date.now() + 2500; } // survives the async re-renders that follow
    const tile = node || $('[data-region-tile="' + CSS.escape(wasOpen.region) + '"]');
    if (tile) tile.focus({ preventScroll: true }); else $('#view').focus({ preventScroll: true });
  } else {
    $('#view').focus({ preventScroll: true });
  }
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
  renderDrawerRoot();
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

/* Display order and names for the patch panel while /api/providers is still loading.
   Everything behavioural (status, identity, capabilities, form) comes from the API. */
const PROVIDER_DEFS = [
  { id: 'aws',   name: 'Amazon Web Services' },
  { id: 'gcp',   name: 'Google Cloud' },
  { id: 'azure', name: 'Microsoft Azure' },
];
const DEFAULT_CAPS = { inventory: false, usecases: false };

async function loadProviders() {
  state.providersLoading = true; state.providersErr = null;
  if (state.route === 'clouds') render();
  try {
    state.providers = await api.providers();
    for (const p of state.providers) {
      if (p.status === 'connected' && !state.inventories[p.id]) loadInventory(p.id, false);
    }
  } catch (e) { if (e.status !== 401) state.providersErr = e.message; }
  state.providersLoading = false;
  render();
}
function providerById(id) { return (state.providers || []).find(p => p.id === id) || null; }
function providerCaps(p) { return Object.assign({}, DEFAULT_CAPS, (p && p.capabilities) || {}); }
function providerName(id) { const p = providerById(id); if (p && p.name) return p.name; const d = PROVIDER_DEFS.find(x => x.id === id); return d ? d.name : id; }
function providerShort(id) { return id === 'aws' ? 'AWS' : id === 'gcp' ? 'GCP' : id === 'azure' ? 'Azure' : String(id).toUpperCase(); }
/** The list the panel shows: API order, with any provider the API does not know about omitted. */
function panelProviders() {
  if (!state.providers) return PROVIDER_DEFS.map(d => Object.assign({ status: 'loading' }, d));
  const known = new Map(state.providers.map(p => [p.id, p]));
  const ordered = PROVIDER_DEFS.filter(d => known.has(d.id)).map(d => known.get(d.id));
  for (const p of state.providers) if (!ordered.includes(p)) ordered.push(p);
  return ordered;
}

async function loadInventory(providerId, refresh) {
  const slot = state.inventories[providerId] || (state.inventories[providerId] = { data: null, err: null, loading: false });
  slot.loading = true; slot.err = null;
  if (state.route === 'clouds') render();
  try { slot.data = await api.inventory(providerId, refresh); }
  catch (e) { if (e.status !== 401) slot.err = e.message; }
  slot.loading = false;
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
  for (const p of panelProviders()) patch.appendChild(renderJack(p));
  root.appendChild(patch);

  if (state.connect) root.appendChild(renderConnect());

  const connected = (state.providers || []).filter(p => p.status === 'connected');
  // a first connect takes the stage; a rotation keeps the inventory on screen — nothing is interrupted
  const formBusy = state.connect && state.connect.stage !== 'done' && state.connect.mode !== 'rotate';
  if (connected.length && !formBusy) {
    // the honest "not built yet" strip sits right under the patch panel, then the real inventories
    const rest = connected.filter(p => !providerCaps(p).inventory);
    if (rest.length) root.appendChild(renderUnsupportedInventories(rest));
    for (const p of connected.filter(p => providerCaps(p).inventory)) root.appendChild(renderInventory(p));
  } else if (!state.connect && state.providers) {
    const first = state.providers.find(p => providerCaps(p).inventory) || state.providers[0];
    root.appendChild(h('div', { class: 'section' },
      h('div', { class: 'state-box' },
        h('div', { class: 'title' }, 'No line connected'),
        h('div', null, 'Plug in a provider to validate credentials' + (first && providerCaps(first).inventory ? ' and pull an inventory across every enabled region.' : '.')),
        first ? h('button', { class: 'btn btn-primary', type: 'button', onclick: () => openConnect(first.id, 'connect') }, icon('plug'), 'Plug in ' + providerShort(first.id)) : null)));
  } else if (!state.providers) {
    root.appendChild(h('div', { class: 'section' }, h('div', { class: 'skeleton', style: 'height:140px' })));
  }
  return root;
}

function renderUnsupportedInventories(providers) {
  const wrap = h('div', { class: 'section' });
  wrap.appendChild(h('div', { class: 'section-head' }, h('span', { class: 'section-title' }, providers.length === 1 ? 'Inventory · ' + providers[0].name : 'Inventory · other lines')));
  const grid = h('div', { class: 'unsupported-grid' });
  for (const p of providers) {
    const slot = state.inventories[p.id] || {};
    const d = slot.data;
    const reason = d && d.supported === false && d.reason ? d.reason : 'Switchboard holds and validates the credentials, but the resource scan and cost estimate only exist for providers whose module declares the inventory capability.';
    grid.appendChild(h('div', { class: 'unsupported panel', role: 'status' },
      h('div', { class: 'unsupported-mark', 'aria-hidden': 'true' }, socketSvg(true, true)),
      h('div', null,
        h('div', { class: 'unsupported-title' }, 'Inventory not built for ', p.name, ' yet'),
        h('div', { class: 'unsupported-sub' }, reason),
        h('div', { class: 'unsupported-caps' },
          capChip('connect', true), capChip('inventory', providerCaps(p).inventory), capChip('cost', providerCaps(p).inventory), capChip('use cases', providerCaps(p).usecases)))));
  }
  wrap.appendChild(grid);
  return wrap;
}
function capChip(label, on) {
  return h('span', { class: 'chip cap' + (on ? ' is-on' : ' is-off'), title: label + (on ? ': available' : ': not built yet') }, h('span', { class: 'lamp ' + (on ? 'ok' : '') }), label);
}

function socketSvg(live, small) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 72 72'); svg.setAttribute('class', 'jack-socket' + (small ? ' sm' : '')); svg.setAttribute('aria-hidden', 'true');
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

/** Identity lines for a connected jack: the API's identity_label first, then whatever else the identity carries. */
function identityLines(p) {
  const idn = p.identity || {};
  const label = p.identity_label || idn.account || idn.client_email || idn.subscription_name || idn.name || idn.id || null;
  const lines = [];
  if (label) lines.push({ text: label, strong: true });
  const seen = new Set([label]);
  const order = ['alias', 'project_id', 'project_name', 'subscription_id', 'tenant_name', 'tenant', 'client_id', 'arn'];
  const pretty = { alias: 'alias', project_id: 'project', project_name: null, subscription_id: 'subscription', tenant_name: 'tenant', tenant: 'tenant id', client_id: 'client', arn: null };
  for (const k of order) {
    const v = idn[k];
    if (!v || seen.has(v)) continue;
    seen.add(v);
    lines.push({ text: String(v), key: pretty[k], long: k === 'arn' });
    if (lines.length >= 4) break;
  }
  return lines;
}

function renderJack(p) {
  const loading = p.status === 'loading';
  const connected = p.status === 'connected';
  const caps = providerCaps(p);
  const cls = 'jack' + (connected ? ' is-live' : '') + (loading ? ' is-loading' : '');
  const jack = h('div', { class: cls, role: 'listitem', 'aria-label': p.name + (connected ? ', connected' : loading ? ', reading' : ', unplugged') });
  jack.appendChild(socketSvg(connected));
  const body = h('div', { class: 'jack-body' });
  body.appendChild(h('div', { class: 'jack-name' }, p.name));
  if (loading) {
    body.appendChild(h('div', { class: 'jack-status' }, h('span', { class: 'lamp unknown' }), h('span', { class: 'state-word unknown' }, 'Reading')));
    body.appendChild(h('div', { class: 'skeleton', style: 'height:12px;width:70%;margin-top:10px' }));
  } else if (connected) {
    body.appendChild(h('div', { class: 'jack-status' }, h('span', { class: 'lamp on' }), h('span', { class: 'state-word on' }, 'Line connected'),
      p.connected_at ? h('span', { class: 'mono jack-since' }, 'since ', h('span', { dataset: { rel: p.connected_at }, title: fmtTime(p.connected_at) }, fmtRel(p.connected_at))) : null));
    const lines = identityLines(p);
    body.appendChild(h('div', { class: 'jack-id' }, lines.length ? lines.map(l => h('div', { class: l.long ? 'jack-arn' : '', title: l.long ? l.text : null },
      l.key ? h('span', { class: 'k' }, l.key + ' ') : null, l.strong ? h('b', null, l.text) : l.text)) : h('div', { class: 'k' }, 'identity not reported')));
    if (p.credentials_updated_at && p.credentials_updated_at !== p.connected_at) {
      body.appendChild(h('div', { class: 'jack-note jack-rotated' }, icon('rotate'), 'credentials updated ', h('span', { dataset: { rel: p.credentials_updated_at }, title: fmtTime(p.credentials_updated_at) }, fmtRel(p.credentials_updated_at))));
    }
    if (!caps.inventory || !caps.usecases) {
      body.appendChild(h('div', { class: 'jack-caps' },
        capChip('inventory', caps.inventory), capChip('use cases', caps.usecases)));
    }
    body.appendChild(h('div', { class: 'jack-actions' },
      h('button', { class: 'btn btn-sm', type: 'button', onclick: () => openConnect(p.id, 'rotate') }, icon('rotate'), 'Rotate credentials'),
      h('button', { class: 'btn btn-sm btn-danger', type: 'button', onclick: () => disconnectProvider(p) }, 'Disconnect')));
  } else {
    body.appendChild(h('div', { class: 'jack-status' }, h('span', { class: 'lamp' }), h('span', { class: 'state-word off' }, 'Unplugged')));
    body.appendChild(h('div', { class: 'jack-note' }, caps.inventory
      ? 'Credentials are checked live before anything is stored, then an inventory is pulled across every enabled region.'
      : 'Credentials are checked live and stored encrypted. Inventory and use cases for this provider are not built yet; the connection is real.'));
    body.appendChild(h('div', { class: 'jack-actions' },
      h('button', { class: 'btn btn-sm btn-primary', type: 'button', onclick: () => openConnect(p.id, 'connect') }, icon('plug'), 'Plug in')));
  }
  jack.appendChild(body);
  return jack;
}

/** Open the credential form for a provider. mode = 'connect' | 'rotate' (same form, same checklist; rotate keeps the old credentials on failure). */
async function openConnect(providerId, mode) {
  const c = { provider: providerId, mode: mode || 'connect', stage: 'loading', fields: null, values: {}, fieldErrors: {}, report: null, shown: 0, error: null, busy: false };
  state.connect = c;
  render();
  try {
    const f = await api.providerForm(providerId);
    if (state.connect !== c) return;
    c.fields = (f && f.fields) || [];
    c.stage = 'form';
  } catch (e) {
    if (e.status === 401) return;
    if (state.connect !== c) return;
    c.stage = 'error'; c.error = 'Could not load the credential form: ' + e.message;
  }
  render();
  setTimeout(() => { const i = $('.connect [data-first-field]'); if (i) i.focus(); }, 0);
}

async function disconnectProvider(p) {
  const ok = await modal({
    title: 'Disconnect ' + p.name + '?',
    sub: 'Switchboard forgets the stored credentials and the cached inventory. Nothing in the cloud is touched.',
    body: h('p', { style: 'color:var(--text-dim)' }, providerCaps(p).usecases ? 'Use cases on this provider become unavailable until a line is plugged in again.' : 'The jack goes back to unplugged; plug it in again with new credentials at any time.'),
    confirmLabel: 'Disconnect', danger: true,
  });
  if (!ok) return;
  try {
    await api.disconnect(p.id);
    delete state.inventories[p.id];
    state.connect = null;
    if (state.openRegion && state.openRegion.provider === p.id) closeRegionDrawer();
    toast(p.name + ' line disconnected.');
    await loadProviders();
  } catch (e) { if (e.status !== 401) toast(e.message, true); }
}

function renderConnect() {
  const c = state.connect;
  const p = providerById(c.provider) || { id: c.provider, name: providerName(c.provider) };
  const caps = providerCaps(p);
  const rotate = c.mode === 'rotate';
  const wrap = h('div', { class: 'connect panel', 'aria-label': (rotate ? 'Rotate credentials for ' : 'Plug in ') + p.name });
  const title = rotate ? 'Rotate credentials for ' + p.name : 'Plug in ' + p.name;
  const sub = rotate
    ? 'The full checklist runs against the new credentials. On success they replace the stored ones atomically; on failure the old ones stay and nothing is interrupted.'
    : 'Every check runs live before anything is stored. Credentials are Fernet-encrypted at rest and never leave the server.';

  if (c.stage === 'loading') {
    wrap.appendChild(h('div', { class: 'panel-body' },
      h('div', { class: 'connect-title' }, title),
      h('div', { class: 'connect-sub' }, sub),
      h('div', { class: 'form-grid' }, [1, 2, 3].map(() => h('div', { class: 'field' }, h('div', { class: 'skeleton', style: 'height:12px;width:40%' }), h('div', { class: 'skeleton', style: 'height:34px' })))),
      h('div', { class: 'form-actions' }, h('span', { class: 'loading' }, 'Loading form'))));
    return wrap;
  }

  if (c.stage === 'form') {
    const err = h('div', { class: 'form-error', role: 'alert', hidden: !c.error }, c.error || '');
    const inputs = {};
    const fields = (c.fields || []).map((f, i) => renderField(f, c, inputs, i === 0));
    const extra = [];
    if (caps.inventory) {
      // Regions to scan are part of the connect body for inventory-capable providers; blank = every enabled region.
      extra.push(h('div', { class: 'field span-2' },
        h('label', { for: 'cf-regions' }, 'Regions ', h('span', { class: 'hint' }, '(optional, comma separated; blank = every enabled region)')),
        inputs.__regions = h('input', { class: 'input', id: 'cf-regions', autocomplete: 'off', spellcheck: 'false', placeholder: 'eu-central-1, eu-west-1', value: c.values.__regions || '' })));
    }
    const form = h('form', { novalidate: true, onsubmit: e => { e.preventDefault(); submitConnect(inputs); } },
      h('div', { class: 'connect-title' }, title),
      h('div', { class: 'connect-sub' }, sub),
      h('div', { class: 'form-grid' }, fields, extra),
      err,
      h('div', { class: 'form-actions' },
        inputs.__btn = h('button', { class: 'btn btn-primary' + (c.busy ? ' is-busy' : ''), type: 'submit', disabled: c.busy }, icon(rotate ? 'rotate' : 'plug'), rotate ? 'Rotate and check' : 'Plug in and check'),
        h('button', { class: 'btn btn-ghost', type: 'button', onclick: () => { state.connect = null; render(); } }, 'Cancel'),
        h('span', { class: 'form-req-note' }, 'Required fields are marked')));
    wrap.appendChild(h('div', { class: 'panel-body' }, form));
    return wrap;
  }

  // checking / done / error
  const rep = c.report;
  const list = h('ol', { class: 'checks', 'aria-live': 'polite' });
  if (rep) {
    rep.checks.forEach((chk, i) => {
      let cls = 'check';
      if (i < c.shown) cls += ' is-done ' + (chk.ok ? 'ok' : 'bad');
      else if (i === c.shown && c.stage === 'checking') cls += ' is-running';
      list.appendChild(h('li', { class: cls },
        h('span', { class: 'check-mark' }, i < c.shown ? icon(chk.ok ? 'check' : 'x') : null),
        h('span', { class: 'check-name' }, chk.name),
        h('span', { class: 'check-detail', title: chk.detail || '' }, i < c.shown ? (chk.detail || '') : '')));
    });
  } else if (c.stage === 'checking') {
    list.appendChild(h('li', { class: 'check is-running' }, h('span', { class: 'check-mark' }), h('span', { class: 'check-name' }, 'Contacting ' + p.name), h('span', { class: 'check-detail' }, '')));
  }
  const idLabel = rep && rep.identity ? (rep.identity.account || rep.identity.client_email || rep.identity.subscription_name || rep.identity.project_id || null) : null;
  const body = h('div', { class: 'panel-body' },
    h('div', { class: 'connect-title' }, rotate ? 'Checking the new credentials' : 'Checking the line'),
    h('div', { class: 'connect-sub' }, idLabel ? ['identity ', h('span', { class: 'mono' }, idLabel)] : 'Running each check against ' + p.name + '…'),
    list);
  const back = () => { state.connect = Object.assign({}, c, { stage: 'form', report: null, shown: 0, error: null, busy: false, fieldErrors: {} }); render(); };
  if (c.stage === 'done' && rep) {
    const failed = rep.checks.filter(x => !x.ok);
    if (rep.ok) {
      body.appendChild(h('div', { class: 'check-summary ok' },
        h('div', null, h('strong', null, rotate ? 'Credentials updated' : 'Line connected'),
          h('div', { class: 'sub' }, rotate ? 'The new credentials replaced the old ones. Nothing was interrupted.' : caps.inventory ? 'Credentials stored encrypted. Pulling the inventory now.' : 'Credentials stored encrypted. Inventory is not built for this provider yet.')),
        h('span', { class: 'lamp on lg' })));
    } else {
      body.appendChild(h('div', { class: 'check-summary bad' },
        h('div', null, h('strong', null, (failed.length === 1 ? 'One check failed' : failed.length + ' checks failed') + (rotate ? ' — the old credentials remain in place' : ' — nothing was stored')),
          h('div', { class: 'sub' }, failed.map(x => x.name + (x.detail ? ': ' + x.detail : '')).join(' · '))),
        h('div', { style: 'display:flex;gap:8px' },
          h('button', { class: 'btn', type: 'button', onclick: back }, 'Try again'),
          h('button', { class: 'btn btn-ghost', type: 'button', onclick: () => { state.connect = null; render(); } }, 'Close'))));
    }
  } else if (c.stage === 'error') {
    body.appendChild(h('div', { class: 'check-summary bad' },
      h('div', null, h('strong', null, 'Could not run the checks'), h('div', { class: 'sub' }, c.error)),
      h('div', { style: 'display:flex;gap:8px' },
        c.fields ? h('button', { class: 'btn', type: 'button', onclick: back }, 'Back') : h('button', { class: 'btn', type: 'button', onclick: () => openConnect(c.provider, c.mode) }, 'Retry'),
        h('button', { class: 'btn btn-ghost', type: 'button', onclick: () => { state.connect = null; render(); } }, 'Close'))));
  }
  wrap.appendChild(body);
  return wrap;
}

/** One form field from the provider's form description. Types: text | password | textarea | file.
 *  `file` is a textarea plus an upload control that reads the chosen file into the textarea client-side. */
function renderField(f, c, inputs, first) {
  const id = 'cf-' + f.name;
  const errId = id + '-err';
  const fieldErr = c.fieldErrors[f.name];
  const wide = f.type === 'textarea' || f.type === 'file';
  const common = { class: 'input' + (fieldErr ? ' is-invalid' : ''), id, autocomplete: 'off', spellcheck: 'false', 'aria-invalid': fieldErr ? 'true' : null, 'aria-describedby': errId, 'aria-required': f.required ? 'true' : null, 'data-first-field': first ? '' : null };
  let input;
  if (f.type === 'textarea' || f.type === 'file') input = h('textarea', Object.assign(common, { rows: f.type === 'file' ? 6 : 3, placeholder: f.type === 'file' ? 'Paste the file contents, or upload it' : '' }), c.values[f.name] || '');
  else input = h('input', Object.assign(common, { type: f.type === 'password' ? 'password' : 'text', value: c.values[f.name] || '' }));
  inputs[f.name] = input;
  const label = h('label', { for: id }, f.label, f.required ? h('span', { class: 'req', title: 'required', 'aria-hidden': 'true' }, ' *') : h('span', { class: 'hint' }, ' (optional)'));
  const parts = [label];
  if (f.type === 'file') {
    const fileIn = h('input', { type: 'file', id: id + '-file', class: 'visually-hidden', accept: '.json,.txt,.pem,.key,application/json,text/plain', tabindex: '-1',
      onchange: e => {
        const file = e.target.files && e.target.files[0];
        if (!file) return;
        if (file.size > 512 * 1024) { setFieldError(c, f.name, 'That file is larger than 512 KB, which is not a credential file.'); render(); return; }
        const rd = new FileReader();
        rd.onload = () => { input.value = String(rd.result || ''); c.values[f.name] = input.value; clearFieldError(c, f.name); nameEl.textContent = file.name + ' · ' + fmtSize(file.size); nameEl.hidden = false; };
        rd.onerror = () => { setFieldError(c, f.name, 'Could not read ' + file.name + '.'); render(); };
        rd.readAsText(file);
      } });
    const nameEl = h('span', { class: 'file-name mono', hidden: true });
    parts.push(h('div', { class: 'file-row' },
      h('button', { class: 'btn btn-sm', type: 'button', onclick: () => fileIn.click() }, icon('upload'), 'Upload file'),
      fileIn, nameEl,
      h('span', { class: 'hint' }, 'Read in the browser; only the contents are sent.')));
  }
  parts.push(input);
  parts.push(h('div', { class: 'field-msg' + (fieldErr ? ' is-error' : ''), id: errId, role: fieldErr ? 'alert' : null }, fieldErr || f.help || ''));
  return h('div', { class: 'field' + (wide ? ' span-2' : '') + (fieldErr ? ' has-error' : '') }, parts);
}
function setFieldError(c, name, msg) { c.fieldErrors[name] = msg; }
function clearFieldError(c, name) { delete c.fieldErrors[name]; }

async function submitConnect(inputs) {
  const c = state.connect;
  const fields = c.fields || [];
  // remember what was typed so a failed attempt can be corrected rather than retyped
  for (const f of fields) c.values[f.name] = inputs[f.name].value;
  if (inputs.__regions) c.values.__regions = inputs.__regions.value;
  // client-side required-field validation: the field's own help text is the error
  c.fieldErrors = {};
  let firstBad = null;
  for (const f of fields) {
    const v = (inputs[f.name].value || '').trim();
    if (f.required && !v) { c.fieldErrors[f.name] = f.help || (f.label + ' is required.'); if (!firstBad) firstBad = f.name; }
  }
  if (firstBad) {
    c.error = 'Fill in the required fields.'; render();
    setTimeout(() => { const el = $('#cf-' + CSS.escape(firstBad)); if (el) el.focus(); }, 0);
    return;
  }
  const body = {};
  for (const f of fields) { const v = inputs[f.name].value; body[f.name] = f.type === 'password' ? v : v.trim(); if (!body[f.name]) body[f.name] = null; }
  if (inputs.__regions) { const rg = inputs.__regions.value.split(',').map(s => s.trim()).filter(Boolean); body.regions = rg.length ? rg : null; }
  const cc = Object.assign({}, c, { stage: 'checking', report: null, shown: 0, error: null, busy: true });
  state.connect = cc;
  render();
  let report;
  try { report = await api.connect(c.provider, body); }
  catch (e) {
    if (e.status === 401) return;
    if (state.connect !== cc) return;
    state.connect = Object.assign({}, cc, { stage: 'error', error: e.message, busy: false });
    render(); return;
  }
  if (state.connect !== cc) return; // cancelled meanwhile
  cc.report = report;
  // Fill in one line at a time. Stop early on the first failure so the eye lands on it.
  for (let i = 0; i < report.checks.length; i++) {
    cc.shown = i; render();
    await sleep(i === 0 ? 300 : 380);
    if (state.connect !== cc) return;
    cc.shown = i + 1; render();
    if (!report.checks[i].ok && report.ok === false) {
      const rest = report.checks.slice(i + 1);
      if (!rest.some(x => !x.ok)) break;
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
    if (cc.mode === 'rotate') toast('Credentials updated for ' + providerName(cc.provider) + '.');
    await loadProviders();
    const p = providerById(cc.provider);
    if (p && p.status === 'connected' && cc.mode !== 'rotate') await loadInventory(cc.provider, providerCaps(p).inventory);
  }
}

function renderInventory(p) {
  const wrap = h('div', { class: 'section', dataset: { inventory: p.id } });
  const slot = state.inventories[p.id] || {};
  const inv = slot.data && slot.data.supported !== false ? slot.data : null;
  const head = h('div', { class: 'section-head' },
    h('div', { class: 'inv-toolbar' },
      h('span', { class: 'section-title' }, 'Inventory · ', providerShort(p.id)),
      inv ? h('span', { class: 'inv-meta' },
        'generated ', h('span', { class: 'mono', title: inv.generated_at }, fmtTime(inv.generated_at)),
        h('span', { style: 'color:var(--text-faint)' }, '(', h('span', { dataset: { rel: inv.generated_at } }, fmtRel(inv.generated_at)), ')'),
        inv.stale ? h('span', { class: 'badge badge-stale', title: inv.error || 'Served from cache older than 10 minutes' }, 'STALE') : null) : null,
      slot.loading ? h('span', { class: 'loading' }, 'Scanning regions') : null),
    h('button', { class: 'btn btn-sm', type: 'button', disabled: !!slot.loading, onclick: () => loadInventory(p.id, true) }, icon('refresh'), 'Refresh'));
  wrap.appendChild(head);

  if (slot.err && !inv) {
    wrap.appendChild(h('div', { class: 'state-box is-error' },
      h('div', { class: 'title' }, 'Inventory failed'), h('div', null, slot.err),
      h('button', { class: 'btn', type: 'button', onclick: () => loadInventory(p.id, true) }, 'Retry')));
    return wrap;
  }
  if (!inv) {
    wrap.appendChild(h('div', { class: 'totals' }, [1, 2, 3, 4, 5, 6].map(() => h('div', { class: 'total' }, h('div', { class: 'skeleton', style: 'height:12px;width:60%' }), h('div', { class: 'skeleton', style: 'height:26px;width:40%;margin-top:8px' })))));
    wrap.appendChild(h('div', { class: 'regions', style: 'margin-top:16px' }, (p.regions && p.regions.length ? p.regions : new Array(8).fill('')).map(() => h('div', { class: 'skeleton', style: 'height:90px' }))));
    return wrap;
  }
  if (slot.err) wrap.appendChild(h('div', { class: 'form-error', style: 'margin-bottom:12px' }, 'Last refresh failed: ' + slot.err + ' — showing the previous scan.'));

  const t = inv.totals || {};
  wrap.appendChild(h('div', { class: 'totals' },
    total('Instances', t.instances, typeof t.running === 'number' ? (t.running === t.instances && t.instances > 0 ? 'all running' : t.running + ' running') : null),
    total('VPCs', t.vpcs, typeof t.subnets === 'number' ? t.subnets + ' subnets' : null),
    total('NAT gateways', t.nat_gateways),
    total('Elastic IPs', t.eips),
    total('Volumes', t.volumes_gb, 'GB'),
    total('Monthly', inv.cost ? fmtUsd(inv.cost.monthly_usd) : '—', inv.cost ? inv.cost.currency : null)));

  const grid = h('div', { class: 'grid-2', style: 'margin-top:20px' });
  const left = h('div');
  const regions = regionList(p, inv);
  left.appendChild(h('div', { class: 'section-head' },
    h('span', { class: 'section-title' }, regions.length + ' regions scanned'),
    h('span', { class: 'inv-hint' }, 'Open a region for every resource in it')));
  const rg = h('div', { class: 'regions' });
  for (const r of regions) rg.appendChild(renderRegionTile(p, r));
  left.appendChild(rg);
  grid.appendChild(left);

  const right = h('div');
  right.appendChild(renderCost(inv));
  right.appendChild(renderGroups(inv));
  grid.appendChild(right);
  wrap.appendChild(grid);
  return wrap;
}
/** Union of scanned regions and the provider's enabled regions, so empty tiles are present. */
function regionList(p, inv) {
  const regionMap = new Map();
  (p.regions || []).forEach(r => regionMap.set(r, { region: r, instances: [], vpcs: [], nat_gateways: [], eips: [], volumes: [], security_groups: [] }));
  (inv.regions || []).forEach(r => regionMap.set(r.region, r));
  return Array.from(regionMap.values()).sort((a, b) => weight(b) - weight(a) || a.region.localeCompare(b.region));
}
function total(k, v, sub) {
  return h('div', { class: 'total' }, h('div', { class: 'total-k' }, k), h('div', { class: 'total-v' }, typeof v === 'number' ? fmtNum(v) : (v == null ? '—' : v), sub ? h('small', null, sub) : null));
}
function weight(r) { return (r.instances || []).length * 10 + (r.vpcs || []).filter(v => !v.default).length * 3 + (r.nat_gateways || []).length + (r.eips || []).length; }
function isEmptyRegion(r) {
  return !(r.instances || []).length && !(r.vpcs || []).some(v => !v.default) && !(r.nat_gateways || []).length && !(r.eips || []).length && !(r.volumes || []).length;
}
function regionCost(r) {
  if (typeof r.monthly_usd === 'number') return r.monthly_usd;
  let sum = 0, any = false;
  for (const k of ['instances', 'nat_gateways', 'eips', 'volumes']) for (const x of (r[k] || [])) if (typeof x.monthly_usd === 'number') { sum += x.monthly_usd; any = true; }
  return any ? sum : null;
}
function regionResourceCount(r) {
  if (typeof r.resource_count === 'number') return r.resource_count;
  return ['instances', 'vpcs', 'nat_gateways', 'eips', 'volumes', 'security_groups'].reduce((n, k) => n + (r[k] || []).length, 0);
}

function renderRegionTile(p, r) {
  const empty = isEmptyRegion(r);
  const defaultOnly = empty && (r.vpcs || []).some(v => v.default);
  const isOpen = !!(state.openRegion && state.openRegion.provider === p.id && state.openRegion.region === r.region);
  const cost = regionCost(r);
  const tile = h('button', { class: 'region' + (empty ? ' is-empty' : '') + (isOpen ? ' is-open' : ''), type: 'button', disabled: empty,
    'aria-haspopup': empty ? null : 'dialog', 'aria-expanded': empty ? null : String(isOpen), dataset: { regionTile: r.region },
    title: empty ? '' : 'Open ' + r.region,
    onclick: () => { if (isOpen) closeRegionDrawer(); else openRegionDrawer(p.id, r.region); } },
    h('div', { class: 'region-name' }, r.region,
      (r.instances || []).some(i => i.state === 'running') ? h('span', { class: 'lamp on', title: 'running instances' }) : (empty ? null : h('span', { class: 'region-open-ic' }, icon('chev')))),
    h('div', { class: 'region-counts' },
      cnt((r.instances || []).length, 'inst'), cnt((r.vpcs || []).length, 'vpc'), cnt((r.nat_gateways || []).length, 'nat'), cnt((r.eips || []).length, 'eip')),
    empty ? h('div', { class: 'region-empty-note' }, defaultOnly ? 'default VPC only' : 'nothing running')
      : h('div', { class: 'region-foot' }, h('span', { class: 'mono' }, cost !== null ? fmtUsd(cost) + ' /mo' : ''), h('span', { class: 'mono dim' }, regionResourceCount(r) + ' resources')));
  return tile;
}
function cnt(n, k) { return h('div', { class: 'region-count' }, h('b', null, n), h('span', null, k)); }

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
   5b. region drawer — the detailed inventory for one region. Opens from the
       right, follows the hash (#/clouds/<provider>/<region>), traps focus,
       returns focus to the tile on close. Re-rendered with the page; scroll
       position and the focused control (by data-fk) survive re-renders.
   ========================================================================== */

const SECTIONS = [
  { key: 'instances',       title: 'Instances' },
  { key: 'vpcs',            title: 'VPCs & subnets' },
  { key: 'nat_gateways',    title: 'NAT gateways' },
  { key: 'eips',            title: 'Elastic IPs' },
  { key: 'volumes',         title: 'Volumes' },
  { key: 'security_groups', title: 'Security groups' },
];

function drawerRegion() {
  const o = state.openRegion; if (!o) return null;
  const slot = state.inventories[o.provider];
  const inv = slot && slot.data && slot.data.supported !== false ? slot.data : null;
  if (!inv) return null;
  return (inv.regions || []).find(r => r.region === o.region) || null;
}

function focusDrawer() {
  setTimeout(() => { const b = $('#drawer-root .rdrawer-close'); if (b) b.focus(); }, 0);
}

function renderDrawerRoot() {
  const root = $('#drawer-root');
  const o = state.openRegion;
  document.documentElement.classList.toggle('has-drawer', !!o);
  if (!o) { clear(root); return; }
  // preserve scroll + focus across the re-render
  const prevBody = $('.rdrawer-body', root);
  const scrollTop = prevBody ? prevBody.scrollTop : 0;
  const active = document.activeElement;
  const fk = active && root.contains(active) ? (active.dataset.fk || (active.classList.contains('rdrawer-close') ? '__close' : null)) : null;
  clear(root);

  const p = providerById(o.provider) || { id: o.provider, name: providerName(o.provider) };
  const slot = state.inventories[o.provider] || {};
  const inv = slot.data && slot.data.supported !== false ? slot.data : null;
  const r = drawerRegion();

  const closeBtn = h('button', { class: 'btn btn-ghost rdrawer-close', type: 'button', 'aria-label': 'Close region drawer', title: 'Close (Esc)' }, icon('x'));
  closeBtn.addEventListener('click', () => closeRegionDrawer());
  const cost = r ? regionCost(r) : null;
  const head = h('header', { class: 'rdrawer-head' },
    h('div', { class: 'rdrawer-headings' },
      h('div', { class: 'rdrawer-kicker' }, providerShort(p.id), ' · region'),
      h('h2', { class: 'rdrawer-title', id: 'rdrawer-title' }, o.region),
      h('div', { class: 'rdrawer-meta' },
        r ? [
          h('span', { class: 'rdrawer-cost' }, cost !== null ? fmtUsd(cost) : '—', h('small', null, ' /mo')),
          h('span', { class: 'sep' }), h('span', null, fmtNum(regionResourceCount(r)), ' resources'),
          h('span', { class: 'sep' }), h('span', { class: 'dim', title: inv.generated_at }, 'generated ', fmtTime(inv.generated_at), ' (', h('span', { dataset: { rel: inv.generated_at } }, fmtRel(inv.generated_at)), ')'),
          inv.stale ? h('span', { class: 'badge badge-stale' }, 'STALE') : null,
        ] : h('span', { class: 'dim' }, slot.loading ? 'Loading inventory' : 'No inventory for this region'))),
    closeBtn);

  const body = h('div', { class: 'rdrawer-body', tabindex: '-1' });
  let filter = null;
  if (r) {
    filter = renderProjectFilter(r);
    for (const sec of SECTIONS) body.appendChild(renderSection(p, r, sec));
  } else if (slot.loading || (!slot.data && !slot.err)) {
    body.appendChild(h('div', { class: 'rdrawer-loading' }, h('span', { class: 'loading' }, 'Loading inventory for ' + o.region)));
    [1, 2, 3].forEach(() => body.appendChild(h('div', { class: 'skeleton', style: 'height:64px;margin:0 20px 12px' })));
  } else if (slot.err) {
    body.appendChild(h('div', { class: 'state-box is-error', style: 'margin:20px' }, h('div', { class: 'title' }, 'Inventory failed'), h('div', null, slot.err),
      h('button', { class: 'btn', type: 'button', onclick: () => loadInventory(p.id, true) }, 'Retry')));
  } else if (!p.status || p.status !== 'connected') {
    body.appendChild(h('div', { class: 'state-box', style: 'margin:20px' }, h('div', { class: 'title' }, p.name + ' is not connected'), h('div', null, 'Plug the line in to scan ' + o.region + '.'),
      h('button', { class: 'btn', type: 'button', onclick: () => closeRegionDrawer() }, 'Close')));
  } else {
    body.appendChild(h('div', { class: 'state-box', style: 'margin:20px' }, h('div', { class: 'title' }, 'Nothing scanned for ' + o.region), h('div', null, 'This region is not in the last inventory. It may not be enabled for the account, or the name is wrong.'),
      h('button', { class: 'btn', type: 'button', onclick: () => closeRegionDrawer() }, 'Close')));
  }

  const aside = h('aside', { class: 'rdrawer', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'rdrawer-title' }, head, filter, body);
  aside.addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeRegionDrawer(); return; }
    if (e.key !== 'Tab') return;
    const f = $$('button, a[href], input, textarea, select, summary, [tabindex]:not([tabindex="-1"])', aside).filter(x => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === aside || document.activeElement === body)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  const backdrop = h('div', { class: 'rdrawer-backdrop', onclick: () => closeRegionDrawer() });
  root.appendChild(backdrop);
  root.appendChild(aside);
  body.scrollTop = scrollTop;
  if (fk) {
    const el = fk === '__close' ? closeBtn : $('[data-fk="' + CSS.escape(fk) + '"]', aside);
    if (el) el.focus({ preventScroll: true });
  }
  const flash = state.drawer.flash;
  if (flash && r) { // consumed only once the region's inventory is on screen
    state.drawer.flash = null;
    setTimeout(() => {
      const el = $('[data-res-id="' + CSS.escape(flash) + '"]', aside);
      if (!el) return;
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      el.classList.add('is-flash'); setTimeout(() => el.classList.remove('is-flash'), 1600);
      const focusable = $('[data-fk]', el); if (focusable) focusable.focus({ preventScroll: true });
    }, 30);
  }
}

/* ---- project filter ---- */

function projectOf(x) { return x && x.tags && x.tags.Project ? String(x.tags.Project) : null; }
function instanceById(r, id) { return (r.instances || []).find(i => i.id === id) || null; }
/** Projects a resource belongs to: its own Project tag, else those of the instance(s) it is attached to. */
function effectiveProjects(r, x) {
  const own = projectOf(x);
  if (own) return [own];
  let ids = [];
  if (Array.isArray(x.attached_to)) ids = x.attached_to;
  else if (x.attached_to || x.instance) ids = [x.attached_to || x.instance];
  else if (x.association && x.association.kind === 'instance') ids = [x.association.id];
  else if (x.association && x.association.kind === 'nat') { const n = (r.nat_gateways || []).find(g => g.id === x.association.id); return n ? effectiveProjects(r, n) : []; }
  return Array.from(new Set(ids.map(id => projectOf(instanceById(r, id))).filter(Boolean)));
}
function matchesProject(r, x, project) {
  if (!project) return true;
  const pjs = effectiveProjects(r, x);
  if (project === '__untagged') return pjs.length === 0;
  return pjs.includes(project);
}
function renderProjectFilter(r) {
  const counts = new Map();
  let untagged = 0;
  for (const sec of SECTIONS) for (const x of (r[sec.key] || [])) {
    const pjs = effectiveProjects(r, x);
    if (!pjs.length) untagged++;
    for (const pj of pjs) counts.set(pj, (counts.get(pj) || 0) + 1);
  }
  const cur = state.drawer.project;
  const chip = (label, value, n) => h('button', { class: 'fchip' + (cur === value ? ' is-on' : ''), type: 'button', role: 'radio', 'aria-checked': String(cur === value), dataset: { fk: 'pj:' + (value || 'all') },
    onclick: () => { state.drawer.project = value; render(); } }, label, typeof n === 'number' ? h('span', { class: 'fchip-n' }, n) : null);
  const row = h('div', { class: 'rdrawer-filter', role: 'radiogroup', 'aria-label': 'Filter by Project tag' },
    h('span', { class: 'fchip-k' }, 'Project'),
    chip('All', null),
    Array.from(counts.entries()).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([k, n]) => chip(k, k, n)),
    untagged ? chip('untagged', '__untagged', untagged) : null);
  return row;
}
/* ---- sections ---- */

function sectionOpen(key, items) {
  const s = state.drawer.sections;
  if (Object.prototype.hasOwnProperty.call(s, key)) return s[key];
  return items.length > 0; // expanded by default only when non-empty
}
function renderSection(p, r, sec) {
  const all = r[sec.key] || [];
  const project = state.drawer.project;
  const items = all.filter(x => matchesProject(r, x, project));
  const open = sectionOpen(sec.key, items);
  const count = project && items.length !== all.length ? items.length + ' of ' + all.length : String(all.length);
  const el = h('section', { class: 'dsec' + (open ? ' is-open' : '') + (all.length ? '' : ' is-empty'), dataset: { sec: sec.key } });
  const headBtn = h('button', { class: 'dsec-head', type: 'button', 'aria-expanded': String(open), 'aria-controls': 'dsec-' + sec.key, dataset: { fk: 'sec:' + sec.key },
    onclick: () => { state.drawer.sections[sec.key] = !open; render(); } },
    icon('chev', 'dsec-chev'), h('span', { class: 'dsec-title' }, sec.title), h('span', { class: 'dsec-n' + (all.length ? '' : ' is-zero') }, count));
  el.appendChild(headBtn);
  if (open) {
    const bodyEl = h('div', { class: 'dsec-body', id: 'dsec-' + sec.key });
    if (!all.length) bodyEl.appendChild(sectionEmpty(sec.key, r.region));
    else if (!items.length) bodyEl.appendChild(h('div', { class: 'dsec-empty' }, 'Nothing here tagged ', h('span', { class: 'mono' }, 'Project=' + (project === '__untagged' ? '(untagged)' : project)), '.'));
    else bodyEl.appendChild(renderers[sec.key](r, items));
    el.appendChild(bodyEl);
  }
  return el;
}
function sectionEmpty(key, region) {
  const t = {
    instances: ['No instances in ' + region + '.', 'Nothing here is running, so the only recurring cost is storage, addresses and gateways.'],
    vpcs: ['No VPCs in ' + region + '.', 'Not even a default VPC; instances cannot be launched here without one.'],
    nat_gateways: ['No NAT gateways.', 'Private subnets in this region have no egress unless they route through something else.'],
    eips: ['No elastic IPs allocated.', 'Nothing idle is being billed for addresses here.'],
    volumes: ['No EBS volumes.', 'No instances means no root volumes, and nothing unattached is lingering.'],
    security_groups: ['No security groups.', 'Every VPC normally carries a default group; none was returned.'],
  }[key] || ['Nothing here.', ''];
  return h('div', { class: 'dsec-empty' }, h('div', { class: 'title' }, t[0]), h('div', null, t[1]));
}
/** Cross-link into another section: opens it, scrolls to and flashes the resource. */
function link(sec, id, label) {
  return h('button', { class: 'xlink mono', type: 'button', title: 'Show ' + id, dataset: { fk: 'x:' + id },
    onclick: () => { state.drawer.sections[sec] = true; state.drawer.flash = id; render(); } }, label || id, icon('link', 'xlink-ic'));
}
function tagChips(tags) {
  const ents = Object.entries(tags || {}).filter(([k]) => k !== 'Name');
  if (!ents.length) return h('span', { class: 'dim' }, 'no tags');
  return h('div', { class: 'tags' }, ents.map(([k, v]) => h('span', { class: 'tag', title: k + '=' + v }, h('span', { class: 'tag-k' }, k), h('span', { class: 'tag-v' }, String(v)))));
}
function yesno(v) { return v === true ? 'yes' : v === false ? 'no' : '—'; }
function lampFor(stateWord) {
  return stateWord === 'running' || stateWord === 'available' || stateWord === 'in-use' ? 'on' : stateWord === 'stopped' ? '' : /pend|shut|delet|fail/.test(stateWord || '') ? 'busy' : 'unknown';
}
function dl(rows) {
  const el = h('dl', { class: 'ddl' });
  for (const [k, v] of rows) { if (v === undefined) continue; el.appendChild(h('div', { class: 'ddl-row' }, h('dt', null, k), h('dd', null, v === null || v === '' ? h('span', { class: 'dim' }, '—') : v))); }
  return el;
}

const renderers = {
  instances(r, items) {
    const wrap = h('div', { class: 'insts' });
    for (const i of items) wrap.appendChild(renderInstance(r, i));
    return wrap;
  },
  vpcs(r, items) {
    const wrap = h('div', { class: 'vpcs' });
    for (const v of items) wrap.appendChild(renderVpc(r, v));
    return wrap;
  },
  nat_gateways(r, items) {
    const wrap = h('div', { class: 'nats' });
    for (const n of items) {
      wrap.appendChild(h('div', { class: 'res', dataset: { resId: n.id } },
        h('div', { class: 'res-head' }, h('span', { class: 'lamp ' + lampFor(n.state) }), h('b', null, n.name || 'NAT gateway'), copyable(n.id, { fk: 'c:' + n.id }), h('span', { class: 'state-word ' + (n.state === 'available' ? 'on' : 'unknown') }, n.state || 'unknown'),
          typeof n.monthly_usd === 'number' ? h('span', { class: 'res-cost mono' }, fmtUsd(n.monthly_usd), h('small', null, ' /mo')) : null),
        dl([
          ['VPC', n.vpc ? link('vpcs', n.vpc, vpcLabel(r, n.vpc)) : null],
          ['Subnet', n.subnet ? link('vpcs', n.subnet, subnetLabel(r, n.subnet)) : null],
          ['Public IP', n.public_ip ? copyable(n.public_ip, { fk: 'c:' + n.id + ':pub' }) : null],
          ['Private IP', n.private_ip ? h('span', { class: 'mono' }, n.private_ip) : null],
          ['Connectivity', n.connectivity_type || null],
          ['Created', n.created ? [fmtTime(n.created), ' ', h('span', { class: 'dim', dataset: { rel: n.created } }, '(' + fmtRel(n.created) + ')')] : null],
          ['Tags', tagChips(n.tags)],
        ])));
    }
    return wrap;
  },
  eips(r, items) {
    const tbl = h('table', { class: 'tbl dtbl' });
    tbl.appendChild(h('thead', null, h('tr', null, h('th', null, 'Address'), h('th', null, 'Allocation'), h('th', null, 'Associated with'), h('th', null, 'Private IP'), h('th', { class: 'num' }, 'Monthly'))));
    tbl.appendChild(h('tbody', null, items.map(e => {
      const a = e.association;
      let assoc;
      if (a && a.kind === 'instance') assoc = link('instances', a.id, instLabel(r, a.id));
      else if (a && a.kind === 'nat') assoc = link('nat_gateways', a.id);
      else if (a && a.kind === 'eni') assoc = h('span', { class: 'mono' }, 'ENI ', a.id);
      else if (e.attached && e.instance) assoc = link('instances', e.instance, instLabel(r, e.instance));
      else if (e.attached) assoc = h('span', { class: 'dim' }, 'attached');
      else assoc = h('span', { class: 'flag' }, icon('flag'), 'idle — billed hourly');
      return h('tr', { class: e.attached ? '' : 'is-flagged', dataset: { resId: e.allocation_id || e.ip } },
        h('td', null, copyable(e.ip, { fk: 'c:' + e.ip }), e.name ? h('div', { class: 'dim', style: 'font-size:12px' }, e.name) : null),
        h('td', { class: 'mono dim' }, e.allocation_id || '—'),
        h('td', null, assoc),
        h('td', { class: 'mono' }, e.private_ip || h('span', { class: 'dim' }, '—')),
        h('td', { class: 'num' }, typeof e.monthly_usd === 'number' ? fmtUsd(e.monthly_usd) : '—'));
    })));
    return h('div', { class: 'tbl-wrap' }, tbl);
  },
  volumes(r, items) {
    const tbl = h('table', { class: 'tbl dtbl' });
    tbl.appendChild(h('thead', null, h('tr', null, h('th', null, 'Volume'), h('th', { class: 'num' }, 'Size'), h('th', null, 'Type'), h('th', null, 'Attached to'), h('th', null, 'AZ'), h('th', null, 'Encrypted'), h('th', { class: 'num' }, 'Monthly'))));
    tbl.appendChild(h('tbody', null, items.map(v => h('tr', { class: v.attached ? '' : 'is-flagged', dataset: { resId: v.id } },
      h('td', null, copyable(v.id, { fk: 'c:' + v.id }), v.name ? h('div', { class: 'dim', style: 'font-size:12px' }, v.name) : null,
        v.created ? h('div', { class: 'dim', style: 'font-size:11px' }, 'created ', h('span', { dataset: { rel: v.created }, title: fmtTime(v.created) }, fmtRel(v.created))) : null),
      h('td', { class: 'num' }, fmtNum(v.size_gb), ' GB'),
      h('td', { class: 'mono' }, v.type || '—', (v.iops || v.throughput) ? h('div', { class: 'dim', style: 'font-size:11px' }, [v.iops ? v.iops + ' IOPS' : null, v.throughput ? v.throughput + ' MB/s' : null].filter(Boolean).join(' · ')) : null),
      h('td', null, v.attached && (v.attached_to || v.instance)
        ? [link('instances', v.attached_to || v.instance, instLabel(r, v.attached_to || v.instance)), v.device ? h('div', { class: 'mono dim', style: 'font-size:11px' }, v.device) : null]
        : h('span', { class: 'flag' }, icon('flag'), 'unattached — billed')),
      h('td', { class: 'mono dim' }, v.az || '—'),
      h('td', null, yesno(v.encrypted)),
      h('td', { class: 'num' }, typeof v.monthly_usd === 'number' ? fmtUsd(v.monthly_usd) : '—')))));
    return h('div', { class: 'tbl-wrap' }, tbl);
  },
  security_groups(r, items) {
    const wrap = h('div', { class: 'sgs' });
    for (const g of items) wrap.appendChild(renderSg(r, g));
    return wrap;
  },
};

function instLabel(r, id) { const i = instanceById(r, id); return i && i.name ? i.name : id; }
function vpcLabel(r, id) { const v = (r.vpcs || []).find(x => x.id === id); return v && v.name ? v.name : id; }
function subnetLabel(r, id) { for (const v of (r.vpcs || [])) for (const s of (v.subnets || [])) if (s.id === id) return s.name || id; return id; }
function sgLabel(r, id) { const g = (r.security_groups || []).find(x => x.id === id); return g && g.name ? g.name : id; }

function renderInstance(r, i) {
  const open = !!state.drawer.inst[i.id];
  const card = h('article', { class: 'inst' + (open ? ' is-open' : ''), dataset: { resId: i.id } });
  const toggle = h('button', { class: 'inst-toggle', type: 'button', 'aria-expanded': String(open), dataset: { fk: 'inst:' + i.id },
    onclick: () => { state.drawer.inst[i.id] = !open; render(); } },
    icon('chev', 'inst-chev'), h('span', { class: 'lamp ' + lampFor(i.state), title: i.state }), h('span', { class: 'inst-name' }, i.name || h('span', { class: 'dim' }, 'unnamed')));
  const head = h('div', { class: 'inst-head' },
    toggle,
    h('div', { class: 'inst-cell' }, h('span', { class: 'k' }, 'id'), copyable(i.id, { fk: 'c:' + i.id })),
    h('div', { class: 'inst-cell' }, h('span', { class: 'k' }, 'type'), h('span', { class: 'mono' }, i.type || '—')),
    h('div', { class: 'inst-cell' }, h('span', { class: 'k' }, 'az'), h('span', { class: 'mono' }, i.az || '—')),
    h('div', { class: 'inst-cell' }, h('span', { class: 'k' }, 'private'), h('span', { class: 'mono' }, i.private_ip || '—')),
    h('div', { class: 'inst-cell' }, h('span', { class: 'k' }, 'public'), h('span', { class: 'mono' + (i.public_ip ? '' : ' dim') }, i.public_ip || '—')),
    h('div', { class: 'inst-cell' }, h('span', { class: 'k' }, 'launched'), h('span', { class: 'mono', title: fmtTime(i.launched) }, h('span', { dataset: { rel: i.launched } }, fmtRel(i.launched)), typeof i.uptime_h === 'number' ? h('span', { class: 'dim' }, ' · up ' + fmtUptime(i.uptime_h)) : null)),
    h('div', { class: 'inst-cell num' }, h('span', { class: 'k' }, 'monthly'), h('span', { class: 'mono' }, typeof i.monthly_usd === 'number' ? fmtUsd(i.monthly_usd) : '—')));
  card.appendChild(head);
  if (open) {
    const vols = (i.volumes || []);
    const sgs = (i.security_groups || []);
    card.appendChild(h('div', { class: 'inst-body' },
      dl([
        ['Instance id', copyable(i.id, { fk: 'c2:' + i.id })],
        ['Name', i.name || null],
        ['State', h('span', { class: 'state-word ' + (i.state === 'running' ? 'on' : i.state === 'stopped' ? 'off' : 'unknown') }, i.state || 'unknown')],
        ['Type', h('span', { class: 'mono' }, i.type || '—')],
        ['Platform', [i.platform || null, i.architecture ? h('span', { class: 'dim' }, ' · ' + i.architecture) : null]],
        ['Availability zone', h('span', { class: 'mono' }, i.az || '—')],
        ['VPC', i.vpc ? link('vpcs', i.vpc, vpcLabel(r, i.vpc)) : null],
        ['Subnet', i.subnet ? link('vpcs', i.subnet, subnetLabel(r, i.subnet)) : null],
        ['Private IP', i.private_ip ? copyable(i.private_ip, { fk: 'c:' + i.id + ':priv' }) : null],
        ['Public IP', i.public_ip ? copyable(i.public_ip, { fk: 'c:' + i.id + ':pub' }) : h('span', { class: 'dim' }, 'none — private only')],
        ['Launched', i.launched ? [fmtTime(i.launched), typeof i.uptime_h === 'number' ? h('span', { class: 'dim' }, ' · up ' + fmtUptime(i.uptime_h)) : null] : null],
        ['AMI', i.ami ? [copyable(i.ami, { fk: 'c:' + i.id + ':ami' }), i.ami_name ? h('div', { class: 'dim mono', style: 'font-size:11.5px;margin-top:2px;word-break:break-all' }, i.ami_name) : null] : null],
        ['IAM instance profile', i.iam_instance_profile ? h('span', { class: 'mono' }, i.iam_instance_profile) : h('span', { class: 'dim' }, 'none')],
        ['Key pair', i.key_name ? h('span', { class: 'mono' }, i.key_name) : h('span', { class: 'dim' }, 'none')],
        ['Root device', i.root_device ? h('span', { class: 'mono' }, i.root_device) : null],
        ['Detailed monitoring', yesno(i.monitoring)],
        ['EBS optimized', yesno(i.ebs_optimized)],
        ['User data', i.user_data_present === true ? 'present' : i.user_data_present === false ? 'none' : '—'],
        ['Monthly cost', typeof i.monthly_usd === 'number' ? h('span', { class: 'mono' }, fmtUsd(i.monthly_usd), h('span', { class: 'dim' }, ' compute only')) : null],
        ['Volumes', vols.length ? h('div', { class: 'linkrow' }, vols.map(id => link('volumes', id, volLabel(r, id)))) : h('span', { class: 'dim' }, 'none')],
        ['Security groups', sgs.length ? h('div', { class: 'linkrow' }, sgs.map(g => link('security_groups', g.id, g.name || g.id))) : h('span', { class: 'dim' }, 'none')],
        ['Tags', tagChips(i.tags)],
      ])));
  }
  return card;
}
function volLabel(r, id) { const v = (r.volumes || []).find(x => x.id === id); return v ? id + ' · ' + v.size_gb + ' GB' : id; }
function fmtUptime(hrs) {
  if (hrs < 48) return Math.round(hrs) + ' h';
  const d = Math.floor(hrs / 24), hh = Math.round(hrs - d * 24);
  return d + ' d' + (hh ? ' ' + hh + ' h' : '');
}

function renderVpc(r, v) {
  const el = h('div', { class: 'res vpc' + (v.default ? ' is-default' : ''), dataset: { resId: v.id } });
  el.appendChild(h('div', { class: 'res-head' },
    h('b', null, v.name || (v.default ? 'default VPC' : 'unnamed VPC')), copyable(v.id, { fk: 'c:' + v.id }), h('span', { class: 'mono' }, v.cidr),
    v.default ? h('span', { class: 'chip' }, 'default') : null,
    v.igw ? h('span', { class: 'mono dim', title: 'internet gateway' }, 'igw ', v.igw) : h('span', { class: 'dim' }, 'no internet gateway'),
    v.dns_hostnames === true ? h('span', { class: 'dim' }, 'DNS hostnames on') : v.dns_hostnames === false ? h('span', { class: 'dim' }, 'DNS hostnames off') : null));
  const subnets = v.subnets || [];
  if (subnets.length) {
    const tbl = h('table', { class: 'tbl dtbl' });
    tbl.appendChild(h('thead', null, h('tr', null, h('th', null, 'Subnet'), h('th', null, 'CIDR'), h('th', null, 'AZ'), h('th', null, 'Default route'))));
    tbl.appendChild(h('tbody', null, subnets.map(s => {
      const target = s.default_route !== undefined ? s.default_route : defaultRouteFor(v, s.route_table);
      const kind = !target ? 'none' : /^igw-/.test(target) ? 'igw' : /^nat-/.test(target) ? 'nat' : 'other';
      return h('tr', { dataset: { resId: s.id } },
        h('td', null, h('div', null, s.name || h('span', { class: 'dim' }, 'unnamed')), copyable(s.id, { fk: 'c:' + s.id, cls: 'copy-sm' })),
        h('td', { class: 'mono' }, s.cidr),
        h('td', { class: 'mono dim' }, s.az ? s.az.replace(/^.*-(\d[a-z])$/, '$1') : '—'),
        h('td', null, h('span', { class: 'route route-' + kind },
          h('span', { class: 'route-badge' }, kind === 'igw' ? 'public' : kind === 'nat' ? 'private' : kind === 'none' ? 'isolated' : 'routed'),
          h('span', { class: 'mono' }, '0.0.0.0/0 → ', target ? (kind === 'nat' ? link('nat_gateways', target) : target) : 'none')),
          s.route_table ? h('div', { class: 'mono dim', style: 'font-size:11px;margin-top:2px' }, 'via ', s.route_table) : null));
    })));
    el.appendChild(h('div', { class: 'tbl-wrap' }, tbl));
  } else {
    el.appendChild(h('div', { class: 'dsec-empty' }, 'No subnets — nothing can be launched into this VPC yet.'));
  }
  const rts = v.route_tables || [];
  if (rts.length) {
    el.appendChild(h('details', { class: 'rts' },
      h('summary', { dataset: { fk: 'rts:' + v.id } }, rts.length + (rts.length === 1 ? ' route table' : ' route tables')),
      h('div', { class: 'rts-body' }, rts.map(rt => h('div', { class: 'rt', dataset: { resId: rt.id } },
        h('div', { class: 'rt-head' }, h('span', null, rt.name || (rt.main ? 'main' : 'route table')), copyable(rt.id, { fk: 'c:' + rt.id, cls: 'copy-sm' }), rt.main ? h('span', { class: 'chip' }, 'main') : null,
          h('span', { class: 'dim' }, (rt.subnets || []).length + ' subnet' + ((rt.subnets || []).length === 1 ? '' : 's'))),
        h('ul', { class: 'routes mono' }, (rt.routes || []).map(x => h('li', null, h('span', null, x.dest || '—'), h('span', { class: 'dim' }, ' → '), x.target || 'local', x.state && x.state !== 'active' ? h('span', { class: 'bad' }, ' ' + x.state) : null))))))));
  }
  return el;
}
function defaultRouteFor(v, rtId) {
  const rt = (v.route_tables || []).find(x => x.id === rtId);
  if (!rt) return null;
  const d = (rt.routes || []).find(x => x.dest === '0.0.0.0/0');
  return d ? d.target : null;
}

function renderSg(r, g) {
  const el = h('div', { class: 'res sg', dataset: { resId: g.id } });
  const attached = g.attached_to || [];
  el.appendChild(h('div', { class: 'res-head' },
    h('b', null, g.name || 'security group'), copyable(g.id, { fk: 'c:' + g.id }),
    g.vpc ? link('vpcs', g.vpc, vpcLabel(r, g.vpc)) : null,
    h('span', { class: 'dim' }, attached.length ? attached.length + ' instance' + (attached.length === 1 ? '' : 's') : 'not attached to any instance')));
  if (g.description) el.appendChild(h('div', { class: 'res-sub' }, g.description));
  if (attached.length) el.appendChild(h('div', { class: 'linkrow', style: 'margin:6px 0 2px' }, attached.map(id => link('instances', id, instLabel(r, id)))));
  el.appendChild(rulesTable('Ingress', g.ingress || [], 'Source', 'No ingress rules — nothing can reach members of this group.'));
  el.appendChild(rulesTable('Egress', g.egress || [], 'Destination', 'No egress rules — members cannot initiate anything.'));
  return el;
}
function rulesTable(title, rules, srcLabel, emptyText) {
  const box = h('div', { class: 'rules' });
  box.appendChild(h('div', { class: 'rules-title' }, title, h('span', { class: 'dim' }, ' ' + rules.length)));
  if (!rules.length) { box.appendChild(h('div', { class: 'dsec-empty compact' }, emptyText)); return box; }
  const tbl = h('table', { class: 'tbl dtbl' });
  tbl.appendChild(h('thead', null, h('tr', null, h('th', null, 'Proto'), h('th', null, 'Ports'), h('th', null, srcLabel))));
  tbl.appendChild(h('tbody', null, rules.map(x => {
    const open = x.source === '0.0.0.0/0' || x.source === '::/0';
    return h('tr', null,
      h('td', { class: 'mono' }, x.proto === 'all' || x.proto == null ? 'all' : x.proto),
      h('td', { class: 'mono' }, x.from == null && x.to == null ? 'all' : x.from === x.to ? String(x.from) : x.from + '–' + x.to),
      h('td', { class: 'mono' + (open ? ' is-open-world' : '') }, x.source == null ? h('span', { class: 'dim' }, '—') : /^sg-/.test(x.source) ? link('security_groups', x.source) : x.source, open ? h('span', { class: 'dim' }, ' anywhere') : null));
  })));
  box.appendChild(h('div', { class: 'tbl-wrap' }, tbl));
  return box;
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
      delete state.outlines[ucId]; // the plan is stale once a job has run
      await loadUsecases(true);
      await loadDetail(ucId, true);
      if (state.expanded === ucId) { loadOutline(ucId, 'on'); loadOutline(ucId, 'off'); loadTopology(ucId, true); } // the drawing regenerates from the cloud after a job
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

/** Why a switch cannot be flipped, from the provider's connection state and capabilities. */
function switchBlocker(uc) {
  const p = providerById(uc.provider);
  const caps = p ? providerCaps(p) : null;
  if (caps && !caps.usecases) {
    return providerName(uc.provider) + (uc.provider_connected ? ' is connected, but use cases are not built for this provider yet — nothing can be operated from Switchboard.' : ' cannot run use cases yet — the provider module only supports connecting.');
  }
  if (!uc.provider_connected) return providerName(uc.provider) + ' line is unplugged — plug it in on the Clouds page to operate this use case.';
  return null;
}

function renderCard(uc) {
  const info = STATE_INFO[uc.state] || STATE_INFO.unknown;
  const busy = /^turning_/.test(uc.state) || !!state.flipping[uc.id];
  const open = state.expanded === uc.id;
  const card = h('div', { class: 'card' + (uc.state === 'on' || busy ? ' is-live' : '') + (uc.state === 'error' ? ' is-error' : '') + (open ? ' is-open' : ''), dataset: { uc: uc.id } });

  const disabledReason = switchBlocker(uc);

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
  if (disabledReason) head.appendChild(h('div', { class: 'card-warn' }, disabledReason, ' ', h('a', { href: KEEP_QUERY + '#/clouds' }, 'Open Clouds')));
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
  loadTopology(id);
  loadOutline(id, 'on');
  loadOutline(id, 'off');
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

  // the drawing first: structure from the cloud, meaning from the manifest
  wrap.appendChild(renderTopology(uc, d));

  const grid = h('div', { class: 'detail-grid' });
  // left: description (what it is, what ON/OFF do, cost, sharing — the network is the drawing above) + source
  const left = h('div');
  left.appendChild(h('div', { class: 'detail-block' }, h('div', { class: 'section-title', style: 'margin-bottom:8px' }, 'Description'),
    h('div', { class: 'md', html: SB.markdown.render(d.description || '_No description in the manifest._') })));
  if (d.source) left.appendChild(h('div', { class: 'detail-block' }, h('div', { class: 'section-title', style: 'margin-bottom:6px' }, 'Source'),
    h('div', { class: 'mono', style: 'font-size:12px;color:var(--text-dim);word-break:break-all' }, d.source.git || '', d.source.ref ? ' @ ' + d.source.ref : '', d.source.commit ? ' · ' + String(d.source.commit).slice(0, 10) : ' · not checked out')));
  grid.appendChild(left);
  // right: status probe
  grid.appendChild(h('div', null, renderProbe(d)));
  wrap.appendChild(grid);

  // outline: what ON and OFF will do — steps + a real plan + declared effects
  const jobId = state.jobFor[uc.id];
  const entry = jobId ? state.jobs[jobId] : null;
  const job = entry && entry.job;
  wrap.appendChild(renderOutline(uc, d, job));

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
  const items = [], tables = [];
  const tone = (v) => /authenticated|healthy|running|ok|^true$|enrolled/i.test(v) ? ' good' : /fail|error|down|^false$|unauth|missing|not enrolled/i.test(v) ? ' poor' : '';
  const cell = (v) => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);
  function walk(prefix, v) {
    if (v && typeof v === 'object' && !Array.isArray(v)) { for (const k of Object.keys(v)) walk(prefix ? prefix + '.' + k : k, v[k]); }
    else if (Array.isArray(v) && v.length && v.every(x => x && typeof x === 'object' && !Array.isArray(x))) tables.push([prefix, v]);   // list of records -> table
    else if (Array.isArray(v)) items.push([prefix, v.map(cell).join(', ')]);
    else items.push([prefix, cell(v)]);
  }
  walk('', s);
  if (items.length > 24) { block.appendChild(h('pre', { class: 'probe-raw' }, JSON.stringify(s, null, 2))); return block; }
  block.appendChild(h('div', { class: 'probe' }, items.map(([k, v]) =>
    h('div', { class: 'probe-item' }, h('span', { class: 'k' }, k), h('span', { class: 'v' + tone(v) }, v)))));
  for (const [name, rows] of tables) {
    // Columns in first-seen order; drop ids that only mean something to the backend.
    const cols = [...new Set(rows.flatMap(r => Object.keys(r)))].filter(c => !/^(group_id|id)$/.test(c));
    const t = h('table', { class: 'drawer-table probe-table' },
      h('thead', null, h('tr', null, cols.map(c => h('th', null, c.replace(/_/g, ' '))))),
      h('tbody', null, rows.map(r => h('tr', null, cols.map(c => { const v = cell(r[c]); return h('td', { class: /_ip$|^version$|enrolled_as/.test(c) ? 'mono' : '' }, h('span', { class: 'v' + tone(v) }, v)); })))));
    block.appendChild(h('div', { class: 'section-title', style: 'margin:12px 0 6px' }, name + ' (' + rows.length + ')'));
    block.appendChild(h('div', { class: 'table-scroll' }, t));
  }
  return block;
}

/* ---- topology: the network drawing ----------------------------------------
   Structure comes from the cloud (GET /api/usecases/{id}/topology: nodes and
   edges built from the live inventory); meaning comes from the manifest
   (roles, declared flows, blocked pairs). Deterministic auto-layout, inline
   SVG, no library. Everything is clickable into the region drawer.

   Layout in one paragraph: the internet node sits top-centre. VPCs form one
   row beneath it, ordered by name; with exactly two VPCs their gateway gutters
   face each other (right gutter on the first, left on the second) so both
   IGWs sit under the internet. Inside a VPC, subnets stack top to bottom
   (public, private, isolated), each a container whose instance cards flow
   into a grid of N columns; N starts at the largest subnet's instance count
   and is reduced one VPC at a time until the row fits the available width
   (below that the canvas scrolls). Every gateway owns a vertical lane in its
   VPC's gutter, and its box sits on the VPC's top edge over that lane; a
   route line runs on the inner side of the lane, declared flows on the
   outer side. Cards connect to the world only through their row's rail (the
   gap above the row) and leave the subnet sideways into the gutter, so lines
   never cross a card or a container they do not belong to. Above the VPCs,
   horizontal trunks carry the traffic: NAT-to-IGW hops nearest the VPCs,
   the IGW-to-internet trunk above that, then one dashed trunk per flow that
   crosses the internet. Ports on cards and on the internet node are ordered
   by the x of their far end, so parallel lines do not cross at the ends.
   ---------------------------------------------------------------------- */

const TOPO = {
  MARGIN: 10, CARD_W: 172, CARD_H: 60, CARD_GAP: 12, ROW_GAP: 30,
  SUB_PAD: 12, SUB_HEAD: 28, SUB_HEAD2: 44, SUB_GAP: 14, SUB_EMPTY_H: 44,
  VPC_PAD: 14, VPC_HEAD: 36, VPC_GAP: 48,
  GW_W: 60, GW_H: 26, LANE: 68, LOCAL_LANE: 30, CHIP_H: 18,
  INET_W: 200, INET_H: 40, MAX_COLS: 6, MAX_SCALE: 1.3,
};
const SVG_NS = 'http://www.w3.org/2000/svg';
function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  if (attrs) for (const k of Object.keys(attrs)) {
    const v = attrs[k];
    if (v === null || v === undefined || v === false) continue;
    if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat(Infinity)) { if (c === null || c === undefined || c === false) continue; el.appendChild(c instanceof Node ? c : document.createTextNode(String(c))); }
  return el;
}
const ROLE_RANK = { pse: 0, connector: 1, app: 2, client: 3 };
const EXPOSURE_RANK = { public: 0, private: 1, isolated: 2 };
const ROLE_WORD = { pse: 'Private Service Edge', connector: 'App Connector', app: 'Application', client: 'Client' };

async function loadTopology(id, refresh) {
  const t = state.topo[id] || (state.topo[id] = { loading: false, data: null, err: null, width: 0, sel: null, refocus: null });
  if (t.loading) return;
  t.loading = true; t.err = null;
  if (state.route === 'usecases') render();
  try { t.data = await api.topology(id, refresh); }
  catch (e) { if (e.status === 401) return; t.err = e.message; }
  t.loading = false;
  if (state.route === 'usecases') render();
}

/** Longest common "word-" prefix of the given names, if it leaves every name non-empty. */
function commonPrefix(names) {
  names = names.filter(Boolean).map(String);
  if (names.length < 2) return '';
  let p = names[0];
  for (const n of names) { let i = 0; while (i < p.length && i < n.length && p[i] === n[i]) i++; p = p.slice(0, i); }
  const cut = Math.max(p.lastIndexOf('-'), p.lastIndexOf('_'), p.lastIndexOf('.'));
  p = cut > 0 ? p.slice(0, cut + 1) : '';
  return p.length >= 4 && names.every(n => n.length > p.length) ? p : '';
}
function trimTo(sv, n) { sv = String(sv || ''); return sv.length > n ? sv.slice(0, n - 1) + '…' : sv; }

/* ---- layout ---- */
function topoLayout(g, avail) {
  const C = TOPO;
  const byId = new Map(); g.nodes.forEach(n => byId.set(n.id, n));
  const kids = new Map();
  for (const n of g.nodes) if (n.parent) { if (!kids.has(n.parent)) kids.set(n.parent, []); kids.get(n.parent).push(n); }
  const cmpLabel = (a, b) => String(a.label || a.id).localeCompare(String(b.label || b.id));
  const children = (id, kind) => (kids.get(id) || []).filter(n => n.kind === kind);
  const vpcs = g.nodes.filter(n => n.kind === 'vpc').sort(cmpLabel);
  const L = { nodes: {}, edges: [], vpcs: [], w: 0, h: 0, scale: 1, unplaced: [], prefix: '' };
  const N = L.nodes;
  L.prefix = commonPrefix(g.nodes.filter(n => n.kind === 'instance' || (/^(subnet|vpc)$/.test(n.kind) && !n.pseudo)).map(n => n.label));
  const strip = label => { const sv = String(label || ''); return L.prefix && sv.startsWith(L.prefix) ? sv.slice(L.prefix.length) : sv; };
  L.strip = strip;

  // VPC plans: subnets, gateways, columns, gutter side
  const plans = vpcs.map((v, i) => {
    const subnets = children(v.id, 'subnet').sort((a, b) => ((EXPOSURE_RANK[a.exposure] ?? 3) - (EXPOSURE_RANK[b.exposure] ?? 3)) || cmpLabel(a, b));
    const gws = [...children(v.id, 'nat').sort(cmpLabel), ...children(v.id, 'igw').sort(cmpLabel)];
    const maxInst = Math.max(1, ...subnets.map(sn => children(sn.id, 'instance').length));
    return { v, subnets, gws, cols: Math.min(C.MAX_COLS, maxInst), side: vpcs.length === 2 && i === 1 ? 'left' : 'right' };
  });
  const subW = cols => cols * C.CARD_W + (cols - 1) * C.CARD_GAP + 2 * C.SUB_PAD;
  const gutterW = p => C.LOCAL_LANE + p.gws.length * C.LANE + 8;
  const vpcW = p => subW(p.cols) + 2 * C.VPC_PAD + gutterW(p);
  const totalW = () => plans.reduce((sum, p) => sum + vpcW(p), 0) + Math.max(0, plans.length - 1) * C.VPC_GAP + 2 * C.MARGIN;
  for (;;) {
    if (totalW() <= avail) break;
    const p = plans.filter(x => x.cols > 1).sort((a, b) => vpcW(b) - vpcW(a))[0];
    if (!p) break;
    p.cols--;
  }

  // helpers over the raw graph
  const vpcOfNode = n => { if (!n) return null; if (n.kind === 'vpc') return n.id; if (n.kind === 'subnet') return n.parent; if (n.kind === 'instance') { const sn = byId.get(n.parent); return sn ? sn.parent : null; } if (n.kind === 'nat' || n.kind === 'igw') return n.parent; return null; };
  const gwOf = (vpcId, kind) => (kids.get(vpcId) || []).find(n => n.kind === kind) || null;
  const egressGw = inst => { const sn = byId.get(inst.parent); const vid = sn ? sn.parent : null; if (!vid) return null; return (sn && sn.exposure === 'private' ? gwOf(vid, 'nat') : null) || gwOf(vid, 'igw') || gwOf(vid, 'nat'); };
  const ingressGw = inst => { const vid = vpcOfNode(inst); return vid ? gwOf(vid, 'igw') : null; };

  // edge chains: [from, ...via, to] with the implicit gateways between an instance and the internet
  const drawn = [];
  g.edges.forEach((e, idx) => {
    if (!/^(route|uplink|flow|blocked)$/.test(e.kind)) return;
    const a = byId.get(e.from), b = byId.get(e.to);
    if (!a || !b) { L.unplaced.push({ label: (e.kind === 'flow' ? 'flow' : e.kind) + ' ' + e.from + ' → ' + e.to + (e.label ? ' (' + e.label + ')' : ''), reason: 'endpoint not in the drawing' }); return; }
    let chain = [a, ...(e.via || []).map(id => byId.get(id)).filter(Boolean), b];
    // a NAT followed by its own IGW then the internet draws as NAT straight up: the hop is implied
    chain = chain.filter((n, i) => !(n.kind === 'igw' && chain[i - 1] && chain[i - 1].kind === 'nat' && chain[i + 1] && chain[i + 1].kind === 'internet' && vpcOfNode(chain[i - 1]) === vpcOfNode(n)));
    const out = [];
    for (let i = 0; i < chain.length; i++) {
      const cur = chain[i], next = chain[i + 1];
      out.push(cur);
      if (!next) break;
      if (cur.kind === 'instance' && next.kind === 'internet') { const gw = egressGw(cur); if (gw) out.push(gw); }
      else if (cur.kind === 'internet' && next.kind === 'instance') { const gw = ingressGw(next); if (gw) out.push(gw); }
    }
    const touchesInet = out.some(n => n.kind === 'internet');
    const vpcSet = new Set(out.map(vpcOfNode).filter(Boolean));
    drawn.push({ e, idx, chain: out, needsTrunk: e.kind !== 'uplink' && (touchesInet || vpcSet.size > 1), lane: {}, trunk: -1 });
  });
  // gateway lanes for flows/blocked, trunks
  const laneCount = {}, localCount = {};
  let trunkCount = 0;
  for (const d of drawn) {
    d.local = {};
    if (d.e.kind === 'flow' || d.e.kind === 'blocked') {
      // which VPCs' local lanes this chain will use: instance-to-instance hops that are not on one rail, and internet-to-instance hops without a gateway
      for (let i = 0; i < d.chain.length - 1; i++) {
        const a = d.chain[i], b = d.chain[i + 1];
        const vids = [];
        if (a.kind === 'instance' && b.kind === 'instance') { if (a.parent !== b.parent || true) { const va = vpcOfNode(a), vb = vpcOfNode(b); if (va) vids.push(va); if (vb && vb !== va) vids.push(vb); } }
        else if (a.kind === 'internet' && b.kind === 'instance') vids.push(vpcOfNode(b));
        else if (a.kind === 'instance' && b.kind === 'internet') vids.push(vpcOfNode(a));
        else if ((a.kind === 'nat' || a.kind === 'igw') && b.kind === 'instance' && vpcOfNode(a) !== vpcOfNode(b)) vids.push(vpcOfNode(b));
        else if (a.kind === 'instance' && (b.kind === 'nat' || b.kind === 'igw') && vpcOfNode(a) !== vpcOfNode(b)) vids.push(vpcOfNode(a));
        for (const vid of vids) if (vid && d.local[vid] === undefined) { const c = localCount[vid] || 0; d.local[vid] = Math.min(2, c); localCount[vid] = c + 1; }
      }
    }
    if (d.e.kind === 'flow' || d.e.kind === 'blocked') for (const n of d.chain) if ((n.kind === 'nat' || n.kind === 'igw') && d.lane[n.id] === undefined) { const c = laneCount[n.id] || 0; d.lane[n.id] = Math.min(2, c); laneCount[n.id] = c + 1; }
    if (d.needsTrunk) d.trunk = trunkCount++;
  }

  // vertical bands above the VPC row
  const inetTop = C.MARGIN;
  const yVpc = inetTop + C.INET_H + 18 + 12 * Math.max(0, trunkCount - 1) + 58;
  L.yVpc = yVpc; L.yHop = yVpc - 22; L.yUplink = yVpc - 40;
  L.trunkY = i => yVpc - 58 - 12 * (Math.max(1, trunkCount) - 1 - i);

  // VPC row geometry
  const W = totalW();
  let x = C.MARGIN;
  const headH = (sn, w) => { if (sn.pseudo) return C.SUB_HEAD; const need = 20 + (strip(sn.label) || sn.id).length * 7.2 + 8 + (sn.cidr ? sn.cidr.length * 6.6 + 10 : 0) + (sn.exposure || 'unknown').length * 6.6 + 18; return need > w ? C.SUB_HEAD2 : C.SUB_HEAD; };
  const subH = sn => { const p = plans.find(x => x.v.id === sn.parent); const hh = headH(sn, subW(p.cols)); const n = children(sn.id, 'instance').length; if (!n) return hh + C.SUB_EMPTY_H - C.SUB_HEAD; const rows = Math.ceil(n / Math.max(1, p.cols)); return hh + C.ROW_GAP + rows * C.CARD_H + (rows - 1) * C.ROW_GAP + C.SUB_PAD; };
  const vpcH = p => C.VPC_HEAD + (p.subnets.length ? p.subnets.reduce((sum, sn) => sum + subH(sn), 0) + (p.subnets.length - 1) * C.SUB_GAP : 40) + C.VPC_PAD;
  const rowH = Math.max(120, ...plans.map(vpcH));
  for (const p of plans) {
    const w = vpcW(p), dir = p.side === 'right' ? 1 : -1;
    const V = { kind: 'vpc', node: p.v, x, y: yVpc, w, h: Math.max(120, vpcH(p)), side: p.side, dir, lanes: {}, subnets: [], gws: [] };
    const sw = subW(p.cols);
    V.subLeft = p.side === 'right' ? x + C.VPC_PAD : x + w - C.VPC_PAD - sw;
    V.subRight = V.subLeft + sw;
    V.localLane = p.side === 'right' ? V.subRight + 12 : V.subLeft - 12;
    p.gws.forEach((gw, k) => {
      const laneX = p.side === 'right' ? V.subRight + C.LOCAL_LANE + k * C.LANE + C.LANE / 2 : V.subLeft - C.LOCAL_LANE - k * C.LANE - C.LANE / 2;
      V.lanes[gw.id] = laneX;
      const hasChip = !!(gw.public_ip || g.nodes.some(n => n.kind === 'eip' && n.attached_to === gw.id));
      N[gw.id] = { kind: gw.kind, node: gw, cx: laneX, cy: yVpc, x: laneX - C.GW_W / 2, y: yVpc - C.GW_H / 2, w: C.GW_W, h: C.GW_H, top: yVpc - C.GW_H / 2, bottom: yVpc + C.GW_H / 2 + (hasChip ? C.CHIP_H + 4 : 0), vpc: p.v.id, laneX, dir, chip: hasChip };
      V.gws.push(gw.id);
    });
    let sy = yVpc + C.VPC_HEAD;
    for (const sn of p.subnets) {
      const insts = children(sn.id, 'instance').sort((a, b) => ((ROLE_RANK[a.role] ?? 4) - (ROLE_RANK[b.role] ?? 4)) || cmpLabel(a, b));
      const S = { kind: 'subnet', node: sn, x: V.subLeft, y: sy, w: sw, h: subH(sn), headH: headH(sn, sw), vpc: p.v.id, side: p.side, dir, insts: [], cols: p.cols, rows: [] };
      insts.forEach((inst, i) => {
        const row = Math.floor(i / p.cols), col = i % p.cols;
        const cx = S.x + C.SUB_PAD + col * (C.CARD_W + C.CARD_GAP);
        const cy = S.y + S.headH + C.ROW_GAP + row * (C.CARD_H + C.ROW_GAP);
        N[inst.id] = { kind: 'instance', node: inst, x: cx, y: cy, w: C.CARD_W, h: C.CARD_H, cx: cx + C.CARD_W / 2, rail: cy - C.ROW_GAP / 2, row, col, subnet: sn.id, vpc: p.v.id, dir };
        S.insts.push(inst.id);
      });
      N[sn.id] = S; V.subnets.push(sn.id);
      sy += S.h + C.SUB_GAP;
    }
    N[p.v.id] = V; L.vpcs.push(p.v.id);
    x += w + C.VPC_GAP;
  }
  const inet = g.nodes.find(n => n.kind === 'internet');
  if (inet) N[inet.id] = { kind: 'internet', node: inet, x: W / 2 - C.INET_W / 2, y: inetTop, w: C.INET_W, h: C.INET_H, cx: W / 2, bottom: inetTop + C.INET_H };
  // idle addresses: a tray top-right, outside any VPC
  const idle = g.nodes.filter(n => n.kind === 'eip' && !n.attached_to).sort(cmpLabel);
  if (idle.length) {
    const tw = 168, th = 26 + idle.length * (C.CHIP_H + 6);
    L.tray = { x: W - C.MARGIN - tw, y: inetTop, w: tw, h: th, ids: idle.map(n => n.id) };
    idle.forEach((n, i) => { N[n.id] = { kind: 'eip', node: n, x: L.tray.x + 10, y: L.tray.y + 24 + i * (C.CHIP_H + 6), w: tw - 20, h: C.CHIP_H, idle: true }; });
  }
  // attached addresses: chips hanging off their node
  for (const n of g.nodes) {
    if (n.kind !== 'eip' || !n.attached_to || N[n.id]) continue;
    const host = N[n.attached_to];
    if (!host) { L.unplaced.push({ label: n.label + ' (address)', reason: 'attached to ' + n.attached_to + ', which is not drawn' }); continue; }
    const cw = Math.max(64, String(n.label || '').length * 6.4 + 14);
    if (host.kind === 'instance') N[n.id] = { kind: 'eip', node: n, x: host.x + host.w - cw - 8, y: host.y + host.h - C.CHIP_H / 2 - 1, w: cw, h: C.CHIP_H, host: n.attached_to };
    else N[n.id] = { kind: 'eip', node: n, x: host.cx - cw / 2, y: host.cy + C.GW_H / 2 + 3, w: cw, h: C.CHIP_H, host: n.attached_to };
  }
  for (const n of g.nodes) if (!N[n.id] && n.kind !== 'internet') L.unplaced.push({ label: (n.label || n.id) + ' (' + n.kind + ')', reason: n.parent ? 'parent ' + n.parent + ' is not in the drawing' : 'no parent' });
  L.w = W; L.h = yVpc + rowH + C.MARGIN + 4;
  if (L.tray && !vpcs.length) L.h = Math.max(L.h, L.tray.y + L.tray.h + C.MARGIN);

  /* ---- routing: two passes; the first collects port requests, the second draws with sorted ports ---- */
  const laneXFor = (gid, d) => { const G = N[gid]; return G.laneX + (d.e.kind === 'route' || d.e.kind === 'uplink' ? -10 : 4 + 8 * (d.lane[gid] || 0)) * G.dir; };
  const localXFor = (vid, d) => { const V = N[vid]; return V.localLane + 8 * (d.local[vid] || 0) * V.dir; };
  const entryReq = {}, inetReq = [], railReq = {};
  let ports = null; // second pass: {entries: {instId: {edgeKey: x}}, inet: {edgeKey: x}, rails: {subnet:row: {edgeKey: y}}}
  function routeAll(final) {
    L.edges = [];
    for (const d of drawn) {
      const pts = [];
      const push = (px, py) => { const l = pts[pts.length - 1]; if (!l || l[0] !== px || l[1] !== py) pts.push([px, py]); };
      const ekey = 'e' + d.idx;
      const entry = (inst, approachX, end) => {
        const key = ekey + end;
        if (!final) { (entryReq[inst.id] = entryReq[inst.id] || []).push({ key, approachX }); return N[inst.id].cx; }
        return ports.entries[inst.id][key];
      };
      // a card touches the world at (entry x, rail y); `end` names the endpoint, a shared end (e.g. 'ab') keeps two cards on one sub-lane
      const port = (inst, approachX, end, railEnd) => {
        const I = N[inst.id], x = entry(inst, approachX, end);
        const rk = I.subnet + ':' + I.row, key = ekey + (railEnd || end);
        if (!final) { (railReq[rk] = railReq[rk] || []).push({ key, span: Math.abs(approachX - I.cx) }); return { x, y: I.rail }; }
        return { x, y: ports.rails[rk][key] };
      };
      const inetPort = (approachX, end, ty) => {
        const key = ekey + end;
        if (!final) { inetReq.push({ key, approachX, ty }); return N[inet.id].cx; }
        return ports.inet[key];
      };
      const ch = d.chain;
      let ok = true;
      for (let i = 0; i < ch.length - 1 && ok; i++) {
        const a = ch[i], b = ch[i + 1], A = N[a.id], B = N[b.id];
        if (!A || !B) { ok = false; break; }
        const first = i === 0, last = i === ch.length - 2;
        const ka = A.kind, kb = B.kind;
        if (ka === 'subnet' && (kb === 'nat' || kb === 'igw')) {
          const sx = A.side === 'right' ? A.x + A.w : A.x, y = A.y + 14, rx = laneXFor(b.id, d);
          push(sx, y); push(rx, y); push(rx, B.bottom);
        } else if (ka === 'instance' && kb === 'instance') {
          if (A.subnet === B.subnet && A.row === B.row) {
            const pa = port(a, B.cx, 'a', 'ab'), pb = port(b, A.cx, 'b', 'ab');
            push(pa.x, A.y); push(pa.x, pa.y); push(pb.x, pb.y); push(pb.x, B.y);
          } else if (A.vpc === B.vpc) {
            const lx = localXFor(A.vpc, d), pa = port(a, lx, 'a'), pb = port(b, lx, 'b');
            push(pa.x, A.y); push(pa.x, pa.y); push(lx, pa.y); push(lx, pb.y); push(pb.x, pb.y); push(pb.x, B.y);
          } else {
            const lxa = localXFor(A.vpc, d), lxb = localXFor(B.vpc, d), ty = L.trunkY(d.trunk), pa = port(a, lxa, 'a'), pb = port(b, lxb, 'b');
            push(pa.x, A.y); push(pa.x, pa.y); push(lxa, pa.y); push(lxa, ty); push(lxb, ty); push(lxb, pb.y); push(pb.x, pb.y); push(pb.x, B.y);
          }
        } else if (ka === 'instance' && (kb === 'nat' || kb === 'igw')) {
          const fx = laneXFor(b.id, d);
          if (A.vpc === B.vpc) { const pa = port(a, fx, 'a'); push(pa.x, A.y); push(pa.x, pa.y); push(fx, pa.y); push(fx, B.bottom); }
          else { const lx = localXFor(A.vpc, d), ty = L.trunkY(d.trunk), pa = port(a, lx, 'a'); push(pa.x, A.y); push(pa.x, pa.y); push(lx, pa.y); push(lx, ty); push(fx, ty); push(fx, B.top); }
        } else if ((ka === 'nat' || ka === 'igw') && kb === 'instance') {
          const fx = laneXFor(a.id, d);
          if (A.vpc === B.vpc) { const pb = port(b, fx, 'b'); push(fx, A.bottom); push(fx, pb.y); push(pb.x, pb.y); push(pb.x, B.y); }
          else { const lx = localXFor(B.vpc, d), ty = L.trunkY(d.trunk), pb = port(b, lx, 'b'); push(fx, A.top); push(fx, ty); push(lx, ty); push(lx, pb.y); push(pb.x, pb.y); push(pb.x, B.y); }
        } else if ((ka === 'nat' || ka === 'igw') && kb === 'internet') {
          const fx = laneXFor(a.id, d), ty = d.e.kind === 'uplink' ? L.yUplink : L.trunkY(d.trunk), px = inetPort(fx, 'a', ty);
          push(fx, A.top); push(fx, ty); push(px, ty); push(px, B.bottom);
        } else if (ka === 'internet' && (kb === 'nat' || kb === 'igw')) {
          const fx = laneXFor(b.id, d), ty = L.trunkY(d.trunk), px = inetPort(fx, 'b', ty);
          push(px, A.bottom); push(px, ty); push(fx, ty); push(fx, B.top);
        } else if (ka === 'internet' && kb === 'instance') {
          const lx = localXFor(B.vpc, d), ty = L.trunkY(d.trunk), px = inetPort(lx, 'b', ty), pb = port(b, lx, 'b');
          push(px, A.bottom); push(px, ty); push(lx, ty); push(lx, pb.y); push(pb.x, pb.y); push(pb.x, B.y);
        } else if (ka === 'instance' && kb === 'internet') {
          const lx = localXFor(A.vpc, d), ty = L.trunkY(d.trunk), px = inetPort(lx, 'a', ty), pa = port(a, lx, 'a');
          push(pa.x, A.y); push(pa.x, pa.y); push(lx, pa.y); push(lx, ty); push(px, ty); push(px, B.bottom);
        } else if ((ka === 'nat' || ka === 'igw') && (kb === 'nat' || kb === 'igw')) {
          const xa = laneXFor(a.id, d), xb = laneXFor(b.id, d);
          if (A.vpc === B.vpc && d.e.kind === 'uplink' && Math.abs(A.cx - B.cx) <= C.LANE + 1) { const sgn = B.cx > A.cx ? 1 : -1; push(A.cx + sgn * C.GW_W / 2, A.cy); push(B.cx - sgn * C.GW_W / 2, B.cy); }
          else if (A.vpc === B.vpc) { const hy = d.e.kind === 'uplink' ? L.yHop : L.yHop - 6; push(xa, A.top); push(xa, hy); push(xb, hy); if (!last || d.e.kind !== 'uplink') push(xb, B.top); }
          else { const ty = L.trunkY(d.trunk); push(xa, A.top); push(xa, ty); push(xb, ty); push(xb, B.top); }
        } else {
          // anything else: a plain L between centres
          const acx = A.cx !== undefined ? A.cx : A.x + A.w / 2, acy = A.y + A.h / 2, bcx = B.cx !== undefined ? B.cx : B.x + B.w / 2, bcy = B.y + B.h / 2;
          push(acx, acy); push(acx, bcy); push(bcx, bcy);
        }
        void first;
      }
      if (!ok || pts.length < 2) continue;
      L.edges.push({ e: d.e, idx: d.idx, pts: simplifyPts(pts), chain: ch.map(n => n.id) });
    }
  }
  routeAll(false);
  const spread = (k, K, pitch, max) => Math.max(-max, Math.min(max, (k - (K - 1) / 2) * pitch));
  ports = { entries: {}, inet: {} };
  for (const [id, reqs] of Object.entries(entryReq)) {
    const I = N[id];
    // the farther a line's far end lies to the right, the further left it enters the card, so drops never cross another line's run
    const order = reqs.map((r, i) => Object.assign({ i }, r)).sort((p, q) => (q.approachX - p.approachX) || (p.i - q.i));
    const K = order.length;
    ports.entries[id] = {};
    order.forEach((r, k) => { ports.entries[id][r.key] = I.cx + spread(k, K, 12, I.w / 2 - 16); });
  }
  ports.rails = {};
  for (const [rk, reqs] of Object.entries(railReq)) {
    const seen = new Map(); reqs.forEach(r => { if (!seen.has(r.key) || seen.get(r.key).span < r.span) seen.set(r.key, r); });
    const order = Array.from(seen.values()).sort((p, q) => q.span - p.span);
    const cut = rk.lastIndexOf(':'), sid = rk.slice(0, cut), row = rk.slice(cut + 1);
    const base = N[sid].y + N[sid].headH + C.ROW_GAP + Number(row) * (C.CARD_H + C.ROW_GAP) - C.ROW_GAP / 2;
    ports.rails[rk] = {};
    order.forEach((r, k) => { ports.rails[rk][r.key] = base + spread(k, order.length, 8, C.ROW_GAP / 2 - 3); });
  }
  if (inet) {
    const cx = N[inet.id].cx;
    const left = inetReq.filter(r => r.approachX < cx).sort((p, q) => (p.ty - q.ty) || (p.approachX - q.approachX));
    const right = inetReq.filter(r => r.approachX >= cx).sort((p, q) => (q.ty - p.ty) || (p.approachX - q.approachX));
    const order = left.concat(right);
    order.forEach((r, k) => { const px = cx + spread(k, order.length, 12, C.INET_W / 2 - 14); ports.inet[r.key] = Math.abs(px - r.approachX) < 10 ? r.approachX : px; });
  }
  routeAll(true);
  return L;
}
function simplifyPts(pts) {
  const out = [];
  for (const p of pts) {
    const a = out[out.length - 2], b = out[out.length - 1];
    if (b && b[0] === p[0] && b[1] === p[1]) continue;
    if (a && b && ((a[0] === b[0] && b[0] === p[0]) || (a[1] === b[1] && b[1] === p[1]))) { out[out.length - 1] = p; continue; }
    out.push(p);
  }
  return out;
}
/** Midpoint of the longest segment, preferring a horizontal one for a readable label. */
function labelSpot(pts, preferVertical) {
  let best = null;
  // a blocked pair marks its strike on a gutter (vertical) run when it has one of any length, never on a shared rail
  const longestVertical = preferVertical ? Math.max(0, ...pts.slice(1).map((p, i) => p[0] === pts[i][0] ? Math.abs(p[1] - pts[i][1]) : 0)) : 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const [x1, y1] = pts[i], [x2, y2] = pts[i + 1];
    const len = Math.abs(x2 - x1) + Math.abs(y2 - y1), horiz = y1 === y2;
    const score = len * (preferVertical ? (horiz ? (longestVertical >= 40 ? 0.01 : 1) : 2) : (horiz ? 1.6 : 1));
    if (!best || score > best.score) best = { score, x: (x1 + x2) / 2, y: (y1 + y2) / 2, horiz, len };
  }
  return best;
}

/* ---- glyphs: one simple shape per role ---- */
function roleGlyph(role) {
  const g = s('g', { class: 'glyph glyph-' + (role || 'none') });
  if (role === 'pse') { g.appendChild(s('path', { d: 'M9 1.5l7 4v7l-7 4-7-4v-7z' })); g.appendChild(s('circle', { cx: 9, cy: 9, r: 2.4, class: 'fill' })); }
  else if (role === 'connector') { g.appendChild(s('circle', { cx: 4.5, cy: 9, r: 3 })); g.appendChild(s('circle', { cx: 13.5, cy: 9, r: 3 })); g.appendChild(s('path', { d: 'M7.5 9h3' })); }
  else if (role === 'app') { g.appendChild(s('rect', { x: 2, y: 3, width: 14, height: 12, rx: 1.5 })); g.appendChild(s('path', { d: 'M2 7h14M5 11h5' })); }
  else if (role === 'client') { g.appendChild(s('rect', { x: 2, y: 2.5, width: 14, height: 10, rx: 1.5 })); g.appendChild(s('path', { d: 'M9 12.5v3M5.5 15.5h7' })); }
  else { g.appendChild(s('circle', { cx: 9, cy: 9, r: 6 })); }
  return g;
}

/* ---- the drawing ---- */
function topoSvg(g, L, ctx) {
  const C = TOPO, N = L.nodes;
  const uid = 'tp' + (topoSvg.seq = (topoSvg.seq || 0) + 1);
  const svg = s('svg', { class: 'topo-svg', viewBox: '0 0 ' + L.w + ' ' + L.h, width: Math.round(L.w * L.scale), height: Math.round(L.h * L.scale), role: 'group', 'aria-label': 'Network drawing' });
  const defs = s('defs');
  const arrow = (id, cls) => s('marker', { id: uid + '-' + id, class: 'mk ' + cls, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' }, s('path', { d: 'M0 0.5L10 5 0 9.5z' }));
  defs.appendChild(arrow('flow', 'mk-flow')); defs.appendChild(arrow('flowhi', 'mk-flow-hi'));
  svg.appendChild(defs);
  const strip = L.strip;
  const idOf = n => n.id;
  const gEdges = s('g', { class: 'topo-edges' });
  const gBoxes = s('g', { class: 'topo-nodes topo-containers' });
  const gNodes = s('g', { class: 'topo-nodes topo-leaves' });

  // VPCs, subnets, instances (DOM order = focus order: vpc, its subnets, their instances, then the gateways)
  for (const vid of L.vpcs) {
    const V = N[vid], v = V.node;
    const gv = s('g', { class: 'tn tn-vpc', dataset: { node: vid, kind: 'vpc' }, tabindex: 0, role: 'button', 'aria-label': 'VPC ' + (v.label || vid) + (v.cidr ? ' ' + v.cidr : '') });
    gv.appendChild(s('rect', { class: 'tn-box', x: V.x, y: V.y, width: V.w, height: V.h, rx: 8 }));
    const tx = V.side === 'right' ? V.x + 14 : V.subLeft;
    gv.appendChild(s('text', { class: 'tn-vpc-name', x: tx, y: V.y + 22 }, strip(v.label) || v.id));
    if (v.cidr) gv.appendChild(s('text', { class: 'tn-vpc-cidr mono', x: tx + 8 + Math.min(220, String(strip(v.label) || v.id).length * 7.6), y: V.y + 22 }, v.cidr));
    if (!V.subnets.length) gv.appendChild(s('text', { class: 'tn-note', x: V.x + 14, y: V.y + C.VPC_HEAD + 20 }, 'no subnets'));
    gBoxes.appendChild(gv);
    for (const sid of V.subnets) {
      const S = N[sid], sn = S.node;
      const gs = s('g', { class: 'tn tn-subnet tn-exp-' + (sn.exposure || 'unknown'), dataset: { node: sid, kind: 'subnet' }, tabindex: 0, role: 'button', 'aria-label': 'Subnet ' + (sn.label || sid) + ' ' + (sn.cidr || '') + ' ' + (sn.exposure || '') });
      gs.appendChild(s('rect', { class: 'tn-box', x: S.x, y: S.y, width: S.w, height: S.h, rx: 6 }));
      gs.appendChild(s('text', { class: 'tn-sub-name', x: S.x + 10, y: S.y + 18 }, strip(sn.label) || sn.id));
      const nameW = Math.min(S.w - 30, String(strip(sn.label) || sn.id).length * 7.2) + 8;
      if (sn.cidr) gs.appendChild(S.headH > C.SUB_HEAD ? s('text', { class: 'tn-sub-cidr mono', x: S.x + 10, y: S.y + 35 }, sn.cidr) : s('text', { class: 'tn-sub-cidr mono', x: S.x + 10 + nameW, y: S.y + 18 }, sn.cidr));
      if (!sn.pseudo) {
        const badgeW = 8 + (sn.exposure || 'unknown').length * 6.6;
        const bx = S.x + S.w - 10 - badgeW;
        gs.appendChild(s('g', { class: 'badge' }, s('rect', { x: bx, y: S.y + 7, width: badgeW, height: 15, rx: 3 }), s('text', { x: bx + badgeW / 2, y: S.y + 18, 'text-anchor': 'middle' }, sn.exposure || 'unknown')));
      }
      if (!S.insts.length) {
        const natHere = g.nodes.find(n => n.kind === 'nat' && n.detail && n.detail.subnet === sid) || g.nodes.find(n => n.kind === 'nat' && n.subnet === sid);
        gs.appendChild(s('text', { class: 'tn-note', x: S.x + 10, y: S.y + S.h - 6 }, natHere ? 'no instances — hosts the NAT gateway' : 'no instances'));
      }
      gBoxes.appendChild(gs);
      for (const iid of S.insts) {
        const I = N[iid], inst = I.node;
        const en = (g.enrolment || {})[iid];
        const lamp = en ? (en.authenticated ? 'on' : 'bad') : (inst.state === 'running' ? 'running' : 'off');
        const gi = s('g', { class: 'tn tn-instance tn-role-' + (inst.role || 'none') + ' lamp-' + lamp, dataset: { node: iid, kind: 'instance' }, tabindex: 0, role: 'button',
          'aria-label': (ROLE_WORD[inst.role] ? ROLE_WORD[inst.role] + ' ' : '') + (inst.label || iid) + (en ? (en.authenticated ? ', enrolled' : ', not enrolled') : '') });
        gi.appendChild(s('rect', { class: 'tn-box', x: I.x, y: I.y, width: I.w, height: I.h, rx: 5 }));
        const gl = roleGlyph(inst.role); gl.setAttribute('transform', 'translate(' + (I.x + 10) + ',' + (I.y + 9) + ')'); gi.appendChild(gl);
        gi.appendChild(s('text', { class: 'tn-name', x: I.x + 34, y: I.y + 22 }, trimTo(strip(inst.label) || iid, 17)));
        gi.appendChild(s('circle', { class: 'tn-lamp', cx: I.x + I.w - 13, cy: I.y + 14, r: 4 }));
        gi.appendChild(s('text', { class: 'tn-sub mono', x: I.x + 10, y: I.y + 44 }, inst.pseudo ? (ROLE_WORD[inst.role] || 'declared role').toLowerCase() : ([inst.type, inst.private_ip].filter(Boolean).join(' · ') || inst.id)));
        gNodes.appendChild(gi);
      }
    }
    for (const gid of V.gws) {
      const G = N[gid], gw = G.node;
      const gg = s('g', { class: 'tn tn-gw tn-' + gw.kind, dataset: { node: gid, kind: gw.kind }, tabindex: 0, role: 'button', 'aria-label': (gw.kind === 'nat' ? 'NAT gateway ' : 'Internet gateway ') + (gw.label || gid) });
      gg.appendChild(s('rect', { class: 'tn-box', x: G.x, y: G.y, width: G.w, height: G.h, rx: 4 }));
      gg.appendChild(s('text', { class: 'tn-gw-name', x: G.cx, y: G.cy + 4, 'text-anchor': 'middle' }, gw.kind === 'nat' ? 'NAT' : 'IGW'));
      gNodes.appendChild(gg);
    }
  }
  // internet
  const inet = g.nodes.find(n => n.kind === 'internet');
  if (inet && N[inet.id]) {
    const I = N[inet.id];
    const gi = s('g', { class: 'tn tn-internet', dataset: { node: inet.id, kind: 'internet' }, tabindex: 0, role: 'button', 'aria-label': 'Internet' });
    gi.appendChild(s('rect', { class: 'tn-box', x: I.x, y: I.y, width: I.w, height: I.h, rx: 20 }));
    gi.appendChild(s('text', { class: 'tn-inet-name', x: I.cx, y: I.y + 25, 'text-anchor': 'middle' }, inet.label || 'Internet'));
    gNodes.insertBefore(gi, gNodes.firstChild);
  }
  // addresses
  if (L.tray) {
    const T = L.tray;
    const gt = s('g', { class: 'topo-tray' });
    gt.appendChild(s('rect', { class: 'tray-box', x: T.x, y: T.y, width: T.w, height: T.h, rx: 6 }));
    gt.appendChild(s('text', { class: 'tray-title', x: T.x + 10, y: T.y + 16 }, 'unattached addresses'));
    gBoxes.appendChild(gt);
  }
  for (const n of g.nodes) {
    if (n.kind !== 'eip' || !N[n.id]) continue;
    const E = N[n.id];
    const ge = s('g', { class: 'tn tn-eip' + (E.idle ? ' is-idle' : ''), dataset: { node: n.id, kind: 'eip' }, tabindex: 0, role: 'button', 'aria-label': 'Elastic IP ' + n.label + (E.idle ? ', unattached' : '') });
    ge.appendChild(s('rect', { class: 'tn-box', x: E.x, y: E.y, width: E.w, height: E.h, rx: 9 }));
    ge.appendChild(s('text', { class: 'tn-eip-ip mono', x: E.x + E.w / 2, y: E.y + 13, 'text-anchor': 'middle' }, n.label));
    gNodes.appendChild(ge);
  }

  // edges: structure first (under), then flows, then blocked (top)
  const order = { route: 0, uplink: 1, flow: 2, blocked: 3 };
  const edges = L.edges.slice().sort((a, b) => (order[a.e.kind] - order[b.e.kind]) || (a.idx - b.idx));
  const spots = edges.map(ed => { const sp = labelSpot(ed.pts, ed.e.kind === 'blocked'); const label = ed.e.label || (ed.e.kind === 'route' ? 'default route' : ed.e.kind === 'uplink' ? 'uplink' : ''); return sp ? Object.assign(sp, { label, w: label.length * 6.3 + 8, x0: sp.x }) : null; });
  // de-conflict: labels that share a horizontal band are packed left to right, then the band is re-centred on its own midpoint
  const bands = [];
  spots.filter(sp => sp && sp.label).forEach(sp => {
    let band = bands.find(b => Math.abs(b.y - sp.y) < 26 && b.items.some(o => Math.abs(o.x0 - sp.x0) < (o.w + sp.w) / 2 + 40)); // a rail's sub-lanes span ±12
    if (!band) { band = { y: sp.y, items: [] }; bands.push(band); }
    band.items.push(sp);
  });
  for (const band of bands) {
    if (band.items.length < 2) continue;
    const items = band.items.sort((a, b) => a.x0 - b.x0);
    const total = items.reduce((sum, o) => sum + o.w, 0) + 10 * (items.length - 1);
    const mid = items.reduce((sum, o) => sum + o.x0, 0) / items.length;
    let x = mid - total / 2;
    for (const o of items) { o.x = x + o.w / 2; x += o.w + 10; if (!o.horiz) o.y += 0; }
  }
  edges.forEach((ed, i) => {
    const e = ed.e, spot = spots[i];
    const dstr = ed.pts.map((p, k) => (k ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ');
    const ge = s('g', { class: 'te te-' + e.kind, dataset: { edge: String(ed.idx), from: e.from, to: e.to, via: ed.chain.join(' ') } });
    ge.appendChild(s('path', { class: 'te-hit', d: dstr }));
    ge.appendChild(s('path', { class: 'te-line', d: dstr, 'marker-end': e.kind === 'flow' ? 'url(#' + uid + '-flow)' : null }));
    if (e.kind === 'uplink' && ed.pts.length > 2) { const last = ed.pts[ed.pts.length - 1]; const target = g.nodes.find(n => n.id === e.to); if (target && (target.kind === 'igw' || target.kind === 'nat')) ge.appendChild(s('circle', { class: 'te-dot', cx: last[0], cy: last[1], r: 2.5 })); }
    if (e.kind === 'blocked' && spot) ge.appendChild(s('path', { class: 'te-strike', d: 'M' + (spot.x0 - 6) + ' ' + (spot.y - 6) + 'L' + (spot.x0 + 6) + ' ' + (spot.y + 6) + 'M' + (spot.x0 + 6) + ' ' + (spot.y - 6) + 'L' + (spot.x0 - 6) + ' ' + (spot.y + 6) }));
    if (spot && spot.label) {
      const lx = spot.horiz ? spot.x : spot.x + 8, ly = spot.horiz ? spot.y - 6 : spot.y + 4;
      ge.appendChild(s('text', { class: 'te-label', x: lx, y: ly, 'text-anchor': spot.horiz ? 'middle' : 'start' }, spot.label));
    }
    gEdges.appendChild(ge);
  });
  svg.appendChild(gBoxes);
  svg.appendChild(gEdges);
  svg.appendChild(gNodes);
  void idOf;
  return svg;
}

/* ---- the off-state schematic: the manifest's declared roles and flows, greyed ---- */
function schematicGraph(decl) {
  const roles = (decl && decl.roles) || {};
  const flows = (decl && decl.flows) || [], blocked = (decl && decl.blocked) || [];
  const names = new Set(Object.keys(roles));
  for (const f of [...flows, ...blocked]) { if (f.from && f.from !== 'internet') names.add(f.from); if (f.to && f.to !== 'internet') names.add(f.to); }
  if (!names.size) return null;
  const rid = n => 'role:' + n;
  const needNat = flows.some(f => (f.via || []).includes('nat')), needIgw = true;
  const nodes = [{ id: 'internet', kind: 'internet', label: 'Internet' },
    { id: 'decl:vpc', kind: 'vpc', label: 'declared topology', cidr: null, parent: null, pseudo: true },
    { id: 'decl:subnet', kind: 'subnet', label: 'roles', cidr: null, parent: 'decl:vpc', exposure: null, pseudo: true }];
  if (needNat) nodes.push({ id: 'decl:nat', kind: 'nat', label: 'NAT', parent: 'decl:vpc', pseudo: true });
  if (needIgw) nodes.push({ id: 'decl:igw', kind: 'igw', label: 'IGW', parent: 'decl:vpc', pseudo: true });
  for (const n of names) nodes.push({ id: rid(n), kind: 'instance', label: n, parent: 'decl:subnet', role: roles[n] || null, type: null, state: null, private_ip: null, pseudo: true });
  const edges = [];
  if (needNat) edges.push({ kind: 'uplink', from: 'decl:nat', to: 'decl:igw' });
  edges.push({ kind: 'uplink', from: 'decl:igw', to: 'internet' });
  const ref = n => n === 'internet' ? 'internet' : rid(n);
  for (const f of flows) edges.push({ kind: 'flow', from: ref(f.from), to: ref(f.to), label: f.label || '', declared: true, via: (f.via || []).map(v => v === 'nat' ? 'decl:nat' : v === 'internet' ? 'internet' : v === 'igw' ? 'decl:igw' : rid(v)) });
  for (const b of blocked) edges.push({ kind: 'blocked', from: ref(b.from), to: ref(b.to), label: b.label || 'blocked', declared: true });
  return { nodes, edges, enrolment: {}, unknown: [], schematic: true };
}

/* ---- inspector ---- */
function topoInspector(g, L, sel, uc) {
  const box = h('div', { class: 'topo-insp-body' });
  const byId = new Map(g.nodes.map(n => [n.id, n]));
  const row = (k, v) => v === null || v === undefined || v === '' ? null : h('div', { class: 'ti-row' }, h('span', { class: 'ti-k' }, k), h('span', { class: 'ti-v' }, v));
  const mono = v => h('span', { class: 'mono' }, v);
  const nameOf = id => { const n = byId.get(id); return n ? (n.label || id) : id; };
  const kindWord = { instance: 'Instance', subnet: 'Subnet', vpc: 'VPC', nat: 'NAT gateway', igw: 'Internet gateway', eip: 'Elastic IP', internet: 'Internet' };
  if (!sel) {
    const count = k => g.nodes.filter(n => n.kind === k).length;
    box.appendChild(h('div', { class: 'ti-title' }, g.schematic ? 'Declared topology' : 'Drawing'));
    box.appendChild(h('div', { class: 'ti-hint' }, g.schematic ? 'Roles and flows from the manifest. Structure appears when the use case is on.' : 'Hover or focus anything for its facts. Click to open it in the region drawer.'));
    if (!g.schematic) {
      box.appendChild(row('Region', mono(g.region || '—')));
      box.appendChild(row('Generated', g.generated_at ? h('span', { title: g.generated_at, dataset: { rel: g.generated_at } }, fmtRel(g.generated_at)) : '—'));
      box.appendChild(row('VPCs', mono(String(count('vpc')))));
      box.appendChild(row('Subnets', mono(String(count('subnet')))));
      box.appendChild(row('Instances', mono(String(count('instance')))));
      box.appendChild(row('Gateways', mono(count('igw') + ' IGW · ' + count('nat') + ' NAT')));
      box.appendChild(row('Addresses', mono(count('eip') + (g.nodes.some(n => n.kind === 'eip' && !n.attached_to) ? ' (' + g.nodes.filter(n => n.kind === 'eip' && !n.attached_to).length + ' idle)' : ''))));
    }
    box.appendChild(row('Flows', mono(g.edges.filter(e => e.kind === 'flow').length + ' declared')));
    box.appendChild(row('Blocked', mono(String(g.edges.filter(e => e.kind === 'blocked').length))));
    if (L && L.prefix) box.appendChild(row('Names', ['shown without the ', mono(L.prefix), ' prefix']));
    return packInspector(box);
  }
  if (sel.edge !== undefined) {
    const e = g.edges[sel.edge]; if (!e) return packInspector(box);
    const words = { route: 'Default route', uplink: 'Uplink', flow: 'Declared flow', blocked: 'Blocked pair' };
    box.appendChild(h('div', { class: 'ti-title ti-edge-' + e.kind }, words[e.kind] || e.kind));
    box.appendChild(row('From', nameOf(e.from)));
    box.appendChild(row('To', nameOf(e.to)));
    box.appendChild(row('Label', e.label ? mono(e.label) : null));
    if (e.via && e.via.length) box.appendChild(row('Via', e.via.map(nameOf).join(' → ')));
    box.appendChild(row('Source', e.kind === 'flow' || e.kind === 'blocked' ? 'manifest' : 'cloud inventory'));
    return packInspector(box);
  }
  const n = byId.get(sel.node); if (!n) return packInspector(box);
  const G = L.nodes[n.id] || {};
  box.appendChild(h('div', { class: 'ti-kicker' }, kindWord[n.kind] || n.kind, n.role && ROLE_WORD[n.role] ? ' · ' + ROLE_WORD[n.role] : ''));
  box.appendChild(h('div', { class: 'ti-title' }, n.label || n.id));
  const en = (g.enrolment || {})[n.id];
  if (n.kind === 'instance') {
    const sn = byId.get(n.parent), v = sn ? byId.get(sn.parent) : null;
    const eip = g.nodes.find(x => x.kind === 'eip' && x.attached_to === n.id);
    box.appendChild(row('Enrolment', en ? h('span', { class: en.authenticated ? 'ok' : 'bad' }, (en.authenticated ? 'authenticated' : 'not authenticated') + (en.label ? ' · ' + en.label : '')) : (n.pseudo ? null : h('span', { class: 'dim' }, 'not an enrolling component'))));
    box.appendChild(row('State', n.state ? h('span', { class: n.state === 'running' ? 'ok' : '' }, n.state) : null));
    box.appendChild(row('Type', n.type ? mono(n.type) : null));
    box.appendChild(row('Private IP', n.private_ip ? mono(n.private_ip) : null));
    box.appendChild(row('Public IP', n.public_ip ? [mono(n.public_ip), eip ? h('span', { class: 'dim' }, ' elastic') : h('span', { class: 'dim' }, ' auto-assigned')] : (n.pseudo ? null : h('span', { class: 'dim' }, 'none'))));
    box.appendChild(row('Subnet', sn ? [sn.label || sn.id, sn.cidr ? [' ', mono(sn.cidr)] : null, sn.exposure ? [' · ', sn.exposure] : null] : null));
    box.appendChild(row('VPC', v ? [v.label || v.id, v.cidr ? [' ', mono(v.cidr)] : null] : null));
    const allows = g.edges.filter(e => e.kind === 'allow' && e.to === n.id);
    if (allows.length) box.appendChild(row('Allowed in', h('div', { class: 'ti-list' }, allows.map(a => h('div', null, mono(a.label || 'all'), h('span', { class: 'dim' }, ' from '), mono(nameOf(a.from)), /^(0\.0\.0\.0\/0|::\/0)$/.test(a.from) ? h('span', { class: 'ti-world' }, ' anywhere') : null)))));
    box.appendChild(row('Id', n.pseudo ? null : mono(n.id)));
  } else if (n.kind === 'subnet') {
    const v = byId.get(n.parent);
    const route = g.edges.find(e => e.kind === 'route' && e.from === n.id);
    box.appendChild(row('CIDR', n.cidr ? mono(n.cidr) : null));
    box.appendChild(row('Exposure', n.exposure ? [n.exposure, h('span', { class: 'dim' }, n.exposure === 'public' ? ' — default route to an internet gateway' : n.exposure === 'private' ? ' — default route through a NAT' : ' — no default route')] : null));
    box.appendChild(row('Default route', route ? [mono(route.label || '0.0.0.0/0'), ' → ', nameOf(route.to)] : (n.pseudo ? null : h('span', { class: 'dim' }, 'none'))));
    box.appendChild(row('AZ', n.az ? mono(n.az) : null));
    box.appendChild(row('Instances', mono(String(g.nodes.filter(x => x.kind === 'instance' && x.parent === n.id).length))));
    box.appendChild(row('VPC', v ? [v.label || v.id, v.cidr ? [' ', mono(v.cidr)] : null] : null));
    box.appendChild(row('Id', n.pseudo ? null : mono(n.id)));
  } else if (n.kind === 'vpc') {
    const subs = g.nodes.filter(x => x.kind === 'subnet' && x.parent === n.id);
    const insts = g.nodes.filter(x => x.kind === 'instance' && subs.some(sn => sn.id === x.parent));
    box.appendChild(row('CIDR', n.cidr ? mono(n.cidr) : null));
    box.appendChild(row('Subnets', mono(subs.length + (subs.length ? ' (' + subs.map(x => x.exposure || '?').join(', ') + ')' : ''))));
    box.appendChild(row('Instances', mono(String(insts.length))));
    box.appendChild(row('Gateways', g.nodes.filter(x => (x.kind === 'igw' || x.kind === 'nat') && x.parent === n.id).map(x => x.kind === 'nat' ? 'NAT' : 'IGW').join(' · ') || h('span', { class: 'dim' }, 'none — isolated')));
    box.appendChild(row('Id', n.pseudo ? null : mono(n.id)));
  } else if (n.kind === 'nat' || n.kind === 'igw') {
    const v = byId.get(n.parent);
    const eip = g.nodes.find(x => x.kind === 'eip' && x.attached_to === n.id);
    box.appendChild(row('Public IP', n.public_ip || (eip && eip.label) ? mono(n.public_ip || eip.label) : null));
    box.appendChild(row('Serves', g.edges.filter(e => e.kind === 'route' && e.to === n.id).map(e => nameOf(e.from)).join(', ') || (n.pseudo ? null : h('span', { class: 'dim' }, 'no subnet routes here'))));
    box.appendChild(row('VPC', v ? [v.label || v.id, v.cidr ? [' ', mono(v.cidr)] : null] : null));
    box.appendChild(row('Id', n.pseudo ? null : mono(n.id)));
  } else if (n.kind === 'eip') {
    box.appendChild(row('Address', mono(n.label)));
    box.appendChild(row('Attached to', n.attached_to ? nameOf(n.attached_to) : h('span', { class: 'flag' }, icon('flag'), 'nothing — idle, billed hourly')));
    box.appendChild(row('Id', mono(n.id)));
  } else if (n.kind === 'internet') {
    box.appendChild(row('Meaning', 'Everything outside the VPCs. A line through here crosses the public internet.'));
    box.appendChild(row('Flows', mono(String(g.edges.filter(e => e.kind === 'flow' && (e.to === n.id || (e.via || []).includes(n.id))).length))));
  }
  const flows = g.edges.filter(e => (e.kind === 'flow' || e.kind === 'blocked') && (e.from === n.id || e.to === n.id));
  if (flows.length) box.appendChild(row('Traffic', h('div', { class: 'ti-list' }, flows.map(e => h('div', { class: e.kind === 'blocked' ? 'bad' : '' }, e.from === n.id ? ['→ ', nameOf(e.to)] : ['← ', nameOf(e.from)], e.label ? [' ', mono(e.label)] : null, e.kind === 'blocked' ? ' (blocked)' : null)))));
  if (!n.pseudo && n.kind !== 'internet' && n.kind !== 'igw' && uc) {
    box.appendChild(h('button', { class: 'btn btn-sm ti-open', type: 'button', onclick: () => openTopoNode(uc, g, n) }, icon('link'), 'Open in region drawer'));
  }
  void G;
  return packInspector(box);
}

function packInspector(box) {
  const head = h('div', { class: 'ti-head' }), rows = h('div', { class: 'ti-rows' });
  let btn = null;
  for (const c of Array.from(box.childNodes)) { if (c.classList && c.classList.contains('ti-row')) rows.appendChild(c); else if (c.classList && c.classList.contains('ti-open')) btn = c; else head.appendChild(c); }
  clear(box); box.appendChild(head); box.appendChild(rows); if (btn) head.appendChild(btn);
  return box;
}
/** Click / Enter on a node: the region drawer, deep-linked to that resource. */
function openTopoNode(uc, g, n) {
  if (!n || n.pseudo || n.kind === 'internet' || !g.region) return;
  const kind = { instance: 'inst', subnet: 'subnet', vpc: 'vpc', nat: 'nat', eip: 'eip' }[n.kind];
  if (!kind) return;
  openResourceDrawer(uc.provider || g.provider || 'aws', g.region, kind, n.id, { returnTo: '#/usecases', returnFocus: n.id });
}

/* ---- the block in the card ---- */
function renderTopology(uc, d) {
  const t = state.topo[uc.id] || (state.topo[uc.id] = { loading: false, data: null, err: null, width: 0, sel: null, refocus: null });
  const block = h('div', { class: 'detail-block topo-block' });
  const data = t.data;
  const live = data && data.nodes && data.nodes.length > 0;
  const head = h('div', { class: 'section-head topo-head' },
    h('div', { class: 'topo-headings' },
      h('span', { class: 'section-title' }, 'Topology'),
      h('span', { class: 'topo-hint' }, live ? 'Boxes and lines come from the cloud; dashed flows and blocked pairs are declared in the manifest.' : 'Structure from the cloud, meaning from the manifest.')),
    h('div', { class: 'topo-tools' },
      data && data.generated_at && live ? h('span', { class: 'topo-when mono', title: data.generated_at }, data.region ? data.region + ' · ' : '', 'generated ', h('span', { dataset: { rel: data.generated_at } }, fmtRel(data.generated_at))) : null,
      h('button', { class: 'btn btn-sm', type: 'button', disabled: !!t.loading, title: 'Rebuild the drawing from the cloud now', onclick: () => loadTopology(uc.id, true) }, icon('refresh'), t.loading ? 'Drawing' : 'Redraw')));
  block.appendChild(head);

  if (!data && t.loading) {
    block.appendChild(h('div', { class: 'topo-stage is-loading' }, h('div', { class: 'skeleton', style: 'height:300px' }), h('span', { class: 'loading topo-loading' }, 'Reading the inventory')));
    return block;
  }
  if (!data && t.err) {
    block.appendChild(h('div', { class: 'state-box is-error' }, h('div', { class: 'title' }, 'Could not draw the topology'), h('div', null, t.err),
      h('button', { class: 'btn', type: 'button', onclick: () => loadTopology(uc.id, true) }, 'Retry')));
    return block;
  }
  if (!data) {
    block.appendChild(h('div', { class: 'state-box' }, h('div', { class: 'title' }, 'Not drawn yet'), h('button', { class: 'btn', type: 'button', onclick: () => loadTopology(uc.id) }, 'Draw')));
    return block;
  }
  let g = data, off = false;
  if (!live) {
    const decl = data.declared || data.topology || (d && d.topology) || null;
    const sk = schematicGraph(decl);
    off = true;
    if (!sk) {
      block.appendChild(h('div', { class: 'topo-stage is-off' },
        h('div', { class: 'topo-offstate' },
          h('div', { class: 'topo-off-title' }, 'Nothing to draw'),
          h('div', { class: 'topo-off-reason' }, data.reason || 'The inventory carries nothing for this use case.'),
          h('div', { class: 'topo-off-hint' }, uc.state === 'off' ? 'Turn it on to draw the network from the cloud. The manifest declares no roles or flows to sketch meanwhile.' : 'The drawing appears once tagged resources exist in the inventory.'))));
      return block;
    }
    g = Object.assign(sk, { region: data.region || null, provider: data.provider || uc.provider, generated_at: data.generated_at, reason: data.reason });
  }
  if (t.err) block.appendChild(h('div', { class: 'form-error', style: 'margin-bottom:10px' }, 'Redraw failed: ' + t.err + ' — showing the previous drawing.'));

  const stage = h('div', { class: 'topo-stage' + (off ? ' is-off' : '') });
  const canvas = h('div', { class: 'topo-canvas', tabindex: '-1', 'aria-label': 'Topology drawing, scrolls horizontally when wider than the card' });
  const aside = h('aside', { class: 'topo-inspector', 'aria-live': 'polite' });
  if (off) {
    const lr = uc.last_run;
    stage.appendChild(h('div', { class: 'topo-offbar' },
      h('span', { class: 'lamp' }), h('span', { class: 'state-word off' }, 'not running'),
      h('span', { class: 'dim' }, data.reason || 'The use case is off.'),
      h('span', { class: 'topo-offbar-run mono' }, lr ? ['last run: ' + lr.action + ' ' + lr.state + ' · ', h('span', { dataset: { rel: lr.ended || lr.started }, title: lr.ended || lr.started }, fmtRel(lr.ended || lr.started))] : 'never run')));
  }
  stage.appendChild(canvas);
  stage.appendChild(aside);
  block.appendChild(stage);
  block.appendChild(topoLegend(g, uc));
  if (g.unknown && g.unknown.length) {
    block.appendChild(h('div', { class: 'topo-unknown' }, h('span', { class: 'dim' }, 'Not drawn: '), g.unknown.map((u, i) => [i ? ', ' : null, h('span', { class: 'mono', title: u.reason || '' }, u.label || u.id || String(u)), u.reason ? h('span', { class: 'dim' }, ' — ' + u.reason) : null])));
  }

  // draw (synchronously with the last known width, then measure and redraw if the width changed)
  const draw = () => {
    const avail = Math.max(320, t.width || canvas.clientWidth || 1100);
    canvas._drawnFor = avail;
    const L = topoLayout(g, avail);
    L.scale = L.w < avail ? Math.min(TOPO.MAX_SCALE, avail / L.w) : 1;
    const svg = topoSvg(g, L, {});
    clear(canvas); canvas.appendChild(svg);
    clear(aside); aside.appendChild(topoInspector(g, L, t.sel, uc));
    if (L.unplaced.length && !$('.topo-unplaced', block)) block.appendChild(h('div', { class: 'topo-unknown topo-unplaced' }, h('span', { class: 'dim' }, 'Not placed: '), L.unplaced.map((u, i) => [i ? ', ' : null, h('span', { class: 'mono' }, u.label), h('span', { class: 'dim' }, ' — ' + u.reason)])));
    wireTopology(svg, canvas, aside, g, L, t, uc);
    return L;
  };
  draw();
  setTimeout(() => {
    if (!canvas.isConnected) return;
    const w = canvas.clientWidth;
    if (w && Math.abs(w - canvas._drawnFor) > 8) { t.width = w; draw(); }
    if (t.refocus) { if (Date.now() > (t.refocusUntil || 0)) t.refocus = null; else { const el = $('[data-node="' + CSS.escape(t.refocus) + '"]', canvas); if (el) el.focus({ preventScroll: true }); } }
  });
  canvas._topoRedraw = () => { const w = canvas.clientWidth; if (w && Math.abs(w - canvas._drawnFor) > 8) { t.width = w; draw(); } };
  return block;
}
window.addEventListener('resize', () => { clearTimeout(renderTopology._rt); renderTopology._rt = setTimeout(() => { $$('.topo-canvas').forEach(c => c._topoRedraw && c._topoRedraw()); }, 120); });

function topoLegend(g, uc) {
  const sw = (cls, d) => s('svg', { class: 'lg-sw', viewBox: '0 0 44 14', 'aria-hidden': 'true' }, s('path', { class: cls, d: d || 'M2 7H42' }));
  const item = (svgEl, text) => h('span', { class: 'lg-item' }, svgEl, text);
  const lamp = cls => h('span', { class: 'lg-lamp ' + cls });
  const badge = exp => h('span', { class: 'lg-badge lg-exp-' + exp }, exp);
  const legend = h('div', { class: 'topo-legend' },
    item(sw('lg-route'), 'default route'),
    item(sw('lg-uplink'), 'uplink to the internet'),
    item(sw('lg-flow', 'M2 7H36'), 'declared flow'),
    item(s('svg', { class: 'lg-sw', viewBox: '0 0 44 14', 'aria-hidden': 'true' }, s('path', { class: 'lg-blocked', d: 'M2 7H42' }), s('path', { class: 'lg-strike', d: 'M18 3l8 8M26 3l-8 8' })), 'blocked'),
    h('span', { class: 'lg-sep' }),
    item(lamp('on'), 'enrolled'), item(lamp('bad'), 'not enrolled'), item(lamp('running'), 'running'),
    h('span', { class: 'lg-sep' }),
    badge('public'), badge('private'), badge('isolated'),
    h('span', { class: 'lg-sep' }),
    item(s('svg', { class: 'lg-glyph', viewBox: '0 0 18 18', 'aria-hidden': 'true' }, roleGlyph('pse')), 'service edge'),
    item(s('svg', { class: 'lg-glyph', viewBox: '0 0 18 18', 'aria-hidden': 'true' }, roleGlyph('connector')), 'connector'),
    item(s('svg', { class: 'lg-glyph', viewBox: '0 0 18 18', 'aria-hidden': 'true' }, roleGlyph('app')), 'app'),
    item(s('svg', { class: 'lg-glyph', viewBox: '0 0 18 18', 'aria-hidden': 'true' }, roleGlyph('client')), 'client'));
  void g; void uc;
  return legend;
}

/** Hover, focus, keyboard and click behaviour on one drawing. */
function wireTopology(svg, canvas, aside, g, L, t, uc) {
  const byId = new Map(g.nodes.map(n => [n.id, n]));
  const nodeEls = new Map(); $$('[data-node]', svg).forEach(el => nodeEls.set(el.dataset.node, el));
  const edgeEls = $$('[data-edge]', svg);
  let sticky = t.sel; // keyboard-focused / last chosen
  const showInsp = sel => { clear(aside); aside.appendChild(topoInspector(g, L, sel, uc)); };
  function related(sel) {
    const nodes = new Set(), edges = new Set();
    if (sel.node !== undefined) {
      nodes.add(sel.node);
      const n = byId.get(sel.node);
      // an address highlights its host; a host highlights its address
      if (n && n.kind === 'eip' && n.attached_to) nodes.add(n.attached_to);
      g.nodes.forEach(x => { if (x.kind === 'eip' && x.attached_to === sel.node) nodes.add(x.id); });
      edgeEls.forEach(el => {
        const chain = (el.dataset.via || '').split(' ');
        if (el.dataset.from === sel.node || el.dataset.to === sel.node || chain.includes(sel.node)) { edges.add(el.dataset.edge); chain.forEach(id => { if (id) nodes.add(id); }); }
      });
    } else if (sel.edge !== undefined) {
      edges.add(String(sel.edge));
      const el = edgeEls.find(x => x.dataset.edge === String(sel.edge));
      if (el) (el.dataset.via || '').split(' ').forEach(id => { if (id) nodes.add(id); });
    }
    return { nodes, edges };
  }
  function highlight(sel) {
    svg.classList.toggle('is-focus', !!sel);
    $$('.is-hi', svg).forEach(el => el.classList.remove('is-hi'));
    if (!sel) return;
    const r = related(sel);
    r.nodes.forEach(id => { const el = nodeEls.get(id); if (el) el.classList.add('is-hi'); });
    edgeEls.forEach(el => { if (r.edges.has(el.dataset.edge)) el.classList.add('is-hi'); });
  }
  const selOf = el => {
    const ne = el.closest('[data-node]'); if (ne) return { node: ne.dataset.node };
    const ee = el.closest('[data-edge]'); if (ee) return { edge: Number(ee.dataset.edge) };
    return null;
  };
  svg.addEventListener('mouseover', e => { const sel = selOf(e.target); if (!sel) return; highlight(sel); showInsp(sel); });
  svg.addEventListener('mouseleave', () => { highlight(sticky); showInsp(sticky); });
  svg.addEventListener('focusin', e => { const sel = selOf(e.target); if (!sel) return; sticky = sel; t.sel = sel; highlight(sel); showInsp(sel); });
  svg.addEventListener('focusout', e => { if (!svg.contains(e.relatedTarget)) { highlight(sticky); showInsp(sticky); } });
  svg.addEventListener('click', e => {
    const sel = selOf(e.target); if (!sel) return;
    if (sel.node !== undefined) { const n = byId.get(sel.node); sticky = sel; t.sel = sel; if (n && !n.pseudo && n.kind !== 'internet' && n.kind !== 'igw') openTopoNode(uc, g, n); else { highlight(sel); showInsp(sel); } }
    else { sticky = sel; t.sel = sel; highlight(sel); showInsp(sel); }
  });
  svg.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const sel = selOf(e.target); if (!sel || sel.node === undefined) return;
    e.preventDefault();
    const n = byId.get(sel.node);
    if (n && !n.pseudo && n.kind !== 'internet' && n.kind !== 'igw') openTopoNode(uc, g, n);
  });
  if (sticky) { highlight(sticky); showInsp(sticky); }
}

/* ---- outline: steps + a real plan + declared effects, per action ---- */

async function loadOutline(id, action, force) {
  const o = state.outlines[id] || (state.outlines[id] = {});
  const cur = o[action];
  if (cur && !force && (cur.loading || cur.data)) return cur.promise || cur;
  const slot = { loading: true, data: null, err: null, status: null, promise: null };
  o[action] = slot;
  slot.promise = (async () => {
    if (state.route === 'usecases') render();
    try { slot.data = await api.outline(id, action); }
    catch (e) { if (e.status === 401) return slot; slot.err = e.message; slot.status = e.status; }
    slot.loading = false;
    if (state.route === 'usecases' && state.outlines[id][action] === slot) render();
    return slot;
  })();
  return slot.promise;
}

function stepsOl(action, steps, job) {
  const active = job && job.action === action;
  const ol = h('ol', { class: 'steps' + (active && job.state === 'running' ? ' is-active' : ''), dataset: { action } });
  steps.forEach((s, i) => {
    const js = active && job.steps && job.steps[i];
    ol.appendChild(h('li', { class: 'step' + (js ? ' ' + js.state : '') },
      h('span', { class: 'step-n', 'aria-hidden': 'true' }),
      h('div', null, h('div', { class: 'step-name' }, s.name), h('div', { class: 'step-run' }, s.run)),
      h('span', { class: 'step-t' }, js && js.started ? fmtDur(js.started, js.ended) : '')));
  });
  return ol;
}

/** Plan entries grouped by resource type, in the order the plan reported them. */
function planGroups(list) {
  const m = new Map();
  for (const e of list || []) { const t = e.type || 'unknown'; if (!m.has(t)) m.set(t, []); m.get(t).push(e); }
  return Array.from(m.entries()).map(([type, items]) => ({ type, items }));
}
/** Distinct network containers in a plan list — not AWS-shaped: aws_vpc, google_compute_network, azurerm_virtual_network. */
function countNetworks(list) { return (list || []).filter(e => /(_vpc|_network|virtual_network)$/.test(e.type || '')).length; }
function plural(n, one, many) { return n === 1 ? one : (many || one + 's'); }

function outlinePart(title, count, body, tone) {
  return h('div', { class: 'opart' + (tone ? ' tone-' + tone : '') },
    h('div', { class: 'opart-head' }, h('span', { class: 'opart-title' }, title), count !== null && count !== undefined ? h('span', { class: 'opart-n' }, typeof count === 'number' ? fmtNum(count) : count) : null),
    body);
}
function planError(msg, onRetry) {
  return h('div', { class: 'plan-error', role: 'alert' },
    h('div', { class: 'plan-error-title' }, 'Plan failed'),
    h('pre', { class: 'plan-error-msg' }, msg),
    onRetry ? h('button', { class: 'btn btn-sm', type: 'button', onclick: onRetry }, icon('refresh'), 'Retry plan') : null);
}
function renderPlanGroup(g, action) {
  return h('li', { class: 'ptype' },
    h('details', null,
      h('summary', null, h('span', { class: 'mono ptype-t' }, g.type), h('span', { class: 'ptype-x' }, ' × '), h('span', { class: 'ptype-n' }, g.items.length)),
      h('ul', { class: 'paddrs mono' }, g.items.map(e => h('li', null, e.address || (e.type + '.' + e.name), e.replace ? h('span', { class: 'dim' }, ' (replace)') : null)))));
}

function renderOutline(uc, d, job) {
  const block = h('div', { class: 'detail-block outline-block' });
  block.appendChild(h('div', { class: 'section-head', style: 'margin-bottom:10px' },
    h('span', { class: 'section-title' }, 'Outline'),
    h('span', { class: 'outline-hint' }, 'Steps and a live plan for each direction. Nothing is applied to produce this.')));
  block.appendChild(h('div', { class: 'outline' },
    renderOutlineColumn(uc, d, 'on', job, { onRetry: () => loadOutline(uc.id, 'on', true) }),
    renderOutlineColumn(uc, d, 'off', job, { onRetry: () => loadOutline(uc.id, 'off', true) })));
  return block;
}

function renderOutlineColumn(uc, d, action, job, opts) {
  opts = opts || {};
  const o = (state.outlines[uc.id] || {})[action] || null;
  const data = o && o.data;
  const plan = data && data.plan;
  const declared = (data && data.declared) || null;
  const steps = (data && data.steps) || (d && d.procedure && d.procedure[action]) || [];
  const col = h('div', { class: 'ocol ocol-' + action });
  if (!opts.noTitle) {
    col.appendChild(h('div', { class: 'proc-col-title' },
      h('span', { class: 'state-word ' + (action === 'on' ? 'on' : 'bad') }, action === 'on' ? 'Turn on' : 'Turn off'),
      plan && plan.ok && plan.generated_at ? h('span', { class: 'dim mono ocol-when', title: plan.generated_at }, 'planned ', h('span', { dataset: { rel: plan.generated_at } }, fmtRel(plan.generated_at))) : null));
  }

  // 1. steps
  col.appendChild(outlinePart('Steps', steps.length, steps.length ? stepsOl(action, steps, job) : h('div', { class: 'opart-empty' }, 'No steps declared.')));

  // 2. plan: generated / destroyed
  const genTitle = action === 'on' ? 'Generated' : 'Destroyed';
  const tone = action === 'on' ? 'accent' : 'bad';
  if (!o || o.loading) {
    col.appendChild(outlinePart(genTitle, null, h('div', { class: 'oplan-loading' }, h('span', { class: 'loading' }, action === 'on' ? 'Planning the build' : 'Planning the destroy'))));
    col.appendChild(outlinePart(action === 'on' ? 'Already present' : 'Unchanged', null, h('div', { class: 'opart-empty' }, 'Waiting for the plan.')));
  } else if (o.err) {
    const msg = o.status === 409 ? o.err + ' The outline is available again when it finishes.' : o.err;
    col.appendChild(outlinePart(genTitle, null, planError(msg, opts.onRetry)));
    col.appendChild(outlinePart(action === 'on' ? 'Already present' : 'Unchanged', '?', h('div', { class: 'opart-empty' }, 'Unknown until a plan succeeds.')));
  } else if (!plan || !plan.ok) {
    col.appendChild(outlinePart(genTitle, null, planError(plan && plan.error ? plan.error : 'The response carried no plan.', opts.onRetry)));
    col.appendChild(outlinePart(action === 'on' ? 'Already present' : 'Unchanged', '?', h('div', { class: 'opart-empty' }, 'Unknown until a plan succeeds.')));
  } else {
    const list = (action === 'on' ? plan.create : plan.destroy) || [];
    const present = (plan.unchanged || []).length;
    const body = h('div');
    if (!list.length) {
      body.appendChild(h('div', { class: 'oplan-none' }, action === 'on'
        ? (present ? 'Nothing to generate — ' + fmtNum(present) + ' ' + plural(present, 'resource') + ' already present.' : 'Nothing to generate — the plan is empty.')
        : (present ? 'Nothing to destroy — the plan leaves all ' + fmtNum(present) + ' ' + plural(present, 'resource') + ' in place.' : 'Nothing to destroy — no resources in state.')));
    } else {
      body.appendChild(h('ul', { class: 'ptypes' }, planGroups(list).map(g => renderPlanGroup(g, action))));
    }
    const upd = plan.update || [];
    if (upd.length) body.appendChild(h('div', { class: 'oplan-extra' }, h('ul', { class: 'ptypes' }, [h('li', { class: 'ptype' }, h('details', null, h('summary', null, h('span', { class: 'ptype-t' }, 'updated in place'), h('span', { class: 'ptype-x' }, ' × '), h('span', { class: 'ptype-n' }, upd.length)), h('ul', { class: 'paddrs mono' }, upd.map(e => h('li', null, e.address)))))])));
    col.appendChild(outlinePart(genTitle, list.length, body, list.length ? tone : null));
    col.appendChild(outlinePart(action === 'on' ? 'Already present' : 'Unchanged', present,
      h('div', { class: 'opart-empty' }, present ? fmtNum(present) + ' ' + plural(present, 'resource') + (action === 'on' ? ' already in state and untouched by this plan.' : ' stay as they are.') : 'Nothing in state is left untouched.')));
  }

  // 3. outside OpenTofu (declared)
  const outsideKey = action === 'on' ? 'creates' : 'destroys';
  const outside = declared ? (declared[outsideKey] || []) : null;
  col.appendChild(outlinePart('Outside OpenTofu', outside ? outside.length : null,
    outside === null ? h('div', { class: 'opart-empty' }, o && o.loading ? 'Loading the manifest’s declared effects.' : 'Declared effects unavailable.')
      : outside.length ? h('ul', { class: 'declared' }, outside.map(t => h('li', null, t)))
      : h('div', { class: 'opart-empty' }, 'Nothing declared outside OpenTofu for ' + action.toUpperCase() + '.')));

  // 4. kept: declared retains + remote state
  const retains = declared ? (declared.retains || []) : null;
  const rs = data && data.retained_state;
  const keptBody = h('div');
  if (retains === null) keptBody.appendChild(h('div', { class: 'opart-empty' }, o && o.loading ? 'Loading.' : 'Unavailable.'));
  else {
    const ul = h('ul', { class: 'declared kept' }, retains.map(t => h('li', null, t)));
    if (rs) ul.appendChild(h('li', { class: 'kept-state' }, 'Remote state: ', h('span', { class: 'mono' }, (rs.backend || 's3') + '://' + (rs.bucket || '<bucket>') + '/' + (rs.key || '')), rs.region ? h('span', { class: 'dim' }, ' (' + rs.region + ')') : null, h('span', { class: 'dim' }, ' — versioned object and the remote lock')));
    else ul.appendChild(h('li', { class: 'kept-state' }, 'Remote state in the S3 backend'));
    keptBody.appendChild(ul);
  }
  col.appendChild(outlinePart('Kept', retains === null ? null : retains.length + (rs ? 1 : 0), keptBody));
  return col;
}

/** The sentence at the top of the confirmation modal, computed from the plan and the declared effects. */
function outlineSentence(uc, action, o) {
  const prov = providerShort(uc.provider);
  const A = action.toUpperCase();
  if (!o || o.loading) return 'Planning ' + A + ' to count exactly what it touches…';
  const data = o.data, plan = data && data.plan;
  const declared = (data && data.declared) || {};
  const kept = (declared.retains || []).length;
  const keptTxt = kept ? ' and keep ' + kept + ' ' + plural(kept, 'thing') + ' outside OpenTofu' : '';
  if (o.err || !plan || !plan.ok) return 'The ' + A + ' plan failed, so the exact resource list is unknown. ' + (data ? A + ' would' + (keptTxt ? keptTxt.replace(' and keep', ' keep') : ' keep nothing') + ' outside OpenTofu.' : 'Retry the plan before confirming.');
  const list = (action === 'on' ? plan.create : plan.destroy) || [];
  const n = list.length, nets = countNetworks(list);
  const across = nets ? ' across ' + nets + ' ' + plural(nets, 'VPC') : '';
  const outside = (declared[action === 'on' ? 'creates' : 'destroys'] || []).length;
  const present = (plan.unchanged || []).length;
  if (action === 'off') {
    if (!n) return 'OFF will destroy nothing — no resources are in state' + keptTxt + '.';
    return 'OFF will destroy ' + fmtNum(n) + ' ' + prov + ' ' + plural(n, 'resource') + across + keptTxt + '.';
  }
  if (!n) return 'ON will generate nothing — ' + fmtNum(present) + ' ' + plural(present, 'resource') + ' already present' + (outside ? '; ' + outside + ' ' + plural(outside, 'thing') + ' still ' + (outside === 1 ? 'happens' : 'happen') + ' outside OpenTofu' : '') + '.';
  return 'ON will generate ' + fmtNum(n) + ' ' + prov + ' ' + plural(n, 'resource') + across + (present ? ', leave ' + fmtNum(present) + ' already present' : '') + (outside ? ' and do ' + outside + ' ' + plural(outside, 'thing') + ' outside OpenTofu' : '') + '.';
}

/* ---- flipping ---- */

async function requestFlip(uc) {
  if (state.flipping[uc.id]) return;
  let d = state.details[uc.id];
  if (!d || !d.procedure) { d = await loadDetail(uc.id); }
  if (!d || !d.procedure) { toast('Could not load the procedure for ' + uc.name + '.', true); return; }

  let action = uc.state === 'on' ? 'off' : uc.state === 'off' ? 'on' : null;
  const body = h('div', { class: 'confirm' });
  if (!action) {
    action = 'off';
    body.appendChild(h('p', { style: 'color:var(--text-dim);margin-bottom:8px' }, 'The state is ', h('b', null, uc.state), '. Choose which procedure to run.'));
    body.appendChild(h('div', { class: 'confirm-pick' },
      h('label', null, h('input', { type: 'radio', name: 'act', value: 'off', checked: true, onchange: () => rebuild('off') }), 'Turn off (destroy)'),
      h('label', null, h('input', { type: 'radio', name: 'act', value: 'on', onchange: () => rebuild('on') }), 'Turn on (rebuild)')));
  }
  const sentence = h('p', { class: 'confirm-sentence', 'aria-live': 'polite' });
  const host = h('div', { class: 'confirm-outline' });
  body.appendChild(sentence); body.appendChild(host);
  let ctl = null, seq = 0;
  function rebuild(a) {
    action = a; const my = ++seq;
    let o = (state.outlines[uc.id] || {})[a];
    if (!o || (!o.loading && !o.data && !o.err)) { loadOutline(uc.id, a).then(() => { if (seq === my) rebuild(a); }); o = (state.outlines[uc.id] || {})[a]; }
    else if (o.loading && o.promise) o.promise.then(() => { if (seq === my) rebuild(a); });
    sentence.textContent = outlineSentence(uc, a, o);
    sentence.className = 'confirm-sentence' + (a === 'off' ? ' is-danger' : '') + (!o || o.loading ? ' is-loading' : '') + (o && !o.loading && (o.err || !(o.data && o.data.plan && o.data.plan.ok)) ? ' is-failed' : '');
    clear(host);
    host.appendChild(renderOutlineColumn(uc, d, a, null, { noTitle: true, onRetry: () => { loadOutline(uc.id, a, true).then(() => { if (seq === my) rebuild(a); }); rebuild(a); } }));
    if (ctl) {
      const ready = !!(o && !o.loading);
      ctl.enableConfirm(ready);
      ctl.okBtn.title = ready ? '' : 'Waiting for the plan';
    }
  }
  rebuild(action);
  const ok = await modal({
    title: (action === 'on' ? 'Turn on ' : 'Turn off ') + uc.name + '?',
    sub: action === 'off' ? 'This destroys the infrastructure the use case created. The outline below is from a live plan.' : 'This creates infrastructure and may take several minutes. The outline below is from a live plan.',
    body, confirmLabel: action === 'on' ? 'Turn on' : 'Turn off', danger: action === 'off', wide: true, confirmDisabled: true,
    setup(c) { ctl = c; rebuild(action); },
  });
  if (!ok) return;
  state.flipping[uc.id] = true; render();
  try {
    const res = await api.flip(uc.id, action);
    const live = ucById(uc.id); if (live) live.state = action === 'on' ? 'turning_on' : 'turning_off';
    state.expanded = uc.id;
    delete state.flipping[uc.id];
    delete state.outlines[uc.id]; // a job invalidates the plan
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
  const connected = new Set((PARAMS.get('connected') === '1' ? 'aws' : PARAMS.get('connected') || '').split(',').filter(Boolean));
  const connectedAtBoot = iso(2 * H);
  const jobs = {};
  let jobSeq = 100;

  const identity = { account: '257394018842', arn: 'arn:aws:sts::257394018842:assumed-role/AWSReservedSSO_AdministratorAccess_9f1c2d/nils', alias: null };
  const REGIONS = ['eu-central-1', 'eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-north-1', 'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2', 'ca-central-1', 'ap-southeast-1', 'ap-southeast-2', 'ap-northeast-1', 'ap-northeast-2', 'ap-south-1', 'sa-east-1', 'ap-northeast-3'];

  /* ---- inventory fixture: every v1.1 field on eu-central-1; money derived from one rate table so
          region totals equal the sum of their lines by construction ---- */
  const RATE = { 'm5.large': 0.115, 't3.medium': 0.048, 't3.micro': 0.012, 't3.medium:win': 0.0744, nat: 0.052, gp3: 0.0952, gp2: 0.08, ipv4: 0.005 };
  const r2 = n => Math.round(n * 100) / 100;
  function inventory(fresh) {
    const lab = { Project: 'zpa-pse-lab', ManagedBy: 'opentofu' };
    const T = (name, extra) => Object.assign({ Name: name }, lab, extra || {});
    const up = 3 * D + 2 * H, up2 = 3 * D + H;
    const VPC = { a: 'vpc-0f1e2d3c4b5a69788', b: 'vpc-0a9b8c7d6e5f41323', def: 'vpc-3b2a1c0d' };
    const SN = { aPub: 'subnet-0a11b22c33d44e55f', bPub: 'subnet-0b22c33d44e55f66a', priv: 'subnet-0c33d44e55f66a77b', mcu: 'subnet-0d44e55f66a77b88c', d1: 'subnet-1a2b3c4d', d2: 'subnet-2b3c4d5e', d3: 'subnet-3c4d5e6f' };
    const SG = { pse: 'sg-0a1b2c3d4e5f6a7b8', conn: 'sg-0b2c3d4e5f6a7b8c9', privConn: 'sg-0c3d4e5f6a7b8c9d0', server: 'sg-0d4e5f6a7b8c9d0e1', mcu: 'sg-0e5f6a7b8c9d0e1f2', dA: 'sg-0f6a7b8c9d0e1f2a3', dB: 'sg-1a7b8c9d0e1f2a3b4', dDef: 'sg-2b8c9d0e1f2a3b4c5' };
    const IGW = { a: 'igw-0a1b2c3d4e5f60718', b: 'igw-0b2c3d4e5f607182a', def: 'igw-3c4d5e6f' };
    const RT = { aPub: 'rtb-0a1b2c3d4e5f60718', aMain: 'rtb-0a9f8e7d6c5b4a392', bPub: 'rtb-0b2c3d4e5f607182a', bPriv: 'rtb-0c3d4e5f6071829b3', bMain: 'rtb-0c9f8e7d6c5b4a394', defMain: 'rtb-3d4e5f6a' };
    const NAT = 'nat-0123456789abcdef0';
    const AMI = { al: 'ami-0e2c8caa4b6378d8c', zpa: 'ami-0c7e1f2a3b4c5d6e7', win: 'ami-0b6d8c7e5f4a3b2c1' };
    const inst = (o) => Object.assign({ state: 'running', platform: 'Linux/UNIX', architecture: 'x86_64', vpc: VPC.b, ami: AMI.zpa, ami_name: 'zpa-connector-el9-25.62.1-x86_64', iam_instance_profile: 'zpa-lab-node', key_name: 'zpa-lab', root_device: '/dev/xvda', monitoring: false, ebs_optimized: true, user_data_present: true }, o);
    // the real lab: VPC A holds the Private Service Edge and its local App Connector in one public subnet;
    // VPC B holds a public subnet for the NAT, the PRIV zone (connector + server) and the MCU zone (Windows client)
    const instances = [
      inst({ id: 'i-0a1b2c3d4e5f60718', name: 'zpa-lab-pse', type: 'm5.large', az: 'eu-central-1a', vpc: VPC.a, subnet: SN.aPub, private_ip: '10.91.10.5', public_ip: '63.188.16.52', launched: iso(up), uptime_h: up / H, ami_name: 'zpa-private-service-edge-el9-25.62.1-x86_64', security_groups: [{ id: SG.pse, name: 'zpa-lab-pse' }], volumes: ['vol-0aa1f3c9d2e4b5a61'], monthly_usd: r2(RATE['m5.large'] * 730), tags: T('zpa-lab-pse') }),
      inst({ id: 'i-0b2c3d4e5f607182a', name: 'zpa-lab-connector', type: 't3.medium', az: 'eu-central-1a', vpc: VPC.a, subnet: SN.aPub, private_ip: '10.91.10.64', public_ip: '3.72.118.204', launched: iso(up), uptime_h: up / H, security_groups: [{ id: SG.conn, name: 'zpa-lab-connector' }], volumes: ['vol-0aa2e4d0c3f5a6b72'], monthly_usd: r2(RATE['t3.medium'] * 730), tags: T('zpa-lab-connector') }),
      inst({ id: 'i-0c3d4e5f6071829b3', name: 'zpa-lab-priv-connector', type: 't3.medium', az: 'eu-central-1a', subnet: SN.priv, private_ip: '10.90.20.21', public_ip: null, launched: iso(up), uptime_h: up / H, security_groups: [{ id: SG.privConn, name: 'zpa-lab-priv-connector' }], volumes: ['vol-0aa3f5e1d4a6b7c83'], monthly_usd: r2(RATE['t3.medium'] * 730), tags: T('zpa-lab-priv-connector') }),
      inst({ id: 'i-0d4e5f607182a9b4c', name: 'zpa-lab-server', type: 't3.micro', az: 'eu-central-1a', subnet: SN.priv, private_ip: '10.90.20.10', public_ip: null, launched: iso(up2), uptime_h: up2 / H, ami: AMI.al, ami_name: 'al2023-ami-2023.5.20240722.0-kernel-6.1-x86_64', security_groups: [{ id: SG.server, name: 'zpa-lab-server' }], volumes: ['vol-0aa4a6f2e5b7c8d94'], monthly_usd: r2(RATE['t3.micro'] * 730), ebs_optimized: false, tags: T('zpa-lab-server') }),
      inst({ id: 'i-0e5f607182a9b4c5d', name: 'zpa-lab-mcu-client', type: 't3.medium', az: 'eu-central-1a', subnet: SN.mcu, private_ip: '10.90.30.15', public_ip: null, launched: iso(up2), uptime_h: up2 / H, platform: 'Windows', ami: AMI.win, ami_name: 'Windows_Server-2022-English-Full-Base-2024.07.10', root_device: '/dev/sda1', security_groups: [{ id: SG.mcu, name: 'zpa-lab-mcu-client' }], volumes: ['vol-0aa5b7a3f6c8d9ea5'], monthly_usd: r2(RATE['t3.medium:win'] * 730), tags: T('zpa-lab-mcu-client') }),
    ];
    const vol = (id, gb, i, dev, extra) => Object.assign({ id, size_gb: gb, type: 'gp3', az: i ? i.az : 'eu-central-1a', iops: 3000, throughput: 125, encrypted: true, state: 'in-use', attached: !!i, instance: i ? i.id : null, attached_to: i ? i.id : null, device: i ? dev : null, created: i ? i.launched : iso(11 * D), name: i ? i.name : null, monthly_usd: r2(gb * RATE.gp3), tags: i ? T(i.name) : {} }, extra || {});
    const volumes = [
      vol('vol-0aa1f3c9d2e4b5a61', 80, instances[0], '/dev/xvda'), vol('vol-0aa2e4d0c3f5a6b72', 80, instances[1], '/dev/xvda'),
      vol('vol-0aa3f5e1d4a6b7c83', 80, instances[2], '/dev/xvda'), vol('vol-0aa4a6f2e5b7c8d94', 8, instances[3], '/dev/xvda'),
      vol('vol-0aa5b7a3f6c8d9ea5', 50, instances[4], '/dev/sda1'),
      vol('vol-0bb6c8d4a7e9f0ab6', 20, null, null, { state: 'available', name: 'mcu-client-restore-test', encrypted: false, tags: { Name: 'mcu-client-restore-test' } }),
    ];
    const sub = (id, name, cidr, az, rt, target) => ({ id, name, cidr, az, public: !!(target && target.startsWith('igw-')), route_table: rt, default_route: target, map_public_ip: !!(target && target.startsWith('igw-')), available_ips: 240 });
    const vpcs = [
      { id: VPC.a, name: 'zpa-lab-vpc-a', cidr: '10.91.0.0/16', default: false, state: 'available', dns_hostnames: true, igw: IGW.a, nat_gateways: [],
        subnets: [sub(SN.aPub, 'zpa-lab-public', '10.91.10.0/24', 'eu-central-1a', RT.aPub, IGW.a)],
        route_tables: [
          { id: RT.aMain, name: null, main: true, routes: [{ dest: '10.91.0.0/16', target: 'local', state: 'active' }], subnets: [], tags: {} },
          { id: RT.aPub, name: 'zpa-lab-public-rt', main: false, routes: [{ dest: '10.91.0.0/16', target: 'local', state: 'active' }, { dest: '0.0.0.0/0', target: IGW.a, state: 'active' }], subnets: [SN.aPub], tags: T('zpa-lab-public-rt') }],
        tags: T('zpa-lab-vpc-a') },
      { id: VPC.b, name: 'zpa-lab-vpc-b', cidr: '10.90.0.0/16', default: false, state: 'available', dns_hostnames: true, igw: IGW.b, nat_gateways: [NAT],
        subnets: [
          sub(SN.bPub, 'zpa-lab-b-public', '10.90.0.0/24', 'eu-central-1a', RT.bPub, IGW.b),
          sub(SN.priv, 'zpa-lab-priv', '10.90.20.0/24', 'eu-central-1a', RT.bPriv, NAT),
          sub(SN.mcu, 'zpa-lab-mcu', '10.90.30.0/24', 'eu-central-1a', RT.bPriv, NAT)],
        route_tables: [
          { id: RT.bMain, name: null, main: true, routes: [{ dest: '10.90.0.0/16', target: 'local', state: 'active' }], subnets: [], tags: {} },
          { id: RT.bPub, name: 'zpa-lab-b-public-rt', main: false, routes: [{ dest: '10.90.0.0/16', target: 'local', state: 'active' }, { dest: '0.0.0.0/0', target: IGW.b, state: 'active' }], subnets: [SN.bPub], tags: T('zpa-lab-b-public-rt') },
          { id: RT.bPriv, name: 'zpa-lab-b-private-rt', main: false, routes: [{ dest: '10.90.0.0/16', target: 'local', state: 'active' }, { dest: '0.0.0.0/0', target: NAT, state: 'active' }], subnets: [SN.priv, SN.mcu], tags: T('zpa-lab-b-private-rt') }],
        tags: T('zpa-lab-vpc-b') },
      { id: VPC.def, name: null, cidr: '172.31.0.0/16', default: true, state: 'available', dns_hostnames: true, igw: IGW.def, nat_gateways: [],
        subnets: [sub(SN.d1, null, '172.31.0.0/20', 'eu-central-1a', RT.defMain, IGW.def), sub(SN.d2, null, '172.31.16.0/20', 'eu-central-1b', RT.defMain, IGW.def), sub(SN.d3, null, '172.31.32.0/20', 'eu-central-1c', RT.defMain, IGW.def)],
        route_tables: [{ id: RT.defMain, name: null, main: true, routes: [{ dest: '172.31.0.0/16', target: 'local', state: 'active' }, { dest: '0.0.0.0/0', target: IGW.def, state: 'active' }], subnets: [SN.d1, SN.d2, SN.d3], tags: {} }],
        tags: {} },
    ];
    const nat_gateways = [{ id: NAT, name: 'zpa-lab-nat', vpc: VPC.b, subnet: SN.bPub, state: 'available', public_ip: '18.193.163.38', private_ip: '10.90.0.12', connectivity_type: 'public', created: iso(up), monthly_usd: r2(RATE.nat * 730), tags: T('zpa-lab-nat') }];
    const eip = (ip, alloc, assoc, extra) => Object.assign({ ip, allocation_id: alloc, attached: !!assoc, instance: assoc && assoc.kind === 'instance' ? assoc.id : null, association: assoc, private_ip: null, name: null, monthly_usd: r2(RATE.ipv4 * 730), tags: {} }, extra || {});
    const eips = [
      eip('63.188.16.52', 'eipalloc-0c2d3e4f5a6b7c8d9', { kind: 'instance', id: instances[0].id, eni: 'eni-0a1b2c3d4e5f60718' }, { private_ip: '10.91.10.5', name: 'zpa-lab-pse-eip', tags: T('zpa-lab-pse-eip') }),
      eip('18.193.163.38', 'eipalloc-0d3e4f5a6b7c8d9e0', { kind: 'nat', id: NAT, eni: 'eni-0b2c3d4e5f607182a' }, { private_ip: '10.90.0.12', name: 'zpa-lab-nat-eip', tags: T('zpa-lab-nat-eip') }),
      eip('3.120.55.17', 'eipalloc-0f5a6b7c8d9e0f1a2', null, { name: 'zpa-lab-pse-eip-old', tags: T('zpa-lab-pse-eip-old') }), // idle, still tagged: shows in the drawing as unattached
      eip('18.185.9.201', 'eipalloc-1a6b7c8d9e0f1a2b3', null, {}),
    ];
    const sg = (id, name, vpc, description, ingress, egress, attached_to, tags) => ({ id, name, vpc, description, ingress, egress, attached_to, tags: tags || {} });
    const rule = (proto, from, to, source) => ({ proto, from, to, source });
    const allOut = [rule('all', null, null, '0.0.0.0/0')];
    const security_groups = [
      sg(SG.pse, 'zpa-lab-pse', VPC.a, 'Private Service Edge: client TLS in, SSH from inside the VPC', [rule('tcp', 443, 443, '0.0.0.0/0'), rule('udp', 443, 443, '0.0.0.0/0'), rule('tcp', 22, 22, '10.91.0.0/16')], allOut, [instances[0].id], T('zpa-lab-pse')),
      sg(SG.conn, 'zpa-lab-connector', VPC.a, 'App Connector: outbound only, SSH from inside the VPC', [rule('tcp', 22, 22, '10.91.0.0/16')], allOut, [instances[1].id], T('zpa-lab-connector')),
      sg(SG.privConn, 'zpa-lab-priv-connector', VPC.b, 'PRIV App Connector: outbound only', [rule('tcp', 22, 22, '10.90.0.0/16')], allOut, [instances[2].id], T('zpa-lab-priv-connector')),
      sg(SG.server, 'zpa-lab-server', VPC.b, 'Protected app: 8080 from the PRIV connector only', [rule('tcp', 8080, 8080, SG.privConn), rule('tcp', 22, 22, '10.90.20.0/24')], allOut, [instances[3].id], T('zpa-lab-server')),
      sg(SG.mcu, 'zpa-lab-mcu-client', VPC.b, 'Windows client: RDP for the demo operator', [rule('tcp', 3389, 3389, '0.0.0.0/0')], allOut, [instances[4].id], T('zpa-lab-mcu-client')),
      sg(SG.dA, 'default', VPC.a, 'default VPC security group', [rule('all', null, null, SG.dA)], allOut, [], {}),
      sg(SG.dB, 'default', VPC.b, 'default VPC security group', [rule('all', null, null, SG.dB)], allOut, [], {}),
      sg(SG.dDef, 'default', VPC.def, 'default VPC security group', [rule('all', null, null, SG.dDef)], allOut, [], {}),
    ];
    const region = (r, extra) => Object.assign({ region: r, instances: [], vpcs: [], nat_gateways: [], eips: [], volumes: [], security_groups: [], monthly_usd: 0, resource_count: 0 }, extra || {});
    const defVpc = (r, id) => ({ id, name: null, cidr: '172.31.0.0/16', default: true, state: 'available', dns_hostnames: true, igw: 'igw-' + id.slice(4), nat_gateways: [], subnets: [], route_tables: [], tags: {} });
    const regions = [
      region('eu-central-1', { instances, vpcs, nat_gateways, eips, volumes, security_groups }),
      region('eu-west-1', { vpcs: [defVpc('eu-west-1', 'vpc-1a2b3c4d')], eips: [eip('54.170.12.88', 'eipalloc-2b7c8d9e0f1a2b3c4', null, {})], security_groups: [sg('sg-2b8c9d0e1f2a3b4c5', 'default', 'vpc-1a2b3c4d', 'default VPC security group', [], allOut, [], {})] }),
      region('us-east-1', { vpcs: [defVpc('us-east-1', 'vpc-5e6f7a8b')], volumes: [{ id: 'vol-0ff9a1b2c3d4e5f60', size_gb: 100, type: 'gp2', az: 'us-east-1a', iops: 300, throughput: null, encrypted: false, state: 'available', attached: false, instance: null, attached_to: null, device: null, created: iso(210 * D), name: 'old-jumpbox-root', monthly_usd: r2(100 * RATE.gp2), tags: { Name: 'old-jumpbox-root' } }] }),
      ...REGIONS.filter(r => !['eu-central-1', 'eu-west-1', 'us-east-1'].includes(r)).map(r => region(r, ['eu-west-2', 'us-west-2', 'ap-southeast-1'].includes(r) ? { vpcs: [defVpc(r, 'vpc-' + r.replace(/-/g, '').slice(0, 8))] } : {})),
    ];
    // cost lines from the resources themselves, so every region total is the sum of its lines
    const lines = [];
    const line = (item, reg, qty, unit, unit_usd, monthly) => { const l = lines.find(x => x.item === item && x.region === reg); if (l) { l.qty += qty; l.monthly_usd = r2(l.monthly_usd + monthly); l.n = (l.n || 1) + 1; } else lines.push({ item, region: reg, qty, unit, unit_usd, monthly_usd: r2(monthly), n: 1 }); };
    const groups = {};
    const attribute = (reg, x, project) => { const k = project ? 'Project=' + project : 'untagged'; groups[k] = groups[k] || { key: k, instances: 0, monthly_usd: 0 }; groups[k].monthly_usd = r2(groups[k].monthly_usd + x.monthly_usd); };
    for (const rg of regions) {
      const byId = new Map(rg.instances.map(i => [i.id, i]));
      const pj = x => (x.tags && x.tags.Project) || null;
      for (const i of rg.instances) { line(i.type + (i.platform === 'Windows' ? ' Windows' : ' Linux'), rg.region, 730, 'hr', RATE[i.type + (i.platform === 'Windows' ? ':win' : '')], i.monthly_usd); attribute(rg.region, i, pj(i)); if (pj(i)) groups['Project=' + pj(i)].instances++; }
      for (const n of rg.nat_gateways) { line('NAT gateway', rg.region, 730, 'hr', RATE.nat, n.monthly_usd); attribute(rg.region, n, pj(n)); }
      for (const v of rg.volumes) { line('EBS ' + v.type + (v.attached ? '' : ' (unattached)'), rg.region, v.size_gb, 'GB-mo', RATE[v.type], v.monthly_usd); attribute(rg.region, v, pj(v) || (v.attached_to && byId.get(v.attached_to) ? pj(byId.get(v.attached_to)) : null)); }
      for (const e of rg.eips) { line(e.attached ? 'Public IPv4' : 'Elastic IP (idle)', rg.region, 730, 'hr', RATE.ipv4, e.monthly_usd); const via = e.association && e.association.kind === 'instance' ? byId.get(e.association.id) : e.association && e.association.kind === 'nat' ? rg.nat_gateways.find(n => n.id === e.association.id) : null; attribute(rg.region, e, pj(e) || (via ? pj(via) : null)); }
      rg.monthly_usd = r2(lines.filter(l => l.region === rg.region).reduce((s, l) => s + l.monthly_usd, 0));
      rg.resource_count = ['instances', 'vpcs', 'nat_gateways', 'eips', 'volumes', 'security_groups'].reduce((s, k) => s + rg[k].length, 0);
    }
    for (const l of lines) { if (l.n > 1) l.item += ' ×' + l.n; delete l.n; }
    const total = r2(lines.reduce((s, l) => s + l.monthly_usd, 0));
    const all = k => regions.reduce((s, rg) => s + rg[k].length, 0);
    return {
      generated_at: fresh ? iso(0) : iso(14 * MIN + 12000),
      stale: !fresh,
      regions,
      totals: { instances: all('instances'), running: all('instances'), vpcs: all('vpcs'), nat_gateways: all('nat_gateways'), eips: all('eips'), volumes_gb: regions.reduce((s, rg) => s + rg.volumes.reduce((t, v) => t + v.size_gb, 0), 0), security_groups: all('security_groups'), subnets: regions.reduce((s, rg) => s + rg.vpcs.reduce((t, v) => t + (v.subnets || []).length, 0), 0) },
      groups: Object.values(groups).sort((a, b) => b.monthly_usd - a.monthly_usd),
      cost: { monthly_usd: total, currency: 'USD', method: 'on-demand list price × 730h', lines, notes: ['Unattached elastic IPs are billed', 'NAT data processing not included', 'Windows price includes the licence surcharge'] },
    };
  }
  let inv = inventory(false);

  /* ---- topology: the manifest's `topology` block per use case, and a builder that derives the
          node/edge graph from the inventory exactly the way the backend does (SPEC v1.2) ---- */
  const MANIFEST_TOPO = {
    'zpa-private-service-edge': {
      roles: { 'zpa-lab-pse': 'pse', 'zpa-lab-connector': 'connector', 'zpa-lab-priv-connector': 'connector', 'zpa-lab-server': 'app', 'zpa-lab-mcu-client': 'client' },
      flows: [
        { from: 'zpa-lab-mcu-client', to: 'zpa-lab-pse', label: 'dials :443', via: ['nat', 'internet'] },
        { from: 'zpa-lab-priv-connector', to: 'zpa-lab-pse', label: 'dials :443', via: ['nat', 'internet'] },
        { from: 'zpa-lab-connector', to: 'zpa-lab-pse', label: 'dials :443 (local)' },
        { from: 'zpa-lab-priv-connector', to: 'zpa-lab-server', label: ':8080 brokered' },
        { from: 'zpa-lab-pse', to: 'internet', label: 'control plane :443' },
      ],
      blocked: [{ from: 'zpa-lab-mcu-client', to: 'zpa-lab-server', label: 'no route' }],
    },
    'zia-cloud-connector-sandbox': {
      roles: { 'zia-cc': 'connector', 'zia-workload': 'app' },
      flows: [{ from: 'zia-workload', to: 'zia-cc', label: 'default route' }, { from: 'zia-cc', to: 'internet', label: 'ZIA tunnel :443', via: ['nat', 'internet'] }],
      blocked: [],
    },
  };
  function topologyFor(ucId, refresh) {
    const uc = usecases[ucId];
    const decl = MANIFEST_TOPO[ucId] || null;
    const base = { generated_at: inv.generated_at, usecase: ucId, provider: uc.provider, region: uc.provider === 'aws' ? 'eu-central-1' : null, nodes: [], edges: [], enrolment: {}, unknown: [], declared: decl };
    if (uc.provider !== 'aws') return Object.assign(base, { reason: 'Inventory is not built for ' + uc.provider + ' yet, so there is nothing to draw from' });
    if (!connected.has('aws')) return Object.assign(base, { reason: 'Amazon Web Services is not connected' });
    if (uc.state === 'off' || (ucId === 'zpa-private-service-edge' && PARAMS.get('topo') === 'off')) return Object.assign(base, { reason: 'The use case is off — nothing tagged ' + 'Project=' + uc.tags.Project + ' is running in eu-central-1' });
    if (refresh) inv = inventory(true);
    const r = inv.regions.find(x => x.region === 'eu-central-1');
    const project = uc.tags.Project;
    const tagged = x => !!(x && x.tags && x.tags.Project === project);
    const nodes = [{ id: 'internet', kind: 'internet', label: 'Internet' }], edges = [], unknown = [];
    const vpcs = r.vpcs.filter(tagged);
    const vpcIds = new Set(vpcs.map(v => v.id));
    const instances = r.instances.filter(tagged);
    if (!vpcs.length && !instances.length) return Object.assign(base, { reason: 'No resources tagged Project=' + project + ' in eu-central-1' + (uc.state === 'turning_on' ? ' yet — the turn-on is still running' : '') });
    const roles = (decl && decl.roles) || {};
    for (const v of vpcs) {
      nodes.push({ id: v.id, kind: 'vpc', label: v.name || v.id, cidr: v.cidr, parent: null, detail: v });
      for (const sn of v.subnets || []) {
        const t = sn.default_route; const exposure = !t ? 'isolated' : /^igw-/.test(t) ? 'public' : /^nat-/.test(t) ? 'private' : 'isolated';
        nodes.push({ id: sn.id, kind: 'subnet', label: sn.name || sn.id, cidr: sn.cidr, parent: v.id, exposure, az: sn.az, detail: sn });
        if (t) edges.push({ kind: 'route', from: sn.id, to: t, label: '0.0.0.0/0' });
      }
      if (v.igw) { nodes.push({ id: v.igw, kind: 'igw', label: 'IGW', parent: v.id }); edges.push({ kind: 'uplink', from: v.igw, to: 'internet' }); }
    }
    for (const n of r.nat_gateways) {
      if (!tagged(n) && !vpcIds.has(n.vpc)) continue;
      nodes.push({ id: n.id, kind: 'nat', label: n.name || 'NAT', parent: n.vpc, public_ip: n.public_ip, subnet: n.subnet, detail: n });
      const v = vpcs.find(x => x.id === n.vpc); if (v && v.igw) edges.push({ kind: 'uplink', from: n.id, to: v.igw });
    }
    const byName = {};
    for (const i of instances) {
      nodes.push({ id: i.id, kind: 'instance', label: i.name || i.id, parent: i.subnet, role: roles[i.name] || null, type: i.type, state: i.state, private_ip: i.private_ip, public_ip: i.public_ip, detail: i });
      byName[i.name] = i.id;
      for (const g of i.security_groups || []) { const sg = r.security_groups.find(x => x.id === g.id); for (const rule of (sg && sg.ingress) || []) edges.push({ kind: 'allow', from: rule.source, to: i.id, label: (rule.proto === 'all' ? 'all' : rule.proto + '/' + (rule.from === rule.to ? rule.from : rule.from + '-' + rule.to)) }); }
    }
    const instIds = new Set(instances.map(i => i.id)), natIds = new Set(nodes.filter(n => n.kind === 'nat').map(n => n.id));
    for (const e of r.eips) {
      const a = e.association;
      const to = a && a.kind === 'instance' && instIds.has(a.id) ? a.id : a && a.kind === 'nat' && natIds.has(a.id) ? a.id : null;
      if (to || tagged(e)) nodes.push({ id: e.allocation_id, kind: 'eip', label: e.ip, attached_to: to, detail: e });
    }
    const natOf = instId => { const i = instances.find(x => x.id === instId); return nodes.find(n => n.kind === 'nat' && i && n.parent === i.vpc); };
    const resolve = (name, what) => { if (name === 'internet') return 'internet'; if (byName[name]) return byName[name]; unknown.push({ kind: what, label: name, reason: 'no running instance named ' + name }); return null; };
    for (const f of (decl && decl.flows) || []) {
      const from = resolve(f.from, 'flow'), to = resolve(f.to, 'flow'); if (!from || !to) continue;
      const via = []; let bad = null;
      for (const v of f.via || []) { if (v === 'internet') via.push('internet'); else if (v === 'nat') { const n = natOf(from); if (n) via.push(n.id); else bad = 'no NAT gateway in the VPC of ' + f.from; } else via.push(v); }
      if (bad) { unknown.push({ kind: 'flow', label: f.from + ' → ' + f.to, reason: bad }); continue; }
      edges.push({ kind: 'flow', from, to, via, label: f.label, declared: true });
    }
    for (const b of (decl && decl.blocked) || []) { const from = resolve(b.from, 'blocked pair'), to = resolve(b.to, 'blocked pair'); if (from && to) edges.push({ kind: 'blocked', from, to, label: b.label || 'blocked', declared: true }); }
    const enrolment = {};
    for (const [k, v] of Object.entries(uc.status || {})) { if (!v || typeof v !== 'object' || !('status' in v)) continue; const id = byName['zpa-lab-' + k.replace(/_/g, '-')]; if (id) enrolment[id] = { authenticated: v.status === 'ZPN_STATUS_AUTHENTICATED', label: k === 'pse' ? 'Private Service Edge' : 'App Connector' }; }
    return Object.assign(base, { generated_at: inv.generated_at, nodes, edges, enrolment, unknown });
  }


  const PSE_DESC = `A reproducible **Zscaler Private Access — Private Service Edge** built end to end in AWS
\`eu-central-1\`, with unattended provisioning-key enrolment. The network is the drawing above:
structure from the live inventory, dashed flows from this manifest.

## What turning it on does

Creates the ZPA groups and provisioning keys (prefixed \`AWS-Lab\`, reused if present), seeds the
keys into SSM Parameter Store as \`SecureString\`, applies the infrastructure, and waits until the
Service Edge and both App Connectors report \`ZPN_STATUS_AUTHENTICATED\`. Each instance reads its
own key from SSM at boot through its IAM role and self-enrols.

## What turning it off does

\`tofu destroy\` removes every AWS resource. The ZPA groups and keys deliberately survive; the enrolled
components show as disconnected in the ZPA portal until the next turn-on.

## Cost

About **$285/month** at on-demand list price while on; zero when off. No vendor licensing: the ZPA
AMIs bill as plain \`RunInstances\`. The Clouds page shows the live figure under \`Project=zpa-pse-lab\`.

## Sharing

Self-contained. The only shared object is the ZPA tenant; no VPC, segment, server group or policy is
shared with anything else. See the [lab repository](https://github.com/nilsujma-dev/zs-zpa-private-service-edge-lab) for the runbook.`;

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
      status: { pse: { enrolled: true, status: 'ZPN_STATUS_AUTHENTICATED', version: '25.62.1' }, connector: { status: 'ZPN_STATUS_AUTHENTICATED' }, priv_connector: { status: PARAMS.get('enrol') === 'partial' ? 'ZPN_STATUS_DISCONNECTED' : 'ZPN_STATUS_AUTHENTICATED' }, client: { app_segment: 'zpa-lab-server', reachable: true }, checked_at: iso(40000) },
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

  /* ---- providers: identities, forms, connection reports ---- */
  const IDENTITY = {
    aws: identity,
    gcp: { client_email: 'switchboard@zs-lab-demo.iam.gserviceaccount.com', project_id: 'zs-lab-demo', project_name: 'Zscaler Lab Demo', project_number: '638201947512' },
    azure: { tenant: '3f1a9c2e-7b4d-4e8f-9a1b-2c3d4e5f6a7b', tenant_name: 'nilsujma.onmicrosoft.com', subscription_name: 'ZS Lab Sandbox', subscription_id: 'a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d', client_id: '9e8d7c6b-5a4f-4e3d-2c1b-0a9f8e7d6c5b' },
  };
  const PROVIDERS = [
    { id: 'aws', name: 'Amazon Web Services', capabilities: { inventory: true, usecases: true } },
    { id: 'gcp', name: 'Google Cloud', capabilities: { inventory: false, usecases: false } },
    { id: 'azure', name: 'Microsoft Azure', capabilities: { inventory: false, usecases: false } },
  ];
  const UNSUPPORTED = {
    gcp: 'Inventory and cost are not built for Google Cloud yet; the connection is real, the scan is not',
    azure: 'Inventory and cost are not built for Azure yet; the connection is real, the scan is not',
  };
  const FORMS = {
    aws: [
      { name: 'access_key_id', label: 'Access key ID', type: 'text', required: true, help: 'AKIA… (long-lived) or ASIA… (SSO session)' },
      { name: 'secret_access_key', label: 'Secret access key', type: 'password', required: true, help: 'Never stored in clear; Fernet-encrypted at rest' },
      { name: 'session_token', label: 'Session token', type: 'textarea', required: false, help: 'Required with ASIA… keys from an SSO session' },
    ],
    gcp: [
      { name: 'service_account_json', label: 'Service account key (JSON)', type: 'file', required: true, help: 'Paste or upload the key file downloaded from IAM → Service accounts → Keys. It must be of type service_account.' },
      { name: 'project_id', label: 'Project ID', type: 'text', required: false, help: 'Defaults to the key’s own project_id; set it to validate the key against another project.' },
    ],
    azure: [
      { name: 'tenant_id', label: 'Tenant ID', type: 'text', required: true, help: 'Directory (tenant) ID of the Entra ID tenant, a GUID' },
      { name: 'client_id', label: 'Client ID', type: 'text', required: true, help: 'Application (client) ID of the service principal, a GUID' },
      { name: 'client_secret', label: 'Client secret', type: 'password', required: true, help: 'The secret *value* (not its ID); Fernet-encrypted at rest' },
      { name: 'subscription_id', label: 'Subscription ID', type: 'text', required: false, help: 'Required only if the principal can see more than one subscription' },
    ],
  };
  const connectedAt = {}, updatedAt = {};
  connected.forEach(id => { connectedAt[id] = connectedAtBoot; });
  if (PARAMS.get('rotated') === '1') connected.forEach(id => { updatedAt[id] = iso(20 * MIN); }); // "credentials updated 20 min ago"
  function providerView(p) {
    const on = connected.has(p.id);
    const idn = on ? IDENTITY[p.id] : null;
    const label = !idn ? null : p.id === 'aws' ? idn.account : p.id === 'gcp' ? idn.client_email : idn.subscription_name;
    return { id: p.id, name: p.name, status: on ? 'connected' : 'disconnected', identity: idn, identity_label: label, regions: on && p.id === 'aws' ? REGIONS : [], connected_at: on ? connectedAt[p.id] : null, credentials_updated_at: on ? (updatedAt[p.id] || connectedAt[p.id]) : null, capabilities: p.capabilities };
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
  /** GCP: bad JSON fails at the first check; a key whose private_key mentions FAIL fails at the token check. */
  function reportGcp(body) {
    let info = null, jsonErr = null;
    try { info = JSON.parse(body.service_account_json || ''); if (!info || info.type !== 'service_account') jsonErr = 'JSON parsed but "type" is ' + JSON.stringify(info && info.type) + ', not "service_account"'; }
    catch (e) { jsonErr = 'not valid JSON: ' + e.message.split('\n')[0]; }
    const idn = IDENTITY.gcp;
    if (jsonErr) return { ok: false, identity: null, checks: [
      { name: 'Service account JSON valid', ok: false, detail: jsonErr },
      { name: 'Token obtainable', ok: false, detail: 'Skipped: the key could not be read' },
      { name: 'Project resolvable', ok: false, detail: 'Skipped: the key could not be read' },
      { name: 'Compute Engine API enabled', ok: false, detail: 'Skipped: the key could not be read' }] };
    const email = info.client_email || idn.client_email, project = (body.project_id || info.project_id || idn.project_id);
    const bad = /fail/i.test(String(info.private_key || '') + String(info.private_key_id || ''));
    return { ok: !bad, identity: bad ? null : Object.assign({}, idn, { client_email: email, project_id: project }), checks: [
      { name: 'Service account JSON valid', ok: true, detail: email },
      { name: 'Token obtainable', ok: !bad, detail: bad ? 'invalid_grant: Invalid JWT Signature.' : 'OAuth2 token for ' + email },
      { name: 'Project resolvable', ok: !bad, detail: bad ? 'Skipped: no token' : idn.project_name + ' (' + project + ') ACTIVE' },
      { name: 'Compute Engine API enabled', ok: !bad, detail: bad ? 'Skipped: no token' : '41 regions' }] };
  }
  /** Azure: a secret containing "fail" is rejected at the token check. */
  function reportAzure(body) {
    const idn = IDENTITY.azure;
    const guid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    const badGuid = !guid.test(body.tenant_id || '') ? 'tenant id' : !guid.test(body.client_id || '') ? 'client id' : null;
    const bad = badGuid ? badGuid + ' is not a GUID' : /fail/i.test(body.client_secret || '') ? 'AADSTS7000215: Invalid client secret provided. Ensure the secret being sent in the request is the client secret value, not the client secret ID.' : null;
    const sub = body.subscription_id || idn.subscription_id;
    return { ok: !bad, identity: bad ? null : Object.assign({}, idn, { tenant: body.tenant_id, client_id: body.client_id, subscription_id: sub }), checks: [
      { name: 'Token from client secret', ok: !bad, detail: bad || 'token for ' + body.client_id + ' in ' + idn.tenant_name },
      { name: 'Subscriptions listable', ok: !bad, detail: bad ? 'Skipped: no token' : '1 visible, 1 enabled' },
      { name: 'Subscription readable', ok: !bad, detail: bad ? 'Skipped: no token' : idn.subscription_name + ' (' + sub + ') Enabled' },
      { name: 'Resource Manager reachable', ok: !bad, detail: bad ? 'Skipped: no token' : '7 resource groups' }] };
  }

  /* ---- outline fixtures: a real-looking plan per use case and action ---- */
  const PSE_ADDRS = [
    'aws_vpc.pse', 'aws_vpc.app',
    'aws_subnet.public', 'aws_subnet.b_public', 'aws_subnet.priv', 'aws_subnet.mcu',
    'aws_internet_gateway.pse', 'aws_internet_gateway.app',
    'aws_nat_gateway.app',
    'aws_eip.pse', 'aws_eip.nat',
    'aws_route_table.pse_public', 'aws_route_table.app_public', 'aws_route_table.app_private', 'aws_route_table.app_client',
    'aws_route.pse_default', 'aws_route.app_public_default', 'aws_route.app_private_default', 'aws_route.app_client_default',
    'aws_route_table_association.pse_public', 'aws_route_table_association.app_public', 'aws_route_table_association.app_private', 'aws_route_table_association.app_server', 'aws_route_table_association.app_client',
    'aws_security_group.pse', 'aws_security_group.connector', 'aws_security_group.nginx', 'aws_security_group.client',
    'aws_vpc_security_group_ingress_rule.pse_tls_tcp', 'aws_vpc_security_group_ingress_rule.pse_tls_udp', 'aws_vpc_security_group_ingress_rule.pse_ssh', 'aws_vpc_security_group_ingress_rule.connector_ssh', 'aws_vpc_security_group_ingress_rule.nginx_http', 'aws_vpc_security_group_ingress_rule.nginx_https',
    'aws_vpc_security_group_egress_rule.pse', 'aws_vpc_security_group_egress_rule.connector', 'aws_vpc_security_group_egress_rule.nginx', 'aws_vpc_security_group_egress_rule.client',
    'aws_instance.pse', 'aws_instance.connector', 'aws_instance.priv_connector', 'aws_instance.server', 'aws_instance.mcu_client',
    'aws_iam_role.zpa', 'aws_iam_instance_profile.zpa', 'aws_iam_role_policy.ssm_read',
    'aws_key_pair.lab',
    'aws_network_acl.app',
  ];
  const CC_ADDRS = ['aws_vpc.cc', 'aws_subnet.cc_public', 'aws_subnet.workload', 'aws_internet_gateway.cc', 'aws_route_table.cc_public', 'aws_route_table.workload', 'aws_route.cc_default', 'aws_route.workload_via_cc', 'aws_route_table_association.cc_public', 'aws_route_table_association.workload', 'aws_security_group.cc', 'aws_security_group.workload', 'aws_instance.cc', 'aws_instance.workload'];
  const entry = a => { const m = /^([a-z0-9_]+)\.([^\[]+)(\[.*\])?$/.exec(a); return { address: a, type: m[1], name: m[2], module: null }; };
  const EFFECTS = {
    'zpa-private-service-edge': {
      on: { creates: [
        'ZPA Service Edge Group, App Connector Groups and provisioning keys — reused by name if they already exist, never duplicated',
        'Three SSM SecureString parameters under /zpa-lab/ holding the provisioning keys (overwritten on every run)',
        'An enrolled Service Edge and two App Connectors in ZPA once the instances boot and dial in (~3 minutes)'], retains: [] },
      off: { destroys: ['Everything OpenTofu manages in this use case: both VPCs and all their subnets, routes, NACLs and security groups, the NAT gateway, both elastic IPs, all five instances and their volumes, the IAM role and instance profile — see the plan'],
        retains: [
          'ZPA Service Edge Group, App Connector Groups and provisioning keys — deleting these is deliberately manual',
          'The enrolled Service Edge and App Connector entries in ZPA, which will show as disconnected and accumulate one per rebuild',
          'The three SSM parameters under /zpa-lab/ (harmless; overwritten on the next ON)',
          'The S3 state object (versioned, so this OFF is recoverable) and the remote lock'] },
    },
    'zia-cloud-connector-sandbox': {
      on: { creates: ['A Cloud Connector registration in ZIA with a provisioning URL — reused if present'], retains: [] },
      off: { destroys: ['The workload subnet and the connector instance — see the plan'], retains: ['The Cloud Connector entry in ZIA, shown as offline until the next ON'] },
    },
    'ot-edge-relay': {
      on: { creates: ['A rendered relay.conf committed to the checkout (not the repository)'], retains: [] },
      off: { destroys: ['The relay instance and its route — see the plan'], retains: ['The Modbus path in ZPA; the LOGO! PLC at 10.1.75.10 is never touched'] },
    },
  };
  function plan(create, destroy, unchanged) {
    const p = { ok: true, generated_at: iso(12000), duration_s: 6.4, create: create.map(entry), update: [], destroy: destroy.map(entry), read: [], unchanged: unchanged.map(entry), change_summary: { add: create.length, change: 0, remove: destroy.length, import: 0, operation: 'plan' } };
    p.summary = { create: p.create.length, update: 0, destroy: p.destroy.length, unchanged: p.unchanged.length, read: 0 };
    return p;
  }
  function outlineFor(ucId, action) {
    const uc = usecases[ucId];
    const eff = (EFFECTS[ucId] || { on: { creates: [], retains: [] }, off: { destroys: [], retains: [] } })[action];
    const base = { action, plan: null, declared: eff, steps: uc.procedure[action], retained_state: { backend: 's3', bucket: 'zs-lab-tfstate-257394018842', key: 'usecases/' + ucId + '/terraform.tfstate', region: 'eu-central-1' } };
    if (ucId === 'zpa-private-service-edge') base.plan = action === 'on' ? plan([], [], PSE_ADDRS) : plan([], PSE_ADDRS, []);
    else if (ucId === 'zia-cloud-connector-sandbox') base.plan = action === 'on' ? plan(CC_ADDRS, [], []) : plan([], [], []);
    else if (ucId === 'ot-edge-relay') base.plan = { ok: false, generated_at: iso(3000), error: 'tofu plan exited 1: Reference to undeclared input variable: An input variable with the name "relay_host" has not been declared. This variable can be declared with a variable "relay_host" {} block.\n\n  on terraform/main.tf line 41, in resource "aws_instance" "relay":\n  41:   user_data = templatefile("${path.module}/relay.sh.tftpl", { host = var.relay_host })' };
    else if (uc.provider !== 'aws') base.plan = { ok: false, generated_at: iso(1000), error: 'Provider "' + uc.provider + '" is connected for credentials only: capabilities.usecases is false, so no plan can run' };
    else base.plan = plan([], [], []);
    return base;
  }

  /* ---- scenes: ?scene=checklist&provider=gcp&shown=3&fail=1  ?scene=form&provider=azure&mode=rotate
          ?page=usecases&expand=<id>&code=1&confirm=1  ?region=eu-central-1&inst=<instance id>
          ?topo=off (PSE drawing in its off state)  ?topo=fail (OT relay drawing errors)  ?enrol=partial (one lamp red)  ?node=<id> (a node selected in the drawing) ---- */
  function scene(state) {
    const page = PARAMS.get('page');
    if (page === 'usecases' || page === 'clouds') location.hash = '#/' + page;
    if (PARAMS.get('region')) location.hash = '#/clouds/aws/' + PARAMS.get('region');
    if (PARAMS.get('inst')) state.drawer.inst[PARAMS.get('inst')] = true;
    if (PARAMS.get('project')) state.drawer.project = PARAMS.get('project');
    if (PARAMS.get('scene') === 'checklist') {
      const prov = PARAMS.get('provider') || 'aws';
      const rep = prov === 'gcp' ? reportGcp({ service_account_json: PARAMS.get('fail') === '1' ? '{"type":"service_account","private_key":"FAIL"}' : '{"type":"service_account"}' })
        : prov === 'azure' ? reportAzure({ tenant_id: IDENTITY.azure.tenant, client_id: IDENTITY.azure.client_id, client_secret: PARAMS.get('fail') === '1' ? 'fail' : 'ok' })
        : report(PARAMS.get('fail') === '1', true);
      const shown = Math.min(rep.checks.length, parseInt(PARAMS.get('shown') || '3', 10));
      state.connect = { provider: prov, mode: PARAMS.get('mode') || 'connect', stage: shown >= rep.checks.length ? 'done' : 'checking', fields: FORMS[prov], values: {}, fieldErrors: {}, report: rep, shown, error: null, busy: true };
    }
    if (PARAMS.get('expand')) state.expanded = PARAMS.get('expand');
    if (PARAMS.get('expand') && PARAMS.get('node')) state.topo[PARAMS.get('expand')] = { loading: false, data: null, err: null, width: 0, sel: { node: PARAMS.get('node') }, refocus: null };
  }
  async function sceneAfter(state) {
    if (PARAMS.get('scene') === 'form') await openConnect(PARAMS.get('provider') || 'aws', PARAMS.get('mode') || 'connect');
    const id = PARAMS.get('expand');
    if (id && usecases[id]) {
      await loadDetail(id);
      loadOutline(id, 'on'); loadOutline(id, 'off'); await loadTopology(id);
      if (PARAMS.get('code') === '1') await toggleCode(usecases[id]);
      if (PARAMS.get('confirm') === '1') { if (!ucById(id)) await loadUsecases(true); if (ucById(id)) requestFlip(ucById(id)); }
    }
  }

  /* ---- api surface ---- */
  const need = async () => { await sleep(120 + Math.random() * 180); if (!authed) { onUnauthorised(); throw new ApiError('Not signed in.', 401, 'unauthorised'); } };
  const listItem = uc => {
    const lr = uc.runs[0] ? { job_id: uc.runs[0].job_id, action: uc.runs[0].action, state: uc.runs[0].state, ended: uc.runs[0].ended, started: uc.runs[0].started } : null;
    return { id: uc.id, name: uc.name, provider: uc.provider, summary: uc.summary, state: uc.state, resources: uc.resources, last_run: lr, provider_connected: connected.has(uc.provider) };
  };
  const api = {
    me: async () => { await sleep(80); if (!authed) throw new ApiError('Not signed in.', 401, 'unauthorised'); return { authenticated: true }; },
    login: async (pw) => { await sleep(500); if (!pw || pw === 'wrong') throw new ApiError('Wrong password.', 401, 'bad_password'); authed = true; return null; },
    logout: async () => { authed = false; return null; },
    providers: async () => { await need(); return PROVIDERS.map(providerView); },
    providerForm: async (id) => { await need(); await sleep(250); if (!FORMS[id]) throw new ApiError("Unknown provider '" + id + "'", 404, 'unknown_provider'); return { fields: FORMS[id] }; },
    connect: async (id, body) => {
      await need(); await sleep(700);
      if (!FORMS[id]) throw new ApiError("Unknown provider '" + id + "'", 404, 'unknown_provider');
      const missing = FORMS[id].filter(f => f.required && !(body[f.name] && String(body[f.name]).trim())).map(f => f.name);
      if (missing.length) throw new ApiError('Invalid request: check ' + missing.join(', '), 422, 'validation_error');
      const rep = id === 'aws' ? report(/fail/i.test(body.access_key_id || ''), !!body.session_token) : id === 'gcp' ? reportGcp(body) : reportAzure(body);
      if (rep.ok) {
        const was = connected.has(id);
        connected.add(id);
        if (!was) connectedAt[id] = iso(0); else updatedAt[id] = iso(0);
        if (id === 'aws') inv = inventory(false);
      }
      return rep;
    },
    disconnect: async (id) => { await need(); connected.delete(id); delete connectedAt[id]; delete updatedAt[id]; return null; },
    inventory: async (id, refresh) => {
      await need();
      if (!connected.has(id)) throw new ApiError("Provider '" + id + "' is not connected", 409, 'provider_not_connected');
      if (id !== 'aws') return { supported: false, reason: UNSUPPORTED[id] || 'Inventory is not built for this provider yet', generated_at: null, stale: false };
      if (refresh) { await sleep(1400); inv = inventory(true); }
      return inv;
    },
    usecases: async () => { await need(); return Object.values(usecases).map(listItem); },
    usecase: async (id) => { await need(); const uc = usecases[id]; if (!uc) throw new ApiError('No such use case.', 404, 'not_found'); return Object.assign(listItem(uc), { description: uc.description, procedure: uc.procedure, source: uc.source, status: uc.status, runs: uc.runs.slice(0, 20) }); },
    topology: async (id, refresh) => { await need(); const uc = usecases[id]; if (!uc) throw new ApiError('No such use case.', 404, 'not_found'); await sleep(refresh ? 900 : 350); if (id === 'ot-edge-relay' && PARAMS.get('topo') === 'fail') throw new ApiError('Inventory scan failed: RequestLimitExceeded', 502, 'inventory_failed'); return topologyFor(id, refresh); },
    outline: async (id, action) => {
      await need(); const uc = usecases[id]; if (!uc) throw new ApiError('No such use case.', 404, 'not_found');
      if (action !== 'on' && action !== 'off') throw new ApiError("Unknown action '" + action + "'", 400, 'bad_action');
      if (uc.runs.some(r => r.state === 'running')) throw new ApiError("A job is already running for use case '" + id + "'", 409, 'job_running');
      await sleep(action === 'on' ? 900 : 1500);
      return outlineFor(id, action);
    },
    flip: async (id, action) => {
      await need(); const uc = usecases[id]; if (!uc) throw new ApiError('No such use case.', 404, 'not_found');
      if (uc.runs.some(r => r.state === 'running')) throw new ApiError('A job is already running for this use case.', 409, 'busy');
      if (uc.provider !== 'aws' || !connected.has('aws')) throw new ApiError('Provider is not connected.', 409, 'provider_disconnected');
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
  return { api, usecases, jobs, scene, sceneAfter, report, reportGcp, reportAzure, outlineFor, inventory, topologyFor, MANIFEST_TOPO };
})();

/* ==========================================================================
   boot
   ========================================================================== */

Object.assign(SB, { render, navigate, api: () => api, MOCK });
document.addEventListener('DOMContentLoaded', boot);
})();
