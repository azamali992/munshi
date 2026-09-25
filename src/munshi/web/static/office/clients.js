/* Clients: every customer with balance, days overdue and credit use in one table; add/edit in the side panel through
   the existing customer routes (whose rule stands: raising or removing a credit limit needs the owner -- the refusal
   is shown as the server words it). A client's page: khata with running balance, open orders, statement link. */
import { $, api, check, day, esc, field, isOwner, money, num, openLink, panel, patch, pill, post, select, table, toast } from './lib.js';

const OPEN = ['draft', 'confirmed', 'allocated', 'dispatched'];
const STATUS = { draft: '', confirmed: 'acc', allocated: 'acc', dispatched: 'warn', delivered: 'good', short: 'crit', cancelled: 'crit' };

export default async function clients(view, [id]) {
  const routes = await api('/api/routes');
  if (id) return clientPage(view, id, routes);
  let rows = await api('/api/office/clients');
  let filter = 'active';
  const act = rows.filter(r => r.active);
  const recv = act.reduce((a, r) => a + Math.max(0, Math.round(r.balance * 100)), 0) / 100;
  const overdue = act.filter(r => r.days_overdue > 0);
  view.header('Clients', `${act.length} active clients`, '<button class="btn primary" id="addC">+ Add client</button>');
  view.page.innerHTML = `<div class="o-kpis">
      <div class="o-kpi"><div class="k">Receivable</div><div class="v">${money(recv)}</div><div class="s">${act.filter(r => r.balance > 0).length} clients owe</div></div>
      <div class="o-kpi"><div class="k">Overdue</div><div class="v ${overdue.length ? 'crit' : ''}">${money(overdue.reduce((a, r) => a + r.overdue_balance, 0))}</div><div class="s">${overdue.length} clients past due</div></div>
      <div class="o-kpi"><div class="k">Over credit limit</div><div class="v ${act.some(r => r.over_limit) ? 'crit' : ''}">${act.filter(r => r.over_limit).length}</div><div class="s">balance above the limit</div></div>
      <div class="o-kpi"><div class="k">Open orders</div><div class="v">${act.reduce((a, r) => a + r.open_orders, 0)}</div><div class="s">draft to dispatched</div></div></div>
    <div class="o-bar"><input class="input o-search" type="search" id="q" placeholder="Search name, phone, route, address…" aria-label="Search clients">
      <div class="chips" style="padding:0" role="group" aria-label="Show">${[['active', 'Active'], ['owes', 'Owes'], ['overdue', 'Overdue'], ['over', 'Over limit'], ['inactive', 'Inactive'], ['all', 'All']].map(([k, l]) => `<button class="chip" data-f="${k}" aria-pressed="${k === filter}">${l}</button>`).join('')}</div></div>
    <div class="o-tw" id="ct"></div>`;
  const filters = { active: r => r.active, owes: r => r.active && r.balance > 0, overdue: r => r.active && r.days_overdue > 0, over: r => r.active && r.over_limit, inactive: r => !r.active, all: () => true };
  const t = table($('#ct'), {
    rows, key: 'customer_id', sort: { key: 'name', dir: 'asc' }, searchKeys: ['name', 'phone', 'route_name', 'address', 'customer_id'], filter: filters[filter],
    onRow: r => { location.hash = `#/clients/${encodeURIComponent(r.customer_id)}`; }, rowClass: r => r.active ? '' : 'off',
    columns: [
      { key: 'name', label: 'Client', render: r => `<b>${esc(r.name)}</b> <span class="mono">${esc(r.customer_id)}</span>` },
      { key: 'phone', label: 'Phone', cls: 'mono' },
      { key: 'route_name', label: 'Route', render: r => esc(r.route_name || '—') },
      { key: 'credit_limit', label: 'Credit limit', num: true, render: r => r.credit_limit ? money(r.credit_limit) : '<span class="dim">no limit</span>' },
      { key: 'balance', label: 'Balance', num: true, render: r => `<b class="${r.over_limit ? 'down' : ''}">${money(r.balance)}</b>` },
      { key: 'days_overdue', label: 'Days overdue', num: true, render: r => r.days_overdue ? `<span class="pill ${r.days_overdue > 30 ? 'crit' : 'warn'}">${r.days_overdue} d</span>` : '<span class="dim">—</span>' },
      { key: 'credit_used_pct', label: 'Limit used', num: true, render: r => r.credit_used_pct === null ? '' : `<span class="${r.over_limit ? 'down' : 'dim'}">${r.credit_used_pct}%</span>` },
      { key: 'open_orders', label: 'Open orders', num: true, render: r => r.open_orders || '<span class="dim">0</span>' },
      { key: 'active', label: 'Active', value: r => r.active ? 1 : 0, render: r => r.active ? pill('active', 'good') : pill('inactive') },
    ],
  });
  $('#q').oninput = e => t.setSearch(e.target.value);
  view.page.querySelectorAll('[data-f]').forEach(b => b.onclick = () => { filter = b.dataset.f; view.page.querySelectorAll('[data-f]').forEach(x => x.setAttribute('aria-pressed', String(x === b))); t.setFilter(filters[filter]); });
  $('#addC').onclick = () => clientPanel(null, routes, async c => { toast(`${c.name} added`); rows = await api('/api/office/clients'); t.setRows(rows); });
}

