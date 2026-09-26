/* Payroll. The owner runs the month as a rail of seven steps -- 1 attendance, 2 adjustments, 3 preview register,
   4 approve, 5 pay, 6 payslips, 7 statutory -- plus staff advances and the statutory rates. A clerk (attendance:write
   without payroll:read) sees exactly one thing here: the attendance grid. No rupee is fetched for a clerk, and none is
   rendered: the grid's data is days, leave, overtime hours and trips.
   Rules the screen restates but never decides (Stream A's engine does): Punjab Labour Code 2026 limits are REFUSALS
   (the server's reason is shown word for word); the per-employee "cash allowed" exemption is the owner's; payroll
   cost is booked on approval (accrual), salaries then stay "owed to staff" until paid.
   REST (plan §9 Stream A): /api/payroll/{period}/{attendance|adjustments|preview|approve|statutory}, /api/payroll/runs/
   {id}[/pay|/reverse], /api/payslips/{id}, /api/staff-advances, /api/statutory-payments, /api/statutory-rates,
   /api/payroll/settings; accounts from /api/accounts (Stream B). */
import { $, $$, api, badge, boundary, can, check, confirmDialog, esc, field, fmtCell, fromPaisa, monthEnd, monthLabel, money, panel, pill, post, refresh,
  renderTable, rupees, select, shiftPeriod, skeleton, t, thisPeriod, toast, todayPk } from './lib.js';

const STEPS = ['attendance', 'adjustments', 'preview', 'approve', 'pay', 'payslips', 'statutory'];
const ADJ_CODES = ['bonus', 'arrears', 'other_earning', 'fine', 'loss_recovery', 'other_deduction'];
const METHODS = ['cash', 'bank', 'jazzcash', 'easypaisa', 'cheque'];
const CASHLESS = ['bank', 'jazzcash', 'easypaisa', 'cheque'];
const KIND_FOR = { cash: 'cash', bank: 'bank', cheque: 'bank', jazzcash: 'wallet', easypaisa: 'wallet' };
const ATT_COLS = [['days_worked', 'pr.a_days', 0.5], ['unpaid_absent', 'pr.a_absent', 0.5], ['annual_leave', 'pr.a_annual', 0.5], ['casual_leave', 'pr.a_casual', 0.5],
  ['sick_leave', 'pr.a_sick', 0.5], ['ot_hours', 'pr.a_ot', 0.25], ['holiday_ot_minutes', 'pr.a_hot', 1], ['restday_ot_minutes', 'pr.a_rot', 1], ['trips', 'pr.a_trips', 1]];
const DAY_KEYS = ['days_worked', 'unpaid_absent', 'annual_leave', 'casual_leave', 'sick_leave'];

