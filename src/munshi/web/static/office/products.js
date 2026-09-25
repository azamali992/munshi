/* Products & prices: the catalogue as one dense table. Owner: inline list-price edit, bulk price change with a
   preview (old -> new) before anything is saved, add/edit in the side panel. Clerk: read-only, cost hidden.
   Every price change -- from here, the phone app's product form, a purchase or an import -- lands in price history. */
import { $, api, check, day, esc, field, isOwner, money, num, panel, patch, pill, post, select, signed, table, toast, when } from './lib.js';

const MODES = [['pct', '± %'], ['add', '± Rs'], ['set', 'Set price to Rs']];

export default async function products(view) {
  const owner = isOwner();
  let rows = await api('/api/office/products');
  let filter = 'active';
  view.header('Products & prices', `${rows.filter(r => r.active).length} active products · list prices in rupees`,
    owner ? '<button class="btn primary" id="addP">+ Add product</button>' : '');
  view.page.innerHTML = `
    ${owner ? '' : '<p class="o-note">Prices and products are set by the owner. You can look anything up here, including each product\'s price history and stock.</p>'}
    <div class="o-bar">
      <input class="input o-search" type="search" id="q" placeholder="Search name, SKU, category…" aria-label="Search products">
      <div class="chips" style="padding:0" role="group" aria-label="Show">${[['active', 'Active'], ['low', 'Low stock'], ['inactive', 'Inactive'], ['all', 'All']].map(([k, l]) => `<button class="chip" data-f="${k}" aria-pressed="${k === filter}">${l}</button>`).join('')}</div>
      <div class="grow"></div>
      ${owner ? `<div class="o-bar" id="bulk" hidden><b id="nsel"></b>
        <select class="input" id="mode" aria-label="Change type" style="width:auto">${MODES.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
        <input class="input num" id="val" type="number" step="any" placeholder="e.g. 5 or -2.5" aria-label="Amount" style="width:130px">
        <select class="input" id="round" aria-label="Round to" style="width:auto" title="Percentage changes are rounded to this"><option value="1">round to Rs 1</option><option value="5">to Rs 5</option><option value="10">to Rs 10</option><option value="0">no rounding</option></select>
        <button class="btn primary" id="prev">Preview change</button><button class="btn ghost" id="clr">Clear</button></div>` : ''}
    </div>
    <div class="o-tw" id="tbl"></div>`;

  const cols = [
    { key: 'name', label: 'Product', render: r => `<b>${esc(r.name)}</b>` },
    { key: 'sku', label: 'SKU', cls: 'mono' },
    { key: 'unit', label: 'Unit', cls: 'dim' },
    { key: 'category', label: 'Category', cls: 'dim' },
    { key: 'unit_price', label: 'List price', num: true, title: owner ? 'Click a price to change it; Enter saves, Esc cancels' : '',
      render: r => owner ? `<button type="button" class="o-price" data-price="${esc(r.sku)}" title="Change the list price of ${esc(r.name)}">${money(r.unit_price)}</button>` : money(r.unit_price) },
    ...(owner ? [
      { key: 'cost_price', label: 'Cost price', num: true, title: 'Last purchase cost (reference)', render: r => money(r.cost_price) },
      { key: 'avg_cost', label: 'Avg cost', num: true, title: 'Moving-average cost of the stock on hand -- what margin and valuation use', render: r => `<span class="dim">${money(r.avg_cost)}</span>` },
      { key: 'margin', label: 'Margin', num: true, value: r => r.unit_price ? (r.unit_price - r.avg_cost) / r.unit_price : null, title: 'List price vs average cost',
        render: r => r.unit_price ? `<span class="${r.unit_price < r.avg_cost ? 'down' : 'dim'}">${((r.unit_price - r.avg_cost) / r.unit_price * 100).toFixed(1)}%</span>` : '' },
    ] : []),
    { key: 'on_hand', label: 'Stock', num: true, render: r => `${num(r.on_hand)}${r.reserved ? ` <span class="dim" title="reserved for allocated orders">(${num(r.reserved)} res.)</span>` : ''}` },
    { key: 'low', label: 'Low', value: r => r.low ? 1 : 0, render: r => r.low ? `<span class="pill crit" title="${esc(r.low_godowns.map(g => `${g.warehouse_id}: ${g.available} available (min ${r.min_stock})`).join('; '))}">low</span>` : '' },
    { key: 'price_changed_at', label: 'Price changed', render: r => `<span class="dim">${r.price_changed_at ? day(r.price_changed_at) : '—'}</span>` },
    { key: 'active', label: 'Active', value: r => r.active ? 1 : 0, render: r => r.active ? pill('active', 'good') : pill('inactive') },
  ];
  const filters = { active: r => r.active, low: r => r.low, inactive: r => !r.active, all: () => true };
  const t = table($('#tbl'), {
    columns: cols, rows, key: 'sku', sort: { key: 'name', dir: 'asc' }, searchKeys: ['name', 'sku', 'category', 'unit'], filter: filters[filter],
    selectable: owner, onSelect: s => paintBulk(s), onRow: r => productPanel(r), rowClass: r => r.active ? '' : 'off',
  });
  const reload = async () => { rows = await api('/api/office/products'); t.setRows(rows); };

  $('#q').oninput = e => t.setSearch(e.target.value);
  view.page.querySelectorAll('[data-f]').forEach(b => b.onclick = () => {
    filter = b.dataset.f; view.page.querySelectorAll('[data-f]').forEach(x => x.setAttribute('aria-pressed', String(x === b))); t.setFilter(filters[filter]);
  });
  if (owner) {
    $('#addP').onclick = () => productPanel(null);
    $('#clr').onclick = () => t.clearSelection();
    $('#prev').onclick = () => bulkPreview();
    $('#val').onkeydown = e => { if (e.key === 'Enter') bulkPreview(); };
    // inline list-price edit
    $('#tbl').addEventListener('click', e => {
      const b = e.target.closest('[data-price]'); if (!b) return;
      const r = rows.find(x => x.sku === b.dataset.price); if (!r) return;
      const inp = document.createElement('input');
      Object.assign(inp, { type: 'number', step: 'any', min: '0.01', value: r.unit_price, className: 'input num', title: 'Enter saves, Esc cancels' });
      inp.style.width = '110px'; inp.setAttribute('aria-label', `New list price for ${r.name}`);
      b.replaceWith(inp); inp.focus(); inp.select();
      let done = false;
      const cancel = () => { if (!done) { done = true; t.render(); } };
      inp.onkeydown = async ev => {
        if (ev.key === 'Escape') return cancel();
        if (ev.key !== 'Enter') return;
        const v = Number(inp.value);
        if (!(v > 0)) { toast('A list price must be above zero.', 4000); return; }
        if (v === r.unit_price) return cancel();
        done = true; inp.disabled = true;
        try {
          await post('/api/office/prices/apply', { skus: [r.sku], mode: 'set', value: v, expected: { [r.sku]: r.unit_price } });
          toast(`${r.name}: ${money(r.unit_price)} → ${money(v)} saved`); await reload();
        } catch (x) { toast(x.message, 6000); await reload(); }
      };
      inp.onblur = () => setTimeout(cancel, 150);
    });
  }

  function paintBulk(sel) {
    if (!owner) return;
    $('#bulk').hidden = !sel.size; $('#nsel').textContent = `${sel.size} selected:`;
  }

  async function bulkPreview() {
    const skus = [...t.selected]; const mode = $('#mode').value; const value = Number($('#val').value); const round_to = Number($('#round').value);
    if (!skus.length) return;
    if ($('#val').value === '' || !isFinite(value)) { toast('Enter the change first (e.g. 5 for +5%, -100 for Rs 100 less).', 4000); $('#val').focus(); return; }
    let pv;
    try { pv = await post('/api/office/prices/preview', { skus, mode, value, round_to }); } catch (x) { toast(x.message, 6000); return; }
    const label = mode === 'pct' ? `${value > 0 ? '+' : ''}${value}%${round_to ? `, rounded to Rs ${round_to}` : ''}` : mode === 'add' ? `${value > 0 ? '+' : ''}${money(value)} each` : `set to ${money(value)}`;
    const bad = pv.rows.filter(r => r.error);
    panel({
      title: `New list prices: ${label}`, wide: true, submitLabel: bad.length ? 'Fix the errors first' : `Save ${pv.changes} new price${pv.changes === 1 ? '' : 's'}`,
      body: `<p class="o-note">Nothing is saved yet. Check old → new, then save. Each change is recorded in the product's price history with your name.</p>
        ${bad.length ? `<p class="o-note crit">${bad.length} product${bad.length === 1 ? '' : 's'} can't take this change -- see the red rows.</p>` : ''}
        <div class="o-tw free"><table class="o-t"><thead><tr><th>Product</th><th class="n">Old</th><th class="n">New</th><th class="n">Change</th><th class="n">%</th></tr></thead><tbody>
        ${pv.rows.map(r => `<tr class="${r.error ? 'err' : r.unchanged ? 'off' : ''}"><td class="wrap"><b>${esc(r.name)}</b> <span class="mono">${esc(r.sku)}</span>${r.error ? `<br><span class="crit">${esc(r.error)}</span>` : r.unchanged ? '<br><span class="dim">no change</span>' : ''}</td>
          <td class="n">${money(r.old)}</td><td class="n"><b>${money(r.new)}</b></td><td class="n ${r.change > 0 ? 'up' : r.change < 0 ? 'down' : ''}">${r.change > 0 ? '+' : ''}${money(r.change)}</td><td class="n dim">${r.change_pct === null ? '' : (r.change_pct > 0 ? '+' : '') + r.change_pct + '%'}</td></tr>`).join('')}
        </tbody></table></div>
        ${field('Reason (optional, kept in the audit trail)', 'reason', '', 'maxlength="120" placeholder="e.g. new depot rate from FFC"')}`,
      onOpen: p => { if (bad.length || !pv.changes) p.el.querySelector('button[type=submit]').disabled = true; },
      onSubmit: async d => {
        const expected = Object.fromEntries(pv.rows.map(r => [r.sku, r.old]));
        try {
          const r = await post('/api/office/prices/apply', { skus, mode, value, round_to, expected, reason: d.reason || '' });
          toast(`${r.saved} price${r.saved === 1 ? '' : 's'} saved`); t.clearSelection(); $('#val').value = ''; await reload();
        } catch (x) { if (x.status === 409) await reload(); throw x; }
      },
    });
  }

  // ---------------------------------------------------------------- product side panel
  async function productPanel(r) {
    const isNew = !r;
    const p = r || { sku: '', name: '', unit: 'bag', category: '', unit_price: '', cost_price: 0, min_stock: 10, units_per_load: 1, aliases: [], active: true };
    const units = ['bag', 'ltr', 'kg', 'pc', 'box'];
    const form = owner ? `
      <div class="o-grid2">${field('Name', 'name', p.name, 'required minlength="2" maxlength="80"')}${field('SKU (your code)', 'sku', p.sku, isNew ? 'maxlength="24" placeholder="blank = automatic" style="text-transform:uppercase"' : 'readonly')}</div>
      <div class="o-grid3">${select('Unit', 'unit', units.includes(p.unit) ? units.map(u => [u, u]) : [[p.unit, p.unit], ...units.map(u => [u, u])], p.unit)}${field('Category', 'category', p.category, 'maxlength="40" list="cats"')}${field('Low-stock alert at', 'min_stock', p.min_stock, 'min="0" step="1" required', 'number')}</div>
      <datalist id="cats">${[...new Set(rows.map(x => x.category).filter(Boolean))].map(c => `<option value="${esc(c)}">`).join('')}</datalist>
      <div class="o-grid3">${field('List price (Rs)', 'unit_price', p.unit_price, 'min="0.01" step="any" required', 'number')}${field('Cost price (Rs)', 'cost_price', p.cost_price, 'min="0" step="any"', 'number')}${field('Van space per unit', 'units_per_load', p.units_per_load, 'min="1" step="1" required', 'number')}</div>
      ${field('Other names people say (comma-separated, Urdu welcome)', 'aliases', (p.aliases || []).join(', '), 'maxlength="300"')}
      ${check('Active (sold, shown to salesmen)', 'active', p.active)}
      ${isNew ? '' : '<p class="hint" style="margin:0">A list-price or cost change here is recorded in the price history below.</p>'}`
      : `<div class="o-kv"><b>SKU</b><span class="mono">${esc(p.sku)}</span><b>Unit</b><span>${esc(p.unit)}</span><b>Category</b><span>${esc(p.category || '—')}</span>
         <b>List price</b><span>${money(p.unit_price)}</span><b>Low-stock alert at</b><span>${num(p.min_stock)}</span><b>Other names</b><span>${esc((p.aliases || []).join(', ') || '—')}</span>
         <b>Status</b><span>${p.active ? 'active' : 'inactive'}</span></div>`;
    const ctl = panel({
      title: isNew ? 'Add product' : p.name, wide: !isNew, submitLabel: isNew ? 'Add product' : 'Save changes',
      body: form + (isNew ? '' : '<div id="pdetail"><p class="muted">Loading stock and price history…</p></div>'),
      onSubmit: owner ? async d => {
        const body = { sku: (d.sku || '').trim().toUpperCase(), name: d.name.trim(), unit: d.unit, category: d.category.trim(), unit_price: Number(d.unit_price), cost_price: Number(d.cost_price || 0),
          min_stock: Number(d.min_stock), units_per_load: Number(d.units_per_load), aliases: d.aliases.split(',').map(s => s.trim()).filter(Boolean), active: d.active };
        if (!(body.unit_price > 0)) throw new Error('The list price must be above zero.');
        // the add route saves over an existing SKU; adding must never overwrite a product by accident
        if (isNew && body.sku && rows.some(x => x.sku === body.sku)) throw new Error(`${body.sku} is already ${rows.find(x => x.sku === body.sku).name} -- pick another SKU or leave it blank.`);
        if (isNew) { await post('/api/products', body); toast(`${body.name} added`); }
        else { await patch(`/api/products/${encodeURIComponent(p.sku)}`, body); toast(`${body.name} saved`); }
        await reload();
      } : null,
    });
    if (isNew) return;
    const [hist, mv] = await Promise.all([api(`/api/office/products/${encodeURIComponent(p.sku)}/price-history`), api(`/api/office/stock/moves?sku=${encodeURIComponent(p.sku)}&limit=8`)]);
    const box = ctl.body.querySelector('#pdetail'); if (!box) return;
    box.innerHTML = `<h3>Stock by godown</h3>
      <div class="o-tw free"><table class="o-t"><thead><tr><th>Godown</th><th class="n">On hand</th><th class="n">Reserved</th><th class="n">Available</th></tr></thead><tbody>
      ${mv.levels.length ? mv.levels.map(l => `<tr><td class="mono">${esc(l.warehouse_id)}</td><td class="n">${num(l.on_hand)}</td><td class="n dim">${num(l.reserved)}</td><td class="n"><b>${num(l.on_hand - l.reserved)}</b></td></tr>`).join('') : '<tr><td colspan="4" class="o-empty">No stock anywhere yet.</td></tr>'}</tbody></table></div>
      <h3>Price history</h3>
      <div class="o-tw free"><table class="o-t"><thead><tr><th>When</th><th>Price</th><th class="n">Old</th><th class="n">New</th><th class="n">Change</th><th>By</th><th>Via</th></tr></thead><tbody>
      ${hist.length ? hist.map(h => `<tr><td class="dim">${esc(when(h.at))}</td><td>${h.field === 'unit_price' ? 'list' : 'cost'}</td><td class="n">${money(h.old)}</td><td class="n"><b>${money(h.new)}</b></td>
        <td class="n ${h.change > 0 ? 'up' : 'down'}">${h.change > 0 ? '+' : ''}${money(h.change)}</td><td>${esc(h.by || '—')}</td><td class="dim">${esc(h.source)}</td></tr>`).join('')
        : '<tr><td colspan="7" class="o-empty">No price changes recorded yet.</td></tr>'}</tbody></table></div>
      <h3>Recent stock movements <a class="link" href="#/inventory/moves/${encodeURIComponent(p.sku)}" style="font-size:13px;margin-inline-start:8px">All movements ›</a></h3>
      <div class="o-tw free"><table class="o-t"><thead><tr><th>When</th><th>Godown</th><th>What</th><th class="n">Qty</th><th class="n">Balance</th><th>Ref</th></tr></thead><tbody>
      ${mv.moves.length ? mv.moves.map(m => `<tr><td class="dim">${esc(when(m.at))}</td><td class="mono">${esc(m.warehouse_id)}</td><td>${esc(m.kind.replace('_', ' '))}</td><td class="n ${m.delta > 0 ? 'up' : 'down'}">${signed(m.delta)}</td><td class="n">${num(m.balance_after)}</td><td class="dim">${esc(m.ref)}</td></tr>`).join('')
        : '<tr><td colspan="6" class="o-empty">No movements yet.</td></tr>'}</tbody></table></div>`;
  }
}
