/* Finance (owner: finance:read / finance:write). Statements read from the derived ledger (plan §2): profit and loss
   (with the prior period beside it, and the reconciliation to operating profit), balance sheet (its check row must be
   0.00), cash flow, trial balance, KPIs and margins; the fixed-asset register and depreciation, loans, capital and
   drawings, a balanced journal entry, and the opening-balances wizard. Every write previews what it does first.
   Every statement is the server's table payload drawn by lib.renderTable -- this screen computes no figure of its own
   except the live debit = credit check of a journal being typed.
   REST (plan §9 Stream B): GET /api/finance/{pnl|balance-sheet|cash-flow|trial-balance|kpis|margins|assets|loans},
   GET /api/finance/general-journal.csv, POST /api/finance/{capital|drawings|loans|loans/{id}/repay|assets|
   assets/{id}/dispose|depreciation/run|journal|journal/{id}/reverse|opening-balances}. */
import { $, $$, api, can, confirmDialog, download, esc, field, fmtCell, fromPaisa, monthEnd, money, panel, post, refresh, renderTable, rupees, select, shiftPeriod, skeleton, t, tabsNav, thisPeriod, toast, todayPk } from './lib.js';

const TABS = [['', 'fi.t_pnl'], ['balance-sheet', 'fi.t_bs'], ['cash-flow', 'fi.t_cf'], ['trial-balance', 'fi.t_tb'], ['kpis', 'fi.t_kpis'], ['margins', 'fi.t_margins'],
  ['assets', 'fi.t_assets'], ['loans', 'fi.t_loans'], ['journal', 'fi.t_journal'], ['opening', 'fi.t_opening']];

export default async function finance(view, [tab = ''] = []) {
  const write = can('finance:write');
  const cur = TABS.some(x => x[0] === tab) ? tab : '';
  view.header(t('fi.finance'), esc(t('fi.finance_sub')), `<button class="btn" id="gj">${esc(t('fi.export_gj'))}</button>`);
  view.page.innerHTML = tabsNav('finance', TABS.map(([k, l]) => [k, t(l)]), cur, t('fi.finance')) + `<div id="fbox" style="display:grid;gap:12px">${skeleton(10, 5)}</div>`;
  $('#gj').onclick = () => { const p = thisPeriod(); download(`/api/finance/general-journal.csv?start=${p}-01&end=${monthEnd(p)}`, `general-journal-${p}.csv`); };
  const box = $('#fbox');
  const accounts = () => api('/api/accounts').then(r => (r.accounts || []).filter(a => a.active !== false)).catch(() => []);
  const P = { box, write, accounts };
  if (cur === '') return statement(P, 'pnl', 'range', true);
  if (cur === 'balance-sheet') return statement(P, 'balance-sheet', 'asof');
  if (cur === 'cash-flow') return statement(P, 'cash-flow', 'range');
  if (cur === 'trial-balance') return statement(P, 'trial-balance', 'asof');
  if (cur === 'kpis') return statement(P, 'kpis', 'asof');
  if (cur === 'margins') return marginsTab(P);
  if (cur === 'assets') return assetsTab(P);
  if (cur === 'loans') return loansTab(P);
  if (cur === 'journal') return journalTab(P);
  return openingTab(P);
}

// ---------------------------------------------------------------- statements
const rangeBar = (s, e, compare) => `<div class="o-bar no-print">
  <label class="f" style="grid-auto-flow:column;align-items:center">${esc(t('fi.from'))}<input class="input" type="date" id="fs" value="${s}"></label>
  <label class="f" style="grid-auto-flow:column;align-items:center">${esc(t('fi.to'))}<input class="input" type="date" id="fe" value="${e}"></label>
  <div class="chips" style="padding:0" role="group" aria-label="${esc(t('fi.quick'))}"><button type="button" class="chip" data-q="0">${esc(t('fi.this_month'))}</button><button type="button" class="chip" data-q="-1">${esc(t('fi.last_month'))}</button></div>
  ${compare ? `<label class="chk"><input type="checkbox" id="fc" checked> ${esc(t('fi.compare'))}</label>` : ''}
  <button class="btn" id="fgo" type="button">${esc(t('fi.show'))}</button><div class="grow"></div><button class="btn" type="button" data-print>${esc(t('print'))}</button></div>`;
