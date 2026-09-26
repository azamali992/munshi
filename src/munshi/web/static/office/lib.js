/* Office console runtime: session (shared with the phone app: same localStorage keys, same token), API client,
   formatting, toast, the side panel, and a small sortable/searchable/selectable table. No framework, no build. */
export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];

export const store = {
  get: (k, d = null) => { try { const v = localStorage.getItem('munshi.' + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem('munshi.' + k, JSON.stringify(v)); } catch { } },
  del: k => { try { localStorage.removeItem('munshi.' + k); } catch { } },
};
export const state = { token: store.get('token'), me: null, onSignedOut: null };

// ---------------------------------------------------------------- formatting
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
export const money = n => {
  const v = Number(n || 0), frac = Math.round(Math.abs(v) * 100) % 100 !== 0;
  return (v < 0 ? '−' : '') + 'Rs ' + Math.abs(v).toLocaleString('en-PK', { minimumFractionDigits: frac ? 2 : 0, maximumFractionDigits: 2 });
};
export const num = n => Number(n || 0).toLocaleString('en-PK');
export const signed = n => (n > 0 ? '+' : n < 0 ? '−' : '') + num(Math.abs(n));
// stored instants are UTC; the business runs on Pakistan time (UTC+5, no DST)
export const when = s => { if (!s) return ''; if (s.length === 10) return s; const d = new Date(s); if (isNaN(d)) return s; return new Date(d.getTime() + 5 * 3600e3).toISOString().replace('T', ' ').slice(0, 16); };
export const day = s => when(s).slice(0, 10);
export const todayPk = () => new Date(Date.now() + 5 * 3600e3).toISOString().slice(0, 10);
export const pill = (txt, cls = '') => `<span class="pill ${cls}">${esc(txt)}</span>`;

// ---------------------------------------------------------------- permissions
export const can = p => !!state.me && state.me.permissions.includes(p);
export const isOwner = () => state.me?.role === 'owner';

// ---------------------------------------------------------------- API
export class ApiError extends Error { constructor(msg, status) { super(msg); this.status = status; } }
function detail(body) {
  if (typeof body !== 'object' || !body || body.detail === undefined) return String(body).slice(0, 200);
  const d = body.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map(x => x && x.msg ? `${(x.loc || []).slice(1).join(' ')}${x.loc && x.loc.length > 1 ? ': ' : ''}${x.msg.replace(/^Value error, /, '')}` : JSON.stringify(x)).join('; ');
  return JSON.stringify(d);
}
export async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }, state.token ? { 'X-Session': state.token } : {}, opts.headers || {});
  let res;
  try { res = await fetch(path, Object.assign({}, opts, { headers })); }
  catch { throw new ApiError('No connection to the Munshi server.', 0); }
  const ct = res.headers.get('content-type') || '';
  const body = ct.includes('json') ? await res.json() : await res.text();
  if (res.status === 401 && state.token) { signOut(true); throw new ApiError('Your session ended -- sign in again.', 401); }
  if (!res.ok) throw new ApiError(detail(body), res.status);
  return body;
}
export const post = (p, b) => api(p, { method: 'POST', body: JSON.stringify(b ?? {}) });
export const patch = (p, b) => api(p, { method: 'PATCH', body: JSON.stringify(b ?? {}) });
export async function download(path, filename) {
  const res = await fetch(path, { headers: { 'X-Session': state.token } });
  if (!res.ok) { let m = 'Download failed'; try { m = detail(await res.json()); } catch { } return toast(m, 5000); }
  const b = await res.blob(), u = URL.createObjectURL(b), a = document.createElement('a');
  a.href = u; a.download = filename; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 2000);
}
/* Open a document in a new tab. The tab is opened synchronously (so no popup blocker objects) and pointed at the
   signed link once the API returns it. */