export default async function payroll(view, args = []) {
  const owner = can('payroll:read');
  // #/payroll/<YYYY-MM>/<step> | #/payroll/advances | #/payroll/rates
  let [a0 = '', a1 = ''] = args;
  const section = ['advances', 'rates'].includes(a0) ? a0 : 'month';
  const period = /^\d{4}-\d{2}$/.test(a0) ? a0 : thisPeriod();
  const step = owner ? (STEPS.includes(a1) ? a1 : 'attendance') : 'attendance';
  const go = (p, s) => { location.hash = `#/payroll/${p}/${s}`; };

  if (!owner) {                      // the clerk: attendance and nothing else
    view.header(t('pr.attendance'), esc(t('pr.clerk_att_sub', { month: monthLabel(period) })), periodPicker(period));
    wirePicker(view, period, p => go(p, 'attendance'));
    view.page.innerHTML = `<p class="o-note">${esc(t('pr.clerk_att_note'))}</p><div id="step" class="o-stack">${skeleton(6, 9)}</div>`;
    return attendanceStep($('#step'), period);
  }

  const sections = `<nav class="o-tabs" aria-label="${esc(t('pr.payroll'))}">${[['month', `#/payroll/${period}/${step}`, t('pr.tab_month')], ['advances', '#/payroll/advances', t('pr.tab_advances')], ['rates', '#/payroll/rates', t('pr.tab_rates')]]
    .map(([k, h, l]) => `<a href="${h}" class="${k === section ? 'active' : ''}" ${k === section ? 'aria-current="page"' : ''}>${esc(l)}</a>`).join('')}</nav>`;
  if (section === 'advances') { view.header(t('pr.payroll'), esc(t('pr.adv_sub'))); view.page.innerHTML = sections + `<div id="step" class="o-stack">${skeleton(4, 8)}</div>`; return advancesTab($('#step')); }
  if (section === 'rates') { view.header(t('pr.payroll'), esc(t('pr.rates_sub'))); view.page.innerHTML = sections + `<div id="step" class="o-stack">${skeleton(6, 6)}</div>`; return ratesTab($('#step')); }

  view.header(t('pr.payroll'), esc(monthLabel(period)), periodPicker(period));
  wirePicker(view, period, p => go(p, step));
  view.page.innerHTML = sections + `<ol class="o-rail" id="rail" aria-label="${esc(t('pr.steps'))}"></ol><div id="state"></div><div id="step" class="o-stack">${skeleton(7, 10)}</div>`;
  const pv = await api(`/api/payroll/${period}/preview`);
  const run = pv.run?.run_id ? pv.run : null;
  const done = s => run ? STEPS.indexOf(s) < STEPS.indexOf('pay') : false;
  $('#rail').innerHTML = STEPS.map(s => `<li class="${done(s) ? 'done' : ''}"><a href="#/payroll/${period}/${s}" ${s === step ? 'aria-current="step"' : ''} class="${!run && ['pay', 'payslips'].includes(s) ? 'off' : ''}" ${!run && ['pay', 'payslips'].includes(s) ? 'aria-disabled="true" tabindex="-1"' : ''}>${esc(t('pr.s_' + s))}</a></li>`).join('');
  $('#state').innerHTML = run
    ? `<p class="o-note good"><span aria-hidden="true">✓</span> ${esc(t('pr.run_line', { run: run.run_id, by: run.approved_by || '', month: monthLabel(period) }))}</p>`
    : `<p class="o-note">${esc(t('pr.draft_line', { month: monthLabel(period), profile: t('pr.profile_' + (pv.profile || 'plc_2026')) }))}</p>`;
  const box = $('#step');
  const ctx = { period, pv, run, go, box };
  if (step === 'attendance') return attendanceStep(box, period);
  if (step === 'adjustments') return adjustmentsStep(ctx);
  if (step === 'preview') return previewStep(ctx);
  if (step === 'approve') return approveStep(ctx);
  if (step === 'pay') return payStep(ctx);
  if (step === 'payslips') return payslipsStep(ctx);
  return statutoryStep(ctx);
}

// ---------------------------------------------------------------- the month picker
const periodPicker = p => `<div class="o-period" role="group" aria-label="${esc(t('pr.month'))}">
  <button type="button" class="btn" id="pPrev" aria-label="${esc(t('pr.prev_month'))}">‹</button>
  <input class="input" type="month" id="pMonth" value="${esc(p)}" max="${esc(shiftPeriod(thisPeriod(), 1))}" aria-label="${esc(t('pr.month'))}" pattern="\\d{4}-\\d{2}">
  <button type="button" class="btn" id="pNext" aria-label="${esc(t('pr.next_month'))}">›</button></div>`;
function wirePicker(view, p, go) {
  $('#pPrev').onclick = () => go(shiftPeriod(p, -1));
  $('#pNext').onclick = () => go(shiftPeriod(p, 1));
  $('#pMonth').onchange = e => { if (/^\d{4}-\d{2}$/.test(e.target.value)) go(e.target.value); };
}

// ================================================================ 1. attendance (owner and clerk)
async function attendanceStep(box, period) {
  const a = await api(`/api/payroll/${period}/attendance`);
  const rows = a.rows || [], dim = a.days_in_month || 30, locked = !!a.locked;
  const orig = new Map(rows.map(r => [r.employee_id, Object.fromEntries(ATT_COLS.map(([k]) => [k, Number(r[k] || 0)]))]));
  box.innerHTML = `
    ${locked ? `<p class="o-note warn"><span aria-hidden="true">🔒</span> ${esc(t('pr.att_locked', { month: monthLabel(period) }))}</p>` : ''}
    <div class="o-bar">${locked ? '' : `<button class="btn" id="fullMonth" type="button">${esc(t('pr.att_full'))}</button>`}<span class="hint">${esc(t('pr.att_hint', { n: dim }))}</span>
      <div class="grow"></div>${locked ? '' : `<span id="dirty" class="hint" aria-live="polite"></span><button class="btn primary" id="saveAtt" type="button" disabled>${esc(t('pr.att_save'))}</button>`}</div>
    <div class="o-tw free o-att"><table class="o-t" id="att"><caption class="sr">${esc(t('pr.attendance'))} ${esc(monthLabel(period))}</caption>
      <thead><tr><th scope="col">${esc(t('pr.c_no'))}</th><th scope="col">${esc(t('pr.c_name'))}</th><th scope="col">${esc(t('pr.c_basis'))}</th>
      ${ATT_COLS.map(([, l]) => `<th scope="col" class="n">${esc(t(l))}</th>`).join('')}<th scope="col" class="n" title="${esc(t('pr.a_counted_hint'))}">${esc(t('pr.a_counted'))}</th></tr></thead>
      <tbody>${rows.length ? rows.map((r, i) => `<tr data-emp="${esc(r.employee_id)}"><td class="mono">${esc(r.emp_no)}</td><th scope="row" style="text-align:start"><b>${esc(r.name)}</b><br><span class="dim">${esc(r.designation || '')}</span></th>
        <td class="dim">${esc(t('pr.basis_' + (r.basis || 'monthly')))}</td>
        ${ATT_COLS.map(([k, l, st], j) => `<td class="n"><input class="input" type="number" inputmode="decimal" min="0" max="${k.endsWith('hours') ? 200 : k.endsWith('minutes') ? 24000 : k === 'trips' ? 400 : dim}" step="${st}" name="${k}" value="${Number(r[k] || 0)}"
          aria-label="${esc(t(l))}: ${esc(r.name)}" data-r="${i}" data-c="${j}" ${locked ? 'disabled' : ''}></td>`).join('')}
        <td class="n sum" data-sum></td></tr>`).join('') : `<tr><td colspan="${ATT_COLS.length + 4}" class="o-empty">${esc(t('pr.att_none'))}</td></tr>`}</tbody></table></div>
    <p class="hint">${esc(a.note || '')}</p>`;
  const tb = $('#att', box);
  const val = (tr, k) => Number(tr.querySelector(`[name=${k}]`).value || 0);
  const paint = tr => {
    const counted = DAY_KEYS.reduce((s, k) => s + val(tr, k), 0);
    const bad = counted > dim;
    tr.querySelector('[data-sum]').innerHTML = `${fmtCell(counted, 'days')}<span class="dim"> / ${dim}</span>${bad ? ` <span class="crit" role="img" aria-label="${esc(t('pr.att_over', { n: dim }))}">⚠</span>` : ''}`;
    DAY_KEYS.forEach(k => tr.querySelector(`[name=${k}]`).classList.toggle('bad', bad));
    const o = orig.get(tr.dataset.emp);
    ATT_COLS.forEach(([k]) => { const inp = tr.querySelector(`[name=${k}]`); inp.classList.toggle('changed', Number(inp.value || 0) !== o[k]); });
    return bad;
  };
  const changed = () => $$('tbody tr[data-emp]', tb).filter(tr => ATT_COLS.some(([k]) => val(tr, k) !== orig.get(tr.dataset.emp)[k]));
  const repaint = () => {
    const bad = $$('tbody tr[data-emp]', tb).map(paint).some(Boolean); const n = changed().length;
    if (locked) return;
    $('#dirty', box).textContent = n ? t('pr.att_changed', { n }) : '';
    $('#saveAtt', box).disabled = !n || bad;
    $('#saveAtt', box).title = bad ? t('pr.att_over', { n: dim }) : '';
  };
  repaint();
  tb.addEventListener('input', e => { if (e.target.matches('input')) repaint(); });
  // Enter moves down the column, like a paper register
  tb.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || !e.target.matches('input[data-r]')) return;
    e.preventDefault(); const r = +e.target.dataset.r + (e.shiftKey ? -1 : 1), c = e.target.dataset.c;
    tb.querySelector(`input[data-r="${r}"][data-c="${c}"]`)?.select();
  });
  if (locked) return;
  $('#fullMonth', box).onclick = () => {
    $$('tbody tr[data-emp]', tb).forEach(tr => { const away = DAY_KEYS.slice(1).reduce((s, k) => s + val(tr, k), 0); tr.querySelector('[name=days_worked]').value = Math.max(0, dim - away); });
    repaint();
  };
  $('#saveAtt', box).onclick = async () => {
    const rowsOut = changed().map(tr => ({ employee_id: tr.dataset.emp, ...Object.fromEntries(ATT_COLS.map(([k]) => [k, val(tr, k)])) }));
    const b = $('#saveAtt', box); b.disabled = true;
    try { await api(`/api/payroll/${period}/attendance`, { method: 'PUT', body: JSON.stringify({ rows: rowsOut, source: 'register' }) }); toast(t('pr.att_saved', { n: rowsOut.length })); refresh(); }
    catch (x) { b.disabled = false; toast(x.message, 7000); }
  };
}

// ================================================================ 2. adjustments (owner)
async function adjustmentsStep({ period, pv, run, box }) {
  const adj = pv.adjustments || [];
  const emps = (pv.table?.rows || []).map(r => [r.emp_no, r.name]);
  box.innerHTML = `
    <div class="o-bar"><p class="hint" style="margin:0">${esc(t('pr.adj_hint'))}</p><div class="grow"></div>${run ? '' : `<button class="btn primary" id="addAdj" type="button">${esc(t('pr.adj_add'))}</button>`}</div>
    ${run ? `<p class="o-note warn">${esc(t('pr.adj_locked'))}</p>` : ''}
    <div class="o-tw free"><table class="o-t"><caption class="sr">${esc(t('pr.s_adjustments'))}</caption><thead><tr><th scope="col">${esc(t('pr.c_name'))}</th><th scope="col">${esc(t('pr.adj_code'))}</th><th scope="col" class="n">${esc(t('pr.amount'))}</th><th scope="col">${esc(t('pr.note'))}</th><th scope="col"></th></tr></thead>
    <tbody>${adj.length ? adj.map(x => `<tr class="${x.voided ? 'off' : ''}"><th scope="row" style="text-align:start">${esc(x.name || x.employee_id)}</th><td>${esc(t('adj.' + x.code))}${x.voided ? ' ' + pill(t('pr.voided')) : ''}</td>
      <td class="n">${fmtCell(x.amount, 'money')}</td><td class="wrap">${esc(x.note || '')}</td>
      <td class="n">${!x.voided && !run ? `<button type="button" class="btn sm ghost" data-void="${esc(x.adj_id)}">${esc(t('pr.void'))}</button>` : ''}</td></tr>`).join('')
      : `<tr><td colspan="5" class="o-empty">${esc(t('pr.adj_none'))}</td></tr>`}</tbody></table></div>`;
  $$('[data-void]', box).forEach(b => b.onclick = () => confirmDialog({
    title: t('pr.void_q'), body: `<p>${esc(t('pr.void_effect'))}</p>`, confirmLabel: t('pr.void'),
    onConfirm: async () => { await api(`/api/payroll/adjustments/${encodeURIComponent(b.dataset.void)}`, { method: 'DELETE' }); toast(t('pr.voided')); refresh(); },
  }));
  $('#addAdj', box)?.addEventListener('click', async () => {
    let people = emps;
    try { const e = await api('/api/employees?status=active'); people = (e.employees || []).map(x => [x.employee_id, `${x.emp_no} · ${x.name}`]); } catch { /* the register's names are enough */ }
    panel({
      title: t('pr.adj_add'), submitLabel: t('pr.adj_save'),
      body: `${select(t('pr.c_name'), 'employee_id', people)}
        <div class="o-grid2">${select(t('pr.adj_code'), 'code', ADJ_CODES.map(c => [c, t('adj.' + c)]))}${field(t('pr.amount_rs'), 'amount', '', 'required min="0.01" step="any"', 'number')}</div>
        ${field(t('pr.adj_note'), 'note', '', 'required minlength="3" maxlength="200"')}
        <p class="hint" id="adjHelp" style="margin:0"></p>
        ${field(t('pr.adj_ref'), 'ref', '', 'maxlength="40" placeholder="EXP-…"')}
        ${check(t('pr.adj_taxable'), 'taxable', true)}`,
      onOpen: c => { const code = c.el.code, help = $('#adjHelp', c.el); const sync = () => { help.textContent = t('adjh.' + code.value); c.el.ref.closest('label').hidden = code.value !== 'loss_recovery'; }; code.onchange = sync; sync(); },
      onSubmit: async d => {
        await post(`/api/payroll/${period}/adjustments`, { employee_id: d.employee_id, code: d.code, amount: Number(d.amount), note: d.note.trim(), ref: d.ref || null, taxable: d.taxable });
        toast(t('saved')); refresh();
      },
    });
  });
}

// ================================================================ 3. preview register (owner)
function totalsStrip(tt) {
  return `<div class="o-kpis">
    <div class="o-kpi"><div class="k">${esc(t('pr.k_gross'))}</div><div class="v">${money(tt.gross)}</div></div>
    <div class="o-kpi"><div class="k">${esc(t('pr.k_deductions'))}</div><div class="v">${money(tt.deductions)}</div></div>
    <div class="o-kpi"><div class="k">${esc(t('pr.k_net'))}</div><div class="v">${money(tt.net)}</div><div class="s">${esc(t('pr.k_net_s'))}</div></div>
    <div class="o-kpi"><div class="k">${esc(t('pr.k_employer'))}</div><div class="v">${money(tt.employer)}</div><div class="s">${esc(t('pr.k_employer_s'))}</div></div>
    <div class="o-kpi"><div class="k">${esc(t('pr.k_cost'))}</div><div class="v">${money(tt.cost ?? (tt.gross + tt.employer))}</div><div class="s">${esc(t('pr.k_cost_s'))}</div></div></div>`;
}
const warningsList = pv => `${(pv.errors || []).map(r => `<p class="o-refuse" role="alert"><b>${esc(t('pr.refused'))}</b> ${esc(r.text || r)}</p>`).join('')}
  ${(pv.warnings || []).length ? `<ul class="o-warnlist" aria-label="${esc(t('pr.warnings'))}">${pv.warnings.map(w => `<li><span aria-hidden="true">⚠</span><span>${esc(w.text || w)}</span></li>`).join('')}</ul>` : ''}`;

function previewStep({ period, pv, run, go, box }) {
  box.innerHTML = `${totalsStrip(pv.totals || {})}${warningsList(pv)}
    ${renderTable(pv.table, { caption: t('pr.register_for', { month: monthLabel(period) }) })}
    <p class="o-fp">${esc(t('pr.fingerprint'))}: ${esc(pv.fingerprint || '—')}</p>
    ${boundary(pv.boundary, pv.rules_verified_on)}
    <div class="o-bar"><div class="grow"></div>${run ? `<button class="btn primary" id="toPay" type="button">${esc(t('pr.s_pay'))} ›</button>` : `<button class="btn primary" id="toApprove" type="button">${esc(t('pr.to_approve'))} ›</button>`}</div>`;
  $('#toApprove', box)?.addEventListener('click', () => go(period, 'approve'));
  $('#toPay', box)?.addEventListener('click', () => go(period, 'pay'));
}

// ================================================================ 4. approve (owner; accrual on approval)
function approveStep({ period, pv, run, go, box }) {
  const tt = pv.totals || {};
  if (run) {
    box.innerHTML = `${totalsStrip(tt)}<div class="o-card" style="display:grid;gap:8px"><b>${esc(t('pr.approved_by', { run: run.run_id, by: run.approved_by || '' }))}</b>
      <p style="margin:0">${esc(t('pr.approved_effect'))}</p>
      <div class="o-bar"><button class="btn primary" id="toPay" type="button">${esc(t('pr.s_pay'))} ›</button><div class="grow"></div><button class="btn danger" id="rev" type="button">${esc(t('pr.reverse_run'))}</button></div></div>`;
    $('#toPay', box).onclick = () => go(period, 'pay');
    $('#rev', box).onclick = () => confirmDialog({
      title: t('pr.reverse_q', { run: run.run_id }), danger: true, confirmLabel: t('pr.reverse_run'), body: `<p>${esc(t('pr.reverse_effect'))}</p>`,
      fields: field(t('pr.f_reason'), 'reason', '', 'required minlength="3" maxlength="200"'),
      onConfirm: async d => { await post(`/api/payroll/runs/${encodeURIComponent(run.run_id)}/reverse`, { reason: d.reason.trim() }); toast(t('pr.reversed')); refresh(); },
    });
    return;
  }
  const blocked = (pv.errors || []).length > 0;
  const n = pv.headcount ?? (pv.table?.rows || []).length;
  box.innerHTML = `${totalsStrip(tt)}${warningsList(pv)}
    <div class="o-card" style="display:grid;gap:8px"><b>${esc(t('pr.approve_does'))}</b>
      <ul style="margin:0;padding-inline-start:20px;display:grid;gap:4px">
        <li>${esc(t('pr.eff_cost', { cost: money(tt.cost ?? (tt.gross + tt.employer)), date: monthEnd(period) }))}</li>
        <li>${esc(t('pr.eff_owed', { net: money(tt.net) }))}</li>
        <li>${esc(t('pr.eff_slips', { n }))}</li>
        <li>${esc(t('pr.eff_lock', { month: monthLabel(period) }))}</li></ul>
      <p class="o-fp" style="margin:0">${esc(t('pr.fingerprint'))}: ${esc(pv.fingerprint || '—')}</p></div>
    ${boundary(pv.boundary, pv.rules_verified_on)}
    <div class="o-bar"><button class="btn" id="back" type="button">‹ ${esc(t('pr.s_preview'))}</button><div class="grow"></div>
      <button class="btn primary" id="approve" type="button" ${blocked ? `disabled title="${esc(t('pr.fix_refusals'))}"` : ''}>${esc(t('pr.approve_btn', { month: monthLabel(period) }))}</button></div>`;
  $('#back', box).onclick = () => go(period, 'preview');
  $('#approve', box).onclick = () => confirmDialog({
    title: t('pr.approve_q', { month: monthLabel(period) }), confirmLabel: t('pr.approve_yes'),
    body: `<p class="o-big">${money(tt.net)}</p><p>${esc(t('pr.approve_restate', { n, net: money(tt.net), cost: money(tt.cost ?? (tt.gross + tt.employer)), date: monthEnd(period) }))}</p>
      <p class="hint">${esc(t('pr.approve_lock_note'))}</p>`,
    onConfirm: async () => {
      try { const r = await post(`/api/payroll/${period}/approve`, { fingerprint: pv.fingerprint }); toast(t('pr.approved_toast', { run: r.run_id })); go(period, 'pay'); }
      catch (x) { if (x.status === 409) setTimeout(refresh, 2500); throw x; }
    },
  });
}

// ================================================================ 5. pay (owner)
async function payStep({ pv, run, box }) {
  if (!run) { box.innerHTML = `<p class="o-note">${esc(t('pr.pay_needs_run'))}</p>`; return; }
  const [r, acc] = await Promise.all([api(`/api/payroll/runs/${encodeURIComponent(run.run_id)}`), api('/api/accounts').catch(() => ({ accounts: [] }))]);
  const accounts = (acc.accounts || []).filter(a => a.active !== false);
  const plc = (pv.profile || r.run?.profile) === 'plc_2026';
  const slips = r.payslips || [];
  const due = slips.filter(s => rupees(s.balance_due) > 0);
  const optsFor = (m, cur) => accounts.filter(a => a.kind === KIND_FOR[m]).map(a => `<option value="${esc(a.account_id)}" ${a.account_id === cur ? 'selected' : ''}>${esc(a.name)}</option>`).join('') || `<option value="">${esc(t('pr.no_account'))}</option>`;
  box.innerHTML = `
    ${plc ? `<p class="o-note">${esc(t('pr.pay_plc_note'))}</p>` : ''}
    <div class="o-bar">${field(t('pr.paid_on'), 'paid_on', todayPk(), 'id="paidOn" style="width:170px"', 'date')}<div class="grow"></div>
      <button class="btn" id="selCash" type="button">${esc(t('pr.sel_cash'))}</button><button class="btn" id="selAll" type="button">${esc(t('pr.sel_all_due'))}</button>
      <button class="btn primary" id="review" type="button" disabled>${esc(t('pr.review_pay'))}</button></div>
    <div class="o-tw free"><table class="o-t" id="payT"><caption class="sr">${esc(t('pr.s_pay'))}</caption><thead><tr><th class="c"><span class="sr">${esc(t('pr.select'))}</span></th><th>${esc(t('pr.c_name'))}</th>
      <th class="n">${esc(t('pr.c_net'))}</th><th class="n">${esc(t('pr.c_paid'))}</th><th class="n">${esc(t('pr.c_due'))}</th><th class="n">${esc(t('pr.c_pay_now'))}</th><th>${esc(t('pr.c_method'))}</th><th>${esc(t('pr.c_account'))}</th><th>${esc(t('pr.c_ref'))}</th></tr></thead>
      <tbody>${slips.map(s => { const owes = rupees(s.balance_due) > 0; return `<tr data-slip="${esc(s.slip_id)}" class="${owes ? '' : 'off'}">
        <td class="c"><input type="checkbox" data-sel aria-label="${esc(t('pr.pay_x', { name: s.name }))}" ${owes ? '' : 'disabled'}></td>
        <th scope="row" style="text-align:start"><b>${esc(s.name)}</b> <span class="mono">${esc(s.emp_no)}</span>${s.cash_allowed ? ` <span class="pill warn o-badge" title="${esc(t('pr.cash_allowed_hint'))}"><span aria-hidden="true">⚑</span> ${esc(t('pr.cash_allowed_short'))}</span>` : ''}<div class="crit" data-why style="font-weight:600;font-size:12.5px;white-space:normal"></div></th>
        <td class="n">${fmtCell(s.net, 'money')}</td><td class="n">${fmtCell(s.paid, 'money')}</td><td class="n"><b>${fmtCell(s.balance_due, 'money')}</b></td>
        <td class="n"><input class="input num" type="number" name="amount" min="0.01" step="0.01" max="${Number(s.balance_due)}" value="${owes ? Number(s.balance_due) : ''}" style="width:120px;text-align:end" aria-label="${esc(t('pr.c_pay_now'))}: ${esc(s.name)}" ${owes ? '' : 'disabled'}></td>
        <td><select class="input" name="method" aria-label="${esc(t('pr.c_method'))}: ${esc(s.name)}" ${owes ? '' : 'disabled'}>${METHODS.map(m => `<option value="${m}" ${m === s.pay_method ? 'selected' : ''}>${esc(t('m.' + m))}</option>`).join('')}</select></td>
        <td><select class="input" name="account" aria-label="${esc(t('pr.c_account'))}: ${esc(s.name)}" ${owes ? '' : 'disabled'}>${optsFor(s.pay_method, s.pay_account_id)}</select></td>
        <td><input class="input" name="ref" maxlength="40" placeholder="${esc(t('pr.ref_ph'))}" style="width:130px" aria-label="${esc(t('pr.c_ref'))}: ${esc(s.name)}" ${owes ? '' : 'disabled'}></td></tr>`; }).join('')
        || `<tr><td colspan="9" class="o-empty">${esc(t('pr.no_slips'))}</td></tr>`}</tbody>
      <tfoot><tr><td></td><td>${esc(t('st.total'))}</td><td class="n">${fmtCell(slips.reduce((a, s) => a + rupees(s.net), 0) / 100, 'money')}</td><td class="n">${fmtCell(slips.reduce((a, s) => a + rupees(s.paid), 0) / 100, 'money')}</td>
        <td class="n">${fmtCell(slips.reduce((a, s) => a + rupees(s.balance_due), 0) / 100, 'money')}</td><td class="n" id="selTot"></td><td colspan="3"></td></tr></tfoot></table></div>
    ${due.length ? '' : `<p class="o-note good">${esc(t('pr.all_paid'))}</p>`}`;
  const tb = $('#payT', box);
  const bySlip = new Map(slips.map(s => [s.slip_id, s]));
  const rowsSel = () => $$('tbody tr[data-slip]', tb).filter(tr => tr.querySelector('[data-sel]')?.checked);
  const check = tr => {
    const s = bySlip.get(tr.dataset.slip), m = tr.querySelector('[name=method]').value, a = Number(tr.querySelector('[name=amount]').value);
    let why = '';
    if (plc && m === 'cash' && !s.cash_allowed) why = t('pr.why_cash', { name: s.name });
    else if (!(a > 0) || rupees(a) > rupees(s.balance_due)) why = t('pr.why_amount', { due: money(s.balance_due) });
    else if (!tr.querySelector('[name=account]').value) why = t('pr.why_account');
    tr.querySelector('[data-why]').textContent = tr.querySelector('[data-sel]').checked ? why : '';
    return why;
  };
  const repaint = () => {
    const sel = rowsSel(); const bad = sel.map(check).filter(Boolean);
    $$('tbody tr[data-slip]', tb).filter(tr => !sel.includes(tr)).forEach(tr => { const w = tr.querySelector('[data-why]'); if (w) w.textContent = ''; });
    const sum = sel.reduce((s, tr) => s + rupees(tr.querySelector('[name=amount]').value), 0);
    $('#selTot', box).innerHTML = sel.length ? `<b>${fmtCell(fromPaisa(sum), 'money')}</b>` : '';
    $('#review', box).disabled = !sel.length || bad.length > 0;
    $('#review', box).textContent = sel.length ? t('pr.review_pay_n', { n: sel.length, amt: money(fromPaisa(sum)) }) : t('pr.review_pay');
  };
  tb.addEventListener('change', e => {
    const tr = e.target.closest('tr[data-slip]'); if (!tr) return;
    if (e.target.name === 'method') tr.querySelector('[name=account]').innerHTML = optsFor(e.target.value, null);
    if (e.target.name && !e.target.matches('[data-sel]')) tr.querySelector('[data-sel]').checked = true;
    repaint();
  });
  tb.addEventListener('input', repaint);
  $('#selCash', box).onclick = () => { $$('tbody tr[data-slip]', tb).forEach(tr => { const cb = tr.querySelector('[data-sel]'); if (!cb.disabled) cb.checked = tr.querySelector('[name=method]').value === 'cash'; }); repaint(); };
  $('#selAll', box).onclick = () => { $$('[data-sel]:not(:disabled)', tb).forEach(cb => { cb.checked = true; }); repaint(); };
  repaint();
  $('#review', box).onclick = () => {
    const paidOn = $('#paidOn', box).value || todayPk();
    const pays = rowsSel().map(tr => ({ slip_id: tr.dataset.slip, amount: Number(tr.querySelector('[name=amount]').value), method: tr.querySelector('[name=method]').value,
      account_id: tr.querySelector('[name=account]').value, ref: tr.querySelector('[name=ref]').value.trim(), paid_on: paidOn }));
    const byAcc = {};
    pays.forEach(p => { byAcc[p.account_id] = (byAcc[p.account_id] || 0) + rupees(p.amount); });
    const total = pays.reduce((s, p) => s + rupees(p.amount), 0);
    confirmDialog({
      title: t('pr.pay_q', { n: pays.length }), confirmLabel: t('pr.pay_yes', { amt: money(fromPaisa(total)) }),
      body: `<p class="o-big">${money(fromPaisa(total))}</p><p>${esc(t('pr.pay_effect', { date: paidOn }))}</p>
        <div class="o-tw free"><table class="o-t"><thead><tr><th>${esc(t('pr.c_account'))}</th><th class="n">${esc(t('pr.amount'))}</th></tr></thead><tbody>
        ${Object.entries(byAcc).map(([a, p]) => `<tr><td>${esc(accounts.find(x => x.account_id === a)?.name || a)}</td><td class="n">${fmtCell(fromPaisa(p), 'money')}</td></tr>`).join('')}</tbody></table></div>`,
      onConfirm: async () => { await post(`/api/payroll/runs/${encodeURIComponent(run.run_id)}/pay`, { payments: pays }); toast(t('pr.paid_toast', { n: pays.length })); refresh(); },
    });
  };
}

// ================================================================ 6. payslips (owner)
async function payslipsStep({ run, box }) {
  if (!run) { box.innerHTML = `<p class="o-note">${esc(t('pr.pay_needs_run'))}</p>`; return; }
  const r = await api(`/api/payroll/runs/${encodeURIComponent(run.run_id)}`);
  const slips = r.payslips || [];
  box.innerHTML = `<div class="o-tw free"><table class="o-t"><caption class="sr">${esc(t('pr.s_payslips'))}</caption><thead><tr><th>${esc(t('pr.slip_no'))}</th><th>${esc(t('pr.c_name'))}</th><th class="n">${esc(t('pr.c_net'))}</th><th class="n">${esc(t('pr.c_paid'))}</th><th>${esc(t('pr.c_status'))}</th><th></th></tr></thead>
    <tbody>${slips.map(s => `<tr><td class="mono">${esc(s.slip_id)}</td><th scope="row" style="text-align:start"><b>${esc(s.name)}</b> <span class="mono">${esc(s.emp_no)}</span></th><td class="n">${fmtCell(s.net, 'money')}</td><td class="n">${fmtCell(s.paid, 'money')}</td>
      <td>${rupees(s.balance_due) <= 0 ? badge(t('pr.st_paid')) : badge(t('pr.st_owed'))}</td><td class="n"><button type="button" class="btn sm" data-slip="${esc(s.slip_id)}">${esc(t('pr.open_slip'))}</button></td></tr>`).join('')
      || `<tr><td colspan="6" class="o-empty">${esc(t('pr.no_slips'))}</td></tr>`}</tbody></table></div>`;
  $$('[data-slip]', box).forEach(b => b.onclick = () => openSlip(b.dataset.slip, slips.find(s => s.slip_id === b.dataset.slip)));
}
export async function openSlip(slipId, row) {
  const ctl = panel({ title: t('pr.payslip_x', { id: slipId }), wide: true, body: skeleton(8, 3), closeLabel: t('close'),
    footer: `<button type="button" class="btn" id="slipPrint">${esc(t('print'))}</button>` });
  const s = await api(`/api/payslips/${encodeURIComponent(slipId)}`);
  const m = s.meta || {};
  const lb = m.leave_balances || {};
  ctl.body.innerHTML = `<article class="o-slip" aria-label="${esc(t('pr.payslip_x', { id: slipId }))}">
    <header class="o-slip-h"><div><h3>${esc(m.business_name || '')}</h3><span class="dim">${esc(t('pr.payslip_for', { month: m.period_label || monthLabel(m.period) }))}</span></div><div class="mono">${esc(m.slip_no || slipId)}</div></header>
    <div class="o-kv"><b>${esc(t('pr.c_name'))}</b><span>${esc(m.name || row?.name || '')} <span class="mono">${esc(m.emp_no || '')}</span></span><b>${esc(t('pr.f_designation'))}</b><span>${esc(m.designation || '')}</span>
      <b>${esc(t('pr.f_cnic'))}</b><span>${m.cnic_last4 ? '•••••••••' + esc(m.cnic_last4) : '—'}</span><b>${esc(t('pr.f_joined'))}</b><span>${esc(m.joined_on || '')}</span>
      <b>${esc(t('pr.c_basis'))}</b><span>${esc(t('pr.basis_' + (m.pay_basis || 'monthly')))}</span><b>${esc(t('pr.slip_days'))}</b><span>${fmtCell(m.days_worked, 'days')} · ${esc(t('pr.a_absent'))} ${fmtCell(m.unpaid_absent || 0, 'days')}</span>
      <b>${esc(t('pr.c_method'))}</b><span>${esc(t('m.' + (m.pay_method || 'cash')))}${m.cash_exemption ? ` <span class="pill warn o-badge"><span aria-hidden="true">⚑</span> ${esc(t('pr.cash_allowed_short'))}</span>` : ''}</span><b>${esc(t('pr.c_status'))}</b><span>${badge(m.paid_status || '')}</span></div>
    ${renderTable(m.boundary && s.table?.note === m.boundary ? { ...s.table, note: null } : s.table, { caption: '' })}
    <div class="o-kv"><b>${esc(t('pr.leave_bal'))}</b><span>${esc(t('pr.leave_line', { a: lb.annual ?? '—', c: lb.casual ?? '—', s: lb.sick ?? '—' }))}</span>
      <b>${esc(t('pr.ytd'))}</b><span>${esc(t('pr.ytd_line', { taxable: money(m.ytd_taxable), tax: money(m.ytd_tax) }))}</span>
      ${m.employer_eobi ? `<b>${esc(t('pr.employer_eobi'))}</b><span>${money(m.employer_eobi)} <span class="hint">(${esc(t('pr.memo'))})</span></span>` : ''}</div>
    ${boundary(m.boundary, m.rules_verified_on)}</article>`;
  $('#slipPrint').onclick = () => window.print();
}

// ================================================================ 7. statutory (owner)
async function statutoryStep({ period, box }) {
  const kinds = [['eobi', t('pr.eobi')], ['ss', t('pr.ss')], ['income_tax', t('pr.wht')]];
  box.innerHTML = kinds.map(([k]) => `<section id="st-${k}" class="o-stack">${skeleton(4, 6)}</section>`).join('');
  const acc = await api('/api/accounts').catch(() => ({ accounts: [] }));
  let verified = null, bline = null;
  await Promise.all(kinds.map(async ([k, label]) => {
    const sec = $(`#st-${k}`, box);
    try {
      const s = await api(`/api/payroll/${period}/statutory?kind=${k}`);
      bline = bline || s.boundary; verified = verified || s.rules_verified_on;
      const owed = rupees(s.due) - rupees(s.paid);
      sec.innerHTML = `${renderTable(s.table, { caption: `${label} · ${monthLabel(period)}`, empty: t('pr.st_nobody') })}
        <div class="o-bar"><span class="hint">${esc(t('pr.st_due_line', { due: money(s.due), paid: money(s.paid) }))}</span><div class="grow"></div>
        ${owed > 0 ? `<button type="button" class="btn" data-pay="${k}" data-amt="${fromPaisa(owed)}">${esc(t('pr.st_record', { what: label }))}</button>` : ''}</div>`;
    } catch (x) { sec.innerHTML = `<p class="o-note crit">${esc(label)}: ${esc(x.message)}</p>`; }
  }));
  box.insertAdjacentHTML('beforeend', boundary(bline, verified));
  $$('[data-pay]', box).forEach(b => b.onclick = () => panel({
    title: t('pr.st_record', { what: kinds.find(k => k[0] === b.dataset.pay)[1] }), submitLabel: t('pr.st_save'),
    body: `<div class="o-grid2">${field(t('pr.amount_rs'), 'amount', b.dataset.amt, 'required min="0.01" step="any"', 'number')}${field(t('pr.paid_on'), 'paid_on', todayPk(), 'required', 'date')}</div>
      <div class="o-grid2">${select(t('pr.c_method'), 'method', CASHLESS.map(m => [m, t('m.' + m)]))}${select(t('pr.c_account'), 'account_id', (acc.accounts || []).filter(a => a.kind !== 'cash').map(a => [a.account_id, a.name]))}</div>
      ${field(t('pr.challan'), 'challan_ref', '', 'required minlength="3" maxlength="40" placeholder="PSID / CPR / PR-03 no."')}`,
    onSubmit: async d => { await post('/api/statutory-payments', { kind: b.dataset.pay, period, amount: Number(d.amount), method: d.method, account_id: d.account_id, challan_ref: d.challan_ref.trim(), paid_on: d.paid_on }); toast(t('saved')); refresh(); },
  }));
}

