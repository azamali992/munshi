/* Munshi mobile app — vanilla JS, no build step. Talks to /api, installs as a PWA. */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const view = $('#view'), nav = $('#nav'), badge = $('#badge'), rolePill = $('#rolePill');
  const state = { token: localStorage.getItem('munshi.token'), role: localStorage.getItem('munshi.role'), me: null, thread: 'main', chatBusy: false };
  const fmt = n => 'Rs ' + Math.round(Number(n || 0)).toLocaleString('en-PK');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const tierPill = t => t === 'high_risk' ? '<span class="pill crit">owner</span>' : t === 'low_risk' ? '<span class="pill warn">clerk</span>' : '<span class="pill">auto</span>';
  const statusPill = s => ({ draft: '', confirmed: 'acc', allocated: 'acc', dispatched: 'warn', delivered: 'good', short: 'crit', cancelled: 'crit', planned: '', approved: 'warn', completed: 'good', pending: '', skipped: 'crit', drafted: 'warn', sent: 'good' }[s] ?? '');
  const bucketPill = b => ({ 'current': 'good', '1-30': 'warn', '31-60': 'crit', '60+': 'crit' }[b] || '');

  let toastT; function toast(msg) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => t.hidden = true, 2600); }

  async function api(path, opts = {}) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, state.token ? { 'X-Session': state.token } : {}, opts.headers || {});
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (res.status === 401) { signOut(); throw new Error('Signed out'); }
    const ct = res.headers.get('content-type') || '';
    const body = ct.includes('json') ? await res.json() : await res.text();
    if (!res.ok) throw new Error(typeof body === 'object' && body.detail ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)) : String(body));
    return body;
  }
  const post = (p, b) => api(p, { method: 'POST', body: JSON.stringify(b) });

  function signOut() { state.token = null; state.role = null; localStorage.removeItem('munshi.token'); localStorage.removeItem('munshi.role'); nav.hidden = true; rolePill.hidden = true; location.hash = '#login'; render(); }

  async function refreshBadge() {
    if (!state.token) return;
    try { const a = await api('/api/approvals'); badge.textContent = a.length; badge.hidden = a.length === 0; } catch { }
  }

  // ---------------------------------------------------------------- views
  const V = {};

  V.login = () => {
    view.innerHTML = `<div class="login"><div>
      <div class="mark"><i></i></div>
      <h1>Munshi</h1>
      <p class="sub">Your AI back office. Enter your PIN.</p>
      <form id="pinForm"><div class="pin">${[0, 1, 2, 3].map(i => `<input class="input" inputmode="numeric" maxlength="1" pattern="[0-9]" data-i="${i}" autocomplete="off">`).join('')}</div>
      <button class="btn primary" type="submit">Sign in</button></form>
      <p class="hint" style="margin-top:14px">Demo PINs — owner 1111 · clerk 2222 · driver 3333</p>
    </div></div>`;
    const ins = [...view.querySelectorAll('.pin input')];
    ins[0].focus();
    ins.forEach((el, i) => { el.addEventListener('input', () => { el.value = el.value.replace(/\D/g, '').slice(-1); if (el.value && ins[i + 1]) ins[i + 1].focus(); if (i === 3 && el.value) $('#pinForm').requestSubmit(); }); el.addEventListener('keydown', e => { if (e.key === 'Backspace' && !el.value && ins[i - 1]) ins[i - 1].focus(); }); });
    $('#pinForm').onsubmit = async e => {
      e.preventDefault();
      try { const r = await post('/api/session', { pin: ins.map(i => i.value).join('') }); state.token = r.token; state.role = r.role; localStorage.setItem('munshi.token', r.token); localStorage.setItem('munshi.role', r.role); await boot(); location.hash = '#today'; }
      catch (err) { toast(err.message); ins.forEach(i => i.value = ''); ins[0].focus(); }
    };
  };

  V.today = async () => {
    view.innerHTML = '<h1>Today</h1><p class="sub">Loading…</p>';
    const d = await api('/api/digest');
    const cashGap = d.cash.collected - d.cash.deposited;
    view.innerHTML = `<h1>Today</h1><p class="sub">${d.date} · ${esc(state.me?.business || '')}</p>
      ${d.pending_approvals ? `<a href="#approvals" class="card row" style="border-color:var(--accent);margin-bottom:10px"><span><b>${d.pending_approvals} action${d.pending_approvals > 1 ? 's' : ''} waiting for approval</b><br><span class="sub" style="margin:0">Nothing moves until someone says yes.</span></span><span class="pill acc">review</span></a>` : ''}
      <div class="grid2">
        <div class="card tile"><div class="k">Orders</div><div class="v">${d.orders.count}</div><div class="s">${fmt(d.orders.value)}${d.orders.draft ? ` · ${d.orders.draft} draft` : ''}</div></div>
        <div class="card tile"><div class="k">Deliveries</div><div class="v">${d.dispatch.delivered}<span style="font-size:15px;color:var(--muted)">/${d.dispatch.stops}</span></div><div class="s">${d.dispatch.plans} plan${d.dispatch.plans === 1 ? '' : 's'}${d.dispatch.short ? ` · <span style="color:var(--crit)">${d.dispatch.short} short</span>` : ''}</div></div>
        <div class="card tile"><div class="k">Cash collected</div><div class="v">${fmt(d.cash.collected)}</div><div class="s">${d.cash.deposited ? `deposited ${fmt(d.cash.deposited)}` : 'not yet deposited'}${cashGap > 0 && d.cash.deposited ? ` · <span style="color:var(--crit)">${fmt(cashGap)} short</span>` : ''}</div></div>
        <div class="card tile"><div class="k">Receivables</div><div class="v">${fmt(d.receivables.total)}</div><div class="s">${d.receivables.customers} accounts${d.receivables.overdue_60 ? ` · <span style="color:var(--crit)">${fmt(d.receivables.overdue_60)} 60+ days</span>` : ''}</div></div>
      </div>
      ${d.low_stock.length ? `<h2>Low stock</h2><div class="chips">${d.low_stock.map(s => `<span class="chip">${esc(s.sku)} · ${s.available} @ ${esc(s.warehouse_id.replace('WH-', ''))}</span>`).join('')}</div>` : ''}
      <h2>Ask a munshi</h2>
      <div class="chips">${['Aaj ka dispatch suggest karo', 'Kaun kitna baqi hai?', 'Remind everyone over 30 days', 'Confirmed orders dikhao'].map(q => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>`;
    view.querySelectorAll('[data-q]').forEach(b => b.onclick = () => { sessionStorage.setItem('munshi.prefill', b.dataset.q); location.hash = '#chat'; });
  };

  function renderMsg(m) {
    if (m.role === 'munshi' || m.role === 'bot') {
      const who = m.meta?.specialist ? m.meta.specialist + ' munshi' : 'munshi';
      let text = m.text;
      let extra = '';
      const j = text.indexOf('Done -- ');
      if (j === 0) { const raw = text.slice(8); try { const o = JSON.parse(raw); text = summarize(o); extra = `<details><summary class="hint">details</summary><pre class="json">${esc(JSON.stringify(o, null, 1))}</pre></details>`; } catch { text = raw; } }
      return `<div class="msg bot"><span class="who">${esc(who)}</span>${esc(text)}${extra}</div>`;
    }
    return `<div class="msg me">${esc(m.text)}</div>`;
  }
  function summarize(o) {
    if (o && o.error) return '⚠ ' + o.error;
    if (Array.isArray(o)) {
      if (!o.length) return 'Nothing to show.';
      if (o[0].route_id && o[0].order_ids) return o.map(p => `${p.route_id}: ${p.order_ids.length} order(s), ${p.load_units} units → ${p.vehicle_id || 'no vehicle fits'}${p.note ? ' (' + p.note + ')' : ''}`).join('\n');
      if (o[0].reminder_id) return `Drafted ${o.length} reminder(s):\n` + o.map(r => `• ${r.customer_id} (${r.tier}, ${r.days_overdue}d, ${fmt(r.amount_due)})`).join('\n');
      if (o[0].balance !== undefined) return o.map(a => `• ${a.name}: ${fmt(a.balance)} · ${a.days_overdue}d (${a.bucket})`).join('\n');
      if (o[0].order_id) return o.map(x => `• ${x.order_id} ${x.customer_name || x.customer_id} — ${fmt(x.total)} · ${x.status}`).join('\n');
      if (o[0].stop_id) return o.map(s => `• #${s.sequence} ${s.customer_id} — ${s.status}${s.cash_collected ? ' · cash ' + fmt(s.cash_collected) : ''}`).join('\n');
      if (o[0].warehouse_id && o[0].available !== undefined) return o.map(s => `• ${s.warehouse_id}: ${s.available} available (${s.on_hand} on hand, ${s.reserved} reserved)`).join('\n');
      return JSON.stringify(o).slice(0, 300);
    }
    if (o.order_id && o.items) return `Order ${o.order_id} for ${o.customer_name || o.customer_id}: ${o.items.map(i => `${i.qty} × ${i.sku}`).join(', ')} — ${fmt(o.total)} · ${o.status}${o.status === 'draft' ? `\nReply "confirm ${o.order_id}" once the customer says yes.` : ''}`;
    if (o.plan_id && o.order_ids) return `Plan ${o.plan_id}: ${o.route_id} on ${o.vehicle_id}, ${o.order_ids.length} order(s), ${o.load_units} units · ${o.status}${o.status === 'planned' ? `\nReply "approve ${o.plan_id}" to load it.` : ''}`;
    if (o.variance !== undefined) return `Deposit for ${o.plan_id}: expected ${fmt(o.expected)}, counted ${fmt(o.counted)}, variance ${fmt(o.variance)}${o.variance < 0 && o.suspect_stops?.length ? `\nLook first at: ${o.suspect_stops.map(s => `${s.customer_id} (cash ${fmt(s.cash_collected)})`).join(', ')}` : ''}`;
    if (o.stop_id && o.invoiced !== undefined) return `Stop ${o.stop_id} closed: ${o.status}, invoiced ${fmt(o.invoiced)}, cash ${fmt(o.cash_collected)}`;
    if (o.customer && o.outstanding !== undefined) return `${o.customer.name}: outstanding ${fmt(o.outstanding)}${o.aging ? ` · ${o.aging.days_overdue}d overdue (${o.aging.bucket})` : ''} · limit ${fmt(o.customer.credit_limit)}`;
    if (o.found === false) return `No customer matches "${o.query}".`;
    if (o.found) return `${o.name} (${o.customer_id}) · ${o.tier} · outstanding ${fmt(o.outstanding)} · limit ${fmt(o.credit_limit)}`;
    if (o.reminder_id) return `Reminder ${o.reminder_id} (${o.tier}) for ${o.customer_id}: "${o.message}" · ${o.status}`;
    if (o.promise_id) return `Promise logged: ${o.customer_id} pays ${fmt(o.amount)} by ${o.promised_date}`;
    if (o.entry_id) return `${o.kind.replace('_', ' ')} ${o.entry_id}: ${fmt(o.amount)} on ${o.customer_id}`;
    if (o.sku && o.available !== undefined) return `${o.sku} at ${o.warehouse_id}: ${o.available} available`;
    if (o.orders && o.cash) return `Orders ${o.orders.count} (${fmt(o.orders.value)}) · delivered ${o.dispatch.delivered}/${o.dispatch.stops} · cash ${fmt(o.cash.collected)} · receivables ${fmt(o.receivables.total)}`;
    if (o.order_id && o.status === 'allocated') return `${o.order_id} allocated at ${o.warehouse_id}.`;
    return JSON.stringify(o).slice(0, 300);
  }
  function renderApproval(p, canApprove) {
    return `<div class="approval" data-aid="${esc(p.approval_id)}"><div class="h"><span>${esc(p.specialist)} munshi · ${esc(p.tool.replace(/_/g, ' '))}</span>${tierPill(p.tier)}</div>
      <div class="s">${esc(p.summary)}</div>
      <div class="btnrow"><button class="btn primary" data-act="approve" ${canApprove ? '' : 'disabled'}>Approve</button><button class="btn danger" data-act="reject">Reject</button>${canApprove ? '' : `<span class="hint" style="align-self:center">needs ${esc(p.needs_role)}</span>`}</div></div>`;
  }

  V.chat = async () => {
    view.innerHTML = `<div class="chat"><div class="row"><h1>Chat</h1><span class="hint">talks to the manager, who routes to a munshi</span></div>
      <div class="chips">${['Chaudhry Farms ko 20 urea aur 5 dap bhej do', 'Confirmed orders dikhao', 'Suggest dispatch for today', 'Kaun kitna baqi hai?', 'Remind everyone over 30 days', 'Digest'].map(q => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      <div id="msgs" class="msgs"></div>
      <form id="composer" class="composer"><input id="txt" class="input" placeholder="Order, stock, dispatch, cash, khata…" autocomplete="off">${state.me?.voice ? '<button type="button" id="mic" class="btn mic" title="Hold to speak">🎙</button>' : ''}<button class="btn primary" type="submit">Send</button></form></div>`;
    const msgs = $('#msgs');
    const hist = await api('/api/chat/' + state.thread);
    const pend = await api('/api/approvals');
    const openIds = new Set(pend.map(p => p.approval_id));
    msgs.innerHTML = hist.map(m => m.meta?.approval_id && openIds.has(m.meta.approval_id) ? renderApproval(pend.find(p => p.approval_id === m.meta.approval_id), pend.find(p => p.approval_id === m.meta.approval_id).can_approve) : renderMsg(m)).join('') || '<div class="empty">Say what needs doing. Urdu, English or both.</div>';
    view.querySelectorAll('[data-q]').forEach(b => b.onclick = () => { $('#txt').value = b.dataset.q; $('#composer').requestSubmit(); });
    const pre = sessionStorage.getItem('munshi.prefill'); if (pre) { sessionStorage.removeItem('munshi.prefill'); $('#txt').value = pre; $('#composer').requestSubmit(); }
    msgs.addEventListener('click', onApprovalClick);
    scrollEnd();
    $('#composer').onsubmit = async e => {
      e.preventDefault(); const txt = $('#txt').value.trim(); if (!txt || state.chatBusy) return;
      $('#txt').value = ''; msgs.insertAdjacentHTML('beforeend', renderMsg({ role: 'me', text: txt })); scrollEnd();
      state.chatBusy = true; const thinking = document.createElement('div'); thinking.className = 'msg bot'; thinking.innerHTML = '<span class="who">manager</span>…'; msgs.appendChild(thinking);
      try { const r = await post('/api/chat', { thread_id: state.thread, text: txt }); thinking.remove(); if (!r.pending) msgs.insertAdjacentHTML('beforeend', renderMsg({ role: 'munshi', text: r.text, meta: { specialist: r.specialist } })); if (r.pending) { const a = (await api('/api/approvals')).find(p => p.approval_id === r.pending.approval_id); msgs.insertAdjacentHTML('beforeend', renderApproval(r.pending, a ? a.can_approve : false)); } }
      catch (err) { thinking.remove(); msgs.insertAdjacentHTML('beforeend', renderMsg({ role: 'munshi', text: '⚠ ' + err.message })); }
      state.chatBusy = false; scrollEnd(); refreshBadge();
    };
    if (state.me?.voice) setupMic($('#mic'), msgs);
    function scrollEnd() { window.scrollTo({ top: document.body.scrollHeight }); }
  };

  async function onApprovalClick(e) {
    const btn = e.target.closest('[data-act]'); if (!btn) return;
    const box = btn.closest('[data-aid]'); const aid = box.dataset.aid; const approve = btn.dataset.act === 'approve';
    let note = ''; if (!approve) note = prompt('Reason (optional)') || '';
    box.querySelectorAll('button').forEach(b => b.disabled = true);
    try { const r = await post('/api/approvals/' + aid, { approve, note }); box.outerHTML = `<div class="msg bot"><span class="who">${esc(r.specialist)} munshi · ${approve ? 'approved' : 'rejected'}</span>${esc(r.text.startsWith('Done -- ') ? (() => { try { return summarize(JSON.parse(r.text.slice(8))); } catch { return r.text.slice(8); } })() : r.text)}</div>`; toast(approve ? 'Approved' : 'Rejected'); }
    catch (err) { toast(err.message); box.querySelectorAll('button').forEach(b => b.disabled = false); }
    refreshBadge();
  }

  V.approvals = async () => {
    view.innerHTML = '<h1>Approvals</h1><p class="sub">Every action that touches stock, money or a customer waits here.</p><div id="alist" class="list"></div>';
    const a = await api('/api/approvals');
    $('#alist').innerHTML = a.length ? a.map(p => renderApproval(p, p.can_approve)).join('') : '<div class="empty">Nothing waiting. The munshis are idle.</div>';
    $('#alist').addEventListener('click', async e => { await onApprovalClick(e); if ($('#alist').querySelectorAll('[data-aid]').length === 0) V.approvals(); });
  };

  V.khata = async () => {
    view.innerHTML = '<h1>Khata</h1><p class="sub">Who owes what, oldest first.</p><div id="klist" class="list"></div>';
    const rows = await api('/api/khata');
    const total = rows.reduce((s, r) => s + r.balance, 0);
    $('#klist').innerHTML = `<div class="card row"><span class="tile"><div class="k">Total receivable</div><div class="v">${fmt(total)}</div></span><span class="pill">${rows.length} accounts</span></div>` +
      (rows.map(r => `<a class="item tap" href="#customer/${esc(r.customer_id)}"><div class="row"><span class="t">${esc(r.name)}</span><span class="pill ${bucketPill(r.bucket)}">${esc(r.bucket)}</span></div><div class="row"><span class="m">${r.days_overdue ? r.days_overdue + ' days overdue' : 'not yet due'} · limit ${fmt(r.credit_limit)}</span><b class="num">${fmt(r.balance)}</b></div></a>`).join('') || '<div class="empty">Nobody owes anything.</div>');
  };

  V.customer = async id => {
    view.innerHTML = `<a class="back" href="#khata">‹ Khata</a><h1>Loading…</h1>`;
    const k = await api('/api/khata/' + id);
    const c = k.customer;
    view.innerHTML = `<a class="back" href="#khata">‹ Khata</a><h1>${esc(c.name)}</h1><p class="sub">${esc(c.customer_id)} · ${esc(c.tier)} · ${esc(c.phone)}</p>
      <div class="grid2"><div class="card tile"><div class="k">Outstanding</div><div class="v">${fmt(k.outstanding)}</div><div class="s">${k.aging ? `${k.aging.days_overdue} days · ${k.aging.bucket}` : 'clear'}</div></div><div class="card tile"><div class="k">Credit limit</div><div class="v">${fmt(c.credit_limit)}</div><div class="s">${k.outstanding > c.credit_limit ? '<span style="color:var(--crit)">over limit</span>' : 'within limit'}</div></div></div>
      ${state.role !== 'driver' && k.outstanding > 0 ? `<div class="btnrow" style="margin:12px 0"><button class="btn primary" id="remind">Draft reminder via Wasooli</button><button class="btn" id="promise">Log a promise</button></div>` : ''}
      <h2>Recent entries</h2><div class="list">${k.recent.slice().reverse().map(e => `<div class="item"><div class="row"><span class="t">${esc(e.kind.replace('_', ' '))}</span><b class="num" style="color:${e.amount < 0 ? 'var(--good)' : 'var(--ink)'}">${fmt(e.amount)}</b></div><span class="m">${esc(e.ref)} · ${esc(e.created_at.slice(0, 10))}${e.due_date ? ' · due ' + esc(e.due_date) : ''}</span></div>`).join('') || '<div class="empty">No entries.</div>'}</div>`;
    const go = txt => { state.thread = 'main'; sessionStorage.setItem('munshi.prefill', txt); location.hash = '#chat'; };
    $('#remind')?.addEventListener('click', () => go(`remind ${c.customer_id}`));
    $('#promise')?.addEventListener('click', () => { const amt = prompt('Amount promised (Rs)'); const d = prompt('By date (YYYY-MM-DD)'); if (amt && d) go(`${c.customer_id} promise ${amt} by ${d}`); });
  };

  V.more = () => {
    const items = [['orders', 'Orders', 'drafts, confirmed, dispatched, delivered'], ['dispatch', 'Dispatch', 'plans, vehicles, stops'], ['driver', 'Driver mode', 'close stops with OTP'], ['stock', 'Stock', 'by godown'], ['customers', 'Customers', 'tiers and limits'], ['reminders', 'Reminders', 'drafted and sent'], ['audit', 'Audit log', 'who approved what'], ['settings', 'Settings', 'role, model, export']];
    view.innerHTML = `<h1>More</h1><div class="list more">${items.filter(([k]) => !(state.role === 'driver' && ['audit', 'customers', 'stock'].includes(k))).map(([k, t, s]) => `<a class="item tap" href="#${k}"><span><span class="t">${t}</span><br><span class="m">${s}</span></span><span class="muted">›</span></a>`).join('')}</div>`;
  };

  V.orders = async () => {
    view.innerHTML = '<h1>Orders</h1><div class="chips" id="f"></div><div id="ol" class="list"></div>';
    const load = async st => { const o = await api('/api/orders' + (st ? '?status=' + st : '')); $('#ol').innerHTML = o.map(x => `<a class="item tap" href="#order/${esc(x.order_id)}"><div class="row"><span class="t">${esc(x.customer_name)}</span><span class="pill ${statusPill(x.status)}">${esc(x.status)}</span></div><div class="row"><span class="m">${esc(x.order_id)} · ${x.items.map(i => `${i.qty}×${i.sku}`).join(', ')}</span><b class="num">${fmt(x.total)}</b></div></a>`).join('') || '<div class="empty">No orders.</div>'; };
    $('#f').innerHTML = ['', 'draft', 'confirmed', 'allocated', 'dispatched', 'delivered', 'short'].map(s => `<button class="chip" data-s="${s}">${s || 'all'}</button>`).join('');
    $('#f').onclick = e => { const b = e.target.closest('[data-s]'); if (b) load(b.dataset.s); };
    load('');
  };
  V.order = async id => {
    const o = await api('/api/orders/' + id);
    view.innerHTML = `<a class="back" href="#orders">‹ Orders</a><h1>${esc(o.order_id)}</h1><p class="sub">${esc(o.customer_name)} · <span class="pill ${statusPill(o.status)}">${esc(o.status)}</span> · ${esc(o.channel)}</p>
      <div class="card"><div class="kv">${o.items.map(i => `<b>${esc(i.sku)}</b><span>${i.qty} × ${fmt(i.unit_price)} = <b class="num">${fmt(i.qty * i.unit_price)}</b></span>`).join('')}<b>Total</b><span class="num"><b>${fmt(o.total)}</b></span><b>Load</b><span>${o.load_units} units</span>${o.warehouse_id ? `<b>Godown</b><span>${esc(o.warehouse_id)}</span>` : ''}${o.source_text ? `<b>Said</b><span>“${esc(o.source_text)}”</span>` : ''}</div></div>
      ${state.role !== 'driver' ? `<div class="btnrow" style="margin-top:12px">${o.status === 'draft' ? `<button class="btn primary" data-say="confirm ${esc(o.order_id)}">Confirm (customer said yes)</button>` : ''}${o.status === 'confirmed' ? `<button class="btn primary" data-say="allocate ${esc(o.order_id)} at WH-MULTAN">Allocate at Multan</button><button class="btn" data-say="allocate ${esc(o.order_id)} at WH-VEHARI">Allocate at Vehari</button>` : ''}</div>` : ''}`;
    view.querySelectorAll('[data-say]').forEach(b => b.onclick = () => { sessionStorage.setItem('munshi.prefill', b.dataset.say); location.hash = '#chat'; });
  };

  V.dispatch = async () => {
    view.innerHTML = '<h1>Dispatch</h1><p class="sub">Plans for today and recent days.</p><div id="pl" class="list"></div>';
    const plans = await api('/api/plans');
    $('#pl').innerHTML = (state.role !== 'driver' ? `<button class="btn primary" data-say="suggest dispatch for today" style="margin-bottom:6px">Ask Godown Munshi to suggest today's plan</button>` : '') +
      (plans.map(p => `<a class="item tap" href="#plan/${esc(p.plan_id)}"><div class="row"><span class="t">${esc(p.route_name)} · ${esc(p.plate)}</span><span class="pill ${statusPill(p.status)}">${esc(p.status)}</span></div><span class="m">${esc(p.plan_id)} · ${esc(p.plan_date)} · ${p.stops.length} stops · ${p.load_units} units · ${p.stops.filter(s => s.status !== 'pending').length}/${p.stops.length} closed</span></a>`).join('') || '<div class="empty">No plans yet. Allocate confirmed orders, then ask for a dispatch suggestion.</div>');
    view.querySelectorAll('[data-say]').forEach(b => b.onclick = () => { sessionStorage.setItem('munshi.prefill', b.dataset.say); location.hash = '#chat'; });
  };
  V.plan = async id => {
    const p = (await api('/api/plans')).find(x => x.plan_id === id); if (!p) { view.innerHTML = '<div class="empty">Plan not found.</div>'; return; }
    view.innerHTML = `<a class="back" href="#dispatch">‹ Dispatch</a><h1>${esc(p.route_name)}</h1><p class="sub">${esc(p.plan_id)} · ${esc(p.plate)} · ${p.load_units} units · <span class="pill ${statusPill(p.status)}">${esc(p.status)}</span></p>
      ${state.role !== 'driver' && p.status === 'planned' ? `<button class="btn primary" data-say="approve ${esc(p.plan_id)}" style="margin-bottom:10px">Approve loading (stock leaves the godown)</button>` : ''}
      ${state.role !== 'driver' && p.status !== 'planned' && p.stops.every(s => s.status !== 'pending') ? `<button class="btn primary" id="dep" style="margin-bottom:10px">Record driver's cash hand-in</button>` : ''}
      <div class="list">${p.stops.map(s => `<div class="item"><div class="row"><span class="t">#${s.sequence} ${esc(s.customer_name)}</span><span class="pill ${statusPill(s.status)}">${esc(s.status)}</span></div><span class="m">${s.items.map(i => `${i.qty}×${i.sku}`).join(', ')} · ${fmt(s.order_total)}${s.cash_collected ? ' · cash ' + fmt(s.cash_collected) : ''}${s.otp && state.role !== 'driver' ? ` · <span class="num">OTP ${esc(s.otp)}</span> <span class="hint">(customer's)</span>` : ''}</span></div>`).join('')}</div>`;
    view.querySelectorAll('[data-say]').forEach(b => b.onclick = () => { sessionStorage.setItem('munshi.prefill', b.dataset.say); location.hash = '#chat'; });
    $('#dep')?.addEventListener('click', () => { const amt = prompt('Cash counted from driver (Rs)'); if (amt) { sessionStorage.setItem('munshi.prefill', `${p.plan_id} driver handed ${amt}`); location.hash = '#chat'; } });
  };

  V.driver = async () => {
    view.innerHTML = '<h1>Driver mode</h1><p class="sub">Your stops in order. Get the OTP from the customer before you close a stop.</p><div id="dl" class="list"></div>';
    const plans = (await api('/api/plans')).filter(p => p.status === 'approved' || p.status === 'loaded');
    $('#dl').innerHTML = plans.map(p => `<h2>${esc(p.route_name)} · ${esc(p.plate)}</h2>` + p.stops.map(s => `<div class="item ${s.status === 'pending' ? 'tap' : ''}" data-stop='${esc(JSON.stringify({ id: s.stop_id, name: s.customer_name, items: s.items, total: s.order_total }))}' ${s.status !== 'pending' ? 'style="opacity:.6"' : ''}><div class="row"><span class="t">#${s.sequence} ${esc(s.customer_name)}</span><span class="pill ${statusPill(s.status)}">${esc(s.status)}</span></div><span class="m">${s.items.map(i => `${i.qty}×${i.sku}`).join(', ')} · ${fmt(s.order_total)}</span></div>`).join('')).join('') || '<div class="empty">No approved plan today.</div>';
    $('#dl').onclick = e => { const it = e.target.closest('.item.tap'); if (it) openStop(JSON.parse(it.dataset.stop)); };
  };
  function openStop(s) {
    view.innerHTML = `<a class="back" href="#driver">‹ Stops</a><h1>${esc(s.name)}</h1><p class="sub">${esc(s.id)} · order ${fmt(s.total)}</p>
      <form id="cf" class="stack"><div class="card stack"><b>Delivered</b>${s.items.map(i => `<div class="stopline"><span>${esc(i.sku)} <span class="hint">(ordered ${i.qty})</span></span><input class="input num" inputmode="numeric" name="d_${esc(i.sku)}" value="${i.qty}" min="0" max="${i.qty}"></div>`).join('')}</div>
      <div class="card stack"><b>Returned (empties / rejected)</b>${s.items.map(i => `<div class="stopline"><span>${esc(i.sku)}</span><input class="input num" inputmode="numeric" name="r_${esc(i.sku)}" value="0" min="0"></div>`).join('')}</div>
      <label class="f">Cash collected (Rs)<input class="input num" inputmode="numeric" name="cash" value="0"></label>
      <label class="f">Customer's OTP<input class="input num" inputmode="numeric" name="otp" maxlength="4" placeholder="4 digits" required></label>
      <button class="btn primary" type="submit">Close stop</button></form>`;
    $('#cf').onsubmit = async e => {
      e.preventDefault(); const f = new FormData(e.target);
      const delivered = s.items.map(i => ({ sku: i.sku, qty: Number(f.get('d_' + i.sku)) })).filter(x => x.qty > 0);
      const returned = s.items.map(i => ({ sku: i.sku, qty: Number(f.get('r_' + i.sku)) })).filter(x => x.qty > 0);
      try { const r = await post(`/api/stops/${s.id}/close`, { delivered_items: delivered, returned_items: returned, cash_collected: Number(f.get('cash')), otp: String(f.get('otp')) }); toast(`Closed: ${r.status}, invoiced ${fmt(r.invoiced)}`); location.hash = '#driver'; }
      catch (err) { toast(err.message); }
    };
  }

  V.stock = async () => {
    view.innerHTML = '<h1>Stock</h1><div id="sl" class="list"></div>';
    const s = await api('/api/stock'); const byW = {}; s.forEach(x => (byW[x.warehouse] ||= []).push(x));
    $('#sl').innerHTML = Object.entries(byW).map(([w, rows]) => `<h2>${esc(w)}</h2>` + rows.map(x => `<div class="item"><div class="row"><span class="t">${esc(x.name)}</span><b class="num" style="color:${x.available <= 10 ? 'var(--crit)' : 'var(--ink)'}">${x.available}</b></div><span class="m">${esc(x.sku)} · ${x.on_hand} on hand · ${x.reserved} reserved</span></div>`).join('')).join('');
  };
  V.customers = async () => {
    view.innerHTML = '<h1>Customers</h1><div id="cl" class="list"></div>';
    const c = await api('/api/customers');
    $('#cl').innerHTML = c.map(x => `<a class="item tap" href="#customer/${esc(x.customer_id)}"><div class="row"><span class="t">${esc(x.name)}</span><span class="pill">${esc(x.tier)}</span></div><div class="row"><span class="m">${esc(x.phone)} · ${esc(x.route_id || '')}</span><b class="num">${fmt(x.outstanding)}</b></div></a>`).join('');
  };
  V.reminders = async () => {
    view.innerHTML = '<h1>Reminders</h1><p class="sub">Templated only. Nothing free-text ever goes to a customer.</p><div id="rl" class="list"></div>';
    const r = await api('/api/reminders');
    $('#rl').innerHTML = r.map(x => `<div class="item"><div class="row"><span class="t">${esc(x.customer_name)}</span><span class="pill ${statusPill(x.status)}">${esc(x.status)}</span></div><span class="m">${esc(x.tier)} · ${x.days_overdue}d · ${fmt(x.amount_due)}</span><span style="font-size:14px">${esc(x.message)}</span>${x.status === 'drafted' && state.role !== 'driver' ? `<div class="btnrow"><button class="btn primary" data-send="${esc(x.reminder_id)}">Approve &amp; send</button></div>` : ''}</div>`).join('') || '<div class="empty">No reminders drafted. Ask Wasooli Munshi in chat.</div>';
    $('#rl').onclick = async e => { const b = e.target.closest('[data-send]'); if (!b) return; b.disabled = true; try { await post(`/api/reminders/${b.dataset.send}/send`, {}); toast('Sent'); V.reminders(); } catch (err) { toast(err.message); b.disabled = false; } };
  };
  V.audit = async () => {
    view.innerHTML = '<h1>Audit log</h1><p class="sub">Every write, who did it, and who approved it.</p><div id="al" class="list"></div>';
    const a = await api('/api/audit');
    $('#al').innerHTML = a.map(x => `<div class="item"><div class="row"><span class="t">${esc(x.action.replace(/_/g, ' '))}</span><span class="pill ${x.approved_by ? 'good' : ''}">${x.approved_by ? 'by ' + esc(x.approved_by) : 'auto'}</span></div><span class="m">${esc(x.actor)} · ${esc(x.entity)} ${esc(x.entity_id)} · ${esc(x.created_at.replace('T', ' ').slice(0, 16))}</span></div>`).join('');
  };
  V.settings = async () => {
    const me = state.me || await api('/api/me');
    view.innerHTML = `<h1>Settings</h1><div class="stack">
      <div class="card kv"><b>Signed in as</b><span>${esc(me.role)}</span><b>Business</b><span>${esc(me.business)}</span><b>Model</b><span>${esc(me.llm)}${me.llm === 'stub' ? ' <span class="hint">(offline rules — set LLM_PROVIDER=groq for a real model)</span>' : ''}</span><b>Voice orders</b><span>${me.voice ? 'on' : 'off <span class="hint">(needs GROQ_API_KEY)</span>'}</span></div>
      ${me.role !== 'driver' ? `<a class="btn" href="/api/export.xlsx" onclick="event.preventDefault();window.munshiExport()">Export everything to Excel</a>` : ''}
      <button class="btn" id="install" hidden>Install on this phone</button>
      <button class="btn danger" id="out">Sign out</button></div>`;
    $('#out').onclick = signOut;
    if (window.__installPrompt) { $('#install').hidden = false; $('#install').onclick = async () => { window.__installPrompt.prompt(); }; }
  };
  window.munshiExport = async () => { const res = await fetch('/api/export.xlsx', { headers: { 'X-Session': state.token } }); if (!res.ok) return toast('Export failed'); const b = await res.blob(); const u = URL.createObjectURL(b); const a = document.createElement('a'); a.href = u; a.download = 'munshi-export.xlsx'; a.click(); URL.revokeObjectURL(u); };

  function setupMic(btn, msgs) {
    let rec, chunks = [];
    const start = async () => { try { const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); rec = new MediaRecorder(stream); chunks = []; rec.ondataavailable = e => chunks.push(e.data); rec.onstop = async () => { stream.getTracks().forEach(t => t.stop()); const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' }); const fd = new FormData(); fd.append('audio', blob, 'note.webm'); msgs.insertAdjacentHTML('beforeend', '<div class="msg me">🎙 …</div>'); try { const res = await fetch('/api/voice?thread_id=' + state.thread, { method: 'POST', headers: { 'X-Session': state.token }, body: fd }); const r = await res.json(); if (!res.ok) throw new Error(r.detail); msgs.lastElementChild.textContent = '🎙 ' + r.transcript; msgs.insertAdjacentHTML('beforeend', renderMsg({ role: 'munshi', text: r.text, meta: { specialist: r.specialist } })); if (r.pending) msgs.insertAdjacentHTML('beforeend', renderApproval(r.pending, true)); } catch (err) { toast(err.message); } refreshBadge(); }; rec.start(); btn.classList.add('rec'); } catch (e) { toast('Microphone not available'); } };
    const stop = () => { if (rec && rec.state === 'recording') rec.stop(); btn.classList.remove('rec'); };
    btn.addEventListener('pointerdown', start); btn.addEventListener('pointerup', stop); btn.addEventListener('pointerleave', stop);
  }

  // ---------------------------------------------------------------- router
  async function render() {
    const h = (location.hash || '#today').slice(1);
    if (!state.token) { nav.hidden = true; return V.login(); }
    const [name, arg] = h.split('/');
    nav.hidden = false;
    nav.querySelectorAll('a').forEach(a => a.classList.toggle('active', a.dataset.tab === name || (name === 'customer' && a.dataset.tab === 'khata') || (['orders', 'order', 'dispatch', 'plan', 'driver', 'stock', 'customers', 'reminders', 'audit', 'settings'].includes(name) && a.dataset.tab === 'more')));
    try { await (V[name] ? V[name](arg) : V.today()); } catch (err) { view.innerHTML = `<div class="empty">⚠ ${esc(err.message)}</div>`; }
    refreshBadge();
  }
  async function boot() {
    if (!state.token) return render();
    try { state.me = await api('/api/me'); rolePill.textContent = state.me.role; rolePill.hidden = false; }
    catch { return; }
    render();
  }
  window.addEventListener('hashchange', render);
  window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); window.__installPrompt = e; });
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => { });
  boot();
})();