export async function openLink(getUrl) {
  const w = window.open('about:blank', '_blank');
  try { const url = await getUrl(); if (w) w.location.href = url; else location.href = url; }
  catch (e) { if (w) w.close(); toast(e.message, 5000); }
}

export function signOut(silent) {
  if (state.token && !silent) post('/api/session/logout').catch(() => { });
  state.token = null; state.me = null; store.del('token'); store.del('me');
  state.onSignedOut?.();
}

// ---------------------------------------------------------------- toast
let toastT;
export function toast(msg, ms = 3000) {
  const el = $('#toast'); clearTimeout(toastT);
  el.textContent = ''; el.textContent = msg; el.classList.add('show');
  toastT = setTimeout(() => el.classList.remove('show'), ms);
}

// ---------------------------------------------------------------- side panel
/* panel({title, body, onSubmit, submitLabel, wide, onOpen}) -- a right-hand drawer holding a form. onSubmit(data, form)
   may return false to keep the panel open; a thrown error is shown inside the panel, in plain words, not as a toast. */
export function panel({ title, body, onSubmit = null, submitLabel = 'Save', wide = false, onOpen = null, footer = '', closeLabel = 'Cancel' }) {
  const wrap = $('#drawer'); wrap.hidden = false;
  wrap.innerHTML = `<div class="scrim"></div><form class="o-panel${wide ? ' wide' : ''}" novalidate role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="o-panel-h"><h2>${esc(title)}</h2><button type="button" class="x" aria-label="Close">✕</button></div>
    <div class="o-panel-b">${body}<p class="formerr" role="alert"></p></div>
    ${onSubmit || footer ? `<div class="o-panel-f">${footer}<button type="button" class="btn ghost" data-close>${esc(closeLabel)}</button>${onSubmit ? `<button class="btn primary" type="submit">${esc(submitLabel)}</button>` : ''}</div>` : ''}</form>`;
  const form = $('form', wrap), err = $('.formerr', wrap);
  const onKey = e => { if (e.key === 'Escape') close(); };
  const close = () => { wrap.hidden = true; wrap.innerHTML = ''; document.removeEventListener('keydown', onKey); };
  document.addEventListener('keydown', onKey);
  $('.scrim', wrap).onclick = close; $('.x', wrap).onclick = close; $$('[data-close]', wrap).forEach(b => b.onclick = close);
  const setError = m => { err.textContent = m || ''; if (m) err.scrollIntoView({ block: 'nearest' }); };
  form.onsubmit = async e => {
    e.preventDefault(); if (!onSubmit) return;
    if (!form.checkValidity()) { form.reportValidity(); return; }
    const data = {}; for (const [k, v] of new FormData(form).entries()) data[k] = v;
    $$('input[type=checkbox][name]', form).forEach(cb => data[cb.name] = cb.checked);
    const btn = $('button[type=submit]', form); btn.disabled = true; setError('');
    try { const r = await onSubmit(data, form); if (r !== false) close(); }
    catch (x) { setError(x.message); }
    finally { btn.disabled = false; }
  };
  const ctl = { el: form, close, setError, body: $('.o-panel-b', wrap) };
  onOpen?.(ctl);
  setTimeout(() => $('input:not([type=hidden]):not([readonly]),select,textarea', form)?.focus(), 30);
  return ctl;
}

export const field = (label, name, value = '', attrs = '', type = 'text') =>
  `<label class="f">${esc(label)}<input class="input${type === 'number' ? ' num' : ''}" type="${type}" name="${name}" value="${esc(value)}" ${attrs}></label>`;
export const select = (label, name, options, value = '', attrs = '') =>
  `<label class="f">${esc(label)}<select class="input" name="${name}" ${attrs}>${options.map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(value) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select></label>`;
export const check = (label, name, checked) => `<label class="chk"><input type="checkbox" name="${name}" ${checked ? 'checked' : ''}> ${esc(label)}</label>`;

