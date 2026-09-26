/* Accounts & banks: where the money physically sits (cash in the galla, banks, JazzCash / Easypaisa wallets), each
   account's book, transfers between own accounts, the cash count and the bank reconciliation (tick what the statement
   shows, enter its balance, the difference must reach 0.00 before it can be saved).
   Owner and clerk (books:read / books:write). For a clerk the API redacts salary, advance and statutory lines into one
   "Staff payments (owner only)" line a day; this screen adds nothing back. Adding an account, method routes and
   posting a cash difference to the books are the owner's (finance:write).
   REST (plan §9 Stream B): GET/POST /api/accounts, GET /api/accounts/{id}/book, POST /api/accounts/transfer,
   POST /api/accounts/{id}/cash-count, POST /api/cash-counts/{id}/post, GET /api/accounts/{id}/reconciliation,
   POST /api/accounts/{id}/clear, POST /api/accounts/{id}/reconciliations, GET/PUT /api/accounts/method-routes. */
import { $, $$, api, can, confirmDialog, esc, field, fmtCell, fromPaisa, money, panel, post, refresh, renderTable, rupees, select, skeleton, t, toast, todayPk } from './lib.js';

const KIND_ICON = { cash: '◫', bank: '⌂', wallet: '▣' };

export default async function accounts(view, [id = '', tab = ''] = []) {
  const write = can('books:write'), owner = can('finance:write'), payroll = can('payroll:read');
  view.header(t('fi.accounts'), t('loading'), `${write ? `<button class="btn" id="xfer">${esc(t('fi.transfer'))}</button>` : ''}${owner ? `<button class="btn" id="routes">${esc(t('fi.routes'))}</button><button class="btn primary" id="addAcc">${esc(t('fi.add_account'))}</button>` : ''}`);
  view.page.innerHTML = `<div class="o-accs" id="accs">${Array.from({ length: 3 }, () => `<div class="o-acc">${skeleton(2, 1)}</div>`).join('')}</div><div id="acc"></div>`;
  const data = await api('/api/accounts');
  const list = (data.accounts || []).filter(a => a.active !== false);
  const cur = list.find(a => a.account_id === id) || list[0];
  view.header(t('fi.accounts'), esc(t('fi.acc_sub', { total: money(data.total ?? list.reduce((s, a) => s + rupees(a.balance), 0) / 100), n: list.length })), view.top.querySelector('.o-bar').innerHTML);
  $('#accs').innerHTML = list.map(a => `<button type="button" class="o-acc" data-acc="${esc(a.account_id)}" aria-pressed="${a === cur}">
      <span class="k"><span aria-hidden="true">${KIND_ICON[a.kind] || ''}</span> ${esc(t('fi.kind_' + a.kind))}${a.number_last4 ? ` · ••${esc(a.number_last4)}` : ''}</span>
      <b>${esc(a.name)}</b><span class="v ${rupees(a.balance) < 0 ? 'neg' : ''}">${money(a.balance)}</span>
      <span class="s">${a.kind === 'cash' ? esc(t('fi.as_of', { d: fmtCell(a.as_of, 'date') })) : a.last_reconciled ? esc(t('fi.reconciled_on', { d: fmtCell(a.last_reconciled, 'date') })) : esc(t('fi.never_reconciled'))}${rupees(a.uncleared_out) || rupees(a.uncleared_in) ? ` · ${esc(t('fi.uncleared', { amt: money(fromPaisa(rupees(a.uncleared_in) - rupees(a.uncleared_out))) }))}` : ''}</span></button>`).join('')
    || `<p class="o-empty">${esc(t('fi.no_accounts'))}</p>`;
  $$('[data-acc]').forEach(b => b.onclick = () => { location.hash = `#/accounts/${encodeURIComponent(b.dataset.acc)}`; });
  $('#xfer')?.addEventListener('click', () => transferPanel(list));
  $('#addAcc')?.addEventListener('click', addAccountPanel);
  $('#routes')?.addEventListener('click', () => routesPanel(list));
  if (!cur) return;

  const tabs = [['', t('fi.book')], ...(cur.kind === 'cash' ? [['count', t('fi.cash_count')]] : [['reconcile', t('fi.reconcile')]])];
  const tb = tabs.some(x => x[0] === tab) ? tab : '';
  $('#acc').innerHTML = `<nav class="o-tabs" aria-label="${esc(cur.name)}">${tabs.map(([k, l]) => `<a href="#/accounts/${encodeURIComponent(cur.account_id)}${k ? '/' + k : ''}" class="${k === tb ? 'active' : ''}" ${k === tb ? 'aria-current="page"' : ''}>${esc(l)}</a>`).join('')}</nav><div id="accBody" style="margin-top:12px;display:grid;gap:12px">${skeleton(8, 7)}</div>`;
  const body = $('#accBody');
  if (tb === 'count') return countTab(body, cur, write, owner);
  if (tb === 'reconcile') return reconcileTab(body, cur, write);
  return bookTab(body, cur, payroll);
}

