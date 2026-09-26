/* Employees: one master record per person; an app login is optional (loaders usually have none, and they never use a
   seat). Owner: add / edit, pay terms, the per-employee "cash allowed" exemption, app logins (a generated PIN is shown
   ONCE in its own dialog -- lib.pinOnce -- and never stored or sent anywhere but the create call), reset PIN, end
   employment, rehire. Clerk (employees:read): names, numbers, designation, status and the login badge -- never a pay
   field; the API does not send one, and this screen does not ask for one.
   REST (plan §9 Stream A): GET/POST /api/employees, GET/PATCH /api/employees/{id}, POST /api/employees/{id}/
   {pay-structure, login, login/reset-pin, end, rehire}. */
import { $, $$, api, can, check, confirmDialog, esc, field, money, panel, patch, pill, pinOnce, post, select, skeleton, t, table, toast, todayPk, when } from './lib.js';

const ROLE_HINTS = ['clerk', 'salesman', 'driver', 'helper', 'loader', 'godown', 'guard', 'accountant', 'other'];
const LOGIN_ROLES = ['clerk', 'salesman', 'driver'];
const METHODS = ['cash', 'bank', 'jazzcash', 'easypaisa', 'cheque'];
const PROVINCES = ['punjab', 'sindh', 'kp', 'balochistan', 'ict'];
const word = (pfx, k) => t(`${pfx}.${k}`);

