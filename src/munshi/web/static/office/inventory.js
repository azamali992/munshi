/* Inventory: stock by product x godown (valuation for the owner), physical stock count, movement history, purchases.
   Every action goes through the existing ledger rules: adjustments (owner) -> POST /api/stock/adjust, transfers ->
   POST /api/stock/transfer, purchases -> POST /api/purchases, a posted count -> /api/office/stock-count/post, which
   books each difference as an adjustment. Nothing here can take stock below zero; the server refuses it. */
import { $, $$, api, day, esc, field, isOwner, money, num, panel, pill, post, select, signed, store, table, toast, todayPk, when } from './lib.js';

const TABS = [['', 'Stock by godown'], ['count', 'Physical count'], ['moves', 'Movements'], ['purchases', 'Purchases']];
const KIND = { sale: 'sale', return: 'return', purchase: 'purchase in', purchase_reversal: 'purchase return', adjust: 'adjustment', transfer_out: 'transfer out', transfer_in: 'transfer in', opening: 'opening', load: 'loaded' };

export default async function inventory(view, [tab = '', arg = '']) {
  const owner = isOwner();
  view.header('Inventory', owner ? 'Stock, value, counts and movements -- every change goes through the stock ledger' : 'Stock, counts and movements -- every change goes through the stock ledger',
    `<button class="btn" id="aPur">Receive purchase</button><button class="btn" id="aTrf">Transfer</button>${owner ? '<button class="btn" id="aAdj">Adjust stock</button>' : ''}`);
  view.page.innerHTML = `<nav class="o-tabs" aria-label="Inventory">${TABS.map(([k, l]) => `<a href="#/inventory${k ? '/' + k : ''}" class="${k === tab ? 'active' : ''}" ${k === tab ? 'aria-current="page"' : ''}>${l}</a>`).join('')}</nav><div id="inv"></div>`;
  const box = $('#inv');
  const [stock, whs] = await Promise.all([api('/api/office/stock'), api('/api/warehouses')]);
  const refresh = () => { location.hash = location.hash; window.dispatchEvent(new HashChangeEvent('hashchange')); };
  $('#aPur').onclick = () => purchasePanel(stock, whs, refresh);
  $('#aTrf').onclick = () => transferPanel(stock, whs, refresh);
  if (owner) $('#aAdj').onclick = () => adjustPanel(stock, whs, refresh);
  if (tab === 'count') return countTab(box, stock, whs, owner, refresh);
  if (tab === 'moves') return movesTab(box, stock, whs, owner, arg);
  if (tab === 'purchases') return purchasesTab(box);
  return matrixTab(box, stock, owner);
}