// ---------------------------------------------------------------- the account book
async function bookTab(body, acc, payroll) {
  const end = todayPk(), start = end.slice(0, 8) + '01';
  body.innerHTML = `<div class="o-bar"><label class="f" style="grid-auto-flow:column;align-items:center">${esc(t('fi.from'))}<input class="input" type="date" id="bs" value="${start}"></label>
    <label class="f" style="grid-auto-flow:column;align-items:center">${esc(t('fi.to'))}<input class="input" type="date" id="be" value="${end}"></label><button class="btn" id="bgo" type="button">${esc(t('fi.show'))}</button></div>
    ${payroll ? '' : `<p class="o-note">${esc(t('fi.redacted_note'))}</p>`}<div id="book">${skeleton(8, 8)}</div>`;
  const load = async () => {
    const b = await api(`/api/accounts/${encodeURIComponent(acc.account_id)}/book?start=${$('#bs').value}&end=${$('#be').value}`);
    $('#book').innerHTML = renderTable(b.table, { empty: t('fi.book_empty') });
  };
  $('#bgo').onclick = () => load().catch(x => toast(x.message, 6000));
  await load();
}

// ---------------------------------------------------------------- cash count
function countTab(body, acc, write, owner) {
  body.innerHTML = `<div class="o-cols"><form class="o-card" id="cc" style="display:grid;gap:10px" novalidate>
      <b>${esc(t('fi.count_title', { name: acc.name }))}</b>
      <p class="hint" style="margin:0">${esc(t('fi.count_hint'))}</p>
      <div class="o-grid2">${field(t('fi.book_says'), 'book', fmtCell(acc.balance, 'money'), 'readonly')}${field(t('fi.counted'), 'counted', '', `required min="0" step="0.01" inputmode="decimal" ${write ? '' : 'disabled'}`, 'number')}</div>
      <p id="ccDiff" class="o-note" aria-live="polite">${esc(t('fi.count_enter'))}</p>
      ${field(t('fi.note'), 'note', '', 'maxlength="200"')}
      <p class="formerr" role="alert"></p>
      <div class="o-bar"><div class="grow"></div><button class="btn primary" type="submit" ${write ? '' : 'disabled'}>${esc(t('fi.count_save'))}</button></div></form>
    <div class="o-card" style="display:grid;gap:6px"><b>${esc(t('fi.count_what'))}</b><p style="margin:0">${esc(t('fi.count_what_1'))}</p><p style="margin:0">${esc(owner ? t('fi.count_what_owner') : t('fi.count_what_clerk'))}</p></div></div>`;
  const f = $('#cc');
  f.counted.oninput = () => {
    if (f.counted.value === '') { $('#ccDiff').className = 'o-note'; $('#ccDiff').textContent = t('fi.count_enter'); return; }
    const d = rupees(f.counted.value) - rupees(acc.balance);
    $('#ccDiff').className = 'o-note ' + (d === 0 ? 'good' : 'warn');
    $('#ccDiff').textContent = d === 0 ? t('fi.count_match') : d < 0 ? t('fi.count_short', { amt: money(fromPaisa(-d)) }) : t('fi.count_over', { amt: money(fromPaisa(d)) });
  };
  f.onsubmit = async e => {
    e.preventDefault(); if (!f.checkValidity()) { f.reportValidity(); return; }
    const err = $('.formerr', f); err.textContent = '';
    try {
      const r = await post(`/api/accounts/${encodeURIComponent(acc.account_id)}/cash-count`, { counted: Number(f.counted.value), note: f.note.value.trim() });
      const d = rupees(r.difference);
      toast(t('fi.count_saved'));
      if (d !== 0 && owner) confirmDialog({
        title: t('fi.post_diff_q'), confirmLabel: t('fi.post_diff'), body: `<p class="o-big">${money(r.difference)}</p><p>${esc(d < 0 ? t('fi.post_short_effect', { amt: money(fromPaisa(-d)) }) : t('fi.post_over_effect', { amt: money(fromPaisa(d)) }))}</p>`,
        onConfirm: async () => { await post(`/api/cash-counts/${encodeURIComponent(r.count_id)}/post`, {}); toast(t('fi.posted')); refresh(); },
      });
      else if (d !== 0) toast(t('fi.count_owner_posts'), 6000);
    } catch (x) { err.textContent = x.message; }
  };
}