// ================================================================ advances (owner)
async function advancesTab(box) {
  const [a, emps, acc] = await Promise.all([api('/api/staff-advances'), api('/api/employees?status=active').catch(() => ({ employees: [] })), api('/api/accounts').catch(() => ({ accounts: [] }))]);
  const lim = a.limits || {};
  const people = (emps.employees || []).map(x => [x.employee_id, `${x.emp_no} · ${x.name}`]);
  box.innerHTML = `
    <div class="o-bar"><button class="btn primary" id="give" type="button">${esc(t('pr.adv_give'))}</button><button class="btn" id="repay" type="button">${esc(t('pr.adv_repay'))}</button><div class="grow"></div></div>
    ${lim.profile === 'plc_2026' ? `<p class="o-note">${esc(t('pr.adv_limits', { max: money(lim.max_advance), pct: lim.max_instalment_pct }))}</p>` : ''}
    ${renderTable(a.table, { empty: t('pr.adv_none'), cell: (c, r) => c.key === 'adv_no' ? `<span class="mono">${esc(r.adv_no)}</span> <button type="button" class="btn sm ghost no-print" data-rev="${esc(r.advance_id || r.adv_no)}">${esc(t('pr.reverse'))}</button>` : undefined })}
    <h3 style="margin:6px 0 0">${esc(t('pr.adv_schedule'))}</h3>
    <div class="o-tw free"><table class="o-t"><thead><tr><th>${esc(t('pr.adv_no'))}</th><th>${esc(t('pr.month'))}</th><th class="n">${esc(t('pr.amount'))}</th><th>${esc(t('pr.c_status'))}</th><th>${esc(t('pr.run'))}</th></tr></thead><tbody>
    ${(a.schedule || []).map(s => `<tr><td class="mono">${esc(s.advance_id)}</td><td>${esc(monthLabel(s.period))}</td><td class="n">${fmtCell(s.amount, 'money')}</td><td>${badge(t('pr.sch_' + s.status))}</td><td class="mono">${esc(s.run_id || '')}</td></tr>`).join('')
      || `<tr><td colspan="5" class="o-empty">${esc(t('pr.adv_none'))}</td></tr>`}</tbody></table></div>`;
  const accOpts = m => (acc.accounts || []).filter(x => x.kind === KIND_FOR[m]).map(x => [x.account_id, x.name]);
  const form = (kind) => panel({
    title: kind === 'repayment' ? t('pr.adv_repay') : t('pr.adv_give'), submitLabel: kind === 'repayment' ? t('pr.adv_repay_save') : t('pr.adv_give_save'),
    body: `${select(t('pr.c_name'), 'employee_id', people)}
      ${kind === 'repayment' ? '' : select(t('pr.adv_kind'), 'kind', [['advance', t('pr.adv_k_advance')], ['loan', t('pr.adv_k_loan')]])}
      <div class="o-grid2">${field(t('pr.amount_rs'), 'amount', '', 'required min="0.01" step="any"', 'number')}${field(t('pr.date'), 'given_on', todayPk(), 'required', 'date')}</div>
      <div class="o-grid2">${select(t('pr.c_method'), 'method', METHODS.map(m => [m, t('m.' + m)]), 'bank')}<label class="f">${esc(t('pr.c_account'))}<select class="input" name="account_id"></select></label></div>
      ${kind === 'repayment' ? '' : `<div class="o-grid2">${field(t('pr.adv_instalment'), 'installment', '', 'min="0" step="any"', 'number')}${field(t('pr.adv_start'), 'start_period', shiftPeriod(thisPeriod(), 0), 'pattern="\\d{4}-\\d{2}"', 'month')}</div>`}
      ${field(t('pr.note'), 'note', '', 'maxlength="200"')}
      ${lim.profile === 'plc_2026' ? `<p class="hint" style="margin:0">${esc(t('pr.adv_limits', { max: money(lim.max_advance), pct: lim.max_instalment_pct }))}</p>` : ''}`,
    onOpen: c => { const m = c.el.method; const sync = () => { c.el.account_id.innerHTML = accOpts(m.value).map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join('') || `<option value="">${esc(t('pr.no_account'))}</option>`; }; m.onchange = sync; sync(); },
    onSubmit: async d => {
      await post('/api/staff-advances', { employee_id: d.employee_id, kind: kind === 'repayment' ? 'repayment' : d.kind, amount: Number(d.amount), given_on: d.given_on, method: d.method,
        account_id: d.account_id || null, installment: Number(d.installment || 0), start_period: d.start_period || null, note: (d.note || '').trim() });
      toast(t('saved')); refresh();
    },
  });
  $('#give', box).onclick = () => form('advance');
  $('#repay', box).onclick = () => form('repayment');
  $$('[data-rev]', box).forEach(b => b.onclick = () => confirmDialog({
    title: t('pr.adv_rev_q', { id: b.dataset.rev }), danger: true, confirmLabel: t('pr.reverse'), body: `<p>${esc(t('pr.adv_rev_effect'))}</p>`,
    fields: field(t('pr.f_reason'), 'reason', '', 'required minlength="3" maxlength="200"'),
    onConfirm: async d => { await post(`/api/staff-advances/${encodeURIComponent(b.dataset.rev)}/reverse`, { reason: d.reason.trim() }); toast(t('pr.reversed')); refresh(); },
  }));
}

