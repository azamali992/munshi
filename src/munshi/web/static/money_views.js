/* Phone-app money screens (Stream C): "My payslips" for every role (payroll:self), the owner's Money card on Today
   and its Money screen, the forced "change your PIN" sheet at first sign-in, and the payment-proof composer (attach a
   photo or PDF to a chat message).
   Loaded by views.js with a dynamic import() after views.js has run. It extends the same globals (window.M core
   helpers, window.V screens) and never edits views.js / core.js: it wraps V.today, V.more and V.chat after they
   render, and installs one narrow fetch hook (below) for the two things core.js has no seam for.
   Styles: an injected <style> that consumes only the semantic tokens of styles.css (so dark mode and Urdu/RTL follow).
   Measured 2026-09-26 (WCAG 2.x, light / dark): money card figures ink/surface 17.15 / 14.63; slip meta muted/surface
   7.70 / 7.69; proof chip text ink-2/surface-2 10.95 / 9.67; chip error crit/crit-soft 6.34 / 6.24; the cash-allowed flag
   warn/warn-soft 6.28 / 7.60; a negative balance crit/surface 7.75 / 7.08. Every control here is at least 48 x 48
   (var(--tap)). */
const M = window.M, V = window.V || (window.V = {});
const { $, $$, view, state, t, esc, api, toast, can, role } = M;

// ---------------------------------------------------------------- formatting
const rs = v => { const n = Number(v || 0), frac = Math.round(Math.abs(n) * 100) % 100 !== 0;
  return (n < 0 ? '−' : '') + 'Rs\u00a0' + Math.abs(n).toLocaleString('en-PK', { minimumFractionDigits: frac ? 2 : 0, maximumFractionDigits: 2 }); };
const ltr = s => `<bdi class="ltr" dir="ltr">${esc(s)}</bdi>`;
// business data (names, payslip lines) is never translated: Latin text keeps the Latin UI face inside Urdu, Urdu keeps its own
const txt = s => /[\u0600-\u06FF]/.test(String(s ?? '')) ? `<bdi>${esc(s)}</bdi>` : ltr(s ?? '');
const MONTHS = { en: ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'],
  ur: ['جنوری', 'فروری', 'مارچ', 'اپریل', 'مئی', 'جون', 'جولائی', 'اگست', 'ستمبر', 'اکتوبر', 'نومبر', 'دسمبر'] };
const monthName = p => { const m = /^(\d{4})-(\d{2})/.exec(p || ''); if (!m) return p || ''; return `${(MONTHS[state.lang] || MONTHS.en)[+m[2] - 1]} ${m[1]}`; };
const h1 = (title, sub = '', back = '') => `${back ? `<a class="back" href="#${back}">‹ ${esc(t('back'))}</a>` : ''}<h1>${esc(title)}</h1>${sub ? `<p class="sub">${sub}</p>` : ''}`;
const skel = n => `<div class="list" aria-hidden="true">${Array.from({ length: n }, () => '<div class="item mv-skel"><i></i><i style="width:40%"></i></div>').join('')}</div><span class="sr" role="status">${esc(t('loading'))}</span>`;