// ---------------------------------------------------------------- bank reconciliation
async function reconcileTab(body, acc, write) {
  let sd = todayPk();
  const load = async () => api(`/api/accounts/${encodeURIComponent(acc.account_id)}/reconciliation?statement_date=${sd}`);
  let r = await load();
  sd = r.statement_date || sd;
  body.innerHTML = `<div class="o-recon"><div style="display:grid;gap:10px;min-width:0">
      <div class="o-bar">${field(t('fi.stmt_date'), 'sd', sd, 'id="sd"', 'date')}${field(t('fi.stmt_balance'), 'sb', '', 'id="sb" step="0.01" inputmode="decimal" style="width:180px"', 'number')}
        <span class="hint">${r.last ? esc(t('fi.last_recon', { d: fmtCell(r.last.statement_date, 'date'), amt: money(r.last.statement_balance) })) : ''}</span></div>
      <p class="hint" style="margin:0">${esc(t('fi.tick_hint'))}</p>
      <div class="o-tw free"><table class="o-t" id="rt"><caption class="sr">${esc(t('fi.reconcile'))}</caption><thead><tr><th class="c">${esc(t('fi.cleared'))}</th><th>${esc(t('fi.date'))}</th><th>${esc(t('fi.doc'))}</th><th>${esc(t('fi.narration'))}</th><th class="n">${esc(t('fi.in'))}</th><th class="n">${esc(t('fi.out'))}</th></tr></thead><tbody></tbody></table></div></div>
    <aside class="o-diff" aria-label="${esc(t('fi.recon_sum'))}" id="diff"></aside></div>`;
  const tbody = $('#rt tbody');
  const paintRows = () => {
    tbody.innerHTML = (r.items || []).map((it, i) => `<tr class="${it.cleared ? 'cleared' : ''}"><td class="c"><input type="checkbox" data-i="${i}" ${it.cleared ? 'checked' : ''} ${write ? '' : 'disabled'} aria-label="${esc(t('fi.cleared'))}: ${esc(it.doc)} ${esc(it.narration)}"></td>
      <td>${fmtCell(it.date, 'date')}</td><td class="mono">${esc(it.doc)}</td><td class="wrap">${esc(it.narration)}</td>
      <td class="n">${rupees(it.amount) > 0 ? fmtCell(it.amount, 'money') : ''}</td><td class="n">${rupees(it.amount) < 0 ? fmtCell(-it.amount, 'money') : ''}</td></tr>`).join('')
      || `<tr><td colspan="6" class="o-empty">${esc(t('fi.recon_none'))}</td></tr>`;
  };
  const paintDiff = () => {
    const book = rupees(r.book_balance);
    const unIn = (r.items || []).filter(x => !x.cleared && rupees(x.amount) > 0).reduce((s, x) => s + rupees(x.amount), 0);
    const unOut = (r.items || []).filter(x => !x.cleared && rupees(x.amount) < 0).reduce((s, x) => s - rupees(x.amount), 0);
    const adjusted = book - unIn + unOut;
    const sbv = $('#sb').value; const stmt = sbv === '' ? null : rupees(sbv);
    const diff = stmt === null ? null : stmt - adjusted;
    const line = (l, v, em) => `<div class="row ${em ? 'em' : ''}"><span>${esc(l)}</span><span>${fmtCell(fromPaisa(v), 'money')}</span></div>`;
    $('#diff').innerHTML = `<b>${esc(t('fi.recon_sum'))}</b>${line(t('fi.bal_books'), book)}${line(t('fi.add_uncleared_out'), unOut)}${line(t('fi.less_uncleared_in'), unIn)}${line(t('fi.adjusted'), adjusted, true)}
      ${stmt === null ? `<p class="hint" style="margin:0">${esc(t('fi.enter_stmt'))}</p>` : line(t('fi.stmt_balance'), stmt)}
      ${diff === null ? '' : `<div class="meter ${diff === 0 ? 'zero' : 'off'}" role="status"><span>${esc(t('fi.difference'))}</span><span>${diff === 0 ? '✓ ' : ''}${fmtCell(fromPaisa(diff), 'money')}</span></div>`}
      <p class="hint" style="margin:0">${esc(diff === 0 ? t('fi.diff_zero') : t('fi.diff_must_zero'))}</p>
      ${write ? `<button type="button" class="btn primary" id="saveRec" ${diff === 0 ? '' : 'disabled'}>${esc(t('fi.save_recon'))}</button>` : ''}`;
    $('#saveRec')?.addEventListener('click', () => confirmDialog({
      title: t('fi.save_recon_q', { name: acc.name }), confirmLabel: t('fi.save_recon'),
      body: `<p>${esc(t('fi.save_recon_effect', { d: fmtCell($('#sd').value, 'date'), amt: money(fromPaisa(stmt)) }))}</p>`,
      onConfirm: async () => { await post(`/api/accounts/${encodeURIComponent(acc.account_id)}/reconciliations`, { statement_date: $('#sd').value, statement_balance: fromPaisa(stmt) }); toast(t('fi.recon_saved')); refresh(); },
    }));
  };
  paintRows(); paintDiff();
  $('#sb').oninput = paintDiff;
  $('#sd').onchange = async e => { sd = e.target.value; try { r = await load(); paintRows(); paintDiff(); } catch (x) { toast(x.message, 6000); } };
  tbody.addEventListener('change', async e => {
    const cb = e.target.closest('[data-i]'); if (!cb) return;
    const it = r.items[+cb.dataset.i]; const on = cb.checked;
    cb.disabled = true;
    try { await post(`/api/accounts/${encodeURIComponent(acc.account_id)}/clear`, { items: [{ source: it.source, source_id: it.source_id }], cleared_on: $('#sd').value, cleared: on }); it.cleared = on; cb.closest('tr').classList.toggle('cleared', on); }
    catch (x) { cb.checked = !on; toast(x.message, 6000); }
    finally { cb.disabled = false; paintDiff(); }
  });
}