// ================================================================ rates + payroll settings (owner)
const rateValue = x => { const n = Number(x.value); if (x.unit === 'Rs' && isFinite(n)) return money(n); if (x.unit === '%' && isFinite(n)) return esc(n + '%');
  return `${esc(x.value)}${x.unit ? ` <span class="hint">${esc(x.unit)}</span>` : ''}`; };
async function ratesTab(box) {
  const [r, s] = await Promise.all([api('/api/statutory-rates'), api('/api/payroll/settings')]);
  const age = d => { const ms = Date.parse(todayPk()) - Date.parse(d); return isFinite(ms) ? Math.round(ms / 864e5) : null; };
  const rates = r.rates || [];
  box.innerHTML = `
    <div class="o-cols"><div class="o-card" style="display:grid;gap:8px"><b>${esc(t('pr.profile'))}</b>
      <p style="margin:0">${esc(t('pr.profile_now', { p: t('pr.profile_' + s.payroll_profile) }))}</p>
      <p class="hint" style="margin:0">${esc(t('pr.profile_hint_' + s.payroll_profile))}</p>
      <div class="o-bar"><button class="btn" id="swap" type="button">${esc(t('pr.profile_switch', { p: t('pr.profile_' + (s.payroll_profile === 'plc_2026' ? 'legacy_1969' : 'plc_2026')) }))}</button></div></div>
    <div class="o-card"><div class="o-kv"><b>${esc(t('pr.f_province'))}</b><span>${esc(t('prov.' + s.payroll_province))}</span><b>${esc(t('pr.eobi_reg'))}</b><span>${s.eobi_registered === '1' ? esc(t('yes')) : esc(t('no'))}</span>
      <b>${esc(t('pr.ss_reg'))}</b><span>${s.ss_registered === '1' ? esc(t('yes')) : esc(t('no'))}</span><b>${esc(t('pr.pay_day'))}</b><span>${esc(s.payroll_pay_day)}</span></div></div></div>
    <div class="o-bar"><h3 style="margin:0">${esc(t('pr.rates'))}</h3><div class="grow"></div><button class="btn" id="addRate" type="button">${esc(t('pr.rate_add'))}</button></div>
    <div class="o-tw free"><table class="o-t"><caption class="sr">${esc(t('pr.rates'))}</caption><thead><tr><th>${esc(t('pr.rate_key'))}</th><th>${esc(t('pr.rate_where'))}</th><th class="n">${esc(t('pr.rate_value'))}</th><th>${esc(t('pr.rate_from'))}</th>
      <th>${esc(t('pr.rate_source'))}</th><th>${esc(t('pr.rate_verified'))}</th><th>${esc(t('pr.rate_grade'))}</th><th>${esc(t('pr.note'))}</th></tr></thead><tbody>
    ${rates.map(x => { const d = age(x.verified_on); return `<tr><th scope="row" style="text-align:start">${esc(t('rk.' + x.key) === 'rk.' + x.key ? x.key : t('rk.' + x.key))}</th><td>${esc(t('prov.' + x.jurisdiction) === 'prov.' + x.jurisdiction ? x.jurisdiction : t('prov.' + x.jurisdiction))}</td>
      <td class="n">${rateValue(x)}</td><td>${fmtCell(x.effective_from, 'date')}</td>
      <td class="wrap">${x.source_url ? `<a class="link" href="${esc(x.source_url)}" target="_blank" rel="noopener noreferrer">${esc(x.source)}</a>` : esc(x.source)}</td>
      <td>${fmtCell(x.verified_on, 'date')}${d !== null ? ` <span class="hint">(${esc(d === 0 ? t('pr.checked_today') : t('pr.days_ago', { n: d }))})</span>` : ''}</td>
      <td>${x.grade === 'U' ? `<span class="pill crit o-badge"><span aria-hidden="true">?</span> ${esc(t('pr.grade_u'))}</span>` : `<span class="pill ${x.grade === 'A' ? 'good' : ''}">${esc(x.grade)}</span>`}</td>
      <td class="wrap">${esc(x.note || '')}</td></tr>`; }).join('') || `<tr><td colspan="8" class="o-empty">${esc(t('pr.rates_none'))}</td></tr>`}</tbody></table></div>
    ${boundary(r.boundary, rates[0]?.verified_on)}`;
  $('#swap', box).onclick = () => {
    const to = s.payroll_profile === 'plc_2026' ? 'legacy_1969' : 'plc_2026';
    confirmDialog({
      title: t('pr.profile_switch_q', { p: t('pr.profile_' + to) }), confirmLabel: t('pr.profile_switch', { p: t('pr.profile_' + to) }), danger: to === 'legacy_1969',
      body: `<p>${esc(t('pr.profile_hint_' + to))}</p><p class="hint">${esc(t('pr.profile_switch_note'))}</p>`,
      onConfirm: async () => { await api('/api/payroll/settings', { method: 'PUT', body: JSON.stringify({ payroll_profile: to }) }); toast(t('saved')); refresh(); },
    });
  };
  $('#addRate', box).onclick = () => panel({
    title: t('pr.rate_add'), submitLabel: t('pr.rate_save'),
    body: `<p class="o-note">${esc(t('pr.rate_add_note'))}</p>
      <div class="o-grid2">${select(t('pr.rate_key'), 'key', [...new Set(rates.map(x => x.key))].map(k => [k, t('rk.' + k) === 'rk.' + k ? k : t('rk.' + k)]))}${select(t('pr.rate_where'), 'jurisdiction', [['pk', t('prov.pk')], ...['punjab', 'sindh', 'kp', 'balochistan', 'ict'].map(p => [p, t('prov.' + p)])])}</div>
      <div class="o-grid2">${field(t('pr.rate_value'), 'value', '', 'required maxlength="400"')}${field(t('pr.rate_from'), 'effective_from', todayPk(), 'required', 'date')}</div>
      <div class="o-grid2">${field(t('pr.rate_source'), 'source', '', 'required minlength="3" maxlength="120"')}${field(t('pr.rate_url'), 'source_url', '', 'maxlength="300" placeholder="https://…"', 'url')}</div>
      <div class="o-grid2">${field(t('pr.rate_verified'), 'verified_on', todayPk(), 'required', 'date')}${select(t('pr.rate_grade'), 'grade', ['A', 'B', 'C', 'D', 'U'].map(g => [g, t('pr.grade_' + g.toLowerCase())]))}</div>
      ${field(t('pr.note'), 'note', '', 'maxlength="200"')}`,
    onSubmit: async d => { await post('/api/statutory-rates', d); toast(t('saved')); refresh(); },
  });
}