// ---------------------------------------------------------------- styles (semantic tokens only)
const css = document.createElement('style'); css.id = 'mv-css';
css.textContent = `
.mv-skel i{display:block;height:12px;border-radius:6px;background:var(--surface-2);width:70%;animation:mv-p 1.2s var(--ease-out) infinite alternate}
@keyframes mv-p{from{opacity:.55}to{opacity:1}}
.mv-slip{display:grid;gap:4px}
.mv-slip .mv-net{font-family:var(--display);font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.mv-kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13.5px}.mv-kv b{color:var(--muted);font-weight:600}
.mv-flag{display:inline-flex;align-items:center;gap:4px;padding:1px 8px;border-radius:999px;border:1px solid currentColor;background:var(--warn-soft);color:var(--warn);font-weight:700;font-size:12px}
.mv-boundary{margin:0;font-size:12.5px;color:var(--ink-2);background:var(--surface-2);border-inline-start:3px solid var(--ink-2);border-radius:8px;padding:8px 12px}
.mv-money{display:grid;gap:8px;margin:10px 0}
.mv-money .row{min-height:var(--tap)}
.mv-acc{display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:var(--tap);border-top:1px solid color-mix(in srgb,var(--line) 35%,transparent);padding-top:6px}
.mv-acc:first-of-type{border-top:0}
.mv-acc .n{font-family:var(--display);font-weight:700;font-size:18px;font-variant-numeric:tabular-nums}
.mv-acc .n.neg{color:var(--crit)}
.mv-acc .k{font-size:12px;color:var(--muted)}
.mv-io{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--muted)}.mv-io b{color:var(--ink);font-variant-numeric:tabular-nums}
.composer.mv-on{flex-wrap:wrap}
.mv-attach{width:var(--tap);height:var(--tap);padding:0;display:grid;place-items:center;font-size:20px;border-radius:11px;flex:0 0 auto}
.mv-chips{flex-basis:100%;order:-1;display:flex;gap:8px;overflow-x:auto;padding:2px 0 4px}
.mv-chip{flex:0 0 auto;display:flex;align-items:center;gap:8px;min-height:var(--tap);max-width:260px;padding:4px 4px 4px 6px;border-radius:12px;background:var(--surface-2);color:var(--ink-2);border:1px solid var(--line);font-size:12.5px}
.mv-chip img,.mv-chip .doc{width:40px;height:40px;border-radius:8px;object-fit:cover;flex:0 0 auto;background:var(--surface);display:grid;place-items:center;font-weight:800;font-size:11px;color:var(--ink-2);border:1px solid var(--line)}
.mv-chip .nm{display:grid;min-width:0}.mv-chip .nm span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mv-chip .st{font-size:11.5px;color:var(--muted)}
.mv-chip.err{background:var(--crit-soft);color:var(--crit);border-color:var(--crit)}.mv-chip.err .st{color:var(--crit);font-weight:700}
.mv-chip .x{min-width:var(--tap);min-height:var(--tap);border:0;background:none;color:inherit;font-size:16px;cursor:pointer;border-radius:10px}
.mv-pick{position:absolute;inset-inline-start:0;bottom:calc(100% + 6px);z-index:6;display:grid;gap:6px;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:8px;box-shadow:var(--shadow-sheet);min-width:220px}
.mv-pick .btn{justify-content:flex-start;text-align:start;display:flex;gap:10px;align-items:center}
.msg.me .mv-thumbs{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.msg.me .mv-thumbs img,.msg.me .mv-thumbs .doc{width:56px;height:56px;border-radius:8px;object-fit:cover;border:2px solid var(--accent-ink);display:grid;place-items:center;font-weight:800;font-size:11px}
.mv-pinwarn{margin:0;padding:10px 12px;border-radius:10px;background:var(--warn-soft);color:var(--warn);font-weight:700;border:1px solid var(--warn)}
@media (prefers-reduced-motion: reduce){.mv-skel i{animation:none}}`;
document.head.appendChild(css);

