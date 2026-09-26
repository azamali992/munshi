/* Month close (owner: finance:write). Where the books stand (closed through a date), the checklist that should be
   green first (payroll approved, depreciation run, cash counted, banks reconciled, the clearing accounts at zero, the
   trial balance balanced), Close with a confirm that restates what a close means, and Reopen with a reason.
   Closing through D makes the database refuse any expense, journal, transfer, payroll or advance dated on or before D;
   reports for a closed month come from its snapshot. The server decides; this screen explains.
   REST (plan §9 Stream B): GET /api/finance/periods, POST /api/finance/periods/close, POST /api/finance/periods/{id}/reopen. */
import { $, $$, api, check, confirmDialog, esc, field, fmtCell, post, refresh, skeleton, t, toast, when } from './lib.js';

export default async function close(view) {
  view.header(t('cl.close'), t('loading'));
  view.page.innerHTML = skeleton(8, 3);
  const p = await api('/api/finance/periods');
  const checklist = p.checklist || [], open = checklist.filter(c => !c.ok);
  const closes = p.closes || [];
  const latest = closes.find(c => !c.reopened_at);
  view.header(t('cl.close'), esc(t('cl.sub', { d: fmtCell(p.through_date, 'date') })));
  view.page.innerHTML = `
    <div class="o-kpis"><div class="o-kpi"><div class="k">${esc(t('cl.closed_through'))}</div><div class="v">${fmtCell(p.through_date, 'date') || '—'}</div><div class="s">${esc(t('cl.closed_s'))}</div></div>
      <div class="o-kpi"><div class="k">${esc(t('cl.checklist'))}</div><div class="v">${checklist.length - open.length}<span class="dim">/${checklist.length}</span></div><div class="s">${esc(open.length ? t('cl.open_items', { n: open.length }) : t('cl.all_green'))}</div></div></div>
    <div class="o-cols"><section style="display:grid;gap:8px"><h3 style="margin:0">${esc(t('cl.before'))}</h3>
      <ul class="o-check">${checklist.map(c => `<li class="${c.ok ? 'ok' : 'no'}"><span class="ic" aria-hidden="true">${c.ok ? '✓' : '!'}</span><span><b>${esc(c.label)}</b><br><span class="d">${esc(c.detail || '')}</span></span>
        <span class="pill ${c.ok ? 'good' : 'warn'}">${esc(c.ok ? t('cl.done') : t('cl.not_yet'))}</span></li>`).join('') || `<li><span></span><span class="dim">${esc(t('cl.no_checklist'))}</span><span></span></li>`}</ul></section>
    <form class="o-card" id="cf" style="display:grid;gap:10px" novalidate><b>${esc(t('cl.close_month'))}</b>
      ${field(t('cl.through'), 'through_date', p.suggested_through || '', 'required', 'date')}
      ${field(t('cl.note'), 'note', '', 'maxlength="200" placeholder="September books checked"')}
      ${open.length ? `<p class="o-note warn">${esc(t('cl.open_warn', { n: open.length }))}</p>${check(t('cl.force'), 'force', false)}` : ''}
      <p class="formerr" role="alert"></p><div class="o-bar"><div class="grow"></div><button class="btn primary" type="submit">${esc(t('cl.close_btn'))}</button></div></form></div>
    <h3 style="margin:4px 0 0">${esc(t('cl.history'))}</h3>
    <div class="o-tw free"><table class="o-t"><caption class="sr">${esc(t('cl.history'))}</caption><thead><tr><th>${esc(t('cl.through'))}</th><th>${esc(t('cl.by'))}</th><th>${esc(t('cl.when'))}</th><th>${esc(t('cl.note'))}</th><th>${esc(t('cl.state'))}</th><th></th></tr></thead><tbody>
    ${closes.map(c => `<tr class="${c.reopened_at ? 'off' : ''}"><th scope="row" style="text-align:start">${fmtCell(c.through_date, 'date')}</th><td>${esc(c.closed_by)}</td><td class="dim">${esc(when(c.closed_at))}</td><td class="wrap">${esc(c.note || '')}</td>
      <td>${c.reopened_at ? `<span class="pill">${esc(t('cl.reopened'))}</span> <span class="dim">${esc(c.reopened_by || '')}: ${esc(c.reopen_reason || '')}</span>` : `<span class="pill good">${esc(t('cl.closed'))}</span>`}</td>
      <td class="n">${c === latest ? `<button type="button" class="btn sm danger" data-reopen="${esc(c.close_id)}">${esc(t('cl.reopen'))}</button>` : ''}</td></tr>`).join('') || `<tr><td colspan="6" class="o-empty">${esc(t('cl.none'))}</td></tr>`}</tbody></table></div>`;

  const f = $('#cf');
  f.force?.addEventListener('change', () => { $('.formerr', f).textContent = ''; });
  f.onsubmit = e => {
    e.preventDefault(); if (!f.checkValidity()) { f.reportValidity(); return; }
    if (open.length && !f.force?.checked) { $('.formerr', f).textContent = t('cl.need_force'); return; }
    $('.formerr', f).textContent = '';
    const d = f.through_date.value;
    confirmDialog({
      title: t('cl.close_q', { d: fmtCell(d, 'date') }), danger: true, confirmLabel: t('cl.close_yes', { d: fmtCell(d, 'date') }),
      body: `<p>${esc(t('cl.effect_1', { d: fmtCell(d, 'date') }))}</p><p>${esc(t('cl.effect_2'))}</p>${open.length ? `<p class="crit"><b>${esc(t('cl.effect_open', { n: open.length }))}</b></p>` : ''}`,
      onConfirm: async () => { await post('/api/finance/periods/close', { through_date: d, note: f.note.value.trim(), force: !!f.force?.checked }); toast(t('cl.closed_toast', { d: fmtCell(d, 'date') })); refresh(); },
    });
  };
  $$('[data-reopen]').forEach(b => b.onclick = () => confirmDialog({
    title: t('cl.reopen_q', { d: fmtCell(latest.through_date, 'date') }), danger: true, confirmLabel: t('cl.reopen'), body: `<p>${esc(t('cl.reopen_effect'))}</p>`,
    fields: field(t('cl.reason'), 'reason', '', 'required minlength="5" maxlength="200"'),
    onConfirm: async x => { await post(`/api/finance/periods/${encodeURIComponent(b.dataset.reopen)}/reopen`, { reason: x.reason.trim() }); toast(t('cl.reopened')); refresh(); },
  }));
}