// ---------------------------------------------------------------- data table
/* table(container, opts): renders a sortable, searchable (and optionally selectable) table and keeps its state.
   columns: [{key, label, num, render(row) -> html, value(row) -> sort value, sortable=true, cls}]
   opts: rows, key (row id field), sort {key, dir}, searchKeys, selectable, onSelect(set), onRow(row, event),
         rowClass(row), empty, footer(rows) -> <tr> html, filter(row) -> bool */
export function table(container, opts) {
  const t = Object.assign({ rows: [], sort: null, search: '', selected: new Set(), selectable: false, empty: 'Nothing here yet.' }, opts);
  const cols = t.columns;
  const val = (c, r) => c.value ? c.value(r) : r[c.key];
  function visible() {
    const q = t.search.trim().toLowerCase();
    let rows = t.rows.filter(r => (!t.filter || t.filter(r)) && (!q || (t.searchKeys || cols.map(c => c.key)).some(k => String(r[k] ?? '').toLowerCase().includes(q))));
    if (t.sort) {
      const c = cols.find(x => x.key === t.sort.key); const d = t.sort.dir === 'desc' ? -1 : 1;
      if (c) rows = [...rows].sort((a, b) => { const x = val(c, a), y = val(c, b); if (x === y) return 0; if (x === null || x === undefined || x === '') return 1; if (y === null || y === undefined || y === '') return -1; return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'en', { numeric: true })) * d; });
    }
    return rows;
  }
  function render() {
    const rows = visible(); t.shown = rows;
    const allSel = t.selectable && rows.length > 0 && rows.every(r => t.selected.has(r[t.key]));
    container.innerHTML = `<table class="o-t"><thead><tr>${t.selectable ? `<th class="c"><input type="checkbox" data-all aria-label="Select all shown" ${allSel ? 'checked' : ''}></th>` : ''}${cols.map(c => {
      const s = c.sortable !== false; const cur = t.sort && t.sort.key === c.key ? (t.sort.dir === 'desc' ? 'descending' : 'ascending') : null;
      return `<th class="${s ? 'sort' : ''} ${c.num ? 'n' : ''}" ${s ? `data-sort="${esc(c.key)}" tabindex="0"` : ''} ${cur ? `aria-sort="${cur}"` : ''} ${c.title ? `title="${esc(c.title)}"` : ''}>${esc(c.label)}</th>`;
    }).join('')}</tr></thead>
      <tbody>${rows.length ? rows.map((r, i) => `<tr data-i="${i}" class="${t.onRow ? 'click' : ''} ${t.selectable && t.selected.has(r[t.key]) ? 'sel' : ''} ${t.rowClass ? t.rowClass(r) : ''}">${t.selectable ? `<td class="c"><input type="checkbox" data-sel aria-label="Select ${esc(r.name || r[t.key])}" ${t.selected.has(r[t.key]) ? 'checked' : ''}></td>` : ''}${cols.map(c => `<td class="${c.num ? 'n' : ''} ${c.cls || ''}">${c.render ? c.render(r) : esc(r[c.key])}</td>`).join('')}</tr>`).join('')
        : `<tr><td colspan="${cols.length + (t.selectable ? 1 : 0)}" class="o-empty">${esc(t.search ? 'No match for “' + t.search + '”.' : t.empty)}</td></tr>`}</tbody>
      ${t.footer && rows.length ? `<tfoot>${t.footer(rows)}</tfoot>` : ''}</table>`;
  }
  const sortBy = k => { t.sort = t.sort && t.sort.key === k ? { key: k, dir: t.sort.dir === 'asc' ? 'desc' : 'asc' } : { key: k, dir: cols.find(c => c.key === k)?.num ? 'desc' : 'asc' }; render(); };
  container.addEventListener('click', e => {
    const th = e.target.closest('th[data-sort]'); if (th) return sortBy(th.dataset.sort);
    if (e.target.matches('[data-all]')) { const on = e.target.checked; t.shown.forEach(r => on ? t.selected.add(r[t.key]) : t.selected.delete(r[t.key])); render(); t.onSelect?.(t.selected); return; }
    const tr = e.target.closest('tbody tr[data-i]'); if (!tr) return;
    const row = t.shown[+tr.dataset.i];
    if (e.target.matches('[data-sel]')) { e.target.checked ? t.selected.add(row[t.key]) : t.selected.delete(row[t.key]); tr.classList.toggle('sel', e.target.checked); const all = $('[data-all]', container); if (all) all.checked = t.shown.every(r => t.selected.has(r[t.key])); t.onSelect?.(t.selected); return; }
    if (e.target.closest('button,a,input,select,label')) return;       // controls inside a row do their own thing
    t.onRow?.(row, e);
  });
  container.addEventListener('keydown', e => { const th = e.target.closest('th[data-sort]'); if (th && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); sortBy(th.dataset.sort); } });
  render();
  return {
    get selected() { return t.selected; }, get shown() { return t.shown; },
    setRows(rows) { t.rows = rows; const ids = new Set(rows.map(r => r[t.key])); [...t.selected].forEach(k => ids.has(k) || t.selected.delete(k)); render(); },
    setSearch(q) { t.search = q; render(); },
    setFilter(f) { t.filter = f; render(); },
    clearSelection() { t.selected.clear(); render(); t.onSelect?.(t.selected); },
    render,
  };
}