// ================================================================ My payslips (every role: payroll:self)
// GET /api/me/payslips returns a light list (slip_id, period, month, gross, deductions, net, paid, status): the
// full payslip (meta + table) is a second call, GET /api/payslips/{slip_id} -- resolved to the caller's own
// employee by payroll:self, same as the owner's per-slip lookup.
let slipCache = null;
async function loadSlips() { const r = await api('/api/me/payslips'); slipCache = r.payslips || []; return slipCache; }
V.payslips = async () => {
  view.innerHTML = h1(t('mv.my_payslips'), esc(t('mv.payslips_sub')), 'more') + skel(3);
  let slips;
  try { slips = await loadSlips(); }
  catch (e) { if (e.status === 404 || e.status === 403) { view.innerHTML = h1(t('mv.my_payslips'), '', 'more') + `<div class="empty">${esc(t('mv.no_payslips_role'))}</div>`; return; } throw e; }
  view.innerHTML = h1(t('mv.my_payslips'), esc(t('mv.payslips_sub')), 'more') + (slips.length
    ? `<div class="list">${slips.map(s => `<a class="item tap mv-slip" href="#payslip/${encodeURIComponent(s.slip_id)}">
        <div class="row"><span class="t">${esc(monthName(s.period))}</span>${s.status ? `<span class="pill ${s.status === 'paid' ? 'good' : 'warn'}">${esc(t('mv.st_' + s.status))}</span>` : ''}</div>
        <div class="row"><span class="m">${ltr(s.slip_id)}</span><span class="mv-net">${ltr(rs(s.net))}</span></div></a>`).join('')}</div>`
    : `<div class="empty">${esc(t('mv.no_payslips'))}</div>`);
};
V.payslip = async id => {
  view.innerHTML = h1(t('mv.payslip'), '', 'payslips') + skel(4);
  let s;
  try { s = await api(`/api/payslips/${encodeURIComponent(id)}`); }
  catch (e) { if (e.status === 404) { view.innerHTML = h1(t('mv.payslip'), '', 'payslips') + `<div class="empty">${esc(t('mv.slip_gone'))}</div>`; return; } throw e; }
  const m = s.meta || {}, lb = m.leave_balances || {}, tb = s.table || { columns: [], rows: [] };
  const colLabel = c => { const k = 'mv.col_' + c.key; return t(k) === k ? c.label : t(k); };
  // a money column shows two decimals on every line when any line has paisa, so the figures line up
  const frac = Object.fromEntries(tb.columns.map(c => [c.key, [...tb.rows, tb.totals || {}].some(r => c.kind === 'money' && Math.round(Math.abs(Number(r[c.key] || 0)) * 100) % 100 !== 0)]));
  const cell = (v, c) => v === null || v === undefined || v === '' ? '' : c.kind === 'money'
    ? ltr((Number(v) < 0 ? '\u2212' : '') + Math.abs(Number(v)).toLocaleString('en-PK', { minimumFractionDigits: frac[c.key] ? 2 : 0, maximumFractionDigits: 2 }))
    : c.kind === 'days' || c.kind === 'qty' ? ltr(Number(v).toLocaleString('en-PK')) : txt(v);
  view.innerHTML = h1(monthName(m.period), `${ltr(s.slip_id)} · ${txt(m.business_name || '')}`, 'payslips') + `
    <div class="card stack">
      <div class="row"><span class="t"><b>${esc(t('mv.net_pay'))}</b></span><span class="mv-slip"><span class="mv-net">${ltr(rs(s.net))}</span></span></div>
      <div class="mv-kv"><b>${esc(t('name'))}</b><span>${txt(m.name || '')} ${ltr(m.emp_no || '')}</span><b>${esc(t('mv.designation'))}</b><span>${txt(m.designation || '')}</span>
        <b>${esc(t('mv.days'))}</b><span>${ltr(m.days_worked ?? '—')}${m.unpaid_absent ? ` · ${esc(t('mv.absent'))} ${ltr(m.unpaid_absent)}` : ''}</span>
        <b>${esc(t('method'))}</b><span>${esc(t('k_' + (m.pay_method || 'cash')))}${m.cash_exemption ? ` <span class="mv-flag"><span aria-hidden="true">⚑</span> ${esc(t('mv.cash_allowed'))}</span>` : ''}</span>
        <b>${esc(t('status'))}</b><span>${esc(t('mv.st_' + (m.paid_status || 'unpaid')))}</span></div></div>
    <figure class="dt" style="margin-top:12px"><div class="dt-wrap"><div class="dt-scroll" role="region" aria-label="${esc(t('mv.payslip'))}" tabindex="0"><table>
      <caption class="sr">${esc(t('mv.payslip'))} ${esc(monthName(m.period))}</caption>
      <thead><tr>${tb.columns.map((c, i) => `<th scope="col" class="${c.align === 'right' ? 'n' : 't'}${i === 0 ? ' dt-name' : ''}">${esc(colLabel(c))}</th>`).join('')}</tr></thead>
      <tbody>${tb.rows.map(r => `<tr${r._em ? ' class="em"' : ''}>${tb.columns.map((c, i) => i === 0 ? `<th scope="row" class="t dt-name">${cell(r[c.key], c)}</th>` : `<td class="${c.align === 'right' ? 'n' : 't'}">${cell(r[c.key], c)}</td>`).join('')}</tr>`).join('')}</tbody>
      ${tb.totals ? `<tfoot><tr>${tb.columns.map((c, i) => i === 0 ? `<th scope="row" class="t dt-name">${esc(t('total'))}</th>` : `<td class="n">${c.key in tb.totals ? cell(tb.totals[c.key], c) : ''}</td>`).join('')}</tr></tfoot>` : ''}
    </table></div></div></figure>
    <div class="card mv-kv" style="margin-top:12px"><b>${esc(t('mv.leave_bal'))}</b><span>${esc(t('mv.leave_line').replace('{a}', lb.annual ?? '—').replace('{c}', lb.casual ?? '—').replace('{s}', lb.sick ?? '—'))}</span>
      <b>${esc(t('mv.ytd'))}</b><span>${ltr(rs(m.ytd_taxable))} · ${esc(t('mv.tax'))} ${ltr(rs(m.ytd_tax))}</span></div>
    ${m.boundary || m.rules_verified_on ? (state.lang === 'ur'   // the rule line in the reader's language; the server's English sentence otherwise
      ? `<p class="mv-boundary" style="margin-top:12px">${esc(t('pr.boundary')).replace('{verified_on}', ltr(m.rules_verified_on || '—'))}</p>`
      : `<p class="mv-boundary ltr" style="margin-top:12px" dir="ltr">${esc(m.boundary || t('pr.boundary').replace('{verified_on}', m.rules_verified_on))}</p>`) : ''}`;
};

