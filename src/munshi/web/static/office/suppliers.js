/* Suppliers (list, add/edit, what we owe them, their khata) and Godowns (list, add). Both go through the existing
   routes: /api/suppliers (purchases:read / purchases:write), /api/payables, /api/warehouses (adding one is owner-only). */
import { $, api, day, esc, field, isOwner, money, num, panel, patch, pill, post, table, toast } from './lib.js';

export async function suppliers(view) {
  let rows = await api('/api/suppliers');
  const total = rows.reduce((a, r) => a + Math.max(0, Math.round(r.balance * 100)), 0) / 100;
  view.header('Suppliers', `${rows.length} suppliers · what we owe them`, '<button class="btn primary" id="addS">+ Add supplier</button>');
  view.page.innerHTML = `<div class="o-kpis"><div class="o-kpi"><div class="k">Payables</div><div class="v">${money(total)}</div><div class="s">${rows.filter(r => r.balance > 0).length} suppliers we owe</div></div></div>
    <div class="o-bar"><input class="input o-search" type="search" id="q" placeholder="Search supplier…" aria-label="Search suppliers"><span class="hint">Click a supplier for their khata. Paying a supplier is the owner's, in the phone app (Suppliers → Pay).</span></div>
    <div class="o-tw" id="st"></div>`;
  const t = table($('#st'), {
    rows, key: 'supplier_id', sort: { key: 'name', dir: 'asc' }, searchKeys: ['name', 'phone', 'address', 'supplier_id'], onRow: r => supplierPanel(r),
    columns: [
      { key: 'name', label: 'Supplier', render: r => `<b>${esc(r.name)}</b> <span class="mono">${esc(r.supplier_id)}</span>` },
      { key: 'phone', label: 'Phone', cls: 'mono' },
      { key: 'address', label: 'Address', cls: 'dim' },
      { key: 'balance', label: 'We owe', num: true, render: r => r.balance > 0 ? `<b>${money(r.balance)}</b>` : `<span class="dim">${money(r.balance)}</span>` },
    ],
    footer: rs => `<tr><td colspan="3">Total</td><td class="n">${money(rs.reduce((a, r) => a + r.balance, 0))}</td></tr>`,
  });
  $('#q').oninput = e => t.setSearch(e.target.value);
  const reload = async () => { rows = await api('/api/suppliers'); t.setRows(rows); };
  $('#addS').onclick = () => editPanel(null, reload);

  async function supplierPanel(s) {
    const ctl = panel({ title: s.name, wide: true, body: '<p class="muted">Loading…</p>', footer: '<button type="button" class="btn" id="editS">Edit supplier</button>' });
    ctl.el.querySelector('#editS').onclick = () => editPanel(s, reload);
    const k = await api(`/api/suppliers/${encodeURIComponent(s.supplier_id)}/khata`);
    // the khata route returns the last 10 entries: balances are walked forward from what was owed before them
    const recent = [...k.recent].sort((a, b) => a.created_at.localeCompare(b.created_at));
    let bal = Math.round(k.balance * 100) - recent.reduce((a, e) => a + Math.round(e.amount * 100), 0);
    const rows2 = recent.map(e => { bal += Math.round(e.amount * 100); return { ...e, running: bal / 100 }; });
    ctl.body.innerHTML = `<div class="o-kv"><b>Phone</b><span class="mono">${esc(s.phone || '—')}</span><b>Address</b><span>${esc(s.address || '—')}</span><b>We owe</b><span><b>${money(k.balance)}</b></span></div>
      <h3>Khata <span class="hint">(last ${recent.length} entries)</span></h3><div class="o-tw free" id="skt"></div>`;
    table(ctl.body.querySelector('#skt'), {
      rows: rows2, key: 'entry_id', empty: 'No bills or payments yet.',
      columns: [
        { key: 'created_at', label: 'Date', render: e => esc(day(e.created_at)), sortable: false },
        { key: 'kind', label: 'Entry', sortable: false, render: e => `${esc(e.kind)}${e.reversal_of ? ' ' + pill('reversal', 'warn') : ''}${e.method ? ` <span class="dim">· ${esc(e.method)}</span>` : ''}` },
        { key: 'ref', label: 'Ref', cls: 'dim', sortable: false },
        { key: 'bill', label: 'Bill', num: true, sortable: false, render: e => e.amount > 0 ? money(e.amount) : '' },
        { key: 'paid', label: 'Paid', num: true, sortable: false, render: e => e.amount < 0 ? money(-e.amount) : '' },
        { key: 'running', label: 'Balance', num: true, sortable: false, render: e => `<b>${money(e.running)}</b>` },
      ],
    });
  }
}