// ---------------------------------------------------------------- transfer between own accounts (preview before save)
function transferPanel(list) {
  const opts = list.map(a => [a.account_id, `${a.name} (${money(a.balance)})`]);
  panel({
    title: t('fi.transfer'), submitLabel: t('fi.transfer_save'),
    body: `<p class="o-note">${esc(t('fi.transfer_note'))}</p>
      <div class="o-grid2">${select(t('fi.from_acc'), 'from_account', opts, list.find(a => a.kind === 'cash')?.account_id)}${select(t('fi.to_acc'), 'to_account', opts, list.find(a => a.kind === 'bank')?.account_id)}</div>
      <div class="o-grid2">${field(t('fi.amount_rs'), 'amount', '', 'required min="0.01" step="0.01"', 'number')}${field(t('fi.date'), 'on_date', todayPk(), 'required', 'date')}</div>
      <div class="o-grid2">${field(t('fi.ref'), 'ref', '', 'maxlength="40" placeholder="deposit slip / IBFT no."')}${field(t('fi.note'), 'note', '', 'maxlength="200"')}</div>
      <div id="xprev" class="o-card" aria-live="polite"></div>`,
    onOpen: c => {
      const f = c.el; const paint = () => {
        const a = list.find(x => x.account_id === f.from_account.value), b = list.find(x => x.account_id === f.to_account.value), amt = rupees(f.amount.value);
        $('#xprev', f).innerHTML = a === b ? `<p class="crit" style="margin:0">${esc(t('fi.same_account'))}</p>` : !amt ? `<p class="hint" style="margin:0">${esc(t('fi.transfer_enter'))}</p>`
          : `<div class="o-kv"><b>${esc(a.name)}</b><span>${money(a.balance)} → <b>${money(fromPaisa(rupees(a.balance) - amt))}</b>${rupees(a.balance) - amt < 0 ? ` <span class="crit">${esc(t('fi.goes_negative'))}</span>` : ''}</span>
             <b>${esc(b.name)}</b><span>${money(b.balance)} → <b>${money(fromPaisa(rupees(b.balance) + amt))}</b></span></div>`;
      };
      f.addEventListener('input', paint); f.addEventListener('change', paint); paint();
    },
    onSubmit: async d => {
      if (d.from_account === d.to_account) throw new Error(t('fi.same_account'));
      await post('/api/accounts/transfer', { from_account: d.from_account, to_account: d.to_account, amount: Number(d.amount), on_date: d.on_date, ref: d.ref.trim(), note: d.note.trim() });
      toast(t('fi.transferred')); refresh();
    },
  });
}