// ================================================================ the owner's Money card (Today) + Money screen
const KIND = { cash: '◫', bank: '⌂', wallet: '▣' };
function moneyCard(d) {
  const accs = (d.accounts || []).filter(a => a.active !== false);
  const total = d.total ?? accs.reduce((s, a) => s + Number(a.balance || 0), 0);
  const tin = d.today_in ?? (accs.every(a => 'today_in' in a) ? accs.reduce((s, a) => s + Number(a.today_in || 0), 0) : null);
  const tout = d.today_out ?? (accs.every(a => 'today_out' in a) ? accs.reduce((s, a) => s + Number(a.today_out || 0), 0) : null);
  return `<a class="card mv-money tap" href="#money" aria-label="${esc(t('mv.money'))}: ${esc(rs(total))}">
    <div class="row"><b>${esc(t('mv.money'))}</b><span class="mv-acc" style="border:0;padding:0"><span class="n">${ltr(rs(total))}</span></span></div>
    ${accs.map(a => `<div class="mv-acc"><span><span aria-hidden="true">${KIND[a.kind] || ''}</span> ${txt(a.name)}<br><span class="k">${esc(t('mv.kind_' + a.kind))}</span></span><span class="n ${Number(a.balance) < 0 ? 'neg' : ''}">${ltr(rs(a.balance))}</span></div>`).join('')}
    ${tin !== null && tout !== null ? `<div class="mv-io"><span>${esc(t('mv.today_in'))} <b>${ltr(rs(tin))}</b></span><span>${esc(t('mv.today_out'))} <b>${ltr(rs(tout))}</b></span></div>` : ''}</a>`;
}
const origToday = V.today;
if (origToday) V.today = async (...a) => {
  await origToday(...a);
  if (role() !== 'owner' || !can('books:read')) return;
  const grid = $('.grid2', view); if (!grid) return;
  const slot = document.createElement('div'); slot.innerHTML = `<div class="card mv-money" aria-hidden="true"><div class="mv-skel"><i></i><i style="width:50%"></i><i style="width:60%"></i></div></div>`;
  grid.after(slot);
  try { slot.innerHTML = moneyCard(await api('/api/accounts')); }
  catch { slot.remove(); }                        // no accounts API yet (or offline): Today stays as it was
};
V.money = async () => {
  view.innerHTML = h1(t('mv.money'), esc(t('mv.money_sub')), 'today') + skel(3);
  const d = await api('/api/accounts');
  view.innerHTML = h1(t('mv.money'), esc(t('mv.money_sub')), 'today') + moneyCard(d) +
    `<p class="hint">${esc(t('mv.money_more'))} <a class="link hit" href="/office#/accounts">${esc(t('office_console'))} ›</a></p>`;
  $('.mv-money', view)?.removeAttribute('href');
};

