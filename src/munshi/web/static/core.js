/* Munshi mobile app — core runtime. Vanilla JS, no build step. Talks to /api, installs as a PWA.
   Owns: session state, API client, i18n, router + per-role navigation, sheets (bottom-sheet forms),
   the driver's offline queue, badge polling. Views live in views.js and register on window.V. */
window.M = (() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const view = $('#view'), nav = $('#nav'), topRight = $('#topRight');
  const store = {
    get: (k, d = null) => { try { const v = localStorage.getItem('munshi.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem('munshi.' + k, JSON.stringify(v)); } catch { } },
    del: k => { try { localStorage.removeItem('munshi.' + k); } catch { } },
  };
  const state = { token: store.get('token'), me: null, thread: 'main', chatBusy: false, lang: store.get('lang', 'en'), online: navigator.onLine, queue: store.get('queue', []) };

  // ---------------------------------------------------------------- i18n
  const t = k => (window.MUNSHI_I18N[state.lang] || {})[k] ?? window.MUNSHI_I18N.en[k] ?? k;
  function setLang(l, rerender = true) { state.lang = l; store.set('lang', l); document.documentElement.lang = l; document.documentElement.dir = l === 'ur' ? 'rtl' : 'ltr'; document.body.classList.toggle('ur', l === 'ur'); if (rerender) render(); }

  // ---------------------------------------------------------------- helpers
  const fmt = n => 'Rs ' + Math.round(Number(n || 0)).toLocaleString('en-PK');
  const num = n => Number(n || 0).toLocaleString('en-PK');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const day = s => (s || '').slice(0, 10);
  const when = s => (s || '').replace('T', ' ').slice(0, 16);
  const statusPill = s => ({ draft: '', confirmed: 'acc', allocated: 'acc', dispatched: 'warn', delivered: 'good', short: 'crit', cancelled: 'crit', planned: '', approved: 'warn', loaded: 'warn', completed: 'good', pending: '', skipped: 'crit', drafted: 'warn', sent: 'good', discarded: '', queued: 'warn', failed: 'crit' }[s] ?? '');
  const bucketPill = b => ({ 'current': 'good', '1-30': 'warn', '31-60': 'crit', '60+': 'crit' }[b] || '');
  const tierPill = tier => tier === 'high_risk' ? `<span class="pill crit">owner</span>` : tier === 'low_risk' ? `<span class="pill warn">clerk</span>` : `<span class="pill">auto</span>`;
  const pill = (txt, cls = '') => `<span class="pill ${cls}">${esc(t(txt) === txt ? txt : t(txt))}</span>`;
  const can = p => !!state.me && state.me.permissions.includes(p);
  const role = () => state.me?.role;
  const today = () => new Date().toISOString().slice(0, 10);
  const daysAgo = n => new Date(Date.now() - n * 864e5).toISOString().slice(0, 10);

  let toastT; function toast(msg, ms = 2600) { const el = $('#toast'); el.textContent = msg; el.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => el.hidden = true, ms); }

  // ---------------------------------------------------------------- API
  class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }, state.token ? { 'X-Session': state.token } : {}, opts.headers || {});
    let res;
    try { res = await fetch(path, Object.assign({}, opts, { headers })); }
    catch (e) { state.online = false; paintOnline(); throw new ApiError(t('offline'), 0); }
    if (!state.online) { state.online = true; paintOnline(); }
    if (res.status === 401 && state.token) { signOut(true); throw new ApiError(t('signed_out'), 401); }
    const ct = res.headers.get('content-type') || '';
    const body = ct.includes('json') ? await res.json() : await res.text();
    if (res.status === 503 && body && body.detail === 'offline') { state.online = false; paintOnline(); throw new ApiError(t('offline'), 0); }   // the service worker's offline reply
    if (!res.ok) throw new ApiError(typeof body === 'object' && body.detail ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)) : String(body).slice(0, 200), res.status);
    return body;
  }
  const post = (p, b) => api(p, { method: 'POST', body: JSON.stringify(b ?? {}) });
  const patch = (p, b) => api(p, { method: 'PATCH', body: JSON.stringify(b ?? {}) });
  const del = p => api(p, { method: 'DELETE' });
  async function download(path, filename) {
    const res = await fetch(path, { headers: { 'X-Session': state.token } });
    if (!res.ok) return toast(t('error'));
    const b = await res.blob(); const u = URL.createObjectURL(b); const a = document.createElement('a'); a.href = u; a.download = filename; a.click(); URL.revokeObjectURL(u);
  }

  // ---------------------------------------------------------------- session
  function signOut(silent) {
    if (state.token && !silent) post('/api/session/logout').catch(() => { });
    state.token = null; state.me = null; store.del('token'); store.del('me');
    nav.hidden = true; topRight.innerHTML = ''; location.hash = '#login'; render();
  }
  async function signedIn(r) {
    state.token = r.token; state.me = r.me; store.set('token', r.token); store.set('me', r.me);
    if (r.me.language && !store.get('lang')) setLang(r.me.language, false);
    paintTop();
    const target = '#' + home();
    if (location.hash === target) await render(); else location.hash = target;   // hashchange renders once
  }

  // ---------------------------------------------------------------- navigation per role
  const NAV = {
    owner: [['today', 'today', '◧'], ['chat', 'chat', '✎'], ['approvals', 'approvals', '✓'], ['khata', 'khata', '☰'], ['more', 'more', '⋯']],
    clerk: [['desk', 'desk', '◧'], ['chat', 'chat', '✎'], ['approvals', 'approvals', '✓'], ['khata', 'khata', '☰'], ['more', 'more', '⋯']],
    salesman: [['book', 'book', '＋'], ['customers', 'customers', '☰'], ['chat', 'chat', '✎'], ['promises', 'promises', '✓'], ['more', 'more', '⋯']],
    driver: [['stops', 'stops', '⌖'], ['chat', 'chat', '✎'], ['more', 'more', '⋯']],
  };
  const home = () => (NAV[role()] || NAV.clerk)[0][0];
  const MORE = {
    owner: ['orders', 'dispatch', 'stock', 'customers', 'products', 'suppliers', 'purchases', 'expenses', 'reports', 'reminders', 'outbox', 'promises', 'notifications', 'audit', 'staff', 'setup', 'settings', 'help'],
    clerk: ['orders', 'dispatch', 'driver', 'stock', 'customers', 'products', 'suppliers', 'purchases', 'expenses', 'reports', 'reminders', 'outbox', 'promises', 'notifications', 'audit', 'settings', 'help'],
    salesman: ['orders', 'stock', 'khata', 'notifications', 'settings', 'help'],
    driver: ['orders', 'customers', 'notifications', 'settings', 'help'],
  };
  const PARENT = { order: 'orders', plan: 'dispatch', customer: 'khata', product: 'products', supplier: 'suppliers', stop: 'stops', notifications: 'more', signup: 'login', help: 'more', doc: 'khata', ledger: 'more' };
  function paintNav() {
    const items = NAV[role()] || NAV.clerk;
    nav.style.gridTemplateColumns = `repeat(${items.length},1fr)`;
    nav.innerHTML = items.map(([k, label, ico]) => `<a href="#${k}" data-tab="${k}"><span class="ico">${ico}</span><span>${esc(t(label))}</span>${k === 'approvals' ? '<b id="badge" class="badge" hidden></b>' : ''}${k === 'stops' ? '<b id="qbadge" class="badge" hidden></b>' : ''}</a>`).join('');
    nav.hidden = false;
  }
  function paintTop() {
    if (!state.me) { topRight.innerHTML = ''; return; }
    topRight.innerHTML = `<a href="#notifications" id="bell" class="bell" title="${esc(t('notifications'))}">🔔<b id="nbadge" class="badge" hidden></b></a><span class="pill role">${esc(state.me.name.split(' ')[0])} · ${esc(state.me.role)}</span>`;
  }
  function paintOnline() { const b = $('#offline'); if (b) b.hidden = state.online; }

  // ---------------------------------------------------------------- badge polling
  async function refreshBadge() {
    if (!state.token || !state.online) return;
    try {
      const b = await api('/api/badge');
      const el = $('#badge'); if (el) { el.textContent = b.approvals; el.hidden = !b.approvals; }
      const nb = $('#nbadge'); if (nb) { nb.textContent = b.unread; nb.hidden = !b.unread; }
      const qb = $('#qbadge'); if (qb) { qb.textContent = b.stops_pending; qb.hidden = !b.stops_pending; }
      document.title = (b.approvals ? `(${b.approvals}) ` : '') + 'Munshi';
    } catch { }
  }
  setInterval(refreshBadge, 30000);

  // ---------------------------------------------------------------- offline queue (driver stop closes)
  function enqueue(item) { state.queue.push(item); store.set('queue', state.queue); paintQueue(); }
  function paintQueue() { const q = $('#queue'); if (!q) return; q.hidden = !state.queue.length; q.innerHTML = `${state.queue.length} ${t('queued')} · <button class="link" id="syncBtn">${t('sync_now')}</button>`; $('#syncBtn')?.addEventListener('click', flushQueue); }
  let flushing = false;
  async function flushQueue() {
    if (!state.queue.length || !state.token || flushing) return;
    flushing = true;
    const remaining = [];
    for (const item of state.queue) {
      try { await post(item.path, item.body); toast(`${t('done')}: ${item.label}`); }
      catch (e) { if (e.status === 0) remaining.push(item); else toast(`${item.label}: ${e.message}`, 4000); }
    }
    state.queue = remaining; store.set('queue', remaining); paintQueue(); flushing = false;
    if (location.hash.startsWith('#stops')) await render();
  }
  window.addEventListener('online', () => { state.online = true; paintOnline(); flushQueue(); });
  window.addEventListener('offline', () => { state.online = false; paintOnline(); });

  // ---------------------------------------------------------------- sheets (forms) and confirms
  function sheet(title, bodyHtml, onSubmit, submitLabel) {
    const wrap = $('#sheet'); wrap.hidden = false;
    wrap.innerHTML = `<div class="sheet-bg"></div><form class="sheet-panel" id="sheetForm"><div class="sheet-h"><h2>${esc(title)}</h2><button type="button" class="x" aria-label="close">✕</button></div><div class="sheet-b">${bodyHtml}</div>
      ${onSubmit ? `<div class="sheet-f"><button type="button" class="btn">${esc(t('cancel'))}</button><button class="btn primary" type="submit">${esc(submitLabel || t('save'))}</button></div>` : ''}</form>`;
    const closeIt = () => { wrap.hidden = true; wrap.innerHTML = ''; };
    wrap.querySelector('.sheet-bg').onclick = closeIt; wrap.querySelector('.x').onclick = closeIt; wrap.querySelector('.sheet-f .btn')?.addEventListener('click', closeIt);
    const form = $('#sheetForm');
    form.onsubmit = async e => {
      e.preventDefault(); if (!onSubmit) return;
      const fd = new FormData(form); const data = {}; for (const [k, v] of fd.entries()) data[k] = v;
      $$('input[type=checkbox]', form).forEach(cb => data[cb.name] = cb.checked);
      const btn = form.querySelector('button[type=submit]'); btn.disabled = true;
      try { const r = await onSubmit(data, form); if (r !== false) closeIt(); } catch (err) { toast(err.message, 4000); } finally { btn.disabled = false; }
    };
    setTimeout(() => { if (!form.contains(document.activeElement)) form.querySelector('input,select,textarea')?.focus(); }, 50);   // don't steal focus from a field the user already tapped
    return { close: closeIt, form };
  }
  function confirmSheet(title, text, onYes, yesLabel) {
    return sheet(title, `<p>${esc(text)}</p>`, async () => { await onYes(); }, yesLabel || t('yes'));
  }
  const field = (label, name, attrs = '', type = 'text', value = '') => `<label class="f">${esc(label)}<input class="input${type === 'number' ? ' num' : ''}" type="${type}" name="${name}" value="${esc(value)}" ${attrs}></label>`;
  const select = (label, name, options, value = '') => `<label class="f">${esc(label)}<select class="input" name="${name}">${options.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>`;

  // ---------------------------------------------------------------- router
  // Renders are serialised: a hash change while a view is still loading waits its turn, so no view ever paints over another.
  let chain = Promise.resolve();
  function render() { chain = chain.then(_render, _render); return chain; }
  async function _render() {
    const h = (location.hash || '#' + (state.token ? home() : 'login')).slice(1);
    const [name, ...rest] = h.split('/'); const arg = rest.join('/');
    const V = window.V || {};
    if (!state.token) { nav.hidden = true; topRight.innerHTML = ''; return (V[name === 'signup' ? 'signup' : 'login'] || V.login)(); }
    if (!state.me) { try { state.me = await api('/api/me'); store.set('me', state.me); } catch (e) { if (e.status === 0 && store.get('me')) state.me = store.get('me'); else return; } }
    paintNav(); paintTop();
    const active = PARENT[name] || name;
    const tabs = (NAV[role()] || NAV.clerk).map(x => x[0]);
    $$('a', nav).forEach(a => a.classList.toggle('active', a.dataset.tab === active || (!tabs.includes(active) && a.dataset.tab === 'more')));
    window.scrollTo(0, 0);
    const fn = V[name];
    if (!fn) { location.hash = '#' + home(); return; }
    try { await fn(arg); } catch (err) { view.innerHTML = `<div class="empty">⚠ ${esc(err.message)}</div>`; }
    paintQueue(); refreshBadge();
  }
  window.addEventListener('hashchange', render);
  window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); window.__installPrompt = e; });
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => { });

  function boot() {
    document.documentElement.lang = state.lang; document.documentElement.dir = state.lang === 'ur' ? 'rtl' : 'ltr'; document.body.classList.toggle('ur', state.lang === 'ur');
    if (store.get('me')) state.me = store.get('me');
    paintOnline(); render(); if (state.online) flushQueue();
  }
  document.addEventListener('DOMContentLoaded', boot);

  return { $, $$, view, state, store, t, setLang, fmt, num, esc, day, when, statusPill, bucketPill, tierPill, pill, can, role, today, daysAgo, toast, api, post, patch, del, download,
           signOut, signedIn, home, MORE, sheet, confirmSheet, field, select, enqueue, flushQueue, render, refreshBadge, paintTop };
})();