const asofBar = d => `<div class="o-bar no-print"><label class="f" style="grid-auto-flow:column;align-items:center">${esc(t('fi.as_of_l'))}<input class="input" type="date" id="fa" value="${d}"></label>
  <button class="btn" id="fgo" type="button">${esc(t('fi.show'))}</button><div class="grow"></div><button class="btn" type="button" data-print>${esc(t('print'))}</button></div>`;

async function statement({ box }, what, mode, compare = false) {
  const p = thisPeriod();
  box.innerHTML = (mode === 'range' ? rangeBar(p + '-01', todayPk(), compare) : asofBar(todayPk())) + `<div id="stbody" style="display:grid;gap:14px">${skeleton(12, 5)}</div>`;
  const load = async () => {
    const q = mode === 'range' ? `start=${$('#fs').value}&end=${$('#fe').value}${compare ? `&compare=${$('#fc').checked ? 1 : 0}` : ''}` : `as_of=${$('#fa').value}`;
    const r = await api(`/api/finance/${what}?${q}`);
    const tables = r.tables?.length ? r.tables : [r.table];
    const alarms = (r.alarms || []).map(a => `<p class="o-refuse" role="alert"><b>${esc(t('fi.alarm'))}</b> ${esc(a.text || a)}</p>`).join('');
    const check = what === 'balance-sheet' && r.difference !== undefined ? `<p class="o-note ${rupees(r.difference) === 0 ? 'good' : 'crit'}">${esc(rupees(r.difference) === 0 ? t('fi.bs_balances') : t('fi.bs_off', { amt: money(r.difference) }))}</p>`
      : what === 'trial-balance' && r.balanced !== undefined ? `<p class="o-note ${r.balanced ? 'good' : 'crit'}">${esc(r.balanced ? t('fi.tb_balances') : t('fi.tb_off'))}</p>` : '';
    $('#stbody').innerHTML = alarms + check + tables.map((tb, i) => renderTable(tb, { id: `st${i}` })).join('');
  };
  $('#fgo').onclick = () => load().catch(x => toast(x.message, 6000));
  $('[data-print]', box).onclick = () => window.print();
  $$('[data-q]', box).forEach(b => b.onclick = () => { const m = shiftPeriod(p, +b.dataset.q); $('#fs').value = m + '-01'; $('#fe').value = +b.dataset.q ? monthEnd(m) : todayPk(); load().catch(x => toast(x.message, 6000)); });
  await load();
}

async function marginsTab({ box }) {
  const p = thisPeriod(); let by = 'product';
  box.innerHTML = `<div class="o-bar"><div class="chips" style="padding:0" role="group" aria-label="${esc(t('fi.by'))}">${['product', 'customer', 'route'].map(k => `<button type="button" class="chip" data-by="${k}" aria-pressed="${k === by}">${esc(t('fi.by_' + k))}</button>`).join('')}</div>
    <label class="f" style="grid-auto-flow:column;align-items:center">${esc(t('fi.from'))}<input class="input" type="date" id="fs" value="${p}-01"></label>
    <label class="f" style="grid-auto-flow:column;align-items:center">${esc(t('fi.to'))}<input class="input" type="date" id="fe" value="${todayPk()}"></label><button class="btn" id="fgo" type="button">${esc(t('fi.show'))}</button></div><div id="mb">${skeleton(8, 6)}</div>`;
  const load = async () => { const r = await api(`/api/finance/margins?by=${by}&start=${$('#fs').value}&end=${$('#fe').value}`); $('#mb').innerHTML = renderTable(r.table); };
  $$('[data-by]', box).forEach(b => b.onclick = () => { by = b.dataset.by; $$('[data-by]', box).forEach(x => x.setAttribute('aria-pressed', String(x === b))); load().catch(x => toast(x.message, 6000)); });
  $('#fgo').onclick = () => load().catch(x => toast(x.message, 6000));
  await load();
}