// ================================================================ payroll, finance and proofs (Stream C)
// ---------------------------------------------------------------- words
/* The console's words for the payroll / finance screens live in ../i18n.js (English + Urdu, one key each) so the
   phone and the console share one dictionary. The console is English today (OFFICE_LANG); the Urdu keys are ready
   for when it gets a language switch. t('key', {n: 3}) fills {n}. */
try { await import('/static/i18n.js'); } catch { /* keys fall back to themselves; never blocks the console */ }
export const OFFICE_LANG = 'en';
export const t = (k, vars) => {
  const L = window.MUNSHI_I18N || {}; let s = (L[OFFICE_LANG] || {})[k] ?? (L.en || {})[k] ?? k;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, v) => (v in vars ? String(vars[v]) : m));
  return s;
};

// ---------------------------------------------------------------- dates
export const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
export const monthLabel = p => { const m = /^(\d{4})-(\d{2})$/.exec(p || ''); return m ? `${MONTHS[+m[2] - 1]} ${m[1]}` : String(p || ''); };
export const thisPeriod = () => todayPk().slice(0, 7);
export const shiftPeriod = (p, n) => { const [y, m] = p.split('-').map(Number); const d = new Date(Date.UTC(y, m - 1 + n, 1)); return d.toISOString().slice(0, 7); };
export const monthEnd = p => { const [y, m] = p.split('-').map(Number); return new Date(Date.UTC(y, m, 0)).toISOString().slice(0, 10); };
export const monthStart = p => p + '-01';

// ---------------------------------------------------------------- the shared table shape
/* renderTable(spec, opts) -> html. `spec` is exactly the server's table payload (domain/accounts.table, the same shape
   the chat bubble renders): {title, columns:[{key,label,align,kind,badge?}], rows (a row may carry _em), totals, note}.
   Money is right-aligned with tabular numerals, always two decimals (so a column's decimal points line up), a real
   minus sign, and no "Rs" in every cell -- the table says "Amounts in Rs" once. Badge columns are a word in a
   bordered pill with a glyph (never colour alone). opts: {caption (visible title, default spec.title), rowAttr(row, i)
   -> extra <tr> attributes, cell(col, row) -> html|undefined to override a cell, empty (text), id, compact}. */