// ---------------------------------------------------------------- add / edit
export function clientPanel(c, routes, done) {
  const isNew = !c;
  const x = c || { name: '', phone: '', address: '', route_id: '', tier: 'standard', credit_limit: 0, credit_days: 30, discount_pct: 0, language: 'ur-en', active: true };
  panel({
    title: isNew ? 'Add client' : `Edit ${x.name}`, submitLabel: isNew ? 'Add client' : 'Save changes',
    body: `<div class="o-grid2">${field('Name', 'name', x.name, 'required minlength="2" maxlength="80"')}${field('Phone', 'phone', x.phone, 'type="tel" maxlength="20" placeholder="0300-1234567"')}</div>
      ${field('Address', 'address', x.address, 'maxlength="160"')}
      <div class="o-grid2">${select('Route', 'route_id', [['', '— no route —'], ...routes.map(r => [r.route_id, r.name])], x.route_id || '')}${select('Tier', 'tier', [['standard', 'Standard'], ['wholesale', 'Wholesale'], ['vip', 'VIP']], x.tier)}</div>
      <div class="o-grid3">${field('Credit limit (Rs, 0 = no limit)', 'credit_limit', x.credit_limit, 'min="0" step="any"', 'number')}${field('Credit days', 'credit_days', x.credit_days, 'min="0" max="365" step="1"', 'number')}${field('Standing discount %', 'discount_pct', x.discount_pct, 'min="0" max="50" step="any"', 'number')}</div>
      ${isOwner() ? '' : '<p class="hint" style="margin:0">You can lower a credit limit; raising or removing one needs the owner.</p>'}
      <div class="o-grid2">${select('Reminder language', 'language', [['ur-en', 'Roman Urdu + English'], ['en', 'English']], x.language)}${isNew ? field('Opening balance (Rs they owe today)', 'opening_balance', 0, 'min="0" step="any"', 'number') : '<div></div>'}</div>
      ${check('Active', 'active', x.active)}`,
    onSubmit: async d => {
      const body = { name: d.name.trim(), phone: d.phone.trim(), address: d.address.trim(), route_id: d.route_id || null, tier: d.tier, credit_limit: Number(d.credit_limit || 0),
        credit_days: Number(d.credit_days || 0), discount_pct: Number(d.discount_pct || 0), language: d.language, active: d.active };
      if (isNew) body.opening_balance = Number(d.opening_balance || 0);
      const r = isNew ? await post('/api/customers', body) : await patch(`/api/customers/${encodeURIComponent(x.customer_id)}`, body);
      await done(r);
    },
  });
}