export default async function employees(view) {
  const owner = can('payroll:read'), manage = can('staff:manage');
  let filter = 'active', data = { employees: [] };
  view.header(t('pr.employees'), t('loading'), manage ? `<button class="btn primary" id="addE">${esc(t('pr.add_employee'))}</button>` : '');
  view.page.innerHTML = `
    ${owner ? '' : `<p class="o-note">${esc(t('pr.clerk_employees_note'))}</p>`}
    <div class="o-bar">
      <input class="input o-search" type="search" id="q" placeholder="${esc(t('pr.search_employees'))}" aria-label="${esc(t('pr.search_employees'))}">
      <div class="chips" style="padding:0" role="group" aria-label="${esc(t('pr.show'))}">${[['active', t('pr.f_active')], ['left', t('pr.f_left')], ['all', t('pr.f_all')]].map(([k, l]) => `<button class="chip" data-f="${k}" aria-pressed="${k === filter}">${esc(l)}</button>`).join('')}</div>
    </div>
    <div class="o-tw" id="tbl">${skeleton(7, owner ? 8 : 6)}</div>`;

  const cols = [
    { key: 'emp_no', label: t('pr.c_no'), cls: 'mono' },
    { key: 'name', label: t('pr.c_name'), render: r => `<b>${esc(r.name)}</b>` },
    { key: 'designation', label: t('pr.c_designation'), cls: 'dim' },
    ...(owner ? [
      { key: 'pay_basis', label: t('pr.c_basis'), render: r => r.pay_basis ? esc(word('pr', 'basis_' + r.pay_basis)) : `<span class="dim">${esc(t('pr.no_terms'))}</span>` },
      { key: 'rate', label: t('pr.c_rate'), num: true, value: r => r.pay_basis === 'daily' ? r.daily_rate : r.basic,
        render: r => r.pay_basis ? `${money(r.pay_basis === 'daily' ? r.daily_rate : r.basic)}${r.pay_basis === 'daily' ? `<span class="dim"> ${esc(t('pr.per_day'))}</span>` : ''}` : '' },
      { key: 'pay_method', label: t('pr.c_paid_by'), render: r => esc(word('m', r.pay_method || 'cash')) },
      { key: 'cash_allowed', label: t('pr.c_cash_allowed'), value: r => r.cash_allowed ? 1 : 0, title: t('pr.cash_allowed_hint'),
        render: r => r.cash_allowed ? `<span class="pill warn o-badge"><span aria-hidden="true">⚑</span> ${esc(t('pr.cash_allowed_short'))}</span>` : '' },
    ] : []),
    { key: 'login', label: t('pr.c_login'), value: r => r.login ? r.login.role : '',
      render: r => r.login ? `<span class="pill ${r.login.active ? 'acc' : ''} o-badge"><span aria-hidden="true">●</span> ${esc(word('role', r.login.role))}</span>${r.login.must_change_pin ? ` <span class="dim" title="${esc(t('pr.must_change_hint'))}">${esc(t('pr.pin_pending'))}</span>` : ''}` : `<span class="dim">${esc(t('pr.no_login'))}</span>` },
    { key: 'status', label: t('pr.c_status'), render: r => r.status === 'active' ? pill(t('pr.st_active'), 'good') : `${pill(t('pr.st_left'))} <span class="dim">${esc(r.left_on || '')}</span>` },
  ];
  const tb = table($('#tbl'), {
    columns: cols, rows: [], key: 'employee_id', sort: { key: 'emp_no', dir: 'asc' }, searchKeys: ['emp_no', 'name', 'designation', 'phone'],
    onRow: r => employeePanel(r), rowClass: r => r.status === 'active' ? '' : 'off',
    empty: t('pr.no_employees'),
  });
  const load = async () => {
    data = await api(`/api/employees?status=${filter}`);
    tb.setRows(data.employees || []);
    const act = (data.employees || []).filter(e => e.status === 'active');
    view.header(t('pr.employees'), esc(t('pr.emp_sub', { n: act.length, l: act.filter(e => e.login).length })), manage ? `<button class="btn primary" id="addE">${esc(t('pr.add_employee'))}</button>` : '');
    $('#addE')?.addEventListener('click', () => employeePanel(null));
  };
  $('#q').oninput = e => tb.setSearch(e.target.value);
  $$('[data-f]', view.page).forEach(b => b.onclick = async () => {
    filter = b.dataset.f; $$('[data-f]', view.page).forEach(x => x.setAttribute('aria-pressed', String(x === b))); await load();
  });
  await load();

  // ---------------------------------------------------------------- one employee
  async function employeePanel(e) {
    const isNew = !e;
    if (!isNew && !manage) return readOnlyPanel(e);
    const p = e || { name: '', father_name: '', phone: '', designation: '', role_hint: 'other', joined_on: todayPk(), province: 'punjab', eobi_covered: false,
      ss_covered: false, tax_mode: 'auto', pay_method: 'cash', payee_ref: '', cash_allowed: false, pay_basis: 'monthly', basic: '', daily_rate: '' };
    const body = `
      <h3>${esc(t('pr.sec_person'))}</h3>
      <div class="o-grid2">${field(t('pr.f_name'), 'name', p.name, 'required minlength="2" maxlength="60"')}${field(t('pr.f_father'), 'father_name', p.father_name || '', 'maxlength="60"')}</div>
      <div class="o-grid3">${field(t('pr.f_phone'), 'phone', p.phone || '', 'inputmode="tel" placeholder="0301-1234567" maxlength="14"', 'tel')}
        ${isNew ? field(t('pr.f_cnic'), 'cnic', '', 'inputmode="numeric" pattern="\\d{13}" maxlength="13" placeholder="13 digits, no dashes"') : `<label class="f">${esc(t('pr.f_cnic'))}<input class="input" value="${p.cnic_last4 ? '•••••••••' + esc(p.cnic_last4) : '—'}" readonly></label>`}
        ${field(t('pr.f_designation'), 'designation', p.designation || '', 'maxlength="40" placeholder="Driver, loader, order booker…"')}</div>
      <div class="o-grid3">${select(t('pr.f_role_hint'), 'role_hint', ROLE_HINTS.map(r => [r, word('rh', r)]), p.role_hint)}
        ${field(t('pr.f_joined'), 'joined_on', p.joined_on || todayPk(), `required ${isNew ? '' : 'readonly'}`, 'date')}
        ${select(t('pr.f_province'), 'province', PROVINCES.map(r => [r, word('prov', r)]), p.province)}</div>
      ${owner ? `<h3>${esc(t('pr.sec_pay'))}</h3>
      <div class="o-grid3">${select(t('pr.f_basis'), 'pay_basis', [['monthly', t('pr.basis_monthly')], ['daily', t('pr.basis_daily')]], p.pay_basis || 'monthly')}
        ${field(t('pr.f_basic'), 'basic', p.basic || '', 'min="0" step="any" data-basis="monthly"', 'number')}${field(t('pr.f_daily'), 'daily_rate', p.daily_rate || '', 'min="0" step="any" data-basis="daily"', 'number')}</div>
      ${isNew ? '' : field(t('pr.f_effective'), 'effective_from', todayPk().slice(0, 8) + '01', '', 'date')}
      <div class="o-grid3">${select(t('pr.f_pay_method'), 'pay_method', METHODS.map(m => [m, word('m', m)]), p.pay_method || 'cash')}
        ${field(t('pr.f_payee_ref'), 'payee_ref', p.payee_ref || '', 'maxlength="34" placeholder="IBAN or wallet number"')}
        ${select(t('pr.f_tax'), 'tax_mode', [['auto', t('pr.tax_auto')], ['off', t('pr.tax_off')]], p.tax_mode || 'auto')}</div>
      <div class="o-grid2">${check(t('pr.f_eobi'), 'eobi_covered', p.eobi_covered)}${check(t('pr.f_ss'), 'ss_covered', p.ss_covered)}</div>
      <div class="o-card" style="display:grid;gap:6px">${check(t('pr.f_cash_allowed'), 'cash_allowed', p.cash_allowed)}<p class="hint" style="margin:0">${esc(t('pr.cash_allowed_hint'))}</p></div>` : ''}
      ${isNew ? loginFields(p) : ''}`;
    const ctl = panel({
      title: isNew ? t('pr.add_employee') : p.name, wide: !isNew, submitLabel: isNew ? t('pr.add_employee') : t('pr.save_changes'), body: body + (isNew ? '' : '<div id="edetail"></div>'),
      onOpen: c => { wireBasis(c.el); if (isNew) wireLogin(c.el); },
      onSubmit: async (d, form) => {
        const person = { name: d.name.trim(), father_name: (d.father_name || '').trim(), phone: (d.phone || '').trim(), designation: (d.designation || '').trim(),
          role_hint: d.role_hint, province: d.province };
        if (isNew) { person.joined_on = d.joined_on; person.cnic = (d.cnic || '').trim(); }
        if (owner) Object.assign(person, { pay_method: d.pay_method, payee_ref: (d.payee_ref || '').trim(), tax_mode: d.tax_mode, eobi_covered: d.eobi_covered, ss_covered: d.ss_covered, cash_allowed: d.cash_allowed });
        const pay = owner ? payTerms(d) : null;
        if (pay && pay.error) throw new Error(pay.error);
        if (isNew) {
          const login = d.want_login ? loginBody(d, person.phone) : null;
          if (login && login.error) throw new Error(login.error);
          if (login) person.app_login = login;
          const r = await post('/api/employees', person);
          wipePin(form);
          if (pay) {
            try { await post(`/api/employees/${encodeURIComponent(r.employee.employee_id)}/pay-structure`, { ...pay, effective_from: person.joined_on }); }
            catch (x) { toast(t('pr.pay_terms_failed', { msg: x.message }), 8000); }
          }
          toast(t('pr.added', { name: r.employee.name }));
          if (r.login?.pin_once) pinOnce({ name: r.employee.name, phone: r.login.phone, pin: r.login.pin_once, text: r.login.whatsapp_text });
          await load(); return;
        }
        await patch(`/api/employees/${encodeURIComponent(p.employee_id)}`, person);
        const changedPay = pay && (pay.pay_basis !== p.pay_basis || Number(pay.basic || 0) !== Number(p.basic || 0) || Number(pay.daily_rate || 0) !== Number(p.daily_rate || 0));
        if (changedPay) await post(`/api/employees/${encodeURIComponent(p.employee_id)}/pay-structure`, { ...pay, effective_from: d.effective_from });
        toast(t('pr.saved_name', { name: person.name })); await load();
      },
    });
    if (!isNew) detailBox(ctl, p);
  }

  function readOnlyPanel(e) {
    const ctl = panel({ title: e.name, wide: true, body: `<div class="o-kv">
      <b>${esc(t('pr.c_no'))}</b><span class="mono">${esc(e.emp_no)}</span><b>${esc(t('pr.f_designation'))}</b><span>${esc(e.designation || '—')}</span>
      <b>${esc(t('pr.f_phone'))}</b><span>${esc(e.phone || '—')}</span><b>${esc(t('pr.f_joined'))}</b><span>${esc(e.joined_on || '')}</span>
      <b>${esc(t('pr.c_status'))}</b><span>${esc(e.status === 'active' ? t('pr.st_active') : t('pr.st_left') + ' ' + (e.left_on || ''))}</span>
      <b>${esc(t('pr.c_login'))}</b><span>${e.login ? esc(word('role', e.login.role)) : esc(t('pr.no_login'))}</span></div>
      <p class="o-note">${esc(t('pr.clerk_employees_note'))}</p><div id="edetail"></div>` });
    detailBox(ctl, e);
  }

  // login state, life history and the owner's actions for an existing employee
  async function detailBox(ctl, e) {
    const box = ctl.body.querySelector('#edetail'); if (!box) return;
    const actions = manage ? `<div class="o-bar no-print">
        ${e.status === 'active' ? (e.login ? `<button type="button" class="btn" data-a="reset">${esc(t('pr.reset_pin'))}</button>` : `<button type="button" class="btn" data-a="login">${esc(t('pr.give_login'))}</button>`) : ''}
        ${owner && e.status === 'active' ? `<button type="button" class="btn" data-a="cash">${esc(e.cash_allowed ? t('pr.cash_revoke') : t('pr.cash_allow'))}</button>` : ''}
        <div class="grow"></div>
        ${e.status === 'active' ? `<button type="button" class="btn danger" data-a="end">${esc(t('pr.end_employment'))}</button>` : `<button type="button" class="btn" data-a="rehire">${esc(t('pr.rehire'))}</button>`}</div>` : '';
    box.innerHTML = `<h3>${esc(t('pr.sec_login'))}</h3>
      <p style="margin:0">${e.login ? esc(t('pr.login_line', { role: word('role', e.login.role), phone: e.login.phone || e.phone || '' })) + (e.login.must_change_pin ? ` <span class="pill warn">${esc(t('pr.pin_pending'))}</span>` : '') : esc(t('pr.no_login_line'))}</p>
      ${owner && e.cash_allowed ? `<p class="o-note warn"><span aria-hidden="true">⚑</span> ${esc(t('pr.cash_allowed_on'))}</p>` : ''}
      ${actions}<h3>${esc(t('pr.sec_history'))}</h3><div id="ehist">${skeleton(3, 3)}</div>`;
    box.querySelectorAll('[data-a]').forEach(b => b.onclick = () => ({ reset: resetPin, login: giveLogin, cash: toggleCash, end: endEmployment, rehire })[b.dataset.a](e, ctl));
    try {
      const d = await api(`/api/employees/${encodeURIComponent(e.employee_id)}`);
      const ev = d.events || [];
      $('#ehist', box).innerHTML = ev.length ? `<div class="o-tw free"><table class="o-t"><thead><tr><th>${esc(t('pr.h_when'))}</th><th>${esc(t('pr.h_what'))}</th><th>${esc(t('pr.h_by'))}</th></tr></thead><tbody>${ev.map(x => `<tr><td class="dim">${esc(when(x.at))}</td><td>${esc(word('ev', x.kind))}</td><td>${esc(x.by_user || '—')}</td></tr>`).join('')}</tbody></table></div>`
        : `<p class="o-empty">${esc(t('pr.no_history'))}</p>`;
    } catch (x) { $('#ehist', box).innerHTML = `<p class="o-note crit">${esc(x.message)}</p>`; }
  }

  function resetPin(e, ctl) {
    confirmDialog({
      title: t('pr.reset_pin_q', { name: e.name }), body: `<p>${esc(t('pr.reset_pin_effect'))}</p>`, confirmLabel: t('pr.reset_pin_go'),
      onConfirm: async () => {
        const r = await post(`/api/employees/${encodeURIComponent(e.employee_id)}/login/reset-pin`, { generate: true });
        const lg = r.login || r; ctl.close();
        setTimeout(() => pinOnce({ name: e.name, phone: lg.phone || e.phone, pin: lg.pin_once, text: lg.whatsapp_text }), 0);   // after this dialog closes
      },
    });
  }

  function giveLogin(e) {   // replaces the employee panel with the login form
    panel({
      title: t('pr.give_login_for', { name: e.name }), submitLabel: t('pr.create_login'), body: loginFields({ phone: e.phone, role_hint: e.role_hint }, true),
      onOpen: c => wireLogin(c.el),
      onSubmit: async (d, form) => {
        const body = loginBody(d, e.phone); if (body.error) throw new Error(body.error);
        const r = await post(`/api/employees/${encodeURIComponent(e.employee_id)}/login`, body);
        wipePin(form);
        const lg = r.login || r;
        toast(t('pr.login_created', { name: e.name })); await load();
        if (lg.pin_once) setTimeout(() => pinOnce({ name: e.name, phone: lg.phone || body.phone, pin: lg.pin_once, text: lg.whatsapp_text }), 0);
      },
    });
  }

  function toggleCash(e, ctl) {
    const on = !e.cash_allowed;
    confirmDialog({
      title: on ? t('pr.cash_allow_q', { name: e.name }) : t('pr.cash_revoke_q', { name: e.name }),
      body: `<p>${esc(on ? t('pr.cash_allow_effect') : t('pr.cash_revoke_effect'))}</p>`, confirmLabel: on ? t('pr.cash_allow') : t('pr.cash_revoke'),
      onConfirm: async () => { await patch(`/api/employees/${encodeURIComponent(e.employee_id)}`, { cash_allowed: on }); toast(t('saved')); ctl.close(); await load(); },
    });
  }

  function endEmployment(e, ctl) {
    confirmDialog({
      title: t('pr.end_q', { name: e.name }), danger: true, confirmLabel: t('pr.end_employment'),
      body: `<p>${esc(t('pr.end_effect'))}</p>${e.login ? `<p><b>${esc(t('pr.end_login_effect'))}</b></p>` : ''}`,
      fields: `<div class="o-grid2">${field(t('pr.f_left_on'), 'left_on', todayPk(), 'required', 'date')}${field(t('pr.f_reason'), 'reason', '', 'required minlength="3" maxlength="120" placeholder="resigned, contract ended…"')}</div>`,
      onConfirm: async d => { await post(`/api/employees/${encodeURIComponent(e.employee_id)}/end`, { left_on: d.left_on, reason: d.reason.trim() }); toast(t('pr.ended', { name: e.name })); ctl.close(); await load(); },
    });
  }

  function rehire(e, ctl) {
    confirmDialog({
      title: t('pr.rehire_q', { name: e.name }), confirmLabel: t('pr.rehire'), body: `<p>${esc(t('pr.rehire_effect'))}</p>`,
      fields: field(t('pr.f_rejoined'), 'joined_on', todayPk(), 'required', 'date'),
      onConfirm: async d => { await post(`/api/employees/${encodeURIComponent(e.employee_id)}/rehire`, { joined_on: d.joined_on }); toast(t('pr.rehired', { name: e.name })); ctl.close(); await load(); },
    });
  }
}