// ---------------------------------------------------------------- stock by godown
function matrixTab(box, s, owner) {
  const low = s.rows.filter(r => r.active && Object.values(r.cells).some(c => c.low)).length;
  box.innerHTML = `<div class="o-kpis" style="margin-top:12px">
      <div class="o-kpi"><div class="k">Units on hand</div><div class="v">${num(s.units)}</div><div class="s">${s.warehouses.length} godown${s.warehouses.length === 1 ? '' : 's'}</div></div>
      ${owner ? `<div class="o-kpi"><div class="k">Stock value at cost</div><div class="v">${money(s.value)}</div><div class="s">moving average</div></div>` : ''}
      <div class="o-kpi"><div class="k">Low stock</div><div class="v ${low ? 'crit' : ''}">${low}</div><div class="s">products at or under their alert level somewhere</div></div>
      <div class="o-kpi"><div class="k">Ledger check</div><div class="v">${s.ledger_ok ? pill('matches', 'good') : pill('mismatch', 'crit')}</div><div class="s">${s.ledger_ok ? 'every godown equals the replay of its movements' : `${s.ledger_mismatches.length} holding(s) differ from the ledger`}</div></div>
    </div>
    ${s.ledger_ok ? '' : `<p class="o-note crit">Stock and the stock ledger disagree for: ${s.ledger_mismatches.map(m => `${esc(m.sku)} @ ${esc(m.warehouse_id)} (stock ${m.on_hand}, ledger ${m.ledger})`).join(', ')}. Tell whoever maintains this installation.</p>`}
    <div class="o-bar" style="margin-top:12px"><input class="input o-search" type="search" id="q" placeholder="Search product or SKU…" aria-label="Search stock"><span class="hint">Click a product for its movements. Numbers are on hand; “res.” is reserved for allocated orders.</span></div>
    <div class="o-tw" id="mtx" style="margin-top:8px"></div>`;
  const cols = [
    { key: 'name', label: 'Product', render: r => `<b>${esc(r.name)}</b> <span class="mono">${esc(r.sku)}</span>${r.active ? '' : ' ' + pill('inactive')}` },
    { key: 'unit', label: 'Unit', cls: 'dim' },
    ...s.warehouses.map(w => ({ key: 'wh:' + w.warehouse_id, label: w.name, num: true, value: r => r.cells[w.warehouse_id].on_hand,
      render: r => { const c = r.cells[w.warehouse_id]; return `<span class="${c.low ? 'down' : ''}" ${c.low ? `title="at or under the alert level (${r.min_stock})"` : ''}>${num(c.on_hand)}</span>${c.reserved ? ` <span class="dim">(${num(c.reserved)} res.)</span>` : ''}`; } })),
    { key: 'on_hand', label: 'Total', num: true, render: r => `<b>${num(r.on_hand)}</b>` },
    { key: 'available', label: 'Available', num: true, render: r => num(r.available) },
    ...(owner ? [{ key: 'avg_cost', label: 'Avg cost', num: true, render: r => r.avg_cost === null ? '' : `<span class="dim">${money(r.avg_cost)}</span>` },
      { key: 'value', label: 'Value at cost', num: true, render: r => money(r.value) }] : []),
  ];
  const t = table($('#mtx'), {
    columns: cols, rows: s.rows, key: 'sku', sort: { key: 'name', dir: 'asc' }, searchKeys: ['name', 'sku', 'category'],
    onRow: r => { location.hash = `#/inventory/moves/${encodeURIComponent(r.sku)}`; },
    footer: rows => `<tr><td>Total (${rows.length} shown)</td><td></td>${s.warehouses.map(w => `<td class="n">${num(rows.reduce((a, r) => a + r.cells[w.warehouse_id].on_hand, 0))}</td>`).join('')}
      <td class="n">${num(rows.reduce((a, r) => a + r.on_hand, 0))}</td><td class="n">${num(rows.reduce((a, r) => a + r.available, 0))}</td>
      ${owner ? `<td></td><td class="n">${money(rows.reduce((a, r) => a + Math.round(r.value * 100), 0) / 100)}</td>` : ''}</tr>`,
  });
  $('#q').oninput = e => t.setSearch(e.target.value);
}

