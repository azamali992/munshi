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
  const state = { token: store.get('token'), me: null, thread: 'main', chatBusy: false, lang: store.get('lang', 'en'), online: navigator.onLine, queue: store.get('queue', []), attention: store.get('attention', []), theme: store.get('theme', 'light') };

  // ---------------------------------------------------------------- i18n
  const t = k => (window.MUNSHI_I18N[state.lang] || {})[k] ?? window.MUNSHI_I18N.en[k] ?? k;
  function setLang(l, rerender = true) { state.lang = l; store.set('lang', l); document.documentElement.lang = l; document.documentElement.dir = l === 'ur' ? 'rtl' : 'ltr'; document.body.classList.toggle('ur', l === 'ur'); if (rerender) render(); }
  // Light is the default; dark is an explicit opt-in. theme.js applies the saved choice before first paint.
  function setTheme(th, rerender = true) { state.theme = th === 'dark' ? 'dark' : 'light'; store.set('theme', state.theme); window.munshiApplyTheme?.(state.theme); if (rerender) render(); }

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

  // Toast: #toast is a permanent role=status live region (never display:none, so screen readers announce it).
  // It sits just above the bottom nav unless that would put it over a primary action -- Close stop, a sheet's
  // Save, the chat composer -- in which case it moves under the top bar. It is pointer-events:none either way,
  // and it follows scrolling while visible so it cannot drift onto a button the user scrolls into its path.
  const PRIMARY = '.btn.primary, .btn.danger, button[type=submit], .sheet-f .btn, .composer';
  let toastT, toastClearT, toastRaf;
  function placeToast() {
    const el = $('#toast'); if (!el.classList.contains('show')) return;
    el.classList.remove('top'); el.style.top = '';
    const r = el.getBoundingClientRect();
    const scope = $('#sheet').hidden ? document : $('#sheet');     // with a sheet open, only its buttons are live
    const hits = $$(PRIMARY, scope).some(b => { const q = b.getBoundingClientRect(); return q.width > 0 && q.height > 0 && q.top < r.bottom + 8 && q.bottom > r.top - 8 && q.left < r.right && q.right > r.left; });
    if (!hits) return;
    const bar = $('.topbar'); el.style.top = Math.max(8, (bar ? bar.getBoundingClientRect().bottom : 0) + 8) + 'px'; el.classList.add('top');
  }
  const onToastScroll = () => { cancelAnimationFrame(toastRaf); toastRaf = requestAnimationFrame(placeToast); };
  function toast(msg, ms = 2600) {
    const el = $('#toast');
    clearTimeout(toastT); clearTimeout(toastClearT);
    el.textContent = ''; el.textContent = msg;                     // reset first so a repeated message is announced again
    el.classList.add('show'); placeToast();
    window.addEventListener('scroll', onToastScroll, { passive: true }); window.addEventListener('resize', onToastScroll);
    toastT = setTimeout(() => {
      el.classList.remove('show'); window.removeEventListener('scroll', onToastScroll); window.removeEventListener('resize', onToastScroll);
      toastClearT = setTimeout(() => { if (!el.classList.contains('show')) { el.textContent = ''; el.classList.remove('top'); el.style.top = ''; } }, 200);
    }, ms);
  }

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
    if (!res.ok) throw new ApiError(typeof body === 'object' && body.detail ? (typeof body.detail === 'string' ? body.detail
      : Array.isArray(body.detail) ? body.detail.map(d => d && d.msg ? `${(d.loc || []).slice(1).join(' ')}${d.loc && d.loc.length > 1 ? ': ' : ''}${d.msg}` : JSON.stringify(d)).join('; ')   // FastAPI 422: readable, not raw JSON
      : JSON.stringify(body.detail)) : String(body).slice(0, 200), res.status);
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
    nav.hidden = true; topRight.innerHTML = ''; paintAttention(); location.hash = '#login'; render();
  }
  async function signedIn(r) {
    state.token = r.token; state.me = r.me; store.set('token', r.token); store.set('me', r.me);
    if (r.me.language && !store.get('lang')) setLang(r.me.language, false);
    paintTop();
    const target = '#' + home();
    if (location.hash === target) await render(); else location.hash = target;   // hashchange renders once
    if (state.online) flushQueue();          // closes queued before a session expired are still waiting; send them now
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
    topRight.innerHTML = `<a href="#notifications" id="bell" class="bell" title="${esc(t('notifications'))}" aria-label="${esc(t('notifications'))}"><span aria-hidden="true">🔔</span><b id="nbadge" class="badge" hidden></b></a><span class="pill role">${esc(state.me.name.split(' ')[0])} · ${esc(state.me.role)}</span>`;
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
  // Two persistent lists in localStorage, both surviving reloads:
  //   queue     -- closes waiting for signal. Retried automatically when the phone comes back online.
  //   attention -- closes the server REFUSED (wrong code, already closed, numbers don't add up...). Resending the
  //                same request can never succeed, but the driver's entered delivery and cash must not vanish,
  //                so they are kept, with the reason, until the driver fixes and resends them or deliberately
  //                removes them after telling a supervisor. Nothing is ever dropped silently.
  // "Retry later" = no answer (offline), session expired (401: sign in again, then it resends), timeout, rate
  // limit, or a server fault (5xx). Any other 4xx is a rejection of this specific request -> attention.
  const retryLater = s => s === 0 || s === 401 || s === 408 || s === 425 || s === 429 || s >= 500;
  const refTime = item => { const ms = Number(String(item.body?.client_ref || '').split(':').pop()); return ms > 0 ? new Date(ms).toISOString() : new Date().toISOString(); };
  function enqueue(item) { state.queue.push({ queued_at: new Date().toISOString(), ...item }); store.set('queue', state.queue); paintQueue(); }
  function paintQueue() { const q = $('#queue'); if (q) { q.hidden = !state.queue.length; q.innerHTML = `${state.queue.length} ${t('queued')} · <button class="link hit" id="syncBtn">${t('sync_now')}</button>`; $('#syncBtn')?.addEventListener('click', flushQueue); } paintAttention(); }
  // Why the server refused, as a plain-language key (translated at render time) plus what the driver can do.
  function rejection(e) {
    const m = String(e.message || ''), s = e.status;
    if (s === 403 && /otp|code/i.test(m)) return { why: 'why_code', kind: 'code' };
    if (s === 409 && /already/i.test(m)) return { why: 'why_closed', kind: 'supervisor' };
    if (s === 409) return { why: 'why_plan', kind: 'supervisor' };
    if (s === 400 || s === 422) return { why: 'why_values', kind: 'fix' };
    if (s === 404) return { why: 'why_gone', kind: 'supervisor' };
    if (s === 403) return { why: 'why_perm', kind: 'supervisor' };
    return { why: 'why_other', kind: 'supervisor' };
  }
  const saveAttention = () => { store.set('attention', state.attention); paintAttention(); };
  // Record a refusal. `replaceId` updates an existing entry in place (a retry of it was refused again).
  function addAttention(item, e, replaceId) {
    const prev = replaceId ? state.attention.find(a => a.id === replaceId) : null;
    const entry = { id: item.body?.client_ref || `${item.stop_id}:${Date.now()}`, stop_id: item.stop_id, path: item.path, body: item.body, label: item.label,
      queued_at: prev?.queued_at || item.queued_at || refTime(item), failed_at: new Date().toISOString(), ...rejection(e), detail: e.message, status: e.status, attempts: (prev?.attempts || 0) + 1 };
    state.attention = state.attention.filter(a => a.id !== entry.id && a.id !== replaceId).concat(entry);
    saveAttention(); return entry;
  }
  const attentionFor = stopId => state.attention.filter(a => a.stop_id === stopId).pop() || null;
  function clearAttention(stopId) { state.attention = state.attention.filter(a => a.stop_id !== stopId); saveAttention(); }
  function removeAttention(id) { state.attention = state.attention.filter(a => a.id !== id); saveAttention(); }
  function paintAttention() {
    const b = $('#attn'); if (!b) return;
    const n = state.attention.length; b.hidden = !n || !state.token || /^#stops?(\/|$)/.test(location.hash);   // the Stops screens show the full cards instead
    if (n) b.innerHTML = `<span aria-hidden="true">⚠</span> ${n} ${esc(t(n === 1 ? 'attn_banner_1' : 'attn_banner'))} <b>›</b>`;
  }
  let flushing = false;
  async function flushQueue() {
    if (!state.queue.length || !state.token || flushing) return;
    flushing = true; let sent = 0, refused = 0;
    try {
      for (const item of [...state.queue]) {
        try { await post(item.path, item.body); sent++; }
        catch (e) {
          if (retryLater(e.status)) { if (e.status === 0 || e.status === 401) break; continue; }   // no signal / signed out: stop and wait; a server fault: keep it, try the next
          addAttention(item, e); refused++;                                                       // refused: moved, with the reason -- never dropped
        }
        state.queue = state.queue.filter(q => q !== item); store.set('queue', state.queue);         // saved after every item: a crash mid-sync loses nothing, and a resend is replayed by client_ref, not re-posted
      }
    } finally { flushing = false; paintQueue(); }
    if (refused) toast(`${refused} ${t('attn_toast')}`, 7000);
    else if (sent) toast(`${t('done')}: ${sent} ${t('synced')}`);
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
    paintOnline(); paintAttention(); render(); if (state.online) flushQueue();
  }
  document.addEventListener('DOMContentLoaded', boot);

  return { $, $$, view, state, store, t, setLang, setTheme, fmt, num, esc, day, when, statusPill, bucketPill, tierPill, pill, can, role, today, daysAgo, toast, api, post, patch, del, download,
           signOut, signedIn, home, MORE, sheet, confirmSheet, field, select, enqueue, flushQueue, render, refreshBadge, paintTop,
           retryLater, addAttention, attentionFor, clearAttention, removeAttention };
})();
