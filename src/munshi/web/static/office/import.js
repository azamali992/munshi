/* Import / export: the existing Excel template, import and export routes (all owner-only), with the import's
   per-row problems shown as a table you can work through, not raw JSON. */
import { $, api, can, download, esc, num, todayPk } from './lib.js';

export default async function importExport(view) {
  view.header('Import / export', 'Excel in, Excel out');
  if (!can('setup:write') && !can('export')) {
    view.page.innerHTML = '<p class="o-note">Importing and exporting the business\'s data is the owner\'s. Ask the owner, or use the tables in this console to look things up.</p>';
    return;
  }
  view.page.innerHTML = `<div class="o-grid2" style="align-items:start">
    <section class="o-card" style="display:grid;gap:10px"><h2 style="margin:0">Import from Excel</h2>
      <p style="margin:0">Customers, products, opening stock and suppliers in one workbook. Download the template, fill the sheets (delete the example rows), and upload it. Rows whose name already exists are updated, not duplicated. Stock rows set the godown's count through the stock ledger -- never below zero.</p>
      <div class="o-bar"><button class="btn" id="tpl">Download template</button></div>
      <label class="f">Workbook (.xlsx, up to 8 MB)<input class="input" type="file" id="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></label>
      <div class="o-bar"><button class="btn primary" id="up" disabled>Import</button><span class="hint" id="upState"></span></div>
    </section>
    <section class="o-card" style="display:grid;gap:10px"><h2 style="margin:0">Export everything</h2>
      <p style="margin:0">One workbook with every customer, product, supplier, stock level, order, khata entry, purchase, expense, stock movement and the audit trail. The export itself is recorded in the audit trail.</p>
      <div class="o-bar"><button class="btn" id="exp" ${can('export') ? '' : 'disabled'}>Download export</button></div>
    </section></div>
    <div id="result"></div>`;
  $('#tpl').onclick = () => download('/api/import/template.xlsx', 'munshi-import-template.xlsx');
  $('#exp').onclick = () => download('/api/export.xlsx', `munshi-${todayPk()}.xlsx`);
  $('#file').onchange = e => { $('#up').disabled = !e.target.files.length; };
  $('#up').onclick = async () => {
    const f = $('#file').files[0]; if (!f) return;
    const fd = new FormData(); fd.append('file', f);
    $('#up').disabled = true; $('#upState').textContent = 'Importing…';
    try { showResult(await api('/api/import', { method: 'POST', body: fd }), f.name); $('#upState').textContent = ''; }
    catch (x) { $('#result').innerHTML = `<p class="o-note crit">${esc(x.message)}</p>`; $('#upState').textContent = ''; }
    finally { $('#up').disabled = false; }
  };
}

function showResult(r, name) {
  const errs = (r.errors || []).map(e => { const m = /^(\w+) row (\d+): (.*)$/s.exec(e); return m ? { sheet: m[1], row: Number(m[2]), msg: m[3] } : { sheet: '', row: '', msg: e }; });
  $('#result').innerHTML = `<h2 style="margin:4px 0 0">Imported ${esc(name)}</h2>
    <div class="o-kpis">${[['Customers', r.customers], ['Opening balances', r.opening_balances], ['Products', r.products], ['Stock rows', r.stock], ['Suppliers', r.suppliers]].map(([k, v]) => `<div class="o-kpi"><div class="k">${k}</div><div class="v">${num(v)}</div><div class="s">saved</div></div>`).join('')}
      <div class="o-kpi"><div class="k">Problems</div><div class="v ${errs.length ? 'crit' : ''}">${errs.length}</div><div class="s">${errs.length ? 'rows not imported' : 'none'}</div></div></div>
    ${errs.length ? `<p class="o-note warn">These rows were skipped; everything else was saved. Fix them in the workbook and upload it again -- rows already imported are updated, not duplicated.</p>
      <div class="o-tw free"><table class="o-t"><thead><tr><th>Sheet</th><th class="n">Row</th><th>Problem</th></tr></thead><tbody>
      ${errs.map(e => `<tr><td>${esc(e.sheet)}</td><td class="n">${esc(e.row)}</td><td class="wrap">${esc(e.msg)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="o-note good">Every row was imported.</p>'}`;
}