// ---------------------------------------------------------------- the optional app login (shared by add + give login)
function loginFields(p, always = false) {
  const role = LOGIN_ROLES.includes(p.role_hint) ? p.role_hint : 'driver';
  return `<h3>${esc(t('pr.sec_login'))}</h3>
    ${always ? '<input type="hidden" name="want_login" value="on">' : check(t('pr.f_want_login'), 'want_login', false)}
    <div id="loginBox" ${always ? '' : 'hidden'} style="display:grid;gap:10px">
      <div class="o-grid2">${select(t('pr.f_login_role'), 'login_role', LOGIN_ROLES.map(r => [r, word('role', r)]), role)}${field(t('pr.f_login_phone'), 'login_phone', p.phone || '', 'inputmode="tel" placeholder="0301-1234567" maxlength="14"', 'tel')}</div>
      <fieldset class="o-card" style="display:grid;gap:4px;margin:0"><legend class="hint" style="padding:0 4px">${esc(t('pr.f_pin_how'))}</legend>
        <label class="chk"><input type="radio" name="pin_mode" value="generate" checked> ${esc(t('pr.pin_generate'))}</label>
        <label class="chk"><input type="radio" name="pin_mode" value="type"> ${esc(t('pr.pin_type'))}</label>
        <label class="f" id="pinTyped" hidden>${esc(t('pr.f_pin'))}<input class="input" type="password" name="pin" inputmode="numeric" autocomplete="new-password" pattern="\\d{4,6}" minlength="4" maxlength="6"></label>
        <p class="hint" style="margin:0">${esc(t('pr.pin_hint'))}</p></fieldset></div>`;
}
function wireLogin(form) {
  const box = $('#loginBox', form), want = form.querySelector('[name=want_login][type=checkbox]');
  if (want) want.onchange = () => { box.hidden = !want.checked; };
  $$('[name=pin_mode]', form).forEach(r => r.onchange = () => { const typed = form.querySelector('[name=pin_mode]:checked').value === 'type'; $('#pinTyped', form).hidden = !typed; form.pin.required = typed; if (!typed) form.pin.value = ''; });
}
function loginBody(d, fallbackPhone) {
  const phone = (d.login_phone || fallbackPhone || '').trim();
  if (!phone) return { error: t('pr.need_phone') };
  if (d.pin_mode === 'type') {
    if (!/^\d{4,6}$/.test(d.pin || '')) return { error: t('pr.pin_digits') };
    return { role: d.login_role, phone, pin: d.pin };
  }
  return { role: d.login_role, phone, generate: true };
}
const wipePin = form => { const i = form?.querySelector?.('[name=pin]'); if (i) i.value = ''; };

function wireBasis(form) {
  const b = form.querySelector('[name=pay_basis]'); if (!b) return;
  const sync = () => $$('[data-basis]', form).forEach(i => { const on = i.dataset.basis === b.value; i.closest('label').hidden = !on; i.required = on; });
  b.onchange = sync; sync();
}
function payTerms(d) {
  const amt = Number(d.pay_basis === 'daily' ? d.daily_rate : d.basic);
  if (d.basic === '' && d.daily_rate === '') return null;               // pay terms can be set later
  if (!(amt > 0)) return { error: t('pr.need_amount') };
  return { pay_basis: d.pay_basis, basic: d.pay_basis === 'monthly' ? amt : 0, daily_rate: d.pay_basis === 'daily' ? amt : 0 };
}