// ================================================================ More: add My payslips (every role) and Money (owner)
const origMore = V.more;
if (origMore) V.more = async (...a) => {
  await origMore(...a);
  const list = $('.list.more', view); if (!list) return;
  const item = (href, title, sub) => `<a class="item tap" href="${href}"><span><span class="t">${esc(title)}</span><br><span class="m">${esc(sub)}</span></span><span class="muted">›</span></a>`;
  list.insertAdjacentHTML('afterbegin', (role() === 'owner' && can('books:read') ? item('#money', t('mv.money'), t('mv.money_more_sub')) : '') + (can('payroll:self') ? item('#payslips', t('mv.my_payslips'), t('mv.payslips_more_sub')) : ''));
};

// ================================================================ first sign-in: change the PIN (forced)
/* Stream A sets me.must_change_pin (and answers every other route 403 {"error":"pin_change_required"}) until the
   employee chooses their own PIN. The sheet cannot be dismissed; everything behind it is inert. The PINs typed here
   go only to POST /api/me/pin -- never stored, never logged -- and the form is wiped as soon as the call returns. */
let pinSheetOpen = false;
function forcePinSheet() {
  if (pinSheetOpen || !state.token) return;
  pinSheetOpen = true;
  const wrap = $('#sheet'); wrap.hidden = false;
  const behind = ['.topbar', '#view', '#nav', '#offline', '#attn'].map(s => $(s)).filter(Boolean);
  behind.forEach(el => { el.inert = true; });
  wrap.innerHTML = `<div class="sheet-bg"></div><form class="sheet-panel" id="mvPin" role="dialog" aria-modal="true" aria-labelledby="mvPinT" novalidate>
    <div class="sheet-h"><h2 id="mvPinT">${esc(t('mv.pin_title'))}</h2></div>
    <div class="sheet-b"><p style="margin:0">${esc(t('mv.pin_why'))}</p>
      <label class="f">${esc(t('mv.pin_old'))}<input class="input num" type="password" name="old_pin" inputmode="numeric" autocomplete="current-password" maxlength="6" required></label>
      <label class="f">${esc(t('mv.pin_new'))}<input class="input num" type="password" name="new_pin" inputmode="numeric" autocomplete="new-password" minlength="4" maxlength="6" pattern="\\d{4,6}" required></label>
      <label class="f">${esc(t('mv.pin_again'))}<input class="input num" type="password" name="again" inputmode="numeric" autocomplete="new-password" minlength="4" maxlength="6" required></label>
      <p class="hint" style="margin:0">${esc(t('mv.pin_rules'))}</p>
      <p class="formerr" role="alert" id="mvPinErr"></p></div>
    <div class="sheet-f"><button type="button" class="btn" id="mvPinOut">${esc(t('signout'))}</button><button class="btn primary" type="submit">${esc(t('mv.pin_save'))}</button></div></form>`;
  const f = $('#mvPin'), err = $('#mvPinErr');
  const wipe = () => { f.old_pin.value = ''; f.new_pin.value = ''; f.again.value = ''; };
  const done = () => { pinSheetOpen = false; behind.forEach(el => { el.inert = false; }); wrap.hidden = true; wrap.innerHTML = ''; };
  $('#mvPinOut').onclick = () => { wipe(); done(); M.signOut(); };
  f.onsubmit = async e => {
    e.preventDefault(); err.textContent = '';
    const o = f.old_pin.value, n = f.new_pin.value;
    if (!/^\d{4,6}$/.test(n)) { err.textContent = t('mv.pin_digits'); f.new_pin.focus(); return; }
    if (n !== f.again.value) { err.textContent = t('mv.pin_mismatch'); f.again.focus(); return; }
    if (n === o) { err.textContent = t('mv.pin_same'); f.new_pin.focus(); return; }
    const b = f.querySelector('button[type=submit]'); b.disabled = true;
    // straight to the network, not through core's api(): its 401 handler would sign the person out on a typo
    try {
      const res = await nativeFetch('/api/me/pin', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session': state.token }, body: JSON.stringify({ old_pin: o, new_pin: n }) });
      let body = {}; try { body = await res.json(); } catch { /* empty */ }
      if (res.ok) { wipe(); done(); toast(t('mv.pin_changed'), 5000); M.signOut(true); return; }
      err.textContent = res.status === 401 ? t('mv.pin_old_wrong') : res.status === 423 ? t('mv.pin_locked') : typeof body.detail === 'string' ? body.detail : t('error');
    } catch { err.textContent = t('offline'); }
    finally { b.disabled = false; }
  };
  setTimeout(() => f.old_pin.focus(), 50);
}
const checkPin = () => { if (state.token && state.me?.must_change_pin) forcePinSheet(); };
window.addEventListener('hashchange', () => setTimeout(checkPin, 0));
setTimeout(checkPin, 0);

// ================================================================ the one fetch hook
/* core.js owns the API client and views.js owns the chat POST, and neither has a seam for (a) the attachment ids a
   chat message carries or (b) the must-change-PIN answer. So: one wrapper around window.fetch that
   - adds `attachment_ids` to the NEXT POST /api/chat body, only while a proof is armed by the composer below;
   - watches /api/* answers for 403 pin_change_required and opens the forced PIN sheet.
   It never reads, stores or logs any other request or response. */
let armedIds = null;
const nativeFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  const url = typeof input === 'string' ? input : input?.url || '';
  if (armedIds && url === '/api/chat' && init && init.method === 'POST' && typeof init.body === 'string') {
    try { const b = JSON.parse(init.body); b.attachment_ids = armedIds; init = Object.assign({}, init, { body: JSON.stringify(b) }); } catch { /* not ours */ }
    armedIds = null;
  }
  const p = nativeFetch(input, init);
  if (!url.startsWith('/api/')) return p;
  return p.then(res => {
    if (res.status === 403) res.clone().json().then(b => { if (b && (b.error === 'pin_change_required' || b.detail === 'pin_change_required' || b.detail?.error === 'pin_change_required')) forcePinSheet(); }).catch(() => { });
    return res;
  });
};