const MONEY_FMT = new Intl.NumberFormat('en-PK', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const GOOD_WORDS = new Set(['cleared', 'paid', 'active', 'ok', 'yes', 'approved', 'in use', 'reconciled', 'open']);
export function fmtCell(v, kind) {
  if (v === null || v === undefined || v === '') return '';
  const n = Number(v);
  if (kind === 'money') return isFinite(n) ? (n < 0 ? '−' : '') + MONEY_FMT.format(Math.abs(n)) : esc(v);
  if (kind === 'qty' || kind === 'days') return isFinite(n) ? (n < 0 ? '−' : '') + Math.abs(n).toLocaleString('en-PK', { maximumFractionDigits: 2 }) : esc(v);
  if (kind === 'pct') return isFinite(n) ? (n < 0 ? '−' : '') + Math.abs(n).toLocaleString('en-PK', { maximumFractionDigits: 1 }) + '%' : esc(v);
  if (kind === 'date') { const m = String(v).match(/^(\d{4})-(\d{2})-(\d{2})/); return m ? `${+m[3]} ${MONTHS[+m[2] - 1].slice(0, 3)} ${m[1]}` : esc(v); }
  return esc(v);
}
export const badge = v => {
  if (v === null || v === undefined || v === '' || v === false) return '';
  const w = String(v); const good = GOOD_WORDS.has(w.toLowerCase());
  return `<span class="pill ${good ? 'good' : 'warn'} o-badge"><span aria-hidden="true">${good ? '✓' : '⚑'}</span> ${esc(w)}</span>`;
};
const isNum = c => c.align === 'right' || ['money', 'qty', 'days', 'pct'].includes(c.kind);
export function renderTable(spec, opts = {}) {
  const cols = spec?.columns || [], rows = spec?.rows || [];
  const hasMoney = cols.some(c => c.kind === 'money');
  const cell = (c, r) => {
    const o = opts.cell?.(c, r); if (o !== undefined) return o;
    const v = r[c.key]; if (c.badge) return badge(v);
    // statement lines keep their indentation ("  Fuel" under "Operating expenses"): leading spaces become a padding
    const lead = c.kind === 'text' && typeof v === 'string' ? v.length - v.trimStart().length : 0;
    return lead ? `<span style="padding-inline-start:${Math.min(lead, 8) * 0.6}em">${esc(v.trimStart())}</span>` : fmtCell(v, c.kind);
  };
  const title = opts.caption ?? spec?.title ?? '';
  const head = `<thead><tr>${cols.map((c, i) => `<th scope="col" class="${isNum(c) ? 'n' : ''}${i === 0 ? ' o-first' : ''}">${esc(c.label)}</th>`).join('')}</tr></thead>`;
  const body = rows.length ? rows.map((r, i) => `<tr class="${r._em ? 'em' : ''}" ${opts.rowAttr ? opts.rowAttr(r, i) : ''}>${cols.map((c, j) => j === 0
    ? `<th scope="row" class="o-first ${isNum(c) ? 'n' : ''}">${cell(c, r)}</th>` : `<td class="${isNum(c) ? 'n' : ''}${c.kind === 'money' && Number(r[c.key]) < 0 ? ' neg' : ''}">${cell(c, r)}</td>`).join('')}</tr>`).join('')
    : `<tr><td colspan="${cols.length || 1}" class="o-empty">${esc(opts.empty || t('st.empty'))}</td></tr>`;
  const tot = spec?.totals && rows.length ? `<tfoot><tr>${cols.map((c, j) => j === 0 ? `<th scope="row" class="o-first">${esc(spec.totals[c.key] ?? t('st.total'))}</th>`
    : `<td class="${isNum(c) ? 'n' : ''}${c.kind === 'money' && Number(spec.totals[c.key]) < 0 ? ' neg' : ''}">${c.key in spec.totals && spec.totals[c.key] !== null ? fmtCell(spec.totals[c.key], c.kind) : ''}</td>`).join('')}</tr></tfoot>` : '';
  return `<figure class="o-st${opts.compact ? ' compact' : ''}" ${opts.id ? `id="${esc(opts.id)}"` : ''}>
    ${title || hasMoney ? `<figcaption class="o-st-h">${title ? `<span class="o-st-t">${esc(title)}</span>` : ''}${hasMoney ? `<span class="o-st-u">${esc(t('st.amounts_rs'))}</span>` : ''}</figcaption>` : ''}
    <div class="o-tw free"><table class="o-t o-stt">${title ? `<caption class="sr">${esc(title)}</caption>` : ''}${head}<tbody>${body}</tbody>${tot}</table></div>
    ${spec?.note ? `<p class="o-st-note">${esc(spec.note)}</p>` : ''}</figure>`;
}
/* The skeleton a table shows while it loads: the same frame and roughly the same height, so nothing jumps. */
export const skeleton = (rows = 6, cols = 5) => `<div class="o-skel" aria-hidden="true">${Array.from({ length: rows }, () => `<div class="o-skel-r">${Array.from({ length: cols }, (_, i) => `<i style="flex:${i === 0 ? 3 : 1}"></i>`).join('')}</div>`).join('')}</div><span class="sr" role="status">${esc(t('loading'))}</span>`;

// ---------------------------------------------------------------- tabs
export const tabsNav = (base, tabs, cur, label) => `<nav class="o-tabs" aria-label="${esc(label)}">${tabs.map(([k, l]) => `<a href="#/${base}${k ? '/' + k : ''}" class="${k === cur ? 'active' : ''}" ${k === cur ? 'aria-current="page"' : ''}>${esc(l)}</a>`).join('')}</nav>`;
export const refresh = () => window.dispatchEvent(new HashChangeEvent('hashchange'));

// ---------------------------------------------------------------- confirm dialog
/* confirmDialog({title, body (html), confirmLabel, danger, onConfirm, fields}) -- a centred modal for the decisions
   that change the books: approve payroll, close a month, end someone's employment. It restates the effect in plain
   words, the safe choice (Cancel) has focus first, Escape cancels, focus stays inside while it is open and returns to
   the button that opened it. onConfirm(data) may throw: the message is shown in the dialog. `fields` is extra form
   html (a reason, a date); its values arrive in `data`. */
/* Modals get their own layer above the side panel, so a confirm opened from a panel returns to it on Cancel. */
const modalRoot = () => {
  let m = document.getElementById('oModal');
  if (!m) { m = document.createElement('div'); m.id = 'oModal'; m.className = 'o-drawer o-modal-root'; m.hidden = true; document.body.appendChild(m); }
  return m;
};
export function confirmDialog({ title, body = '', confirmLabel = t('confirm'), danger = false, onConfirm, fields = '' }) {
  const wrap = modalRoot(); const back = document.activeElement; wrap.hidden = false;
  wrap.innerHTML = `<div class="scrim"></div><form class="o-modal${danger ? ' danger' : ''}" role="alertdialog" aria-modal="true" aria-labelledby="cdT" aria-describedby="cdB" novalidate>
    <h2 id="cdT">${esc(title)}</h2><div id="cdB" class="o-modal-b">${body}</div>${fields}
    <p class="formerr" role="alert"></p>
    <div class="o-modal-f"><button type="button" class="btn ghost" data-close>${esc(t('cancel'))}</button><button type="submit" class="btn ${danger ? 'danger solid' : 'primary'}">${esc(confirmLabel)}</button></div></form>`;
  const form = $('form', wrap), err = $('.formerr', wrap);
  const close = () => { wrap.hidden = true; wrap.innerHTML = ''; document.removeEventListener('keydown', onKey, true); back?.focus?.(); };
  const onKey = e => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); close(); return; }
    if (e.key !== 'Tab') return;
    const f = $$('button,input,select,textarea,a[href]', form).filter(x => !x.disabled); if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
  };
  document.addEventListener('keydown', onKey, true);
  $('.scrim', wrap).onclick = close; $('[data-close]', wrap).onclick = close;
  form.onsubmit = async e => {
    e.preventDefault(); if (!form.checkValidity()) { form.reportValidity(); return; }
    const data = {}; for (const [k, v] of new FormData(form).entries()) data[k] = v;
    const btn = $('button[type=submit]', form); btn.disabled = true; err.textContent = '';
    try { const r = await onConfirm?.(data); if (r !== false) close(); } catch (x) { err.textContent = x.message; } finally { btn.disabled = false; }
  };
  setTimeout(() => (fields ? $('input,select,textarea', form) : $('[data-close]', form))?.focus(), 30);
  return { close, el: form };
}