// ---------------------------------------------------------------- physical count
function countTab(box, s, whs, owner, refresh) {
  let wh = store.get('office.count.wh') || whs.find(w => w.default)?.warehouse_id || whs[0]?.warehouse_id;
  const draftKey = () => `office.count.${wh}`;
  let draft = store.get(draftKey(), {});
  box.innerHTML = `<div class="o-bar" style="margin-top:12px">
      <label class="f" style="min-width:200px">Godown<select class="input" id="cwh">${whs.map(w => `<option value="${esc(w.warehouse_id)}" ${w.warehouse_id === wh ? 'selected' : ''}>${esc(w.name)}</option>`).join('')}</select></label>
      <label class="f">Count date<input class="input" type="date" id="cdate" value="${todayPk()}" max="${todayPk()}"></label>
      <div class="grow"></div>
      <button class="btn ghost" id="fill" title="Put the system quantity into every row you haven't counted -- then change only the rows that differ">Fill blanks with system qty</button>
      <button class="btn ghost" id="clear">Clear counts</button>
      <button class="btn primary" id="cprev">Preview differences</button></div>
    <p class="o-note">Enter what you physically counted. Rows left blank are not counted and not changed. ${owner ? 'Posting books each difference as a stock adjustment with the reason “stock count &lt;date&gt;”.' : 'Only the owner can post a count: prepare it here, preview it, then ask the owner to post it (your entries stay in this browser).'}</p>
    <div class="o-tw" id="ctbl"></div>`;
  const rowsFor = () => s.rows.filter(r => r.active || r.cells[wh]?.on_hand).map(r => ({ sku: r.sku, name: r.name, unit: r.unit, system: r.cells[wh]?.on_hand ?? 0, reserved: r.cells[wh]?.reserved ?? 0 }));
  const cols = [
    { key: 'name', label: 'Product', render: r => `<b>${esc(r.name)}</b> <span class="mono">${esc(r.sku)}</span>` },
    { key: 'unit', label: 'Unit', cls: 'dim' },
    { key: 'system', label: 'System', num: true, render: r => num(r.system) },
    { key: 'reserved', label: 'Reserved', num: true, render: r => r.reserved ? num(r.reserved) : '<span class="dim">0</span>' },
    { key: 'counted', label: 'Counted', num: true, sortable: false, render: r => `<input class="input qty" type="number" min="0" step="1" inputmode="numeric" data-sku="${esc(r.sku)}" value="${draft[r.sku] ?? ''}" aria-label="Counted ${esc(r.name)}">` },
    { key: 'diff', label: 'Difference', num: true, sortable: false, render: r => diffCell(r) },
  ];
  const diffCell = r => { const v = draft[r.sku]; if (v === undefined || v === '') return '<span class="dim">—</span>'; const d = Number(v) - r.system; return d ? `<span class="${d > 0 ? 'up' : 'down'}">${signed(d)}</span>` : '<span class="dim">0</span>'; };
  const t = table($('#ctbl'), { columns: cols, rows: rowsFor(), key: 'sku', sort: { key: 'name', dir: 'asc' } });
  const save = () => store.set(draftKey(), draft);
  $('#ctbl').addEventListener('input', e => {
    const inp = e.target.closest('input[data-sku]'); if (!inp) return;
    if (inp.value === '') delete draft[inp.dataset.sku]; else draft[inp.dataset.sku] = inp.value;
    save(); const r = t.shown.find(x => x.sku === inp.dataset.sku); inp.closest('tr').lastElementChild.innerHTML = diffCell(r);
  });
  $('#ctbl').addEventListener('keydown', e => {      // Enter moves down the column, like a spreadsheet
    const inp = e.target.closest('input[data-sku]'); if (!inp || e.key !== 'Enter') return;
    e.preventDefault(); const all = $$('input[data-sku]', $('#ctbl')); all[all.indexOf(inp) + 1]?.focus();
  });
  $('#cwh').onchange = e => { wh = e.target.value; store.set('office.count.wh', wh); draft = store.get(draftKey(), {}); t.setRows(rowsFor()); };
  $('#fill').onclick = () => { t.shown.forEach(r => { if (draft[r.sku] === undefined) draft[r.sku] = String(r.system); }); save(); t.render(); };
  $('#clear').onclick = () => { draft = {}; save(); t.render(); };
  $('#cprev').onclick = async () => {
    const lines = Object.entries(draft).filter(([, v]) => v !== '').map(([sku, v]) => ({ sku, counted: Number(v) }));
    if (!lines.length) return toast('Enter at least one counted quantity.', 4000);
    if (lines.some(l => !Number.isInteger(l.counted) || l.counted < 0)) return toast('Counts must be whole numbers, zero or more.', 4000);
    const cdate = $('#cdate').value || todayPk();
    let pv; try { pv = await post('/api/office/stock-count/preview', { warehouse_id: wh, lines }); } catch (x) { return toast(x.message, 6000); }
    const shown = pv.rows.filter(r => r.diff || r.error);
    const whName = whs.find(w => w.warehouse_id === wh)?.name || wh;
    panel({
      title: `Stock count · ${whName} · ${cdate}`, wide: true,
      submitLabel: owner ? (pv.errors ? 'Fix the errors first' : pv.differences ? `Post ${pv.differences} adjustment${pv.differences === 1 ? '' : 's'}` : 'Nothing to post') : 'Only the owner can post',
      body: `<div class="o-kpis"><div class="o-kpi"><div class="k">Counted</div><div class="v">${pv.lines}</div><div class="s">products</div></div>
          <div class="o-kpi"><div class="k">Differences</div><div class="v">${pv.differences}</div><div class="s">${pv.lines - pv.differences - pv.errors} match the system</div></div>
          <div class="o-kpi"><div class="k">Units</div><div class="v"><span class="up">+${num(pv.units_up)}</span> / <span class="down">−${num(pv.units_down)}</span></div><div class="s">found / missing</div></div>
          ${owner ? `<div class="o-kpi"><div class="k">Value change</div><div class="v ${pv.value_change < 0 ? 'down' : ''}">${money(pv.value_change)}</div><div class="s">at today's average cost</div></div>` : ''}</div>
        ${pv.errors ? `<p class="o-note crit">${pv.errors} row${pv.errors === 1 ? '' : 's'} can't be posted -- see below. Nothing is posted until every row is right.</p>` : ''}
        ${shown.length ? `<div class="o-tw free"><table class="o-t"><thead><tr><th>Product</th><th class="n">System</th><th class="n">Counted</th><th class="n">Difference</th>${owner ? '<th class="n">Value</th>' : ''}</tr></thead><tbody>
          ${shown.map(r => `<tr class="${r.error ? 'err' : ''}"><td class="wrap"><b>${esc(r.name)}</b> <span class="mono">${esc(r.sku)}</span>${r.error ? `<br><span class="crit">${esc(r.error)}</span>` : ''}</td><td class="n">${num(r.system)}</td><td class="n">${num(r.counted)}</td>
            <td class="n ${r.diff > 0 ? 'up' : 'down'}">${signed(r.diff)}</td>${owner ? `<td class="n">${money(r.value_change)}</td>` : ''}</tr>`).join('')}</tbody></table></div>`
          : '<p class="o-note good">Every counted product matches the system. Nothing to post.</p>'}
        <p class="hint" style="margin:0">Each difference is posted as a stock adjustment with the reason <b>stock count ${esc(cdate)}</b>, priced at the moving-average cost, in one go: all of it or none of it. If stock moves before you post (a van is loaded, a purchase arrives), nothing is posted and you preview again.</p>`,
      onOpen: p => { if (!owner || pv.errors || !pv.differences) p.el.querySelector('button[type=submit]').disabled = true; },
      onSubmit: async () => {
        const r = await post('/api/office/stock-count/post', { warehouse_id: wh, count_date: cdate, lines: pv.rows.map(x => ({ sku: x.sku, counted: x.counted, system: x.system })) });
        draft = {}; save(); toast(`${r.posted.length} adjustment${r.posted.length === 1 ? '' : 's'} posted (${r.reason})`, 5000); refresh();
      },
    });
  };
}

// ---------------------------------------------------------------- movements
async function movesTab(box, s, whs, owner, sku) {
  sku = sku || s.rows[0]?.sku;
  box.innerHTML = `<div class="o-bar" style="margin-top:12px">
      <label class="f" style="min-width:260px">Product<select class="input" id="msku">${s.rows.map(r => `<option value="${esc(r.sku)}" ${r.sku === sku ? 'selected' : ''}>${esc(r.name)} (${esc(r.sku)})</option>`).join('')}</select></label>
      <label class="f">Godown<select class="input" id="mwh"><option value="">All godowns</option>${whs.map(w => `<option value="${esc(w.warehouse_id)}">${esc(w.name)}</option>`).join('')}</select></label></div>
    <div id="mres"><p class="muted">Loading…</p></div>`;
  const load = async () => {
    const wh = $('#mwh').value; const k = $('#msku').value;
    const d = await api(`/api/office/stock/moves?sku=${encodeURIComponent(k)}&limit=500${wh ? `&warehouse_id=${encodeURIComponent(wh)}` : ''}`);
    $('#mres').innerHTML = `<p class="hint">${esc(d.name)}: ${d.levels.map(l => `${esc(l.warehouse_id)} <b>${num(l.on_hand)}</b>`).join(' · ') || 'no stock'} · showing ${d.moves.length} of ${d.total_moves} movement${d.total_moves === 1 ? '' : 's'}, newest first. “Balance” is the godown's stock after that movement.</p><div class="o-tw" id="mt"></div>`;
    table($('#mt'), {
      rows: d.moves, key: 'move_id', empty: 'No movements for this product yet.',
      columns: [
        { key: 'at', label: 'When', render: m => `<span class="dim">${esc(when(m.at))}</span>` },
        { key: 'warehouse_id', label: 'Godown', cls: 'mono' },
        { key: 'kind', label: 'What', render: m => esc(KIND[m.kind] || m.kind) },
        { key: 'delta', label: 'Qty', num: true, render: m => `<span class="${m.delta > 0 ? 'up' : 'down'}">${signed(m.delta)}</span>` },
        { key: 'balance_after', label: 'Balance', num: true, render: m => num(m.balance_after), sortable: false },
        ...(owner ? [{ key: 'value', label: 'Value moved', num: true, render: m => `<span class="dim">${money(m.value)}</span>` }] : []),
        { key: 'ref', label: 'Reference / reason', cls: 'wrap', render: m => `<span class="dim">${esc(m.ref)}${m.order_id && m.order_id !== m.ref ? ' · ' + esc(m.order_id) : ''}</span>` },
      ],
    });
  };
  $('#msku').onchange = () => { history.replaceState(null, '', `#/inventory/moves/${encodeURIComponent($('#msku').value)}`); load(); };
  $('#mwh').onchange = load;
  await load();
}

// ---------------------------------------------------------------- purchases
async function purchasesTab(box) {
  const rows = await api('/api/purchases');
  box.innerHTML = `<p class="hint" style="margin-top:12px">The last 100 purchases and purchase returns. “Receive purchase” (top right) books a supplier's bill: the stock comes in at the bill's cost and the bill goes on the supplier's khata.</p><div class="o-tw" id="pt"></div>`;
  table($('#pt'), {
    rows, key: 'purchase_id', sort: { key: 'created_at', dir: 'desc' }, empty: 'No purchases recorded yet.',
    columns: [
      { key: 'created_at', label: 'Date', render: p => esc(day(p.created_at)) },
      { key: 'purchase_id', label: 'No.', cls: 'mono' },
      { key: 'supplier_name', label: 'Supplier' },
      { key: 'warehouse_id', label: 'Godown', cls: 'mono' },
      { key: 'items', label: 'Items', sortable: false, cls: 'wrap', render: p => `<span class="dim">${p.items.map(i => `${num(i.qty)} × ${esc(i.sku)} @ ${money(i.unit_cost)}`).join(', ')}</span>` },
      { key: 'invoice_ref', label: 'Bill ref', cls: 'dim' },
      { key: 'total', label: 'Total', num: true, render: p => money(p.total) },
      { key: 'paid_amount', label: 'Paid', num: true, render: p => money(p.paid_amount) },
      { key: 'reversal_of', label: '', sortable: false, render: p => p.reversal_of ? pill('return', 'warn') : '' },
    ],
  });
}

// ---------------------------------------------------------------- action panels
const prodOptions = s => s.rows.filter(r => r.active).map(r => [r.sku, `${r.name} (${r.sku})`]);
const whOptions = whs => whs.map(w => [w.warehouse_id, w.name]);

function adjustPanel(s, whs, done) {
  const def = whs.find(w => w.default)?.warehouse_id || whs[0]?.warehouse_id;
  panel({
    title: 'Adjust stock', submitLabel: 'Post adjustment',
    body: `<p class="o-note">For damage, expiry, theft or a count correction on one product. Posted to the stock ledger at the moving-average cost; stock can never go below zero. (To post a whole count at once, use Physical count.)</p>
      <div class="o-grid2">${select('Godown', 'warehouse_id', whOptions(whs), def)}${select('Product', 'sku', prodOptions(s))}</div>
      <p class="hint" id="adjOn" style="margin:0"></p>
      <div class="o-grid2">${select('Direction', 'dir', [['-1', 'Remove from stock'], ['1', 'Add to stock']], '-1')}${field('Quantity', 'qty', '', 'min="1" step="1" required', 'number')}</div>
      <div class="o-grid2">${select('Reason', 'why', [['damaged', 'Damaged'], ['expired', 'Expired'], ['count correction', 'Count correction'], ['theft / missing', 'Theft / missing'], ['other', 'Other']], 'damaged')}${field('Note', 'note', '', 'maxlength="80" placeholder="e.g. 3 bags torn in the rain"')}</div>`,
    onOpen: p => { const f = p.el; const show = () => { const r = s.rows.find(x => x.sku === f.sku.value); f.querySelector('#adjOn').textContent = r ? `On hand at this godown: ${num(r.cells[f.warehouse_id.value]?.on_hand ?? 0)} ${r.unit}` : ''; }; f.sku.onchange = show; f.warehouse_id.onchange = show; show(); },
    onSubmit: async d => {
      const qty = Number(d.qty); if (!Number.isInteger(qty) || qty < 1) throw new Error('The quantity must be a whole number, 1 or more.');
      const reason = (d.why + (d.note.trim() ? `: ${d.note.trim()}` : '')).slice(0, 120);
      await post('/api/stock/adjust', { warehouse_id: d.warehouse_id, sku: d.sku, delta: Number(d.dir) * qty, reason });
      toast('Adjustment posted'); done();
    },
  });
}

function transferPanel(s, whs, done) {
  if (whs.length < 2) return toast('A transfer needs two godowns -- add another under Godowns.', 5000);
  panel({
    title: 'Transfer between godowns', submitLabel: 'Transfer',
    body: `<p class="o-note">Moves units from one godown to another. Only available stock (on hand less reserved) can move.</p>
      <div class="o-grid2">${select('From', 'from_warehouse', whOptions(whs), whs[0].warehouse_id)}${select('To', 'to_warehouse', whOptions(whs), whs[1].warehouse_id)}</div>
      ${select('Product', 'sku', prodOptions(s))}<p class="hint" id="trAv" style="margin:0"></p>
      ${field('Quantity', 'qty', '', 'min="1" step="1" required', 'number')}`,
    onOpen: p => { const f = p.el; const show = () => { const r = s.rows.find(x => x.sku === f.sku.value); f.querySelector('#trAv').textContent = r ? `Available at the source godown: ${num(r.cells[f.from_warehouse.value]?.available ?? 0)} ${r.unit}` : ''; }; f.sku.onchange = show; f.from_warehouse.onchange = show; show(); },
    onSubmit: async d => {
      if (d.from_warehouse === d.to_warehouse) throw new Error('Pick two different godowns.');
      await post('/api/stock/transfer', { from_warehouse: d.from_warehouse, to_warehouse: d.to_warehouse, sku: d.sku, qty: Number(d.qty) });
      toast('Transfer posted'); done();
    },
  });
}

async function purchasePanel(s, whs, done) {
  const sups = await api('/api/suppliers');
  if (!sups.length) return toast('Add a supplier first (Suppliers).', 5000);
  const prods = s.rows.filter(r => r.active);
  const products = await api('/api/office/products');
  const cost = Object.fromEntries(products.map(p => [p.sku, p.cost_price ?? '']));
  const line = () => `<div class="o-line"><select class="input" data-l="sku" aria-label="Product">${prods.map(r => `<option value="${esc(r.sku)}">${esc(r.name)} (${esc(r.sku)})</option>`).join('')}</select>
    <input class="input num" data-l="qty" type="number" min="1" step="1" placeholder="Qty" aria-label="Quantity" required><input class="input num" data-l="cost" type="number" min="0" step="any" placeholder="Unit cost Rs" aria-label="Unit cost"><button type="button" class="x" aria-label="Remove line">✕</button></div>`;
  panel({
    title: 'Receive purchase', wide: true, submitLabel: 'Book purchase',
    body: `<p class="o-note">Stock comes in at the bill's unit cost (into the moving average) and the bill goes on the supplier's khata. Leave a unit cost blank to use the product's last cost.</p>
      <div class="o-grid3">${select('Supplier', 'supplier_id', sups.map(x => [x.supplier_id, x.name]))}${select('Into godown', 'warehouse_id', whOptions(whs), whs.find(w => w.default)?.warehouse_id)}${field("Supplier's bill no.", 'invoice_ref', '', 'maxlength="40"')}</div>
      <h3>Lines</h3><div class="o-lines" id="plines">${line()}</div><div><button type="button" class="btn sm" id="addl">+ Add line</button></div>
      <div class="o-grid2">${field('Paid now (Rs)', 'paid_amount', 0, 'min="0" step="any"', 'number')}<div><b>Bill total</b><div class="o-kpi" style="margin-top:4px"><div class="v" id="ptot">Rs 0</div></div></div></div>`,
    onOpen: p => {
      const f = p.el, box = f.querySelector('#plines');
      const total = () => { let t = 0; $$('.o-line', box).forEach(l => { const q = Number($('[data-l=qty]', l).value || 0), sku = $('[data-l=sku]', l).value; const c = $('[data-l=cost]', l).value === '' ? Number(cost[sku] || 0) : Number($('[data-l=cost]', l).value); t += q * c; }); f.querySelector('#ptot').textContent = money(t); };
      f.querySelector('#addl').onclick = () => { box.insertAdjacentHTML('beforeend', line()); total(); };
      box.addEventListener('click', e => { if (e.target.matches('.x') && $$('.o-line', box).length > 1) { e.target.closest('.o-line').remove(); total(); } });
      box.addEventListener('input', total); box.addEventListener('change', total);
    },
    onSubmit: async (d, f) => {
      const items = $$('.o-line', f).map(l => ({ sku: $('[data-l=sku]', l).value, qty: Number($('[data-l=qty]', l).value), unit_cost: $('[data-l=cost]', l).value === '' ? 0 : Number($('[data-l=cost]', l).value) }));
      if (items.some(i => !Number.isInteger(i.qty) || i.qty < 1)) throw new Error('Every line needs a whole-number quantity of 1 or more.');
      const r = await post('/api/purchases', { supplier_id: d.supplier_id, warehouse_id: d.warehouse_id, invoice_ref: d.invoice_ref, paid_amount: Number(d.paid_amount || 0), items });
      toast(`Purchase ${r.purchase_id} booked: ${money(r.total)}`, 5000); done();
    },
  });
}