// ================================================================ payment proof composer
const MAX_BYTES = 5 * 1024 * 1024, MAX_SIDE = 1600, OK_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'application/pdf'];
const kb = n => n >= 1048576 ? (Math.ceil(n / 104857.6) / 10).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';   // rounded UP: 5 MB + 10 bytes reads 5.1 MB
// icons as inline SVG in currentColor (an emoji renders differently on every phone, and not at all on some)
const svg = d => `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${d}</svg>`;
const ICON = {
  clip: svg('<path d="M21 11.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l8.6-8.6a3.7 3.7 0 0 1 5.2 5.2l-8.6 8.6a1.8 1.8 0 0 1-2.6-2.6l7.9-7.9"/>'),
  cam: svg('<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>'),
  pic: svg('<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 17l-5-5-9 8"/>'),
};
async function decode(file) {
  if (window.createImageBitmap) { try { return await createImageBitmap(file, { imageOrientation: 'from-image' }); } catch { /* fall through */ } }
  const url = await new Promise((ok, no) => { const r = new FileReader(); r.onload = () => ok(r.result); r.onerror = () => no(r.error); r.readAsDataURL(file); });   // data: is allowed by the CSP; blob: is not
  return await new Promise((ok, no) => { const i = new Image(); i.onload = () => ok(i); i.onerror = () => no(new Error('decode')); i.src = url; });
}
/* Resize to at most 1600 px on the long side and re-encode as JPEG. Re-encoding also drops the photo's EXIF (GPS). */
async function prepare(file) {
  if (file.type === 'application/pdf') {
    if (file.size > MAX_BYTES) throw new Error(t('mv.too_big').replace('{size}', kb(file.size)));
    return { blob: file, name: file.name || 'proof.pdf', thumb: null, pdf: true };
  }
  if (!file.type.startsWith('image/')) throw new Error(t('mv.bad_type'));
  let img;
  try { img = await decode(file); } catch { throw new Error(t('mv.bad_image')); }
  const w0 = img.width, h0 = img.height, k = Math.min(1, MAX_SIDE / Math.max(w0, h0));
  const c = document.createElement('canvas'); c.width = Math.max(1, Math.round(w0 * k)); c.height = Math.max(1, Math.round(h0 * k));
  const g = c.getContext('2d'); g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height); g.drawImage(img, 0, 0, c.width, c.height);
  let blob = null;
  for (const q of [0.82, 0.7, 0.55]) { blob = await new Promise(ok => c.toBlob(ok, 'image/jpeg', q)); if (blob && blob.size <= MAX_BYTES) break; }
  if (!blob) throw new Error(t('mv.bad_image'));
  if (blob.size > MAX_BYTES) throw new Error(t('mv.too_big').replace('{size}', kb(blob.size)));
  const tc = document.createElement('canvas'), tk = 96 / Math.max(c.width, c.height); tc.width = Math.round(c.width * tk); tc.height = Math.round(c.height * tk);
  tc.getContext('2d').drawImage(c, 0, 0, tc.width, tc.height);
  img.close?.();
  return { blob, name: (file.name || 'proof').replace(/\.[^.]+$/, '') + '.jpg', thumb: tc.toDataURL('image/jpeg', 0.7), pdf: false };
}