// ---------------------------------------------------------------- fixed assets and depreciation
async function assetsTab({ box, write, accounts }) {
  const r = await api('/api/finance/assets');
  box.innerHTML = `<div class="o-bar">${write ? `<button class="btn primary" id="addA" type="button">${esc(t('fi.asset_add'))}</button><button class="btn" id="dep" type="button">${esc(t('fi.dep_run'))}</button>` : ''}
    <span class="hint">${r.depreciated_through ? esc(t('fi.dep_through', { m: r.depreciated_through })) : ''}</span></div>
    ${renderTable(r.table, { empty: t('fi.assets_none'), cell: (c, row) => c.key === 'asset' && write && row.status !== 'disposed' ? `${esc(row.asset)} <button type="button" class="btn sm ghost no-print" data-disp="${esc(row.asset_id)}">${esc(t('fi.dispose'))}</button>` : undefined })}`;
  if (!write) return;
  const accs = await accounts();
  $('#addA').onclick = () => panel({
    title: t('fi.asset_add'), submitLabel: t('fi.asset_save'),
    body: `<div class="o-grid2">${field(t('fi.asset_name'), 'name', '', 'required minlength="2" maxlength="80" placeholder="Hyundai Shehzore LES-2231"')}${select(t('fi.asset_cat'), 'category', ['vehicle', 'building', 'land', 'furniture', 'equipment', 'computer', 'other'].map(k => [k, t('fa.' + k)]))}</div>
      <div class="o-grid3">${field(t('fi.cost_rs'), 'cost', '', 'required min="0.01" step="0.01"', 'number')}${field(t('fi.salvage_rs'), 'salvage', '0', 'min="0" step="0.01"', 'number')}${field(t('fi.life'), 'life_months', '60', 'required min="0" step="1"', 'number')}</div>
      <div class="o-grid3">${field(t('fi.acquired'), 'acquired_on', todayPk(), 'required', 'date')}${select(t('fi.funded_by'), 'funded_by', [['paid', t('fi.fund_paid')], ['payable', t('fi.fund_payable')], ['opening', t('fi.fund_opening')]])}${select(t('fi.pay_from'), 'account_id', accs.map(a => [a.account_id, a.name]))}</div>
      <p id="aprev" class="o-note" aria-live="polite"></p>`,
    onOpen: c => { const f = c.el; const paint = () => { const cost = rupees(f.cost.value), sal = rupees(f.salvage.value), n = Number(f.life_months.value); $('#aprev', f).textContent = cost && n > 0 ? t('fi.dep_preview', { amt: money(fromPaisa(Math.floor((cost - sal) / n))), n }) : n === 0 && cost ? t('fi.dep_none') : ''; f.account_id.closest('label').hidden = f.funded_by.value !== 'paid'; }; f.addEventListener('input', paint); f.addEventListener('change', paint); paint(); },
    onSubmit: async d => {
      const acc = accs.find(a => a.account_id === d.account_id);
      await post('/api/finance/assets', { name: d.name.trim(), category: d.category, cost: Number(d.cost), salvage: Number(d.salvage || 0), life_months: Number(d.life_months), acquired_on: d.acquired_on,
        funded_by: d.funded_by, method: d.funded_by === 'paid' ? (acc?.kind === 'cash' ? 'cash' : acc?.kind === 'wallet' ? (acc.provider || 'jazzcash') : 'bank') : null, account_id: d.funded_by === 'paid' ? d.account_id : null });
      toast(t('saved')); refresh();
    },
  });
  $('#dep').onclick = () => { const through = shiftPeriod(thisPeriod(), -1); confirmDialog({
    title: t('fi.dep_q', { m: through }), confirmLabel: t('fi.dep_run'), body: `<p>${esc(t('fi.dep_effect', { m: through }))}</p>`,
    fields: field(t('fi.dep_month'), 'through_period', through, 'required pattern="\\d{4}-\\d{2}"', 'month'),
    onConfirm: async d => { const r2 = await post('/api/finance/depreciation/run', { through_period: d.through_period }); toast(t('fi.posted') + (r2.je_id ? ` · ${r2.je_id}` : '')); refresh(); },
  }); };
  $$('[data-disp]', box).forEach(b => b.onclick = () => panel({
    title: t('fi.dispose_x', { id: b.dataset.disp }), submitLabel: t('fi.dispose'),
    body: `<div class="o-grid2">${field(t('fi.date'), 'on_date', todayPk(), 'required', 'date')}${field(t('fi.proceeds'), 'proceeds', '0', 'min="0" step="0.01"', 'number')}</div>${select(t('fi.pay_into'), 'account_id', accs.map(a => [a.account_id, a.name]))}`,
    onSubmit: async d => { const acc = accs.find(a => a.account_id === d.account_id); await post(`/api/finance/assets/${encodeURIComponent(b.dataset.disp)}/dispose`, { on_date: d.on_date, proceeds: Number(d.proceeds || 0), method: acc?.kind === 'cash' ? 'cash' : 'bank', account_id: d.account_id }); toast(t('saved')); refresh(); },
  }));
}