// ---------------------------------------------------------------- one client
async function clientPage(view, id, routes) {
  const k = await api(`/api/khata/${encodeURIComponent(id)}`);
  const c = k.customer, a = k.aging;
  const route = routes.find(r => r.route_id === c.route_id);
  view.header(c.name, `<a class="link" href="#/clients">‹ All clients</a> · <span class="mono">${esc(c.customer_id)}</span>${c.active ? '' : ' · inactive'}`,
    '<button class="btn" id="stmt">Statement ↗</button><button class="btn primary" id="edit">Edit client</button>');
  let bal = 0;
  const ledger = k.ledger.map(e => { bal += Math.round(e.amount * 100); return { ...e, running: bal / 100 }; });
  const open = k.orders.filter(o => OPEN.includes(o.status));
  const used = c.credit_limit ? Math.round(k.outstanding / c.credit_limit * 1000) / 10 : null;
  view.page.innerHTML = `<div class="o-kpis">
      <div class="o-kpi"><div class="k">Balance</div><div class="v ${c.credit_limit && k.outstanding > c.credit_limit ? 'crit' : ''}">${money(k.outstanding)}</div><div class="s">${k.outstanding < 0 ? 'in advance (we owe them)' : 'they owe'}</div></div>
      <div class="o-kpi"><div class="k">Credit limit</div><div class="v">${c.credit_limit ? money(c.credit_limit) : 'no limit'}</div><div class="s">${used === null ? '' : `${used}% used`} · ${c.credit_days} days</div></div>
      <div class="o-kpi"><div class="k">Oldest overdue</div><div class="v ${a && a.days_overdue > 30 ? 'crit' : ''}">${a && a.days_overdue ? a.days_overdue + ' days' : '—'}</div><div class="s">${a ? `bucket ${esc(a.bucket)}` : 'nothing unpaid'}</div></div>
      <div class="o-kpi"><div class="k">Open orders</div><div class="v">${open.length}</div><div class="s">${money(open.reduce((s, o) => s + o.total, 0))}</div></div></div>
    <div class="o-card"><div class="o-kv"><b>Phone</b><span class="mono">${esc(c.phone || '—')}</span><b>Address</b><span>${esc(c.address || '—')}</span><b>Route</b><span>${esc(route ? route.name : '—')}</span>
      <b>Tier</b><span>${esc(c.tier)}</span><b>Standing discount</b><span>${c.discount_pct}%</span>${k.promise ? `<b>Promise</b><span>${money(k.promise.amount)} by ${esc(k.promise.promised_date)}</span>` : ''}</div></div>
    <h2 style="margin:6px 0 0">Khata</h2><p class="hint" style="margin:0">Oldest first. Click an entry to open its invoice or receipt.</p>
    <div class="o-tw" id="kt"></div>
    <h2 style="margin:6px 0 0">Open orders</h2><div class="o-tw free" id="ot"></div>
    <h2 style="margin:6px 0 0">Recent orders</h2><div class="o-tw free" id="rt"></div>`;
  table($('#kt'), {
    rows: ledger, key: 'entry_id', empty: 'No khata entries yet.',
    onRow: e => openLink(async () => (await api(`/api/documents/${encodeURIComponent(e.entry_id)}`)).public_url),
    rowClass: e => e.reversal_of ? 'off' : '',
    columns: [
      { key: 'created_at', label: 'Date', render: e => esc(day(e.created_at)), sortable: false },
      { key: 'doc_no', label: 'No.', cls: 'mono', render: e => esc(e.doc_no || e.entry_id), sortable: false },
      { key: 'kind', label: 'Entry', sortable: false, render: e => `${esc(e.kind === 'invoice' && e.method === 'adjustment' ? 'opening balance' : e.kind.replace('_', ' '))}${e.reversal_of ? ` ${pill('reversal', 'warn')}` : ''}${e.method && e.method !== 'adjustment' ? ` <span class="dim">· ${esc(e.method)}</span>` : ''}` },
      { key: 'ref', label: 'Ref', cls: 'dim', sortable: false },
      { key: 'due_date', label: 'Due', cls: 'dim', sortable: false, render: e => esc(e.due_date || '') },
      { key: 'debit', label: 'Debit', num: true, sortable: false, render: e => e.amount > 0 ? money(e.amount) : '' },
      { key: 'credit', label: 'Credit', num: true, sortable: false, render: e => e.amount < 0 ? money(-e.amount) : '' },
      { key: 'running', label: 'Balance', num: true, sortable: false, render: e => `<b>${money(e.running)}</b>` },
    ],
    footer: rows => `<tr><td colspan="5">Balance</td><td class="n">${money(rows.filter(e => e.amount > 0).reduce((s, e) => s + e.amount, 0))}</td><td class="n">${money(-rows.filter(e => e.amount < 0).reduce((s, e) => s + e.amount, 0))}</td><td class="n">${money(k.outstanding)}</td></tr>`,
  });
  const orderCols = [
    { key: 'order_id', label: 'Order', cls: 'mono' },
    { key: 'created_at', label: 'Date', render: o => esc(day(o.created_at)) },
    { key: 'status', label: 'Status', render: o => pill(o.status, STATUS[o.status] || '') },
    { key: 'items', label: 'Items', sortable: false, cls: 'wrap', render: o => `<span class="dim">${(o.lines || o.items || []).map(i => `${num(i.qty)} × ${esc(i.name || i.sku)}`).join(', ')}</span>` },
    { key: 'total', label: 'Total', num: true, render: o => money(o.total) },
  ];
  table($('#ot'), { rows: open, key: 'order_id', columns: orderCols, empty: 'No open orders.' });
  table($('#rt'), { rows: k.orders.filter(o => !OPEN.includes(o.status)).slice(0, 10), key: 'order_id', columns: orderCols, empty: 'No delivered or cancelled orders yet.' });
  $('#stmt').onclick = () => openLink(async () => (await api(`/api/statements/${encodeURIComponent(id)}/link`)).url);
  $('#edit').onclick = () => clientPanel(c, routes, async () => { toast('Client saved'); window.dispatchEvent(new HashChangeEvent('hashchange')); });
}
