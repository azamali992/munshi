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
export function panel({ title, body, onSubmit = null, submitLabel = 'Save', wide = false, onOpen = null, footer = '' }) {
  const wrap = $('#drawer'); wrap.hidden = false;
  wrap.innerHTML = `<div class="scrim"></div><form class="o-panel${wide ? ' wide' : ''}" novalidate role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="o-panel-h"><h2>${esc(title)}</h2><button type="button" class="x" aria-label="Close">✕</button></div>
    <div class="o-panel-b">${body}<p class="formerr" role="alert"></p></div>
    ${onSubmit || footer ? `<div class="o-panel-f">${footer}<button type="button" class="btn ghost" data-close>Cancel</button>${onSubmit ? `<button class="btn primary" type="submit">${esc(submitLabel)}</button>` : ''}</div>` : ''}</form>`;
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