// ---------------------------------------------------------------- loans
async function loansTab({ box, write, accounts }) {
  const r = await api('/api/finance/loans');
  box.innerHTML = `<div class="o-bar">${write ? `<button class="btn primary" id="addL" type="button">${esc(t('fi.loan_add'))}</button>` : ''}</div>
    ${renderTable(r.table, { empty: t('fi.loans_none'), cell: (c, row) => c.key === 'loan' && write && rupees(row.outstanding) > 0 ? `<span class="mono">${esc(row.loan)}</span> <button type="button" class="btn sm ghost no-print" data-repay="${esc(row.loan_id || row.loan)}">${esc(t('fi.repay'))}</button>` : undefined })}`;
  if (!write) return;
  const accs = await accounts();
  const methodOf = id => { const a = accs.find(x => x.account_id === id); return a?.kind === 'cash' ? 'cash' : a?.kind === 'wallet' ? (a.provider === 'easypaisa' ? 'easypaisa' : 'jazzcash') : 'bank'; };
  $('#addL').onclick = () => panel({
    title: t('fi.loan_add'), submitLabel: t('fi.loan_save'),
    body: `<div class="o-grid2">${field(t('fi.lender'), 'lender', '', 'required minlength="2" maxlength="80"')}${select(t('fi.kind'), 'kind', ['bank', 'informal', 'family', 'other'].map(k => [k, t('ln.' + k)]))}</div>
      <div class="o-grid3">${field(t('fi.amount_rs'), 'amount', '', 'required min="0.01" step="0.01"', 'number')}${field(t('fi.date'), 'on_date', todayPk(), 'required', 'date')}${select(t('fi.pay_into'), 'account_id', accs.map(a => [a.account_id, a.name]))}</div>
      ${field(t('fi.terms'), 'terms', '', 'maxlength="200" placeholder="markup, instalments…"')}`,
    onSubmit: async d => { await post('/api/finance/loans', { lender: d.lender.trim(), kind: d.kind, amount: Number(d.amount), method: methodOf(d.account_id), account_id: d.account_id, on_date: d.on_date, terms: d.terms.trim() }); toast(t('saved')); refresh(); },
  });
  $$('[data-repay]', box).forEach(b => b.onclick = () => panel({
    title: t('fi.repay_x', { id: b.dataset.repay }), submitLabel: t('fi.repay'),
    body: `<div class="o-grid3">${field(t('fi.principal'), 'principal', '', 'required min="0.01" step="0.01"', 'number')}${field(t('fi.interest'), 'interest', '0', 'min="0" step="0.01"', 'number')}${field(t('fi.date'), 'on_date', todayPk(), 'required', 'date')}</div>${select(t('fi.pay_from'), 'account_id', accs.map(a => [a.account_id, a.name]))}`,
    onSubmit: async d => { await post(`/api/finance/loans/${encodeURIComponent(b.dataset.repay)}/repay`, { principal: Number(d.principal), interest: Number(d.interest || 0), method: methodOf(d.account_id), account_id: d.account_id, on_date: d.on_date }); toast(t('saved')); refresh(); },
  }));
}