let outsidePick = null;                               // the open attach menu closes on a tap anywhere else (one listener, not one per visit)
document.addEventListener('click', e => outsidePick?.(e));
const origChat = V.chat;
if (origChat) V.chat = async (...a) => { await origChat(...a); proofComposer(); };
function proofComposer() {
  const form = $('#composer'); if (!form || form.dataset.mv) return;
  form.dataset.mv = '1'; form.classList.add('mv-on');
  const items = [];                                    // {id?, name, size, thumb, pdf, status: 'up'|'ok'|'err', error, file}
  const chips = document.createElement('div'); chips.className = 'mv-chips'; chips.id = 'mvChips'; chips.setAttribute('aria-live', 'polite'); chips.hidden = true;
  const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'btn mv-attach'; btn.setAttribute('aria-haspopup', 'menu'); btn.setAttribute('aria-expanded', 'false');
  btn.setAttribute('aria-label', t('mv.attach')); btn.title = t('mv.attach'); btn.innerHTML = ICON.clip;
  const cam = Object.assign(document.createElement('input'), { type: 'file', accept: 'image/*', hidden: true }); cam.setAttribute('capture', 'environment');
  const gal = Object.assign(document.createElement('input'), { type: 'file', accept: 'image/jpeg,image/png,image/webp,image/*,application/pdf', hidden: true, multiple: true });
  cam.setAttribute('aria-hidden', 'true'); gal.setAttribute('aria-hidden', 'true'); cam.tabIndex = -1; gal.tabIndex = -1;
  form.prepend(chips); $('#txt', form).before(btn); form.append(cam, gal);

  const pick = document.createElement('div'); pick.className = 'mv-pick'; pick.hidden = true; pick.setAttribute('role', 'menu');
  pick.innerHTML = `<button type="button" class="btn" role="menuitem" data-src="cam">${ICON.cam} ${esc(t('mv.take_photo'))}</button><button type="button" class="btn" role="menuitem" data-src="gal">${ICON.pic} ${esc(t('mv.from_gallery'))}</button>`;
  form.append(pick);
  const closePick = () => { pick.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  btn.onclick = () => { const open = pick.hidden; pick.hidden = !open; btn.setAttribute('aria-expanded', String(open)); if (open) pick.querySelector('button').focus(); };
  pick.onclick = e => { const b = e.target.closest('[data-src]'); if (!b) return; closePick(); (b.dataset.src === 'cam' ? cam : gal).click(); };
  pick.addEventListener('keydown', e => { if (e.key === 'Escape') { closePick(); btn.focus(); } });
  outsidePick = e => { if (!pick.hidden && !pick.contains(e.target) && !btn.contains(e.target)) closePick(); };

  const paint = () => {
    chips.hidden = !items.length;
    chips.innerHTML = items.map((it, i) => `<div class="mv-chip ${it.status === 'err' ? 'err' : ''}" role="group" aria-label="${esc(it.name)}">
      ${it.thumb ? `<img src="${it.thumb}" alt="">` : `<span class="doc" aria-hidden="true">${it.pdf ? 'PDF' : '…'}</span>`}
      <span class="nm"><span dir="ltr">${esc(it.name)}</span><span class="st">${it.status === 'up' ? esc(t('mv.uploading')) : it.status === 'ok' ? `${esc(t('mv.attached'))} · ${ltr(kb(it.size))}` : esc(it.error || t('error'))}</span></span>
      ${it.status === 'err' && it.file ? `<button type="button" class="x" data-retry="${i}" aria-label="${esc(t('try_again'))}: ${esc(it.name)}">↻</button>` : ''}
      <button type="button" class="x" data-rm="${i}" aria-label="${esc(t('mv.remove'))}: ${esc(it.name)}">✕</button></div>`).join('');
  };
  chips.onclick = e => {
    const rm = e.target.closest('[data-rm]'), rt = e.target.closest('[data-retry]');
    if (rm) { items.splice(+rm.dataset.rm, 1); paint(); $('#txt', form).focus(); }
    if (rt) { const it = items[+rt.dataset.retry]; upload(it, it.file); }
  };
  const upload = async (it, file) => {
    it.status = 'up'; it.error = ''; paint();
    try {
      const p = await prepare(file);
      Object.assign(it, { name: p.name, thumb: p.thumb, pdf: p.pdf, size: p.blob.size });
      paint();
      const fd = new FormData(); fd.append('file', p.blob, p.name);
      const r = await api('/api/attachments', { method: 'POST', body: fd });
      Object.assign(it, { id: r.id, size: r.size || p.blob.size, status: 'ok', file: null });
    } catch (x) {
      it.status = 'err'; it.error = x.status === 413 ? t('mv.too_big').replace('{size}', kb(it.size || file.size)) : x.status === 429 ? t('mv.slow_down') : x.message;
      it.file = x.status === 413 || /MB|type|image/i.test(x.message) ? null : file;   // a retry cannot fix a too-big or unreadable file
    }
    paint();
  };
  const add = files => {
    for (const f of files) {
      if (items.length >= 4) { toast(t('mv.max_four'), 4000); break; }
      if (!OK_TYPES.includes(f.type) && !f.type.startsWith('image/')) { toast(t('mv.bad_type'), 5000); continue; }
      if (f.type === 'application/pdf' && f.size > MAX_BYTES) { toast(t('mv.too_big').replace('{size}', kb(f.size)), 6000); continue; }
      const it = { name: f.name || 'proof', size: f.size, thumb: null, pdf: f.type === 'application/pdf', status: 'up', file: f };
      items.push(it); upload(it, f);
    }
  };
  cam.onchange = () => { add([...cam.files]); cam.value = ''; };
  gal.onchange = () => { add([...gal.files]); gal.value = ''; };

  // the send: wait for uploads, then arm the ids for exactly this POST /api/chat and hand over to views.js
  const send = form.onsubmit;
  form.onsubmit = e => {
    e.preventDefault();
    if (!items.length) return send.call(form, e);
    if (items.some(x => x.status === 'up')) { toast(t('mv.wait_upload'), 3500); return; }
    if (items.some(x => x.status === 'err')) { toast(t('mv.fix_failed'), 4500); return; }
    if (state.chatBusy) return;
    const input = $('#txt', form);
    if (!input.value.trim()) input.value = t('mv.proof_text');
    const sent = items.splice(0);
    armedIds = sent.map(x => x.id);
    const r = send.call(form, e);                     // synchronously adds the "me" bubble, then POSTs
    const mine = $$('#msgs .msg.me').pop();
    if (mine) mine.insertAdjacentHTML('beforeend', `<span class="mv-thumbs">${sent.map(x => x.thumb ? `<img src="${x.thumb}" alt="${esc(t('mv.proof_alt'))}">` : `<span class="doc" aria-label="${esc(x.name)}">PDF</span>`).join('')}</span>`);
    paint();
    return r;
  };
}

// ================================================================ first paint
/* This module arrives after core.js has already rendered the first screen (it is a dynamic import). If that screen
   is one this module extends, paint it again; if the page was opened on a screen that only exists here (#payslips,
   #money) core.js has already sent it home -- send it back. */
const OURS = ['payslips', 'payslip', 'money'], WRAPPED = ['today', 'more', 'chat', ...OURS];
const opened = ((performance.getEntriesByType?.('navigation') || [])[0]?.name || '').split('#')[1] || '';
if (state.token && OURS.includes(opened.split('/')[0]) && location.hash.slice(1) !== opened) location.hash = '#' + opened;
else if (state.token && WRAPPED.includes((location.hash.slice(1) || '').split('/')[0] || M.home?.())) M.render();

export default V;