// ---------------------------------------------------------------- a generated PIN, shown once
/* pinOnce({name, phone, pin, text}) -- the only place a generated PIN is ever shown. It lives in this dialog's DOM and
   in this closure, nowhere else: it is never stored (localStorage, sessionStorage), never logged, never sent to any
   endpoint. "Send on WhatsApp" is a wa.me link built here in the browser from the API's ready text; the PIN leaves
   the device only when the owner taps Send in WhatsApp itself. Closing the dialog wipes it; it cannot be shown again. */
export const waNumber = phone => { const d = String(phone || '').replace(/\D/g, ''); return /^03\d{9}$/.test(d) ? '92' + d.slice(1) : /^923\d{9}$/.test(d) ? d : ''; };
export function pinOnce({ name, phone, pin, text }) {
  const wrap = modalRoot(); wrap.hidden = false;
  const wa = `https://wa.me/${waNumber(phone)}?text=${encodeURIComponent(text || '')}`;
  wrap.innerHTML = `<div class="scrim"></div><div class="o-modal o-pinbox" role="alertdialog" aria-modal="true" aria-labelledby="pinT" aria-describedby="pinW">
    <h2 id="pinT">${esc(t('pr.pin_for', { name }))}</h2>
    <p id="pinW" class="o-note warn"><span aria-hidden="true">⚠</span> ${esc(t('pr.pin_once_warn'))}</p>
    <div class="o-pin" aria-label="${esc(t('pr.pin_is'))} ${esc(pin.split('').join(' '))}"><span aria-hidden="true">${esc(pin.slice(0, 3))}</span><span aria-hidden="true">${esc(pin.slice(3))}</span></div>
    <p class="hint" style="margin:0">${esc(t('pr.pin_first_signin'))}${phone ? ` · ${esc(phone)}` : ''}</p>
    <div class="o-modal-f"><button type="button" class="btn" id="pinCopy">${esc(t('pr.copy_pin'))}</button>
      <a class="btn" id="pinWa" href="${esc(wa)}" target="_blank" rel="noopener noreferrer">${esc(t('pr.send_whatsapp'))}</a>
      <button type="button" class="btn primary" id="pinDone">${esc(t('pr.pin_given'))}</button></div></div>`;
  const onKey = e => { if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); ask(); } };
  const close = () => { wrap.hidden = true; wrap.innerHTML = ''; document.removeEventListener('keydown', onKey, true); pin = ''; text = ''; };
  // closing loses the PIN for good: an accidental Escape or scrim click asks first
  const ask = () => { if (confirm(t('pr.pin_close_q'))) close(); };
  document.addEventListener('keydown', onKey, true);
  $('.scrim', wrap).onclick = ask; $('#pinDone').onclick = close;
  $('#pinCopy').onclick = async () => { try { await navigator.clipboard.writeText(pin); toast(t('copied')); } catch { toast(t('pr.copy_failed'), 4000); } };
  setTimeout(() => $('#pinCopy')?.focus(), 30);
}

// ---------------------------------------------------------------- the statutory boundary line
export const boundary = (txt, verified) => `<p class="o-boundary"><b>${esc(t('pr.not_advice'))}</b> ${esc(txt || t('pr.boundary', { verified_on: verified || '—' }))}</p>`;
export const rupees = v => Math.round(Number(v || 0) * 100);           // paisa, for exact client-side arithmetic
export const fromPaisa = p => p / 100;