// ---------------------------------------------------------------- capital, drawings and the journal
async function journalTab({ box, write, accounts }) {
  if (!write) { box.innerHTML = `<p class="o-note">${esc(t('fi.owner_only'))}</p>`; return; }
  const [accs, tb] = await Promise.all([accounts(), api(`/api/finance/trial-balance?as_of=${todayPk()}`).catch(() => null)]);
  const codes = (tb?.table?.rows || []).map(r => [r.code, r.account]);
  const methodOf = id => { const a = accs.find(x => x.account_id === id); return a?.kind === 'cash' ? 'cash' : a?.kind === 'wallet' ? (a.provider === 'easypaisa' ? 'easypaisa' : 'jazzcash') : 'bank'; };
  box.innerHTML = `<div class="o-cols">
    ${['capital', 'drawings'].map(k => `<form class="o-card" data-cap="${k}" style="display:grid;gap:10px" novalidate><b>${esc(t('fi.' + k))}</b><p class="hint" style="margin:0">${esc(t('fi.' + k + '_hint'))}</p>
      <div class="o-grid3">${field(t('fi.amount_rs'), 'amount', '', 'required min="0.01" step="0.01"', 'number')}${select(k === 'capital' ? t('fi.pay_into') : t('fi.pay_from'), 'account_id', accs.map(a => [a.account_id, a.name]))}${field(t('fi.date'), 'on_date', todayPk(), 'required', 'date')}</div>
      ${field(t('fi.note'), 'note', '', 'maxlength="200"')}<p class="formerr" role="alert"></p><div class="o-bar"><div class="grow"></div><button class="btn primary" type="submit">${esc(t('fi.' + k + '_save'))}</button></div></form>`).join('')}</div>
    <form class="o-card" id="jv" style="display:grid;gap:10px" novalidate><b>${esc(t('fi.jv'))}</b><p class="hint" style="margin:0">${esc(t('fi.jv_hint'))}</p>
      <div class="o-grid3">${field(t('fi.date'), 'entry_date', todayPk(), 'required', 'date')}${select(t('fi.jv_kind'), 'kind', ['adjustment', 'bank_charge', 'cash_count', 'opening'].map(k => [k, t('jk.' + k)]))}${field(t('fi.memo'), 'memo', '', 'required minlength="3" maxlength="200"')}</div>
      <datalist id="chart">${codes.map(([c, n]) => `<option value="${esc(c)}">${esc(n)}</option>`).join('')}</datalist>
      <div class="o-journal" id="jl"><div class="o-jline hint"><span>${esc(t('fi.jv_account'))}</span><span>${esc(t('fi.jv_money_acc'))}</span><span class="n">${esc(t('fi.debit'))}</span><span class="n">${esc(t('fi.credit'))}</span><span></span></div></div>
      <div class="o-bar"><button type="button" class="btn" id="jadd">${esc(t('fi.jv_line'))}</button><div class="grow"></div><span id="jbal" aria-live="polite"></span></div>
      <p class="formerr" role="alert"></p><div class="o-bar"><div class="grow"></div><button class="btn primary" type="submit" id="jpost" disabled>${esc(t('fi.jv_post'))}</button></div></form>
    <form class="o-card o-bar" id="jrev" novalidate>${field(t('fi.jv_reverse_id'), 'je_id', '', 'required pattern="JV-\\d{4}-\\d{6}" placeholder="JV-2026-000001" style="width:200px"')}${field(t('fi.f_reason'), 'reason', '', 'required minlength="3" maxlength="200"')}<button class="btn danger" type="submit">${esc(t('fi.jv_reverse'))}</button><p class="formerr" role="alert"></p></form>`;

  $$('[data-cap]', box).forEach(f => f.onsubmit = e => {
    e.preventDefault(); if (!f.checkValidity()) { f.reportValidity(); return; }
    const k = f.dataset.cap, acc = accs.find(a => a.account_id === f.account_id.value);
    confirmDialog({
      title: t('fi.' + k + '_q', { amt: money(f.amount.value) }), confirmLabel: t('fi.' + k + '_save'),
      body: `<p>${esc(t('fi.' + k + '_effect', { amt: money(f.amount.value), acc: acc?.name || '', date: f.on_date.value }))}</p>`,
      onConfirm: async () => { await post(`/api/finance/${k}`, { amount: Number(f.amount.value), method: methodOf(f.account_id.value), account_id: f.account_id.value, on_date: f.on_date.value, note: f.note.value.trim() }); toast(t('fi.posted')); f.reset(); },
    });
  });

  const jl = $('#jl', box);
  const addLine = () => {
    const d = document.createElement('div'); d.className = 'o-jline';
    d.innerHTML = `<input class="input" list="chart" name="code" placeholder="6000:fuel, 3000…" aria-label="${esc(t('fi.jv_account'))}" maxlength="40">
      <select class="input" name="macc" aria-label="${esc(t('fi.jv_money_acc'))}"><option value="">—</option>${accs.map(a => `<option value="${esc(a.account_id)}">${esc(a.name)}</option>`).join('')}</select>
      <input class="input num" type="number" step="0.01" min="0" name="dr" aria-label="${esc(t('fi.debit'))}"><input class="input num" type="number" step="0.01" min="0" name="cr" aria-label="${esc(t('fi.credit'))}">
      <button type="button" class="x" aria-label="${esc(t('fi.jv_remove'))}">✕</button>`;
    d.querySelector('.x').onclick = () => { d.remove(); bal(); }; jl.appendChild(d); bal();
  };
  const lines = () => $$('.o-jline:not(.hint)', jl).map(d => ({ account_code: d.querySelector('[name=code]').value.trim(), money_account_id: d.querySelector('[name=macc]').value || null,
    debit: Number(d.querySelector('[name=dr]').value || 0), credit: Number(d.querySelector('[name=cr]').value || 0) }));
  const bal = () => {
    const ls = lines().filter(l => l.account_code && (l.debit || l.credit));
    const dr = ls.reduce((s, l) => s + rupees(l.debit), 0), cr = ls.reduce((s, l) => s + rupees(l.credit), 0);
    const bad = ls.some(l => (l.debit > 0) === (l.credit > 0) || (l.account_code === '1000') !== !!l.money_account_id);
    const ok = ls.length >= 2 && dr === cr && dr > 0 && !bad;
    $('#jbal', box).innerHTML = !ls.length ? `<span class="hint">${esc(t('fi.jv_bal', { dr: fmtCell(0, 'money'), cr: fmtCell(0, 'money') }))}</span>` : `<span class="${ok ? 'up' : 'down'}">${esc(t('fi.jv_bal', { dr: fmtCell(fromPaisa(dr), 'money'), cr: fmtCell(fromPaisa(cr), 'money') }))} ${ok ? '✓' : dr !== cr ? esc(t('fi.jv_diff', { amt: fmtCell(fromPaisa(dr - cr), 'money') })) : bad ? esc(t('fi.jv_bad')) : ''}</span>`;
    $('#jpost', box).disabled = !ok;
  };
  jl.addEventListener('input', bal); jl.addEventListener('change', bal);
  addLine(); addLine();
  $('#jadd', box).onclick = addLine;
  $('#jv', box).onsubmit = e => {
    e.preventDefault(); const f = e.target; if (!f.checkValidity()) { f.reportValidity(); return; }
    const ls = lines().filter(l => l.account_code && (l.debit || l.credit));
    confirmDialog({
      title: t('fi.jv_q'), confirmLabel: t('fi.jv_post'),
      body: `<div class="o-tw free"><table class="o-t"><thead><tr><th>${esc(t('fi.jv_account'))}</th><th class="n">${esc(t('fi.debit'))}</th><th class="n">${esc(t('fi.credit'))}</th></tr></thead><tbody>${ls.map(l => `<tr><td>${esc(l.account_code)}${l.money_account_id ? ' · ' + esc(l.money_account_id) : ''}</td><td class="n">${fmtCell(l.debit || '', 'money')}</td><td class="n">${fmtCell(l.credit || '', 'money')}</td></tr>`).join('')}</tbody></table></div><p>${esc(t('fi.jv_effect'))}</p>`,
      onConfirm: async () => { const r = await post('/api/finance/journal', { entry_date: f.entry_date.value, kind: f.kind.value, memo: f.memo.value.trim(), lines: ls }); toast(t('fi.posted') + (r.je_id ? ` · ${r.je_id}` : '')); refresh(); },
    });
  };
  $('#jrev', box).onsubmit = e => {
    e.preventDefault(); const f = e.target; if (!f.checkValidity()) { f.reportValidity(); return; }
    confirmDialog({ title: t('fi.jv_reverse_q', { id: f.je_id.value }), danger: true, confirmLabel: t('fi.jv_reverse'), body: `<p>${esc(t('fi.jv_reverse_effect'))}</p>`,
      onConfirm: async () => { await post(`/api/finance/journal/${encodeURIComponent(f.je_id.value.trim())}/reverse`, { reason: f.reason.value.trim() }); toast(t('fi.posted')); f.reset(); } });
  };
}