function editPanel(s, done) {
  const isNew = !s; const x = s || { name: '', phone: '', address: '' };
  panel({
    title: isNew ? 'Add supplier' : `Edit ${x.name}`, submitLabel: isNew ? 'Add supplier' : 'Save changes',
    body: `${field('Name', 'name', x.name, 'required minlength="2" maxlength="80"')}
      <div class="o-grid2">${field('Phone', 'phone', x.phone, 'type="tel" maxlength="20"')}${isNew ? field('Opening balance (Rs we owe them today)', 'opening_balance', 0, 'min="0" step="any"', 'number') : '<div></div>'}</div>
      ${field('Address', 'address', x.address, 'maxlength="160"')}`,
    onSubmit: async d => {
      const body = { name: d.name.trim(), phone: d.phone.trim(), address: d.address.trim(), opening_balance: isNew ? Number(d.opening_balance || 0) : 0 };
      if (isNew) await post('/api/suppliers', body); else await patch(`/api/suppliers/${encodeURIComponent(x.supplier_id)}`, body);
      toast(isNew ? `${body.name} added` : 'Supplier saved'); await done();
    },
  });
}

export async function godowns(view) {
  const owner = isOwner();
  const [whs, stock] = await Promise.all([api('/api/warehouses'), api('/api/office/stock')]);
  const info = Object.fromEntries(stock.warehouses.map(w => [w.warehouse_id, w]));
  const rows = whs.map(w => ({ ...w, units: info[w.warehouse_id]?.units ?? 0, value: info[w.warehouse_id]?.value ?? null,
    products: stock.rows.filter(r => r.cells[w.warehouse_id]?.on_hand > 0).length }));
  view.header('Godowns', `${whs.length} godown${whs.length === 1 ? '' : 's'}`, owner ? '<button class="btn primary" id="addW">+ Add godown</button>' : '');
  view.page.innerHTML = `${owner ? '' : '<p class="o-note">Adding a godown is the owner\'s.</p>'}<div class="o-tw free" id="gt"></div>
    <p class="hint">The default godown is where orders are allocated from and purchases arrive unless you pick another (change it in the phone app: Settings).</p>`;
  table($('#gt'), {
    rows, key: 'warehouse_id', sort: { key: 'name', dir: 'asc' }, onRow: w => { location.hash = '#/inventory'; },
    columns: [
      { key: 'name', label: 'Godown', render: w => `<b>${esc(w.name)}</b>${w.default ? ' ' + pill('default', 'acc') : ''}` },
      { key: 'warehouse_id', label: 'Code', cls: 'mono' },
      { key: 'products', label: 'Products in stock', num: true },
      { key: 'units', label: 'Units on hand', num: true, render: w => num(w.units) },
      ...(owner ? [{ key: 'value', label: 'Value at cost', num: true, render: w => money(w.value) }] : []),
    ],
  });
  if (owner) $('#addW').onclick = () => panel({
    title: 'Add godown', submitLabel: 'Add godown',
    body: `${field('Name', 'name', '', 'required minlength="2" maxlength="60" placeholder="e.g. Khanewal Godown"')}${field('Code (optional)', 'warehouse_id', '', 'maxlength="20" placeholder="blank = WH-<NAME>" style="text-transform:uppercase"')}`,
    onSubmit: async d => {
      const code = d.warehouse_id.trim().toUpperCase();
      // the existing route saves over a godown with the same code (a rename); adding must not do that by accident
      const auto = 'WH-' + (d.name.trim().toUpperCase().replace(/[^A-Z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 16) || 'X');   // the server's own rule
      const eff = code || auto;
      if (whs.some(w => w.warehouse_id === eff)) throw new Error(`${eff} is already a godown (${whs.find(w => w.warehouse_id === eff).name}) -- ${code ? 'pick another code' : 'type a different code'}.`);
      const w = await post('/api/warehouses', { name: d.name.trim(), warehouse_id: d.warehouse_id.trim() }); toast(`${w.name} added (${w.warehouse_id})`); window.dispatchEvent(new HashChangeEvent('hashchange')); },
  });
}