function addAccountPanel() {
  panel({
    title: t('fi.add_account'), submitLabel: t('fi.add_account'),
    body: `<div class="o-grid2">${select(t('fi.kind'), 'kind', [['bank', t('fi.kind_bank')], ['wallet', t('fi.kind_wallet')], ['cash', t('fi.kind_cash')]])}${field(t('fi.acc_name'), 'name', '', 'required minlength="1" maxlength="60" placeholder="HBL current"')}</div>
      <div class="o-grid2">${field(t('fi.provider'), 'provider', '', 'maxlength="40" placeholder="HBL, Meezan, jazzcash…"')}${field(t('fi.last4'), 'number_last4', '', 'maxlength="4" pattern="\\d{0,4}" inputmode="numeric"')}</div>
      <div class="o-grid2">${field(t('fi.opening_bal'), 'opening_balance', '0', 'step="0.01"', 'number')}${field(t('fi.opening_date'), 'opening_date', todayPk(), '', 'date')}</div>
      <p class="hint" style="margin:0">${esc(t('fi.opening_hint'))}</p>`,
    onSubmit: async d => { await post('/api/accounts', { ...d, opening_balance: Number(d.opening_balance || 0) }); toast(t('saved')); refresh(); },
  });
}

async function routesPanel(list) {
  const r = await api('/api/accounts/method-routes').catch(() => ({ routes: [] }));
  const cur = Object.fromEntries((r.routes || []).map(x => [x.method, x.account_id]));
  const methods = ['cash', 'bank', 'cheque', 'jazzcash', 'easypaisa'];
  panel({
    title: t('fi.routes'), submitLabel: t('save'),
    body: `<p class="o-note">${esc(t('fi.routes_note'))}</p>${methods.map(m => select(t('m.' + m), 'm_' + m, [['', t('fi.unassigned')], ...list.map(a => [a.account_id, a.name])], cur[m] || '')).join('')}
      ${field(t('fi.effective'), 'effective_from', todayPk(), 'required', 'date')}`,
    onSubmit: async d => {
      const routes = methods.filter(m => d['m_' + m] && d['m_' + m] !== cur[m]).map(m => ({ method: m, account_id: d['m_' + m] }));
      if (!routes.length) return;
      await api('/api/accounts/method-routes', { method: 'PUT', body: JSON.stringify({ routes, effective_from: d.effective_from }) }); toast(t('saved')); refresh();
    },
  });
}