// ---------------------------------------------------------------- opening balances wizard
async function openingTab({ box, write, accounts }) {
  if (!write) { box.innerHTML = `<p class="o-note">${esc(t('fi.owner_only'))}</p>`; return; }
  const accs = await accounts();
  box.innerHTML = `<form id="ob" class="o-card" style="display:grid;gap:12px" novalidate>
    <p class="o-note">${esc(t('fi.ob_note'))}</p>
    ${field(t('fi.ob_asof'), 'as_of', todayPk().slice(0, 8) + '01', 'required style="width:200px"', 'date')}
    <h3 style="margin:0">1 · ${esc(t('fi.ob_money'))}</h3>
    <div class="o-grid3">${accs.map(a => field(a.name, 'm_' + a.account_id, '', 'step="0.01" placeholder="0"', 'number')).join('')}</div>
    <h3 style="margin:0">2 · ${esc(t('fi.ob_assets'))}</h3><div id="obA" class="o-journal"></div><div><button type="button" class="btn" id="obAddA">${esc(t('fi.ob_add_asset'))}</button></div>
    <h3 style="margin:0">3 · ${esc(t('fi.ob_loans'))}</h3><div id="obL" class="o-journal"></div><div><button type="button" class="btn" id="obAddL">${esc(t('fi.ob_add_loan'))}</button></div>
    <h3 style="margin:0">4 · ${esc(t('fi.ob_check'))}</h3><div id="obSum" aria-live="polite"></div>
    <p class="hint" style="margin:0">${esc(t('fi.ob_ar_ap'))}</p>
    <p class="formerr" role="alert"></p><div class="o-bar"><div class="grow"></div><button class="btn primary" type="submit">${esc(t('fi.ob_review'))}</button></div></form>`;
  const f = $('#ob', box);
  const row = (host, html) => { const d = document.createElement('div'); d.className = 'o-jline'; d.innerHTML = html + `<button type="button" class="x" aria-label="${esc(t('fi.jv_remove'))}">✕</button>`; d.querySelector('.x').onclick = () => { d.remove(); sum(); }; host.appendChild(d); };
  $('#obAddA', f).onclick = () => row($('#obA', f), `<input class="input" name="a_name" placeholder="${esc(t('fi.asset_name'))}" aria-label="${esc(t('fi.asset_name'))}"><select class="input" name="a_cat" aria-label="${esc(t('fi.asset_cat'))}">${['vehicle', 'building', 'land', 'furniture', 'equipment', 'computer', 'other'].map(k => `<option value="${k}">${esc(t('fa.' + k))}</option>`).join('')}</select>
    <input class="input num" type="number" step="0.01" min="0" name="a_cost" placeholder="${esc(t('fi.cost_rs'))}" aria-label="${esc(t('fi.cost_rs'))}"><input class="input num" type="number" min="0" step="1" name="a_life" value="60" aria-label="${esc(t('fi.life'))}">`);
  $('#obAddL', f).onclick = () => row($('#obL', f), `<input class="input" name="l_lender" placeholder="${esc(t('fi.lender'))}" aria-label="${esc(t('fi.lender'))}"><select class="input" name="l_kind" aria-label="${esc(t('fi.kind'))}">${['bank', 'informal', 'family', 'other'].map(k => `<option value="${k}">${esc(t('ln.' + k))}</option>`).join('')}</select>
    <input class="input num" type="number" step="0.01" min="0" name="l_amt" placeholder="${esc(t('fi.amount_rs'))}" aria-label="${esc(t('fi.amount_rs'))}"><span></span>`);
  const collect = () => ({
    money: accs.map(a => ({ account_id: a.account_id, amount: Number(f['m_' + a.account_id].value || 0) })).filter(m => m.amount),
    assets: $$('#obA .o-jline', f).map(d => ({ name: d.querySelector('[name=a_name]').value.trim(), category: d.querySelector('[name=a_cat]').value, cost: Number(d.querySelector('[name=a_cost]').value || 0), life_months: Number(d.querySelector('[name=a_life]').value || 0), acquired_on: f.as_of.value })).filter(a => a.name && a.cost),
    loans: $$('#obL .o-jline', f).map(d => ({ lender: d.querySelector('[name=l_lender]').value.trim(), kind: d.querySelector('[name=l_kind]').value, amount: Number(d.querySelector('[name=l_amt]').value || 0) })).filter(l => l.lender && l.amount),
  });
  const sum = () => {
    const c = collect();
    const m = c.money.reduce((s, x) => s + rupees(x.amount), 0), a = c.assets.reduce((s, x) => s + rupees(x.cost), 0), l = c.loans.reduce((s, x) => s + rupees(x.amount), 0);
    $('#obSum', f).innerHTML = renderTable({ title: '', columns: [{ key: 'line', label: t('fi.line'), kind: 'text', align: 'left' }, { key: 'amount', label: t('fi.amount'), kind: 'money', align: 'right' }],
      rows: [{ line: t('fi.ob_money'), amount: fromPaisa(m) }, { line: t('fi.ob_assets'), amount: fromPaisa(a) }, { line: t('fi.ob_less_loans'), amount: -fromPaisa(l) }, { line: t('fi.ob_equity'), amount: fromPaisa(m + a - l), _em: true }] }, { compact: true, caption: '' });
    return c;
  };
  f.addEventListener('input', sum); sum();
  f.onsubmit = e => {
    e.preventDefault(); if (!f.checkValidity()) { f.reportValidity(); return; }
    const c = sum(); if (!c.money.length && !c.assets.length && !c.loans.length) { $('.formerr', f).textContent = t('fi.ob_empty'); return; }
    const eq = c.money.reduce((s, x) => s + rupees(x.amount), 0) + c.assets.reduce((s, x) => s + rupees(x.cost), 0) - c.loans.reduce((s, x) => s + rupees(x.amount), 0);
    confirmDialog({
      title: t('fi.ob_q', { d: f.as_of.value }), confirmLabel: t('fi.ob_post'),
      body: `<p class="o-big">${money(fromPaisa(eq))}</p><p>${esc(t('fi.ob_effect', { d: f.as_of.value, n: c.money.length + c.assets.length + c.loans.length }))}</p>`,
      onConfirm: async () => { const r = await post('/api/finance/opening-balances', { as_of: f.as_of.value, ...c }); toast(t('fi.posted') + (r.je_id ? ` · ${r.je_id}` : '')); refresh(); },
    });
  };
}
