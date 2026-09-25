/* Munshi views. Each role gets its own home and navigation; every screen checks permissions from /api/me. */
(() => {
  const { $, $$, view, state, t, setLang, fmt, num, esc, day, when, statusPill, bucketPill, tierPill, can, role, today, daysAgo, toast, api, post, patch, del, download,
          signOut, signedIn, home, MORE, sheet, confirmSheet, field, select, enqueue, render, refreshBadge } = M;
  const V = window.V = {};
  const h1 = (title, sub = '', back = '') => `${back ? `<a class="back" href="#${back}">‹ ${esc(t('back'))}</a>` : ''}<h1>${esc(title)}</h1>${sub ? `<p class="sub">${sub}</p>` : ''}`;
  const kpi = (k, v, s = '', cls = '') => `<div class="card tile ${cls}"><div class="k">${esc(k)}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
  const empty = txt => `<div class="empty">${esc(txt || t('nothing'))}</div>`;
  const item = (inner, href = '', extra = '') => href ? `<a class="item tap" href="${href}" ${extra}>${inner}</a>` : `<div class="item" ${extra}>${inner}</div>`;
  const row = (l, r) => `<div class="row"><span class="t">${l}</span>${r}</div>`;
  const meta = s => `<span class="m">${s}</span>`;
  const btn = (label, attrs = '', cls = 'btn') => `<button class="${cls}" ${attrs}>${esc(label)}</button>`;
  const prefill = txt => { sessionStorage.setItem('munshi.prefill', txt); location.hash = '#chat'; };
  const go = h => { location.hash = h; };
  const bars = (rows, key, label, max) => {   // tiny inline bar chart
    const mx = max || Math.max(1, ...rows.map(r => r[key]));
    return `<div class="bars">${rows.map(r => `<div class="bar"><span class="bl">${esc(r[label])}</span><span class="bt"><i style="width:${Math.max(2, r[key] / mx * 100)}%"></i></span><span class="bv num">${fmt(r[key])}</span></div>`).join('')}</div>`;
  };
  const dateRange = (id, start, end) => `<div class="row" style="gap:6px;margin:6px 0 10px"><input class="input" type="date" id="${id}s" value="${start}"><input class="input" type="date" id="${id}e" value="${end}"><button class="btn" id="${id}go">→</button></div>`;

  // ================================================================ login / signup
  V.login = async () => {
    let cfg = { demo: false, demo_users: [], signup_open: true }; try { cfg = await api('/api/config'); } catch { }
    view.innerHTML = `<div class="login"><div>
      <div class="mark"><i></i></div><h1>${esc(t('app'))}</h1><p class="sub">${esc(t('tagline'))}</p>
      <form id="lf" class="stack" style="max-width:320px;margin:0 auto">
        ${field(t('phone'), 'phone', 'inputmode="tel" autocomplete="tel" placeholder="0300-1234567" required', 'tel')}
        ${field(t('pin'), 'pin', 'inputmode="numeric" autocomplete="current-password" maxlength="6" required', 'password')}
        <button class="btn primary" type="submit">${esc(t('signin'))}</button></form>
      ${cfg.demo ? `<p class="hint" style="margin-top:16px">${esc(t('demo_accounts'))}</p><div class="chips center">${cfg.demo_users.map(u => `<button class="chip" data-phone="${esc(u.phone)}" data-pin="${esc(u.pin)}">${esc(u.role)} · ${esc(u.name.split(' ')[0])}</button>`).join('')}</div>` : ''}
      ${cfg.signup_open ? `<p style="margin-top:18px"><a class="link" href="#signup">${esc(t('create_business'))} ›</a></p>` : ''}
      <p class="hint" style="margin-top:6px"><button class="link hit" data-lang="en" lang="en" aria-pressed="${state.lang === 'en'}">English</button> · <button class="link hit" data-lang="ur" lang="ur" aria-pressed="${state.lang === 'ur'}">اردو</button></p>
    </div></div>`;
    $$('[data-lang]').forEach(b => b.onclick = () => setLang(b.dataset.lang));
    $$('[data-phone]').forEach(b => b.onclick = () => { $('[name=phone]').value = b.dataset.phone; $('[name=pin]').value = b.dataset.pin; $('#lf').requestSubmit(); });
    $('#lf').onsubmit = async e => {
      e.preventDefault(); const f = new FormData(e.target);
      try { const r = await post('/api/session', { phone: f.get('phone'), pin: f.get('pin'), device: navigator.userAgent.slice(0, 60) }); await signedIn(r); if (r.other_businesses?.length) toast(`You also work at ${r.other_businesses.map(b => b.name).join(', ')} — switch in Settings.`, 5000); }
      catch (err) { toast(err.message, 4000); }
    };
  };
  V.signup = () => {
    view.innerHTML = `<div class="login"><div style="width:100%;max-width:360px;margin:0 auto">
      <h1>${esc(t('create_business'))}</h1><p class="sub">${esc(t('tagline'))}</p>
      <form id="sf" class="stack">
        ${field(t('business_name'), 'business_name', 'required minlength="2" maxlength="80"')}
        ${field(t('city'), 'city', 'maxlength="60"')}
        ${field(t('your_name'), 'owner_name', 'required minlength="2"')}
        ${field(t('phone'), 'phone', 'inputmode="tel" required placeholder="0300-1234567"', 'tel')}
        ${field(t('choose_pin'), 'pin', 'inputmode="numeric" minlength="4" maxlength="6" required', 'password')}
        ${select(t('language'), 'language', [['en', 'English'], ['ur', 'اردو']], state.lang)}
        <label class="chk"><input type="checkbox" name="sample_data" checked> ${esc(t('sample_data'))}</label>
        <button class="btn primary" type="submit">${esc(t('create_business'))}</button>
        <a class="link" href="#login">${esc(t('have_account'))}</a></form></div></div>`;
    $('#sf').onsubmit = async e => {
      e.preventDefault(); const f = new FormData(e.target); const body = Object.fromEntries(f.entries()); body.sample_data = f.get('sample_data') === 'on';
      try { await signedIn(await post('/api/signup', body)); toast(`${t('welcome')}, ${body.owner_name}!`); } catch (err) { toast(err.message, 5000); }
    };
  };

  // ================================================================ owner: Today
  function setupCard(me) {
    const s = me.setup; const steps = [['products', s.products, '#products'], ['customers', s.customers, '#customers'], ['vehicle', s.vehicles, '#setup'], ['route', s.routes, '#setup'], ['staff', s.staff - 1, '#staff']];
    const done = steps.filter(x => x[1] > 0).length;
    if (done === steps.length) return '';
    return `<div class="card" style="margin-bottom:10px"><div class="row"><b>${esc(t('setup_progress'))}</b><span class="pill acc">${done}/${steps.length}</span></div><p class="sub" style="margin:4px 0 8px">${esc(t('setup_hint'))}</p>
      <div class="chips">${steps.map(([k, n, href]) => `<a class="chip ${n > 0 ? 'ok' : ''}" href="${href}">${n > 0 ? '✓ ' : '+ '}${esc(t(k) === k ? k : t(k))}</a>`).join('')}</div></div>`;
  }
  V.today = async () => {
    view.innerHTML = h1(t('today'), t('loading'));
    const [d, notes] = await Promise.all([api('/api/digest'), api('/api/notifications?unread=true')]);
    const cashGap = d.cash.collected - d.cash.deposited;
    const alerts = notes.filter(n => n.kind !== 'approval').slice(0, 5);
    view.innerHTML = h1(t('today'), `${d.date} · ${esc(d.business)}`) + setupCard(state.me) +
      (d.pending_approvals ? `<a href="#approvals" class="card row alert" style="margin-bottom:10px"><span><b>${d.pending_approvals} ${esc(t('waiting_approval'))}</b><br><span class="sub" style="margin:0">${esc(t('nothing_moves'))}</span></span><span class="pill acc">${esc(t('approve'))}</span></a>` : '') +
      `<div class="grid2">
        ${kpi(t('orders_today'), d.orders.count, `${fmt(d.orders.value)}${d.orders.draft ? ` · ${d.orders.draft} ${t('draft')}` : ''}`)}
        ${kpi(t('deliveries'), `${d.dispatch.delivered}<span class="dim">/${d.dispatch.stops}</span>`, `${d.dispatch.plans} plan${d.dispatch.plans === 1 ? '' : 's'}${d.dispatch.short ? ` · <span class="crit">${d.dispatch.short} ${t('short')}</span>` : ''}`)}
        ${kpi(t('cash_collected'), fmt(d.cash.collected + d.cash.office_payments), d.cash.deposited ? `hand-in ${fmt(d.cash.deposited)}${cashGap > 0 ? ` · <span class="crit">${fmt(cashGap)} ${t('short')}</span>` : ''}` : `office ${fmt(d.cash.office_payments)}`)}
        ${kpi(t('receivables'), fmt(d.receivables.total), `${d.receivables.customers} ${t('accounts')}${d.receivables.overdue_60 ? ` · <span class="crit">${fmt(d.receivables.overdue_60)} 60+</span>` : ''}`)}
        ${kpi(t('invoiced_today'), fmt(d.sales.invoiced), `${t('expenses_today')} ${fmt(d.cash.expenses)}`)}
        ${kpi(t('payables'), fmt(d.payables), d.broken_promises ? `<span class="crit">${d.broken_promises} ${t('broken')} ${t('promises').toLowerCase()}</span>` : `${d.pending_reminders} ${t('reminders').toLowerCase()} drafted`)}
      </div>
      ${alerts.length ? `<h2>${esc(t('notifications'))}</h2><div class="list">${alerts.map(n => `<a class="item tap" href="#notifications"><div class="row"><span class="t">${esc(n.text)}</span></div>${meta(when(n.created_at))}</a>`).join('')}</div>` : ''}
      ${d.low_stock.length ? `<h2>${esc(t('low_stock'))}</h2><div class="chips">${d.low_stock.map(s => `<a class="chip" href="#stock">${esc(s.sku)} · ${s.available} @ ${esc(s.warehouse_id.replace('WH-', ''))}</a>`).join('')}</div>` : ''}
      <h2>${esc(t('ask_munshi'))}</h2>
      <div class="chips">${['Profit this month', 'Kaun kitna baqi hai?', 'Aaj ka cashbook', 'Slow stock 30 days', 'What do we owe suppliers', 'Remind everyone over 30 days'].map(q => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      <h2>${esc(t('reports'))}</h2><div class="chips">${[['sales', 'sales'], ['profit', 'profit'], ['collections', 'collections'], ['cashbook', 'cashbook'], ['valuation', 'valuation']].map(([k, l]) => `<a class="chip" href="#reports/${k}">${esc(t(l))}</a>`).join('')}</div>`;
    $$('[data-q]').forEach(b => b.onclick = () => prefill(b.dataset.q));
  };

  // ================================================================ clerk: Desk
  V.desk = async () => {
    view.innerHTML = h1(t('desk'), t('loading'));
    const [d, orders, plans] = await Promise.all([api('/api/digest'), api('/api/orders'), api('/api/plans')]);
    const by = s => orders.filter(o => o.status === s);
    const open = plans.filter(p => ['approved', 'loaded'].includes(p.status));
    const toDeposit = plans.filter(p => p.status !== 'planned' && p.status !== 'cancelled' && p.stops.every(s => s.status !== 'pending') && p.deposited < p.cash_collected);
    view.innerHTML = h1(t('desk'), `${d.date} · ${esc(d.business)}`) + setupCard(state.me) +
      (d.pending_approvals ? `<a href="#approvals" class="card row alert" style="margin-bottom:10px"><span><b>${d.pending_approvals} ${esc(t('waiting_approval'))}</b></span><span class="pill acc">${esc(t('approve'))}</span></a>` : '') +
      `<div class="grid2">
        ${kpi(t('draft'), by('draft').length, 'to confirm')}${kpi(t('confirmed'), by('confirmed').length, 'to allocate')}
        ${kpi(t('allocated'), by('allocated').length, 'to plan')}${kpi(t('stops'), `${d.dispatch.delivered}<span class="dim">/${d.dispatch.stops}</span>`, `${open.length} open plan${open.length === 1 ? '' : 's'}`)}
      </div>
      <div class="btnrow" style="margin:12px 0">${btn(t('new_order'), 'id="newOrder"', 'btn primary')}${btn(t('record_payment'), 'id="pay"')}${btn(t('add_expense'), 'id="exp"')}${btn(t('receive_stock'), 'id="recv"')}</div>
      ${toDeposit.length ? `<h2>${esc(t('record_handin'))}</h2><div class="list">${toDeposit.map(p => `<a class="item tap" href="#plan/${p.plan_id}">${row(`${esc(p.route_name)} · ${esc(p.plate)}`, `<b class="num">${fmt(p.cash_collected)}</b>`)}${meta(`${p.plan_id} · ${p.plan_date}`)}</a>`).join('')}</div>` : ''}
      ${by('draft').length ? `<h2>${esc(t('draft'))} → ${esc(t('confirm'))}</h2><div class="list">${by('draft').map(orderRow).join('')}</div>` : ''}
      ${by('confirmed').length ? `<h2>${esc(t('confirmed'))} → ${esc(t('allocate'))}</h2><div class="list">${by('confirmed').map(orderRow).join('')}</div>` : ''}
      ${by('allocated').length ? `<h2>${esc(t('allocated'))} → ${esc(t('plan_dispatch'))}</h2><div class="list">${by('allocated').map(orderRow).join('')}</div><div class="btnrow" style="margin-top:8px"><a class="btn primary" href="#dispatch">${esc(t('plan_dispatch'))} ›</a></div>` : ''}
      ${open.length ? `<h2>${esc(t('dispatch'))}</h2><div class="list">${open.map(planRow).join('')}</div>` : ''}
      <h2>${esc(t('ask_munshi'))}</h2>
      <div class="chips">${['Chaudhry Farms ko 20 urea aur 5 dap bhej do', 'Suggest dispatch for today', 'Kaun kitna baqi hai?', 'Aaj ka cashbook', 'Remind everyone over 30 days'].map(q => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>`;
    $$('[data-q]').forEach(b => b.onclick = () => prefill(b.dataset.q));
    $('#newOrder').onclick = () => orderForm(); $('#pay').onclick = () => paymentForm(); $('#exp').onclick = () => expenseForm(); $('#recv').onclick = () => purchaseForm();
  };
  const orderRow = o => `<a class="item tap" href="#order/${esc(o.order_id)}">${row(esc(o.customer_name), `<span class="pill ${statusPill(o.status)}">${esc(t(o.status))}</span>`)}<div class="row">${meta(`${esc(o.order_id)} · ${o.items.map(i => `${i.qty}×${i.sku}`).join(', ')}`)}<b class="num">${fmt(o.total)}</b></div></a>`;
  const planRow = p => `<a class="item tap" href="#plan/${esc(p.plan_id)}">${row(`${esc(p.route_name)} · ${esc(p.plate)}`, `<span class="pill ${statusPill(p.status)}">${esc(t(p.status))}</span>`)}${meta(`${esc(p.plan_id)} · ${esc(p.plan_date)} · ${p.stops.length} ${t('stops').toLowerCase()} · ${p.load_units} units · ${p.stops.filter(s => s.status !== 'pending').length}/${p.stops.length} closed`)}</a>`;

  // ================================================================ salesman: Book
  V.book = async () => {
    view.innerHTML = h1(t('book'), 'Book a draft order at the counter. The office confirms it.');
    view.innerHTML += `<div id="bookForm"></div>`;
    await orderForm($('#bookForm'));
  };
  async function orderForm(container, existing) {
    const [customers, products] = await Promise.all([api('/api/customers'), api('/api/products')]);
    const canPrice = role() !== 'salesman';
    const html = `<div class="stack">
      <label class="f">${esc(t('customer'))}<input class="input" list="custs" name="customer" placeholder="${esc(t('search'))}…" required autocomplete="off" ${existing ? 'readonly' : ''} value="${existing ? esc(existing.customer_name) : ''}"><datalist id="custs">${customers.map(c => `<option value="${esc(c.name)}">`).join('')}</datalist></label>
      <div id="custInfo" class="hint"></div>
      <div id="lines" class="stack"></div>
      <button type="button" class="btn" id="addLine">+ ${esc(t('items'))}</button>
      ${field(t('note'), 'notes', 'maxlength="200"', 'text', existing?.notes || '')}
      <div class="row"><b>${esc(t('total'))}</b><b class="num" id="ototal">Rs 0</b></div></div>`;
    const submit = async data => {
      const cust = customers.find(c => c.name === data.customer); if (!cust) throw new Error('Pick a customer from the list');
      const items = form.__items();
      if (!items.length) throw new Error('Pick at least one product from the list');
      if (existing) { const r = await patch('/api/orders/' + existing.order_id, { items, notes: data.notes || '' }); toast(t('saved')); go('#order/' + r.order_id); render(); return; }
      const r = await post('/api/orders', { customer_id: cust.customer_id, items, notes: data.notes || '' });
      toast(r.credit_hold ? `Saved as draft — credit hold: ${r.credit_hold}` : `${t('saved')}: ${r.order_id}`, r.credit_hold ? 6000 : 3000);
      go('#order/' + r.order_id);
    };
    let form;
    if (container) {
      container.innerHTML = `<form id="bookF">${html}<div class="btnrow" style="margin-top:10px"><button class="btn primary" type="submit">${esc(t('save'))}</button></div></form>`;
      form = container.querySelector('#bookF'); form.onsubmit = async e => { e.preventDefault(); try { await submit(Object.fromEntries(new FormData(form).entries())); } catch (err) { toast(err.message, 4000); } };
    } else { form = sheet(existing ? `${t('edit')} ${existing.order_id}` : t('new_order'), html, submit).form; }
    const lines = $('#lines', form); const totalEl = $('#ototal', form);
    const dl = document.createElement('datalist'); dl.id = 'prods'; dl.innerHTML = products.map(p => `<option value="${esc(p.name)} (${esc(p.sku)})">`).join(''); form.appendChild(dl);
    const findProd = v => { const m = v.match(/\(([^)]+)\)\s*$/); const sku = m ? m[1] : v.trim().toUpperCase(); return products.find(p => p.sku === sku) || products.find(p => p.name.toLowerCase() === v.trim().toLowerCase()) || products.find(p => (p.aliases || []).some(a => a.toLowerCase() === v.trim().toLowerCase())); };
    const addLine = (pre) => { const d = document.createElement('div'); d.className = 'oline row'; d.innerHTML = `<input class="input psel" list="prods" placeholder="${esc(t('products'))}…" autocomplete="off"><input class="input num qty" type="number" min="1" value="${pre ? pre.qty : 1}" style="width:72px">${canPrice ? `<input class="input num price" type="number" min="0" placeholder="${esc(t('rate'))}" style="width:88px" title="negotiated price (blank = list)">` : ''}<button type="button" class="x" aria-label="Remove line">✕</button>`; d.querySelector('.x').onclick = () => { d.remove(); recalc(); }; d.addEventListener('input', recalc); lines.appendChild(d); if (pre) { const p = products.find(x => x.sku === pre.sku); d.querySelector('.psel').value = p ? `${p.name} (${p.sku})` : pre.sku; if (canPrice) d.querySelector('.price').value = pre.unit_price; } else if (products.length <= 12 && lines.children.length === 1) d.querySelector('.psel').value = `${products[0].name} (${products[0].sku})`; recalc(); };
    const lineItems = () => $$('.oline', form).map(l => ({ p: findProd(l.querySelector('.psel').value), qty: Number(l.querySelector('.qty').value || 0), price: canPrice && l.querySelector('.price').value !== '' ? Number(l.querySelector('.price').value) : null }));
    const recalc = () => { const cust = customers.find(c => c.name === form.customer.value); const disc = cust ? cust.discount_pct || 0 : 0; totalEl.textContent = fmt(lineItems().reduce((s, x) => s + (x.p ? (x.price !== null ? x.price : x.p.unit_price * (1 - disc / 100)) * x.qty : 0), 0)); $$('.oline .psel', form).forEach(i => i.classList.toggle('bad', !!i.value && !findProd(i.value))); };
    $('#addLine', form).onclick = () => addLine(); if (existing) existing.items.forEach(addLine); else addLine();
    form.__items = () => lineItems().filter(x => x.p && x.qty > 0).map(x => ({ sku: x.p.sku, qty: x.qty, unit_price: x.price }));
    form.customer.addEventListener('input', () => { const c = customers.find(x => x.name === form.customer.value); $('#custInfo', form).innerHTML = c ? `${esc(c.customer_id)} · ${t('outstanding')} <b class="num">${fmt(c.outstanding)}</b> · ${t('limit')} ${fmt(c.credit_limit)}${c.outstanding > c.credit_limit && c.credit_limit ? ` · <span class="crit">${t('over_limit')}</span>` : ''}${c.discount_pct ? ` · ${c.discount_pct}% off` : ''}` : ''; recalc(); });
  }

  // ================================================================ driver: Stops
  // A close the office refused after an offline sync ("needs attention"): what the driver entered, why it was
  // refused in plain words, and what to do next. Lives in localStorage (core.js) until fixed or deliberately removed.
  const stamp = iso => { const d = new Date(iso); return isNaN(d) ? when(iso) : d.toLocaleString('en-PK', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }); };
  const lineList = ls => (ls || []).map(i => `${i.qty} × ${esc(i.sku)}`).join(', ') || '—';
  function attnCard(a, { open = true } = {}) {
    const b = a.body || {};
    const action = a.kind === 'code' ? t('fix_code') : a.kind === 'fix' ? t('fix_retry') : t('try_again');
    return `<div class="card attn" data-attn="${esc(a.id)}">
      <div class="row"><b>${esc(a.label || a.stop_id)}</b><span class="pill crit">${esc(a.kind === 'supervisor' ? t('see_supervisor') : t('needs_attention'))}</span></div>
      <p class="attn-why">${esc(t(a.why))}</p>
      <div class="kv"><b>${esc(t('delivered_qty'))}</b><span>${lineList(b.delivered_items)}</span><b>${esc(t('returned'))}</b><span>${lineList(b.returned_items)}</span>
        <b>${esc(t('cash_collected'))}</b><span class="num">${fmt(b.cash_collected)}</span><b>${esc(t('code_entered'))}</b><span class="num">${esc(b.otp || '—')}</span>
        ${b.note ? `<b>${esc(t('note'))}</b><span>${esc(b.note)}</span>` : ''}<b>${esc(t('saved_at'))}</b><span>${esc(stamp(a.queued_at))}</span>
        <b>${esc(t('server_said'))}</b><span class="hint">${esc(a.detail || '')}${a.attempts > 1 ? ` · ${esc(t('attempts'))} ${a.attempts}` : ''}</span></div>
      <div class="btnrow">${open ? `<a class="btn ${a.kind === 'supervisor' ? '' : 'primary'}" href="#stop/${esc(a.stop_id)}">${esc(action)}</a>` : ''}<button type="button" class="btn danger" data-attn-del="${esc(a.id)}">${esc(t('attn_remove'))}</button></div></div>`;
  }
  const onAttnRemove = e => {
    const b = e.target.closest('[data-attn-del]'); if (!b) return;
    confirmSheet(t('attn_remove_title'), t('attn_remove_q'), async () => { M.removeAttention(b.dataset.attnDel); render(); }, t('attn_remove'));
  };
  V.stops = async () => {
    const attn = state.attention;
    view.innerHTML = h1(t('stops'), t('your_stops')) +
      (attn.length ? `<h2 class="crit">⚠ ${esc(t('needs_attention'))} (${attn.length})</h2><p class="attn-intro">${esc(t('attn_intro'))}</p><div id="attnList" class="list">${attn.map(a => attnCard(a)).join('')}</div>` : '') +
      `<div id="queue" class="banner" hidden></div><div id="dl" class="list"></div>`;
    $('#attnList')?.addEventListener('click', onAttnRemove);
    let plans = [];
    try { plans = await api('/api/driver/today'); M.store.set('stops_cache', plans); } catch (e) { plans = M.store.get('stops_cache', []); if (!plans.length && !attn.length) throw e; toast(t('offline')); }
    const queued = new Set(state.queue.map(q => q.stop_id)); const refused = new Set(attn.map(a => a.stop_id));
    $('#dl').innerHTML = plans.map(p => `<h2>${esc(p.route_name)} · ${esc(p.plate)} · ${esc(p.plan_date)}</h2>` + p.stops.map(s => {
      const q = queued.has(s.stop_id), r = refused.has(s.stop_id); const open = (s.status === 'pending' && !q) || r;
      const pillHtml = r ? `<span class="pill crit">${esc(t('needs_attention'))}</span>` : `<span class="pill ${q ? 'warn' : statusPill(s.status)}">${q ? esc(t('queued')) : esc(t(s.status))}</span>`;
      return `<div class="item ${open ? 'tap' : ''}" data-stop="${esc(s.stop_id)}" ${!open ? 'style="opacity:.65"' : ''}>${row(`#${s.sequence} ${esc(s.customer_name)}`, pillHtml)}${meta(`${esc(s.address || '')}${s.address ? ' · ' : ''}${s.items.map(i => `${i.qty}×${i.sku}`).join(', ')} · ${fmt(s.order_total)}`)}${s.phone ? `<a class="call" href="tel:${esc(s.phone)}"><span aria-hidden="true">📞</span> ${esc(s.phone)}</a>` : ''}</div>`;
    }).join('')).join('') || empty(t('no_stops'));
    // today's stops, plus any older stop that still has a refused entry (so its form can still be reopened and resent)
    const oldIndex = M.store.get('stops_index', {}) || {};
    M.store.set('stops_index', Object.assign(Object.fromEntries([...refused].filter(k => oldIndex[k]).map(k => [k, oldIndex[k]])), Object.fromEntries(plans.flatMap(p => p.stops.map(s => [s.stop_id, s])))));
    // a tap on the phone number calls; it must not also open the stop
    $('#dl').onclick = e => { if (e.target.closest('a[href^="tel:"]')) return; const it = e.target.closest('.item.tap'); if (it) go('#stop/' + it.dataset.stop); };
  };
  V.stop = async id => {
    const s = (M.store.get('stops_index', {}) || {})[id]; let a = M.attentionFor(id);
    if (!s && !a) { go('#stops'); return; }
    if (!s) {   // the stop is no longer on today's plans (plan closed/cancelled): show what was entered, nothing to resend against
      view.innerHTML = h1(a.label || id, esc(id), 'stops') + `<div id="attnList" class="list">${attnCard(a, { open: false })}</div>`;
      $('#attnList').addEventListener('click', onAttnRemove); return;
    }
    // Prefill from a refused entry so the driver never re-types what they already entered; only the code starts empty when it was the problem.
    let pre = a ? a.body : null;
    const qtyOf = (ls, sku) => (ls || []).filter(x => x.sku === sku).reduce((n, x) => n + x.qty, 0);
    const dVal = i => pre ? qtyOf(pre.delivered_items, i.sku) : i.qty, rVal = i => pre ? qtyOf(pre.returned_items, i.sku) : 0;
    view.innerHTML = h1(s.customer_name, `${esc(id)} · ${fmt(s.order_total)}${s.address ? ' · ' + esc(s.address) : ''}`, 'stops') +
      (a ? `<div class="list" id="attnList" style="margin-bottom:12px">${attnCard(a, { open: false })}</div>` : '') +
      `<form id="cf" class="stack"><div class="card stack"><b>${esc(t('delivered_qty'))}</b>${s.items.map(i => `<div class="stopline"><span>${esc(i.sku)} <span class="hint">(${i.qty})</span></span><input class="input num" type="number" inputmode="numeric" name="d_${esc(i.sku)}" value="${dVal(i)}" min="0" max="${i.qty}"></div>`).join('')}</div>
      <div class="card stack"><b>${esc(t('returned'))}</b>${s.items.map(i => `<div class="stopline"><span>${esc(i.sku)}</span><input class="input num" type="number" inputmode="numeric" name="r_${esc(i.sku)}" value="${rVal(i)}" min="0"></div>`).join('')}</div>
      <label class="f">${esc(t('cash_collected'))} (Rs)<input class="input num big" type="number" inputmode="numeric" name="cash" value="${pre ? Number(pre.cash_collected) || 0 : 0}" min="0"></label>
      <label class="f">${esc(t('customer_code'))}<input class="input num big${a && a.kind === 'code' ? ' bad' : ''}" inputmode="numeric" name="otp" maxlength="4" pattern="\\d{4}" placeholder="• • • •" required autocomplete="one-time-code" value="${pre && a.kind !== 'code' ? esc(pre.otp || '') : ''}"></label>
      ${field(t('note'), 'note', 'maxlength="200" placeholder="e.g. 2 bags refused, torn"', 'text', pre ? pre.note || '' : '')}
      <p id="cfErr" class="formerr" role="alert"></p>
      <button class="btn primary big" type="submit">${esc(t('close_stop'))}</button></form>`;
    $('#attnList')?.addEventListener('click', onAttnRemove);
    if (a && a.kind === 'code') setTimeout(() => $('[name=otp]')?.focus(), 50);
    $('#cf').onsubmit = async e => {
      e.preventDefault(); const f = new FormData(e.target); const submitBtn = e.target.querySelector('button[type=submit]');
      const delivered = s.items.map(i => ({ sku: i.sku, qty: Number(f.get('d_' + i.sku)) })).filter(x => x.qty > 0);
      const returned = s.items.map(i => ({ sku: i.sku, qty: Number(f.get('r_' + i.sku)) })).filter(x => x.qty > 0);
      const cash = Number(f.get('cash'));
      // Resending a refused entry unchanged keeps its client_ref (the server replays, never double-posts); changed numbers get a new one.
      const same = pre && JSON.stringify([pre.delivered_items, pre.returned_items, Number(pre.cash_collected)]) === JSON.stringify([delivered, returned, cash]);
      const body = { delivered_items: delivered, returned_items: returned, cash_collected: cash, otp: String(f.get('otp')), client_ref: same && pre.client_ref ? pre.client_ref : id + ':' + Date.now(), note: String(f.get('note') || '') };
      const item = { path: `/api/stops/${id}/close`, body, stop_id: id, label: s.customer_name };
      $('#cfErr').textContent = ''; submitBtn.disabled = true; submitBtn.textContent = t('closing');
      try { const r = await post(item.path, body); M.clearAttention(id); toast(`${t('done')}: ${t(r.status)}, ${fmt(r.invoiced)}`); go('#stops'); }
      catch (err) {
        submitBtn.disabled = false; submitBtn.textContent = t('close_stop');
        if (err.status === 0 || err.status === 401) { enqueue(item); M.clearAttention(id); toast(t('queued_offline'), 4000); go('#stops'); return; }   // no signal / session expired: keep it on the phone, it sends itself later
        if (a && !M.retryLater(err.status)) {   // a retry of a refused entry was refused again: keep the latest attempt, with the new reason
          a = M.addAttention(item, err, a.id); pre = a.body; $('#attnList').innerHTML = attnCard(a, { open: false });
        }
        $('#cfErr').textContent = err.message; toast(err.message, 4000);
      }
    };
  };
  V.driver = V.stops;

  // ================================================================ chat + approvals
  const DETAILS = '\n\nDone -- ';   // platform.DETAILS: a readable reply, then its raw result (folded under "details")
  function renderMsg(m) {
    if (m.role === 'munshi' || m.role === 'bot') {
      const who = m.meta?.specialist ? m.meta.specialist + ' munshi' : 'munshi';
      let text = m.text, extra = '';
      const cut = text.indexOf(DETAILS);   // a readable reply with the raw result folded after it
      if (cut > 0) { const raw = text.slice(cut + DETAILS.length); text = text.slice(0, cut); let o = raw; try { o = JSON.stringify(JSON.parse(raw), null, 1); } catch { /* keep raw */ } extra = `<details><summary class="hint">details</summary><pre class="json">${esc(o)}</pre></details>`; }
      else if (text.startsWith('Done -- ')) { const raw = text.slice(8); try { const o = JSON.parse(raw); text = summarize(o); extra = `<details><summary class="hint">details</summary><pre class="json">${esc(JSON.stringify(o, null, 1))}</pre></details>`; } catch { text = raw; } }
      return `<div class="msg bot"><span class="who">${esc(who)}${m.meta?.resolved ? ' · ' + (m.meta.approved ? t('approved') : t('reject')) : ''}</span>${esc(text)}${extra}</div>`;
    }
    return `<div class="msg me">${esc(m.text)}${m.meta?.user && m.meta.user !== state.me?.name ? `<span class="who">${esc(m.meta.user)}</span>` : ''}</div>`;
  }
  function summarize(o) {
    if (o && o.error) return '⚠ ' + o.error;
    if (Array.isArray(o)) {
      if (!o.length) return t('nothing');
      if (o[0].route_id && o[0].order_ids) return o.map(p => `${p.route_id}: ${p.order_ids.length} order(s), ${p.load_units} units → ${p.vehicle_id || 'no vehicle fits'}${p.note ? ' (' + p.note + ')' : ''}`).join('\n');
      if (o[0].reminder_id) return `Drafted ${o.length} reminder(s):\n` + o.map(r => `• ${r.customer_id} (${r.tier}, ${r.days_overdue}d, ${fmt(r.amount_due)})`).join('\n');
      if (o[0].balance !== undefined && o[0].bucket) return o.map(a => `• ${a.name}: ${fmt(a.balance)} · ${a.days_overdue}d (${a.bucket})`).join('\n');
      if (o[0].balance !== undefined && o[0].supplier_id) return o.map(a => `• ${a.name}: ${fmt(a.balance)}`).join('\n');
      if (o[0].order_id) return o.map(x => `• ${x.order_id} ${x.customer_name || x.customer_id} — ${fmt(x.total)} · ${x.status}`).join('\n');
      if (o[0].stop_id) return o.map(s => `• #${s.sequence} ${s.customer_name || s.customer_id} — ${s.status}${s.cash_collected ? ' · cash ' + fmt(s.cash_collected) : ''}`).join('\n');
      if (o[0].warehouse_id && o[0].available !== undefined) return o.map(s => `• ${s.warehouse_id}: ${s.available} available (${s.on_hand} on hand, ${s.reserved} reserved)`).join('\n');
      if (o[0].days_without_sale !== undefined) return o.map(s => `• ${s.name}: ${s.on_hand} on hand, ${fmt(s.value_at_cost)} at cost`).join('\n');
      if (o[0].revenue !== undefined && o[0].name) return o.map(s => `• ${s.name}: ${fmt(s.revenue)}`).join('\n');
      if (o[0].promise_id && o[0].broken !== undefined) return o.map(p => `• ${p.name}: ${fmt(p.amount)} promised by ${p.date}`).join('\n');
      return JSON.stringify(o).slice(0, 300);
    }
    if (o.order_id && o.items) return `Order ${o.order_id} for ${o.customer_name || o.customer_id}: ${o.items.map(i => `${i.qty} × ${i.sku}`).join(', ')} — ${fmt(o.total)} · ${o.status}${o.status === 'draft' ? `\nReply "confirm ${o.order_id}" once the customer says yes.` : ''}`;
    if (o.plan_id && o.order_ids) return `Plan ${o.plan_id}: ${o.route_id} on ${o.vehicle_id}, ${o.order_ids.length} order(s), ${o.load_units} units · ${o.status}${o.status === 'planned' ? `\nReply "approve ${o.plan_id}" to load it.` : ''}`;
    if (o.variance !== undefined) return `Deposit for ${o.plan_id}: expected ${fmt(o.expected)}, counted ${fmt(o.counted)}, variance ${fmt(o.variance)}${o.variance < 0 && o.suspect_stops?.length ? `\nLook first at: ${o.suspect_stops.map(s => `${s.customer_name || s.customer_id} (cash ${fmt(s.cash_collected)})`).join(', ')}` : ''}`;
    if (o.stop_id && o.invoiced !== undefined) return `Stop ${o.stop_id} closed: ${o.status}, invoiced ${fmt(o.invoiced)}, cash ${fmt(o.cash_collected)}${o.invoice_id ? ` · ${o.invoice_id}` : ''}`;
    if (o.customer && o.outstanding !== undefined) return `${o.customer.name}: outstanding ${fmt(o.outstanding)}${o.aging ? ` · ${o.aging.days_overdue}d overdue (${o.aging.bucket})` : ''} · limit ${fmt(o.customer.credit_limit)}${o.promise ? ` · promise ${fmt(o.promise.amount)} by ${o.promise.date}${o.promise.broken ? ' (broken)' : ''}` : ''}`;
    if (o.supplier && o.balance !== undefined) return `${o.supplier.name}: we owe ${fmt(o.balance)}`;
    if (o.found === false) return `No match for "${o.query}".`;
    if (o.found && o.customer_id) return `${o.name} (${o.customer_id}) · ${o.tier} · outstanding ${fmt(o.outstanding)} · limit ${fmt(o.credit_limit)}`;
    if (o.found && o.supplier_id) return `${o.name} (${o.supplier_id}) · we owe ${fmt(o.balance)}`;
    if (o.reminder_id) return `Reminder ${o.reminder_id} (${o.tier}) for ${o.customer_id}: "${o.message}" · ${o.status}`;
    if (o.promise_id) return `Promise logged: ${o.customer_id} pays ${fmt(o.amount)} by ${o.promised_date}`;
    if (o.purchase_id) return `Received ${o.items.map(i => `${i.qty} × ${i.sku}`).join(', ')} from ${o.supplier_name || o.supplier_id} into ${o.warehouse_id} — bill ${fmt(o.total)}. We now owe ${fmt(o.balance)}.`;
    if (o.entry_id && o.supplier_id) return `Paid ${o.supplier_name || o.supplier_id} ${fmt(-o.amount)} (${o.method}). Balance ${fmt(o.balance)}.`;
    if (o.entry_id && o.kind === 'payment') return `Receipt ${o.entry_id}: ${fmt(-o.amount)} from ${o.customer_name || o.customer_id} by ${o.method}. Balance ${fmt(o.outstanding)}.`;
    if (o.entry_id) return `${o.kind.replace('_', ' ')} ${o.entry_id}: ${fmt(o.amount)} on ${o.customer_id}`;
    if (o.expense_id) return `Expense ${o.expense_id}: ${fmt(o.amount)} · ${o.category}${o.note ? ' · ' + o.note : ''}`;
    if (o.transfer_id) return `Moved ${o.qty} × ${o.sku} from ${o.from} to ${o.to}.`;
    if (o.sku && o.available !== undefined) return `${o.sku} at ${o.warehouse_id}: ${o.available} available`;
    if (o.orders && o.cash) return `Orders ${o.orders.count} (${fmt(o.orders.value)}) · delivered ${o.dispatch.delivered}/${o.dispatch.stops} · cash ${fmt(o.cash.collected)} · receivables ${fmt(o.receivables.total)} · payables ${fmt(o.payables)}`;
    if (o.order_id && o.status === 'allocated') return `${o.order_id} allocated at ${o.warehouse_id}.`;
    if (o.net !== undefined && o.gross_margin !== undefined) return `${o.start} → ${o.end}: revenue ${fmt(o.revenue)}, cost ${fmt(o.cost_of_goods)}, gross margin ${fmt(o.gross_margin)}, expenses ${fmt(o.expenses)}, net ${fmt(o.net)}`;
    if (o.revenue !== undefined && o.by_product) return `${o.start} → ${o.end}: ${fmt(o.revenue)} on ${o.invoices} invoices, margin ${o.margin_pct}%. Top: ${o.by_product.slice(0, 3).map(p => `${p.name} ${fmt(p.revenue)}`).join(', ')}`;
    if (o.collection_rate_pct !== undefined) return `${o.start} → ${o.end}: invoiced ${fmt(o.invoiced)}, collected ${fmt(o.collected)} (${o.collection_rate_pct}%). Overdue 60+: ${fmt(o.aging.buckets['60+'])}`;
    if (o.at_cost !== undefined) return `Stock worth ${fmt(o.at_cost)} at cost, ${fmt(o.at_sale)} at sale price.`;
    if (o.cash_in && o.cash_out) return `Cashbook ${o.date}: in ${fmt(o.total_in)} + hand-ins ${fmt(o.total_handins)}, out ${fmt(o.total_out)}, net ${fmt(o.net)}`;
    if (o.moves) return `${o.name}: ` + o.levels.map(l => `${l.warehouse_id} ${l.available} available`).join(', ') + `\n` + o.moves.slice(0, 5).map(m => `• ${day(m.created_at)} ${m.kind} ${m.delta > 0 ? '+' : ''}${m.delta} (${m.ref})`).join('\n');
    return JSON.stringify(o).slice(0, 300);
  }
  // ---------------------------------------------------------------- approval cards
  // The server sends each sentence as a template key + structured vars (and the English text as a fallback),
  // so the same card reads in English or Urdu. Names, ids and figures are always Latin script: each goes in its
  // own LTR isolate with the UI font, so it never picks up the Nastaliq face or reorders inside Urdu text.
  const APPROVALS = new Map();                                   // approval_id -> last payload rendered (for the confirm sheet)
  const ltr = s => `<bdi class="ltr" dir="ltr">${esc(s)}</bdi>`;
  const money = v => ltr(fmt(v).replace(' ', '\u00a0'));        // "Rs" never wraps away from its amount
  // a translatable word: this language's k_<word>, else its plain key (order statuses are already translated), else English
  const word = k => { const L = window.MUNSHI_I18N[state.lang] || {}; return L['k_' + k] ?? L[k] ?? window.MUNSHI_I18N.en['k_' + k] ?? String(k).replace(/_/g, ' '); };
  const cardVar = v => {
    if (Array.isArray(v)) return v.map(c => ltr(`${c.name} ${num(c.before)} → ${num(c.after)}`)).join(state.lang === 'ur' ? '، ' : ', ');
    if (v && typeof v === 'object') return 'rs' in v ? money(v.rs) : 'k' in v ? esc(word(v.k)) : '';
    if (typeof v === 'number') return ltr(num(v));
    return ltr(v ?? '');
  };
  const fill = (tpl, vars) => esc(tpl).replace(/\{(\w+)\}/g, (m, k) => (vars && k in vars ? cardVar(vars[k]) : m));
  const cardText = (key, vars, fallback) => { const k = 'ap.' + key; const tpl = key ? t(k) : k; return tpl === k ? esc(fallback || '') : fill(tpl, vars); };
  const ago = iso => {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (!isFinite(s)) return esc(when(iso));
    const n = s < 60 ? 0 : s < 3600 ? Math.floor(s / 60) : s < 86400 ? Math.floor(s / 3600) : Math.floor(s / 86400);
    return fill(t(s < 60 ? 'ago_now' : s < 3600 ? 'ago_min' : s < 86400 ? 'ago_hr' : 'ago_day'), { n });
  };
  const ownerStep = p => (p.card?.needs_role || p.needs_role) === 'owner' || p.needs_role_now === 'owner' || p.tier === 'high_risk';
  function linesTable(c) {
    const ls = c.lines || []; if (!ls.length) return '';
    const rate = ls.some(l => l.unit_price !== null && l.unit_price !== undefined), amt = ls.some(l => l.line_total !== null && l.line_total !== undefined);
    const unit = u => (u === 'days' || u === 'units') ? ` <span class="u">${esc(t('u_' + u))}</span>` : '';
    return `<table class="ap-lines"><thead><tr><th scope="col">${esc(t('ap_item'))}</th><th scope="col" class="n">${esc(t('quantity'))}</th>${rate ? `<th scope="col" class="n">${esc(t('rate'))}</th>` : ''}${amt ? `<th scope="col" class="n">${esc(t('amount'))}</th>` : ''}</tr></thead>
      <tbody>${ls.map(l => `<tr><td><bdi class="ltr" dir="ltr">${esc(l.name)}</bdi></td><td class="n">${ltr(num(l.qty))}${unit(l.unit)}</td>${rate ? `<td class="n">${l.unit_price === null ? '—' : money(l.unit_price)}</td>` : ''}${amt ? `<td class="n">${l.line_total === null ? '—' : money(l.line_total)}</td>` : ''}</tr>`).join('')}</tbody>
      ${c.total !== null && c.total !== undefined && amt ? `<tfoot><tr><th scope="row" colspan="${1 + (rate ? 2 : 1)}">${esc(t('total'))}</th><td class="n">${money(c.total)}</td></tr></tfoot>` : ''}</table>`;
  }
  function renderApproval(p) {
    APPROVALS.set(p.approval_id, p);
    const c = p.card || { title: p.summary, lines: [], warnings: [], facts: [] };
    const id = 'ap-' + esc(p.approval_id); const hi = ownerStep(p);
    const tier = `<span class="ap-tier ${hi ? 'hi' : 'lo'}"><span aria-hidden="true">${hi ? '▲' : '●'}</span> ${esc(t(hi ? 'ap_tier_owner' : 'ap_tier_clerk'))}</span>`;
    const facts = (c.facts || []).map(f => `<b>${esc(t(f.key))}</b><span>${f.value && typeof f.value === 'object' ? cardVar(f.value) : `<bdi>${esc(f.value)}</bdi>`}</span>`).join('');
    const who = c.requested_by || p.requested_by;
    const asked = fill(t('ap_asked_by'), { name: who || t(p.requested_by_role) }).replace('{ago}', ago(p.created_at));
    const blocked = !p.can_approve ? `<p class="ap-blocked"><span aria-hidden="true">⏳</span> ${esc(p.blocked_code && t('blk_' + p.blocked_code) !== 'blk_' + p.blocked_code ? t('blk_' + p.blocked_code) : (p.blocked_reason || t('ap_waiting')))}</p>` : '';
    const approveBtn = p.can_approve ? `<button type="button" class="btn primary" data-act="approve">${esc(t('approve'))}</button>` : '';
    const noBtn = p.can_withdraw ? `<button type="button" class="btn" data-act="withdraw">${esc(t('withdraw'))}</button>`
      : (p.can_reject ?? can('approvals:decide')) ? `<button type="button" class="btn danger" data-act="reject">${esc(t('reject'))}</button>` : '';
    return `<section class="approval${hi ? ' hi' : ''}" data-aid="${esc(p.approval_id)}" role="region" aria-labelledby="${id}-t">
      <div class="ap-top">${tier}<span class="ap-src">${esc(p.specialist)} munshi</span></div>
      ${p.still_waiting ? `<p class="ap-still">${esc(t('ap_still'))}</p>` : ''}
      <h3 class="ap-title" id="${id}-t">${cardText(c.title_key, c.title_vars, c.title || p.summary)}</h3>
      ${linesTable(c)}
      ${c.effect ? `<p class="ap-effect">${cardText(c.effect_key, c.effect_vars, c.effect)}</p>` : ''}
      ${(c.warnings || []).map(w => `<p class="ap-warn"><span aria-hidden="true">⚠</span><span>${cardText(w.key, w.vars, w.text)}</span></p>`).join('')}
      ${facts ? `<div class="kv ap-facts">${facts}</div>` : ''}
      ${c.quote ? `<div class="quote"><bdi>${esc(c.quote)}</bdi></div>` : ''}
      <p class="ap-meta">${asked}${c.approver ? ` · ${cardText(c.approver.key, c.approver.vars, c.approver.text)}` : ''}</p>
      ${blocked}
      ${approveBtn || noBtn ? `<div class="ap-actions">${approveBtn}${noBtn}</div>` : ''}</section>`;
  }
  const CHIPS = { owner: ['Profit this month', 'Kaun kitna baqi hai?', 'Restock WH-MULTAN 100 urea received', 'Credit note Rana Brothers 5000 damaged bags', 'Pay Fauji 100000 by bank', 'Digest'],
    clerk: ['Chaudhry Farms ko 20 urea aur 5 dap bhej do', 'Suggest dispatch for today', 'Received 100 urea from Fauji at 3600', 'Chaudhry Farms paid 20000 jazzcash', 'Expense diesel 5000', 'Remind everyone over 30 days'],
    salesman: ['Rana Brothers ko 5 dap bhej do', 'Rana Brothers ka khata', 'Urea stock kitna hai', 'Haji Sons promise 50000 by ' + daysAgo(-7)],
    driver: ['Stops for today', 'Close STP-… delivered all cash 50000 otp 1234'] };
  V.chat = async () => {
    view.innerHTML = `<div class="chat"><div class="row"><h1>${esc(t('chat'))}</h1><span class="hint">manager → munshi</span></div>
      <div class="chips">${(CHIPS[role()] || CHIPS.clerk).map(q => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      <div id="msgs" class="msgs"></div>
      <form id="composer" class="composer"><input id="txt" class="input" placeholder="${esc(t('composer_hint'))}" autocomplete="off" maxlength="1000">${state.me?.voice ? '<button type="button" id="mic" class="btn mic" title="Hold to speak">🎙</button>' : ''}<button class="btn primary" type="submit">${esc(t('send'))}</button></form></div>`;
    const msgs = $('#msgs');
    const [hist, pend] = await Promise.all([api('/api/chat/' + state.thread), can('approvals:read') ? api('/api/approvals') : Promise.resolve([])]);
    const openIds = new Map(pend.map(p => [p.approval_id, p]));
    msgs.innerHTML = hist.map(m => m.meta?.approval_id && openIds.has(m.meta.approval_id) ? renderApproval(openIds.get(m.meta.approval_id)) : renderMsg(m)).join('') || `<div class="empty">${esc(t('say_it'))}</div>`;
    $$('[data-q]').forEach(b => b.onclick = () => { $('#txt').value = b.dataset.q; $('#composer').requestSubmit(); });
    const pre = sessionStorage.getItem('munshi.prefill'); if (pre) { sessionStorage.removeItem('munshi.prefill'); $('#txt').value = pre; $('#composer').requestSubmit(); }
    msgs.addEventListener('click', onApprovalClick);
    const scrollEnd = () => window.scrollTo({ top: document.body.scrollHeight });
    scrollEnd();
    $('#composer').onsubmit = async e => {
      e.preventDefault(); const txt = $('#txt').value.trim(); if (!txt || state.chatBusy) return;
      $('#txt').value = ''; msgs.insertAdjacentHTML('beforeend', renderMsg({ role: 'me', text: txt })); scrollEnd();
      state.chatBusy = true; const thinking = document.createElement('div'); thinking.className = 'msg bot'; thinking.innerHTML = '<span class="who">manager</span>…'; msgs.appendChild(thinking);
      try {
        const r = await post('/api/chat', { thread_id: state.thread, text: txt }); thinking.remove();
        // a message held behind an earlier card brings that card down to here, so it can be decided without scrolling
        if (r.pending) $$(`[data-aid="${CSS.escape(r.pending.approval_id)}"]`, msgs).forEach(x => x.remove());
        msgs.insertAdjacentHTML('beforeend', r.pending ? renderApproval(r.pending) : renderMsg({ role: 'munshi', text: r.text, meta: { specialist: r.specialist } }));
        if (r.pending?.still_waiting) toast(t('ap_still_toast'));
      }
      catch (err) { thinking.remove(); msgs.insertAdjacentHTML('beforeend', renderMsg({ role: 'munshi', text: '⚠ ' + err.message })); }
      state.chatBusy = false; scrollEnd(); refreshBadge();
    };
    if (state.me?.voice) setupMic($('#mic'), msgs);
  };
  async function onApprovalClick(e) {
    const b = e.target.closest('[data-act]'); if (!b) return;
    const box = b.closest('[data-aid]'); const aid = box.dataset.aid; const act = b.dataset.act; const approve = act === 'approve';
    const p = APPROVALS.get(aid) || {}; const c = p.card || {};
    const run = async note => {
      box.querySelectorAll('button').forEach(x => x.disabled = true);
      try {
        const r = await post('/api/approvals/' + aid, { approve, note });
        const label = approve ? t('approved') : act === 'withdraw' ? t('withdrawn') : t('rejected');
        box.outerHTML = `<div class="msg bot"><span class="who">${esc(r.specialist)} munshi · ${esc(label)}</span>${esc(r.text.indexOf(DETAILS) > 0 ? r.text.slice(0, r.text.indexOf(DETAILS)) : r.text.startsWith('Done -- ') ? (() => { try { return summarize(JSON.parse(r.text.slice(8))); } catch { return r.text.slice(8); } })() : r.text)}</div>`;
        APPROVALS.delete(aid); toast(label);
      }
      catch (err) { toast(err.message, 4000); box.querySelectorAll('button').forEach(x => x.disabled = false); }
      refreshBadge();
    };
    if (approve && !ownerStep(p)) return run('');
    if (approve) {   // an owner-level action is never one tap: restate what it does and how much, then a deliberate Yes
      const title = cardText(c.title_key, c.title_vars, c.title || p.summary);
      const amount = c.total !== null && c.total !== undefined && (c.lines || []).length ? `<p class="ap-sum">${esc(t('total'))}: ${money(c.total)}</p>` : '';
      sheet(t('ap_confirm_title'), `<p class="ap-q">${fill(t('ap_confirm_q'), {}).replace('{title}', title)}</p>${amount}${c.effect ? `<p class="ap-effect">${cardText(c.effect_key, c.effect_vars, c.effect)}</p>` : ''}${(c.warnings || []).map(w => `<p class="ap-warn"><span aria-hidden="true">⚠</span><span>${cardText(w.key, w.vars, w.text)}</span></p>`).join('')}`,
        async () => { await run(''); }, t('ap_confirm_yes'));
      return;
    }
    if (act === 'withdraw') return confirmSheet(t('withdraw_title'), t('withdraw_q'), async () => { await run('withdrawn by the requester'); }, t('withdraw'));
    sheet(t('reject'), field(t('reason'), 'note', 'maxlength="200"'), async d => { await run(d.note || ''); }, t('reject'));
  }
  V.approvals = async () => {
    view.innerHTML = h1(t('approvals'), t('nothing_moves')) + '<div id="alist" class="list"></div><h2 id="hh" hidden>History</h2><div id="hist" class="list"></div>';
    const [a, hist] = await Promise.all([api('/api/approvals'), api('/api/approvals/history')]);
    $('#alist').innerHTML = a.length ? a.map(renderApproval).join('') : empty(t('nothing_waiting'));
    $('#alist').addEventListener('click', async e => { await onApprovalClick(e); });
    if (hist.length) { $('#hh').hidden = false; $('#hist').innerHTML = hist.slice(0, 30).map(x => item(row(`${esc(x.specialist)} · ${esc(x.tool.replace(/_/g, ' '))}`, `<span class="pill ${x.status === 'approved' ? 'good' : 'crit'}">${esc(x.status)}</span>`) + meta(`${esc(x.resolved_by || '')} · ${when(x.resolved_at)}${x.note ? ' · ' + esc(x.note) : ''}`))).join(''); }
  };
  V.notifications = async () => {
    view.innerHTML = h1(t('notifications')) + '<div id="nl" class="list"></div>';
    const n = await api('/api/notifications');
    $('#nl').innerHTML = n.map(x => item(row(esc(x.text), x.read ? '' : '<span class="pill acc">new</span>') + meta(`${esc(x.kind)} · ${when(x.created_at)}`), x.kind === 'approval' ? '#approvals' : x.kind === 'variance' ? '#plan/' + x.ref : x.kind === 'credit_hold' ? '#order/' + x.ref : '')).join('') || empty();
    if (n.some(x => !x.read)) post('/api/notifications/read').then(refreshBadge).catch(() => { });
  };

  // ================================================================ khata / customers
  V.khata = async () => {
    view.innerHTML = h1(t('khata'), t('who_owes')) + '<div id="klist" class="list"></div>';
    const k = await api('/api/khata'); const s = k.summary;
    $('#klist').innerHTML = `<div class="card"><div class="row"><span class="tile"><div class="k">${esc(t('total_receivable'))}</div><div class="v">${fmt(s.total)}</div></span><span class="pill">${s.customers} ${esc(t('accounts'))}</span></div>
      <div class="buckets">${Object.entries(s.buckets).map(([b, v]) => `<span class="pill ${bucketPill(b)}">${esc(b)} ${fmt(v)}</span>`).join('')}</div></div>` +
      (can('reminders:send') ? `<div class="btnrow">${btn(t('draft_due'), 'id="due"')}${btn(t('record_payment'), 'id="pay"')}</div>` : '') +
      (k.broken_promises.length ? `<h2>${esc(t('broken'))} ${esc(t('promises')).toLowerCase()}</h2>` + k.broken_promises.map(p => `<a class="item tap" href="#customer/${esc(p.customer_id)}">${row(esc(p.name), `<b class="num crit">${fmt(p.amount)}</b>`)}${meta(`promised by ${esc(p.date)}`)}</a>`).join('') : '') +
      `<h2>${esc(t('accounts'))}</h2>` + (k.rows.map(r => `<a class="item tap" href="#customer/${esc(r.customer_id)}">${row(esc(r.name), `<span class="pill ${bucketPill(r.bucket)}">${esc(r.bucket)}</span>`)}<div class="row">${meta(`${r.days_overdue ? r.days_overdue + ' ' + t('days_overdue') : t('not_due')} · ${t('limit')} ${fmt(r.credit_limit)}${r.promise && !r.promise.kept ? ` · promise ${fmt(r.promise.amount)} ${r.promise.broken ? '<span class="crit">broken</span>' : 'by ' + r.promise.date}` : ''}`)}<b class="num">${fmt(r.balance)}</b></div></a>`).join('') || empty());
    $('#due')?.addEventListener('click', async () => { try { const r = await post('/api/reminders/due?min_days=1'); toast(`Drafted ${r.length}`); go('#reminders'); } catch (e) { toast(e.message); } });
    $('#pay')?.addEventListener('click', () => paymentForm());
  };
  V.customer = async id => {
    view.innerHTML = h1(t('loading'), '', 'khata');
    const k = await api('/api/khata/' + id); const c = k.customer;
    view.innerHTML = h1(c.name, `${esc(c.customer_id)} · ${esc(c.tier)} · ${esc(c.phone)}${c.address ? ' · ' + esc(c.address) : ''}`, 'khata') +
      `<div class="grid2">${kpi(t('outstanding'), fmt(k.outstanding), k.aging ? `${k.aging.days_overdue} ${t('days_overdue')} · ${k.aging.bucket}` : 'clear')}${kpi(t('credit_limit'), fmt(c.credit_limit), k.outstanding > c.credit_limit && c.credit_limit ? `<span class="crit">${t('over_limit')}</span>` : t('within_limit'))}</div>
      ${k.promise ? `<div class="card" style="margin-top:10px">${row(`${t('promises')}: ${fmt(k.promise.amount)} by ${esc(k.promise.date)}`, `<span class="pill ${k.promise.kept ? 'good' : k.promise.broken ? 'crit' : 'warn'}">${k.promise.kept ? t('kept') : k.promise.broken ? t('broken') : t('open')}</span>`)}</div>` : ''}
      <div class="btnrow" style="margin:12px 0">${can('payments:write') ? btn(t('record_payment'), 'id="pay"', 'btn primary') : ''}${can('orders:create') ? btn(t('new_order'), 'id="ord"') : ''}${can('reminders:send') && k.outstanding > 0 ? btn(t('draft_reminder'), 'id="remind"') : ''}${can('khata:read') ? btn(t('log_promise'), 'id="promise"') : ''}${can('customers:write') ? btn(t('edit'), 'id="edit"') : ''}<a class="btn" href="/api/statements/${esc(c.customer_id)}/html" target="_blank" id="stmt">${esc(t('statement'))}</a>${can('reminders:send') ? btn(t('share') + ' ' + t('statement').toLowerCase(), 'id="stmtShare"') : ''}</div>
      <h2>${esc(t('recent_entries'))}</h2><div class="list">${k.ledger.slice().reverse().map(e => `<a class="item tap" href="#doc/${esc(e.entry_id)}">${row(esc(e.kind.replace('_', ' ')) + (e.method ? ` <span class="pill">${esc(e.method)}</span>` : ''), `<b class="num" style="color:${e.amount < 0 ? 'var(--good)' : 'var(--ink)'}">${fmt(e.amount)}</b>`)}${meta(`${esc(e.entry_id)} · ${esc(e.ref)} · ${day(e.created_at)}${e.due_date ? ' · due ' + esc(e.due_date) : ''}`)}</a>`).join('') || empty()}</div>
      ${k.orders.length ? `<h2>${esc(t('orders'))}</h2><div class="list">${k.orders.map(orderRow).join('')}</div>` : ''}`;
    $('#stmt').onclick = e => { e.preventDefault(); openAuthed(`/api/statements/${c.customer_id}/html`); };
    $('#stmtShare')?.addEventListener('click', async () => { const l = await api(`/api/statements/${c.customer_id}/link`); const digits = (c.phone || '').replace(/\D/g, '').replace(/^0/, '92'); const txt = `${state.me.business.name}: ${c.name}, aap ka statement: ${l.url} — balance Rs ${Math.round(k.outstanding).toLocaleString('en-PK')}`; window.open(`https://wa.me/${digits}?text=${encodeURIComponent(txt)}`, '_blank'); });
    $('#pay')?.addEventListener('click', () => paymentForm(c));
    $('#ord')?.addEventListener('click', () => orderForm());
    $('#remind')?.addEventListener('click', async () => { try { const r = await post('/api/reminders', { customer_id: c.customer_id }); toast(r.drafted === false ? r.reason : `Drafted (${r.tier})`); go('#reminders'); } catch (e) { toast(e.message); } });
    $('#promise')?.addEventListener('click', () => sheet(t('log_promise'), field(t('amount'), 'amount', 'required min="1"', 'number') + field(t('by_date'), 'promised_date', 'required', 'date', daysAgo(-7)), async d => { await post('/api/promises', { customer_id: c.customer_id, amount: Number(d.amount), promised_date: d.promised_date }); toast(t('saved')); render(); }));
    $('#edit')?.addEventListener('click', () => customerForm(c));
  };
  async function openAuthed(path) {   // open an authenticated HTML document in a new tab via blob
    const res = await fetch(path, { headers: { 'X-Session': state.token } }); if (!res.ok) return toast(t('error'));
    const b = await res.blob(); const u = URL.createObjectURL(b); window.open(u, '_blank'); setTimeout(() => URL.revokeObjectURL(u), 60000);
  }
  async function paymentForm(c) {
    const customers = c ? [c] : await api('/api/customers');
    const m = await api('/api/methods');
    sheet(t('record_payment'), (c ? `<p><b>${esc(c.name)}</b> · ${t('outstanding')} ${fmt(c.outstanding ?? 0)}</p>` : select(t('customer'), 'customer_id', customers.map(x => [x.customer_id, `${x.name} · ${fmt(x.outstanding)}`]))) +
      field(t('amount'), 'amount', 'required min="1" inputmode="numeric"', 'number') + select(t('method'), 'method', m.payment_methods.map(x => [x, x])) + field(t('reference'), 'ref', 'maxlength="80" placeholder="txn id / cheque no."'),
      async d => { const r = await post('/api/payments', { customer_id: c ? c.customer_id : d.customer_id, amount: Number(d.amount), method: d.method, ref: d.ref || '' }); toast(`${t('saved')}: ${r.entry_id}`); go('#doc/' + r.entry_id); });
  }
  V.customers = async () => {
    view.innerHTML = h1(t('customers')) + `<div class="row" style="gap:8px"><input class="input" id="q" placeholder="${esc(t('search'))}…">${can('customers:write') ? btn('+', 'id="add"', 'btn primary') : ''}</div><div id="cl" class="list" style="margin-top:10px"></div>`;
    const load = async q => { const c = await api('/api/customers' + (q ? '?q=' + encodeURIComponent(q) : '')); $('#cl').innerHTML = c.map(x => `<a class="item tap" href="#customer/${esc(x.customer_id)}">${row(esc(x.name), x.tier ? `<span class="pill">${esc(x.tier)}</span>` : '')}<div class="row">${meta(`${esc(x.phone)} · ${esc(x.route_id || '')}${x.address ? ' · ' + esc(x.address) : ''}`)}${x.outstanding !== undefined ? `<b class="num">${fmt(x.outstanding)}</b>` : ''}</div></a>`).join('') || empty(); };
    let tm; $('#q').oninput = () => { clearTimeout(tm); tm = setTimeout(() => load($('#q').value.trim()), 250); };
    $('#add')?.addEventListener('click', () => customerForm());
    load('');
  };
  async function customerForm(c) {
    const routes = await api('/api/routes');
    sheet(c ? t('edit') : t('add_customer'), field(t('name'), 'name', 'required', 'text', c?.name || '') + field(t('phone'), 'phone', '', 'tel', c?.phone || '') + field(t('address'), 'address', '', 'text', c?.address || '') +
      select(t('route'), 'route_id', [['', '—'], ...routes.map(r => [r.route_id, r.name])], c?.route_id || '') + select('Tier', 'tier', [['standard', 'standard'], ['wholesale', 'wholesale'], ['vip', 'vip']], c?.tier || 'standard') +
      field(t('credit_limit'), 'credit_limit', 'min="0"', 'number', c?.credit_limit ?? 0) + field('Credit days', 'credit_days', 'min="0" max="365"', 'number', c?.credit_days ?? 30) + field('Discount %', 'discount_pct', 'min="0" max="50" step="0.5"', 'number', c?.discount_pct ?? 0) +
      select(t('language'), 'language', [['ur-en', 'Urdu (Roman)'], ['en', 'English']], c?.language || 'ur-en') + (c ? `<label class="chk"><input type="checkbox" name="active" ${c.active ? 'checked' : ''}> ${esc(t('active'))}</label>` : field('Opening balance (what they owe today)', 'opening_balance', 'min="0"', 'number', 0)),
      async d => { const body = { ...d, credit_limit: Number(d.credit_limit), credit_days: Number(d.credit_days), discount_pct: Number(d.discount_pct), opening_balance: Number(d.opening_balance || 0), route_id: d.route_id || null, active: c ? !!d.active : true }; const r = c ? await patch('/api/customers/' + c.customer_id, body) : await post('/api/customers', body); toast(t('saved')); go('#customer/' + r.customer_id); if (c) render(); });
  }

  // ================================================================ orders
  V.orders = async () => {
    view.innerHTML = h1(t('orders')) + `<div class="chips" id="f"></div><div id="ol" class="list"></div>`;
    const load = async st => { const o = await api('/api/orders' + (st ? '?status=' + st : '')); $('#ol').innerHTML = o.map(orderRow).join('') || empty(); };
    $('#f').innerHTML = ['', 'draft', 'confirmed', 'allocated', 'dispatched', 'delivered', 'short', 'cancelled'].map(s => `<button class="chip" data-s="${s}">${esc(s ? t(s) : t('all'))}</button>`).join('') + (can('orders:create') ? `<button class="chip ok" id="new">+ ${esc(t('new_order'))}</button>` : '');
    $('#f').onclick = e => { const b = e.target.closest('[data-s]'); if (b) load(b.dataset.s); };
    $('#new')?.addEventListener('click', () => orderForm());
    load('');
  };
  V.order = async id => {
    const o = await api('/api/orders/' + id);
    const whs = can('dispatch:write') ? await api('/api/warehouses') : [];
    view.innerHTML = h1(o.order_id, `${esc(o.customer_name)} · <span class="pill ${statusPill(o.status)}">${esc(t(o.status))}</span> · ${esc(o.channel)}${o.created_by ? ' · ' + esc(o.created_by) : ''}`, 'orders') +
      `<div class="card"><div class="kv">${o.items.map(i => `<b>${esc(i.sku)}</b><span>${i.qty} × ${fmt(i.unit_price)} = <b class="num">${fmt(i.qty * i.unit_price)}</b></span>`).join('')}<b>${esc(t('total'))}</b><span class="num"><b>${fmt(o.total)}</b>${o.discount_pct ? ` <span class="hint">(${o.discount_pct}% off)</span>` : ''}</span><b>Load</b><span>${o.load_units} units</span>${o.warehouse_id ? `<b>${esc(t('godown'))}</b><span>${esc(o.warehouse_id)}</span>` : ''}${o.source_text ? `<b>Said</b><span>“${esc(o.source_text)}”</span>` : ''}${o.notes ? `<b>${esc(t('note'))}</b><span>${esc(o.notes)}</span>` : ''}</div></div>
      ${can('dispatch:write') || (o.status === 'draft' && can('orders:create')) ? `<div class="btnrow" style="margin-top:12px">${o.status === 'draft' && can('orders:create') ? btn(t('edit'), 'id="editO"') : ''}${o.status === 'draft' && can('dispatch:write') ? btn(t('confirm_order'), 'id="confirm"', 'btn primary') : ''}${o.status === 'confirmed' ? whs.map(w => btn(`${t('allocate')} @ ${w.name}`, `data-wh="${esc(w.warehouse_id)}"`, 'btn primary')).join('') : ''}${o.status === 'allocated' ? `<a class="btn primary" href="#dispatch">${esc(t('plan_dispatch'))} ›</a>` : ''}${['draft', 'confirmed', 'allocated'].includes(o.status) ? btn(t('cancel_order'), 'id="cancel"', 'btn danger') : ''}</div>` : ''}
      ${o.audit?.length ? `<h2>${esc(t('audit'))}</h2><div class="list">${o.audit.map(a => item(row(esc(a.action.replace(/_/g, ' ')), `<span class="pill ${a.approved_by ? 'good' : ''}">${a.approved_by ? esc(a.approved_by) : 'auto'}</span>`) + meta(`${esc(a.actor)}${a.user ? ' · ' + esc(a.user) : ''} · ${when(a.created_at)}`))).join('')}</div>` : ''}`;
    $('#editO')?.addEventListener('click', () => orderForm(null, o));
    $('#confirm')?.addEventListener('click', async () => { try { await post(`/api/orders/${id}/confirm`); toast(t('confirmed')); render(); } catch (e) { toast(e.message, 6000); } });
    $$('[data-wh]').forEach(b => b.onclick = async () => { try { await post(`/api/orders/${id}/allocate`, { warehouse_id: b.dataset.wh }); toast(t('allocated')); render(); } catch (e) { toast(e.message, 5000); } });
    $('#cancel')?.addEventListener('click', () => sheet(t('cancel_order'), field(t('reason'), 'reason', 'required'), async d => { await post(`/api/orders/${id}/cancel`, { reason: d.reason }); toast(t('cancelled')); render(); }, t('cancel_order')));
  };

  // ================================================================ dispatch
  V.dispatch = async () => {
    view.innerHTML = h1(t('dispatch'), t('plans_today')) + '<div id="pl" class="list"></div>';
    const plans = await api('/api/plans');
    $('#pl').innerHTML = (can('dispatch:write') ? `<div class="btnrow">${btn(t('suggest_plan'), 'id="suggest"', 'btn primary')}${btn(t('plan_dispatch'), 'id="newPlan"')}</div>` : '') + (plans.map(planRow).join('') || empty(t('no_plans')));
    $('#suggest')?.addEventListener('click', async () => {
      const s = await api('/api/dispatch/suggest');
      if (!s.length) return toast('No allocated orders waiting.');
      sheet(t('suggest_plan'), `<div class="list">${s.map((x, i) => `<label class="item chk"><input type="checkbox" name="s${i}" ${x.vehicle_id ? 'checked' : ''}> <span>${esc(x.route_id)}: ${x.order_ids.length} order(s), ${x.load_units} units → ${x.vehicle_id || '<span class="crit">no vehicle fits</span>'}${x.note ? ` <span class="hint">${esc(x.note)}</span>` : ''}</span></label>`).join('')}</div>`,
        async d => { let n = 0; for (let i = 0; i < s.length; i++) if (d['s' + i] && s[i].vehicle_id) { await post('/api/plans', { route_id: s[i].route_id, vehicle_id: s[i].vehicle_id, order_ids: s[i].order_ids, plan_date: s[i].plan_date }); n++; } toast(`${n} plan(s) created`); render(); }, t('plan_dispatch'));
    });
    $('#newPlan')?.addEventListener('click', planForm);
  };
  async function planForm() {
    const [routes, vehicles, orders] = await Promise.all([api('/api/routes'), api('/api/vehicles'), api('/api/orders?status=allocated')]);
    if (!orders.length) return toast('No allocated orders to plan.');
    sheet(t('plan_dispatch'), select(t('route'), 'route_id', routes.map(r => [r.route_id, `${r.name} (${r.warehouse_id})`])) + select(t('vehicle'), 'vehicle_id', vehicles.map(v => [v.vehicle_id, `${v.plate} · ${v.capacity_units} units`])) + field(t('date'), 'plan_date', '', 'date', today()) +
      `<b>${esc(t('orders'))}</b><div class="list">${orders.map(o => `<label class="item chk"><input type="checkbox" name="o_${esc(o.order_id)}" checked> <span>${esc(o.customer_name)} · ${o.load_units} units · ${fmt(o.total)} <span class="hint">${esc(o.warehouse_id)}</span></span></label>`).join('')}</div>`,
      async d => { const ids = orders.filter(o => d['o_' + o.order_id]).map(o => o.order_id); const p = await post('/api/plans', { route_id: d.route_id, vehicle_id: d.vehicle_id, order_ids: ids, plan_date: d.plan_date }); toast(`${t('saved')}: ${p.plan_id}`); go('#plan/' + p.plan_id); });
  }
  V.plan = async id => {
    const p = await api('/api/plans/' + id);
    const allClosed = p.stops.every(s => s.status !== 'pending');
    view.innerHTML = h1(p.route_name, `${esc(p.plan_id)} · ${esc(p.plate)} · ${p.load_units} units · ${esc(p.plan_date)} · <span class="pill ${statusPill(p.status)}">${esc(t(p.status))}</span>`, 'dispatch') +
      (can('dispatch:write') ? `<div class="btnrow" style="margin-bottom:10px">${p.status === 'planned' ? btn(t('approve_loading'), 'id="approve"', 'btn primary') + btn(t('cancel'), 'id="cancelP"', 'btn danger') : ''}${btn('Loading sheet', 'id="sheet"')}${p.status !== 'planned' && p.status !== 'cancelled' && allClosed && can('payments:write') ? btn(`${t('record_handin')} (${fmt(p.cash_collected)} collected, ${fmt(p.deposited)} in)`, 'id="dep"', 'btn primary') : ''}</div>` : '') +
      `<div class="list">${p.stops.map(s => `<div class="item">${row(`#${s.sequence} ${esc(s.customer_name)}`, `<span class="pill ${statusPill(s.status)}">${esc(t(s.status))}</span>`)}${meta(`${s.items.map(i => `${i.qty}×${i.sku}`).join(', ')} · ${fmt(s.order_total)}${s.cash_collected ? ' · cash ' + fmt(s.cash_collected) : ''}${s.otp && role() !== 'driver' ? ` · <span class="num">OTP ${esc(s.otp)}</span> <span class="hint">(customer's)</span>` : ''}${s.status !== 'pending' && s.delivered_items?.length ? ` · delivered ${s.delivered_items.map(i => `${i.qty}×${i.sku}`).join(', ')}` : ''}`)}</div>`).join('')}</div>`;
    $('#sheet')?.addEventListener('click', () => openAuthed(`/api/plans/${id}/loading-sheet`));
    $('#approve')?.addEventListener('click', () => confirmSheet(t('approve_loading'), `${p.load_units} units leave ${p.warehouse_id}. Each customer gets a delivery code.`, async () => { await post(`/api/plans/${id}/approve`); toast(t('approved')); render(); }, t('approve')));
    $('#cancelP')?.addEventListener('click', () => confirmSheet(t('cancel'), 'Cancel this plan? Orders stay allocated.', async () => { await post(`/api/plans/${id}/cancel`); toast(t('cancelled')); render(); }, t('yes')));
    $('#dep')?.addEventListener('click', () => sheet(t('record_handin'), `<p>${t('cash_collected')}: <b>${fmt(p.cash_collected)}</b>${p.deposited ? ` · already in: ${fmt(p.deposited)}` : ''}</p>` + field('Counted from driver (Rs)', 'amount_counted', 'required min="0" inputmode="numeric"', 'number'),
      async d => { const r = await post(`/api/plans/${id}/deposit`, { amount_counted: Number(d.amount_counted) }); toast(r.variance < 0 ? `Short by ${fmt(-r.variance)}${r.suspect_stops.length ? ' — look at ' + r.suspect_stops.map(s => s.customer_name).join(', ') : ''}` : r.variance > 0 ? `Over by ${fmt(r.variance)}` : 'Reconciled exactly', 6000); render(); }));
  };

  // ================================================================ stock, products
  V.stock = async () => {
    view.innerHTML = h1(t('stock')) + (can('dispatch:write') ? `<div class="btnrow">${can('settings:write') ? btn(t('adjust'), 'id="adj"') : ''}${btn(t('transfer'), 'id="trf"')}${can('purchases:write') ? btn(t('receive_stock'), 'id="recv"', 'btn primary') : ''}</div>` : '') + '<div id="sl" class="list"></div>';
    const s = await api('/api/stock'); const byW = {}; s.forEach(x => (byW[x.warehouse] ||= []).push(x));
    $('#sl').innerHTML = Object.entries(byW).map(([w, rows]) => `<h2>${esc(w)}</h2>` + rows.map(x => `<a class="item tap" href="#ledger/${esc(x.sku)}">${row(esc(x.name), `<b class="num" style="color:${x.available <= x.min_stock ? 'var(--crit)' : 'var(--ink)'}">${x.available} <span class="hint">${esc(x.unit)}</span></b>`)}${meta(`${esc(x.sku)} · ${x.on_hand} ${t('on_hand')} · ${x.reserved} ${t('reserved')}${x.available <= x.min_stock ? ` · <span class="crit">${t('low_stock')}</span>` : ''}`)}</a>`).join('')).join('') || empty();
    $('#adj')?.addEventListener('click', async () => { const [whs, prods] = await Promise.all([api('/api/warehouses'), api('/api/products')]); sheet(t('adjust'), select(t('godown'), 'warehouse_id', whs.map(w => [w.warehouse_id, w.name])) + select('Product', 'sku', prods.map(p => [p.sku, p.name])) + field('Change (+ received, − write-off)', 'delta', 'required', 'number') + field(t('reason'), 'reason', 'required minlength="3"'), async d => { await post('/api/stock/adjust', { warehouse_id: d.warehouse_id, sku: d.sku, delta: Number(d.delta), reason: d.reason }); toast(t('saved')); render(); }); });
    $('#trf')?.addEventListener('click', async () => { const [whs, prods] = await Promise.all([api('/api/warehouses'), api('/api/products')]); sheet(t('transfer'), select('From', 'from_warehouse', whs.map(w => [w.warehouse_id, w.name])) + select('To', 'to_warehouse', whs.map(w => [w.warehouse_id, w.name]), whs[1]?.warehouse_id) + select('Product', 'sku', prods.map(p => [p.sku, p.name])) + field(t('quantity'), 'qty', 'required min="1"', 'number'), async d => { await post('/api/stock/transfer', { ...d, qty: Number(d.qty) }); toast(t('saved')); render(); }); });
    $('#recv')?.addEventListener('click', () => purchaseForm());
  };
  V.ledger = async sku => {
    const l = await api('/api/reports/stock-ledger/' + sku);
    view.innerHTML = h1(l.name, `${esc(sku)} · ${l.levels.map(x => `${esc(x.warehouse_id)} ${x.available} ${t('available')}`).join(' · ')}`, 'stock') + `<h2>${esc(t('stock_ledger'))}</h2><div class="list">${l.moves.map(m => item(row(`${esc(m.kind.replace('_', ' '))} <span class="hint">${esc(m.warehouse_id)}</span>`, `<b class="num" style="color:${m.delta > 0 ? 'var(--good)' : 'var(--crit)'}">${m.delta > 0 ? '+' : ''}${m.delta}</b>`) + meta(`${esc(m.ref)} · ${when(m.created_at)}`))).join('') || empty()}</div>`;
  };
  V.products = async () => {
    view.innerHTML = h1(t('products')) + (can('setup:write') ? `<div class="btnrow">${btn(t('add_product'), 'id="add"', 'btn primary')}</div>` : '') + '<div id="pl" class="list"></div>';
    const p = await api('/api/products?include_inactive=' + (can('setup:write') ? 'true' : 'false'));
    $('#pl').innerHTML = p.map(x => `<div class="item ${can('setup:write') ? 'tap' : ''}" data-sku="${esc(x.sku)}">${row(esc(x.name) + (x.active ? '' : ` <span class="pill">${t('inactive')}</span>`), `<b class="num">${fmt(x.unit_price)}</b>`)}${meta(`${esc(x.sku)} · ${esc(x.unit)}${x.category ? ' · ' + esc(x.category) : ''}${x.cost_price ? ` · cost ${fmt(x.cost_price)}` : ''}${x.aliases?.length ? ' · ' + esc(x.aliases.join(', ')) : ''}`)}</div>`).join('') || empty();
    $('#add')?.addEventListener('click', () => productForm());
    if (can('setup:write')) $('#pl').onclick = e => { const it = e.target.closest('[data-sku]'); if (it) productForm(p.find(x => x.sku === it.dataset.sku)); };
  };
  function productForm(p) {
    sheet(p ? t('edit') : t('add_product'), field('SKU', 'sku', p ? 'readonly' : 'placeholder="UREA-50"', 'text', p?.sku || '') + field(t('name'), 'name', 'required', 'text', p?.name || '') + field('Sale price', 'unit_price', 'required min="0"', 'number', p?.unit_price ?? '') + field('Cost price', 'cost_price', 'min="0"', 'number', p?.cost_price ?? 0) +
      select('Unit', 'unit', ['bag', 'ltr', 'pc', 'kg', 'box', 'carton'].map(u => [u, u]), p?.unit || 'bag') + field(t('category'), 'category', '', 'text', p?.category || '') + field('Other names (comma-separated, Urdu welcome)', 'aliases', '', 'text', (p?.aliases || []).join(', ')) + field('Load units per item', 'units_per_load', 'min="1"', 'number', p?.units_per_load ?? 1) + field('Low-stock alert at', 'min_stock', 'min="0"', 'number', p?.min_stock ?? 10) + (p ? `<label class="chk"><input type="checkbox" name="active" ${p.active ? 'checked' : ''}> ${esc(t('active'))}</label>` : ''),
      async d => { const body = { ...d, unit_price: Number(d.unit_price), cost_price: Number(d.cost_price), units_per_load: Number(d.units_per_load), min_stock: Number(d.min_stock), aliases: d.aliases.split(',').map(s => s.trim()).filter(Boolean), active: p ? !!d.active : true }; if (p) await patch('/api/products/' + p.sku, body); else await post('/api/products', body); toast(t('saved')); render(); });
  }

  // ================================================================ suppliers, purchases, expenses
  V.suppliers = async () => {
    view.innerHTML = h1(t('suppliers')) + `<div class="btnrow">${can('purchases:write') ? btn(t('add_supplier'), 'id="add"') + btn(t('receive_stock'), 'id="recv"', 'btn primary') : ''}</div><div id="sl" class="list"></div>`;
    const s = await api('/api/suppliers');
    $('#sl').innerHTML = `<div class="card row"><span class="tile"><div class="k">${esc(t('payables'))}</div><div class="v">${fmt(s.reduce((a, x) => a + x.balance, 0))}</div></span></div>` + s.map(x => `<a class="item tap" href="#supplier/${esc(x.supplier_id)}">${row(esc(x.name), `<b class="num">${fmt(x.balance)}</b>`)}${meta(`${esc(x.supplier_id)} · ${esc(x.phone || '')}`)}</a>`).join('');
    $('#add')?.addEventListener('click', () => sheet(t('add_supplier'), field(t('name'), 'name', 'required') + field(t('phone'), 'phone', '', 'tel') + field(t('address'), 'address') + field('Opening balance (what you owe today)', 'opening_balance', 'min="0"', 'number', 0), async d => { await post('/api/suppliers', { ...d, opening_balance: Number(d.opening_balance || 0) }); toast(t('saved')); render(); }));
    $('#recv')?.addEventListener('click', () => purchaseForm());
  };
  V.supplier = async id => {
    const k = await api(`/api/suppliers/${id}/khata`); const s = k.supplier;
    view.innerHTML = h1(s.name, `${esc(s.supplier_id)} · ${esc(s.phone || '')}`, 'suppliers') + `<div class="grid2">${kpi(t('balance'), fmt(k.balance), 'we owe')}${kpi(t('purchases'), k.purchases.length, 'recent')}</div>
      <div class="btnrow" style="margin:12px 0">${can('purchases:write') ? btn(t('receive_stock'), 'id="recv"', 'btn primary') : ''}${can('settings:write') ? btn(t('pay_supplier'), 'id="pay"') : ''}</div>
      <h2>${esc(t('recent_entries'))}</h2><div class="list">${k.recent.slice().reverse().map(e => item(row(esc(e.kind) + (e.method ? ` <span class="pill">${esc(e.method)}</span>` : ''), `<b class="num" style="color:${e.amount < 0 ? 'var(--good)' : 'var(--ink)'}">${fmt(e.amount)}</b>`) + meta(`${esc(e.entry_id)} · ${esc(e.ref)} · ${day(e.created_at)}`))).join('') || empty()}</div>
      <h2>${esc(t('purchases'))}</h2><div class="list">${k.purchases.map(p => item(row(p.items.map(i => `${i.qty}×${i.sku}`).join(', '), `<b class="num">${fmt(p.total)}</b>`) + meta(`${esc(p.purchase_id)} · ${esc(p.warehouse_id)} · ${day(p.created_at)}${p.invoice_ref ? ' · ' + esc(p.invoice_ref) : ''}`))).join('') || empty()}</div>`;
    $('#recv')?.addEventListener('click', () => purchaseForm(s));
    $('#pay')?.addEventListener('click', async () => { const m = await api('/api/methods'); sheet(t('pay_supplier'), `<p><b>${esc(s.name)}</b> · ${t('balance')} ${fmt(k.balance)}</p>` + field(t('amount'), 'amount', 'required min="1"', 'number') + select(t('method'), 'method', m.payment_methods.map(x => [x, x])) + field(t('reference'), 'ref'), async d => { await post(`/api/suppliers/${id}/pay`, { amount: Number(d.amount), method: d.method, ref: d.ref || '' }); toast(t('saved')); render(); }); });
  };
  async function purchaseForm(s) {
    const [suppliers, products, whs] = await Promise.all([api('/api/suppliers'), api('/api/products'), api('/api/warehouses')]);
    if (!suppliers.length) return toast('Add a supplier first.');
    const html = (s ? `<p><b>${esc(s.name)}</b></p>` : select(t('supplier'), 'supplier_id', suppliers.map(x => [x.supplier_id, x.name]))) + select(t('godown'), 'warehouse_id', whs.map(w => [w.warehouse_id, w.name]), whs.find(w => w.default)?.warehouse_id) +
      `<div id="plines" class="stack"></div><button type="button" class="btn" id="addP">+ ${esc(t('items'))}</button>` + field(t('bill_ref'), 'invoice_ref') + field(t('paid_now'), 'paid_amount', 'min="0"', 'number', 0) + `<div class="row"><b>${esc(t('total'))}</b><b class="num" id="ptotal">Rs 0</b></div>`;
    const { form } = sheet(t('receive_stock'), html, async d => {
      const items = form.__items();
      if (!items.length) throw new Error('Pick at least one product from the list');
      const r = await post('/api/purchases', { supplier_id: s ? s.supplier_id : d.supplier_id, warehouse_id: d.warehouse_id, items, invoice_ref: d.invoice_ref || '', paid_amount: Number(d.paid_amount || 0) });
      toast(`${t('saved')}: ${r.purchase_id} · ${fmt(r.total)}`); render();
    });
    const lines = $('#plines', form);
    const dl = document.createElement('datalist'); dl.id = 'pprods'; dl.innerHTML = products.map(p => `<option value="${esc(p.name)} (${esc(p.sku)})">`).join(''); form.appendChild(dl);
    const findProd = v => { const m = v.match(/\(([^)]+)\)\s*$/); const sku = m ? m[1] : v.trim().toUpperCase(); return products.find(p => p.sku === sku) || products.find(p => p.name.toLowerCase() === v.trim().toLowerCase()); };
    const recalc = () => { $('#ptotal', form).textContent = fmt($$('.pline', form).reduce((a, l) => a + Number(l.querySelector('.q').value || 0) * Number(l.querySelector('.c').value || 0), 0)); };
    const addLine = () => { const d = document.createElement('div'); d.className = 'pline row'; d.innerHTML = `<input class="input psel" list="pprods" placeholder="${esc(t('products'))}…" autocomplete="off"><input class="input num q" type="number" min="1" value="1" style="width:70px" placeholder="qty"><input class="input num c" type="number" min="0" value="0" style="width:90px" placeholder="cost">`; d.querySelector('.psel').addEventListener('change', e => { const p = findProd(e.target.value); if (p) d.querySelector('.c').value = p.cost_price || 0; recalc(); }); d.addEventListener('input', recalc); lines.appendChild(d); if (products.length <= 12 && lines.children.length === 1) { d.querySelector('.psel').value = `${products[0].name} (${products[0].sku})`; d.querySelector('.c').value = products[0].cost_price || 0; } recalc(); };
    form.__items = () => $$('.pline', form).map(l => ({ p: findProd(l.querySelector('.psel').value), qty: Number(l.querySelector('.q').value || 0), cost: Number(l.querySelector('.c').value || 0) })).filter(x => x.p && x.qty > 0).map(x => ({ sku: x.p.sku, qty: x.qty, unit_cost: x.cost }));
    $('#addP', form).onclick = addLine; addLine();
  }
  V.purchases = async () => {
    view.innerHTML = h1(t('purchases')) + `<div class="btnrow">${can('purchases:write') ? btn(t('receive_stock'), 'id="recv"', 'btn primary') : ''}</div><div id="pl" class="list"></div>`;
    const p = await api('/api/purchases');
    $('#pl').innerHTML = p.map(x => `<a class="item tap" href="#supplier/${esc(x.supplier_id)}">${row(esc(x.supplier_name), `<b class="num">${fmt(x.total)}</b>`)}${meta(`${x.items.map(i => `${i.qty}×${i.sku}`).join(', ')} · ${esc(x.warehouse_id)} · ${day(x.created_at)}${x.paid_amount ? ` · paid ${fmt(x.paid_amount)}` : ''}`)}</a>`).join('') || empty();
    $('#recv')?.addEventListener('click', () => purchaseForm());
  };
  V.expenses = async () => {
    view.innerHTML = h1(t('expenses')) + `<div class="btnrow">${can('expenses:write') ? btn(t('add_expense'), 'id="add"', 'btn primary') : ''}</div><div id="el"></div>`;
    const e = await api('/api/expenses');
    const byCat = {}; e.rows.forEach(x => byCat[x.category] = (byCat[x.category] || 0) + x.amount);
    $('#el').innerHTML = `<div class="card">${row(`${e.start} → ${e.end}`, `<b class="num">${fmt(e.total)}</b>`)}${bars(Object.entries(byCat).map(([c, v]) => ({ c, v })).sort((a, b) => b.v - a.v), 'v', 'c')}</div><div class="list" style="margin-top:10px">${e.rows.slice().reverse().map(x => item(row(`${esc(x.category)}${x.note ? ' · ' + esc(x.note) : ''}`, `<b class="num">${fmt(x.amount)}</b>`) + meta(`${esc(x.expense_date)} · ${esc(x.method)} · ${esc(x.paid_by)}`))).join('') || empty()}</div>`;
    $('#add')?.addEventListener('click', () => expenseForm());
  };
  async function expenseForm() {
    const m = await api('/api/methods');
    sheet(t('add_expense'), select(t('category'), 'category', m.expense_categories.map(c => [c, c])) + field(t('amount'), 'amount', 'required min="1" inputmode="numeric"', 'number') + field(t('note'), 'note') + select(t('method'), 'method', m.payment_methods.map(x => [x, x])) + field(t('date'), 'expense_date', '', 'date', today()),
      async d => { await post('/api/expenses', { ...d, amount: Number(d.amount) }); toast(t('saved')); if (location.hash === '#expenses') render(); });
  }

  // ================================================================ reports
  V.reports = async which => {
    const tabs = [['sales', 'sales'], ['profit', 'profit'], ['collections', 'collections'], ['cashbook', 'cashbook'], ['valuation', 'valuation'], ['slow', 'slow_stock'], ['top', 'top_customers']];
    which = which || 'sales';
    view.innerHTML = h1(t('reports')) + `<div class="chips">${tabs.map(([k, l]) => `<a class="chip ${k === which ? 'ok' : ''}" href="#reports/${k}">${esc(t(l))}</a>`).join('')}</div><div id="rp">${t('loading')}</div>`;
    const rp = $('#rp'); const s0 = daysAgo(29), e0 = today();
    const withRange = async (path, draw) => { rp.innerHTML = dateRange('r', s0, e0) + '<div id="rb"></div>'; const load = async () => { $('#rb').innerHTML = draw(await api(`${path}?start=${$('#rs').value}&end=${$('#re').value}`)); }; $('#rgo').onclick = load; await load(); };
    if (which === 'sales') await withRange('/api/reports/sales', r => `<div class="grid2">${kpi(t('revenue'), fmt(r.revenue), `${r.invoices} invoices`)}${kpi(t('gross_margin'), fmt(r.gross_margin), `${r.margin_pct}%`)}</div><h2>By product</h2>${bars(r.by_product.slice(0, 10), 'revenue', 'name')}<h2>By customer</h2>${bars(r.by_customer.slice(0, 10), 'revenue', 'name')}${r.by_booker?.length > 1 ? `<h2>By who booked it</h2>${bars(r.by_booker, 'revenue', 'name')}` : ''}<h2>By day</h2>${bars(r.by_day, 'revenue', 'date')}`);
    else if (which === 'profit') await withRange('/api/reports/profit', r => `<div class="grid2">${kpi(t('revenue'), fmt(r.revenue))}${kpi(t('cost'), fmt(r.cost_of_goods))}${kpi(t('gross_margin'), fmt(r.gross_margin))}${kpi(t('expenses'), fmt(r.expenses))}</div><div class="card" style="margin-top:10px">${row(`<b>${esc(t('net'))}</b>`, `<b class="num" style="font-size:20px;color:${r.net >= 0 ? 'var(--good)' : 'var(--crit)'}">${fmt(r.net)}</b>`)}</div><h2>${esc(t('expenses'))}</h2>${bars(Object.entries(r.expenses_by_category).map(([c, v]) => ({ c, v })), 'v', 'c')}<p class="hint">Cost of goods uses each product's current cost price.</p>`);
    else if (which === 'collections') await withRange('/api/reports/collections', r => `<div class="grid2">${kpi('Invoiced', fmt(r.invoiced))}${kpi('Collected', fmt(r.collected), `${r.collection_rate_pct}%`)}</div><h2>${esc(t('method'))}</h2>${bars(Object.entries(r.by_method).map(([c, v]) => ({ c, v })), 'v', 'c')}<h2>Aging</h2>${bars(Object.entries(r.aging.buckets).map(([c, v]) => ({ c, v })), 'v', 'c')}`);
    else if (which === 'cashbook') { rp.innerHTML = `<input class="input" type="date" id="cd" value="${e0}"><div id="cb"></div>`; const load = async () => { const r = await api('/api/reports/cashbook?date=' + $('#cd').value); const line = x => item(row(`${esc(x.kind)} · ${esc(x.who)}`, `<b class="num">${fmt(x.amount)}</b>`) + meta(`${esc(x.ref)}${x.by ? ' · ' + esc(x.by) : ''}`)); $('#cb').innerHTML = `<div class="grid2" style="margin-top:10px">${kpi('In', fmt(r.total_in + r.total_handins))}${kpi('Out', fmt(r.total_out))}</div><div class="card" style="margin-top:10px">${row(`<b>${esc(t('net'))}</b>`, `<b class="num">${fmt(r.net)}</b>`)}</div><h2>Cash in</h2><div class="list">${[...r.cash_in, ...r.driver_handins].map(line).join('') || empty()}</div><h2>Cash out</h2><div class="list">${r.cash_out.map(line).join('') || empty()}</div>`; }; $('#cd').onchange = load; await load(); }
    else if (which === 'valuation') { const r = await api('/api/reports/stock-valuation'); rp.innerHTML = `<div class="grid2">${kpi('At cost', fmt(r.at_cost))}${kpi('At sale', fmt(r.at_sale))}</div><div class="list" style="margin-top:10px">${r.rows.map(x => item(row(`${esc(x.name)} <span class="hint">${esc(x.warehouse_id)}</span>`, `<b class="num">${fmt(x.at_cost)}</b>`) + meta(`${x.on_hand} on hand · at sale ${fmt(x.at_sale)}`))).join('')}</div>`; }
    else if (which === 'slow') { const r = await api('/api/reports/slow-stock?days=30'); rp.innerHTML = `<p class="sub">Stock on hand with no sale in 30 days.</p><div class="list">${r.map(x => `<a class="item tap" href="#ledger/${esc(x.sku)}">${row(esc(x.name), `<b class="num">${fmt(x.value_at_cost)}</b>`)}${meta(`${x.on_hand} on hand`)}</a>`).join('') || empty('Everything moved in the last 30 days.')}</div>`; }
    else if (which === 'top') { const r = await api('/api/reports/top-customers?days=30'); rp.innerHTML = `<p class="sub">${esc(t('last_30'))}</p>${bars(r, 'revenue', 'name')}`; }
  };

  // ================================================================ reminders, promises, outbox
  V.reminders = async () => {
    view.innerHTML = h1(t('reminders'), t('templated_only')) + (can('reminders:send') ? `<div class="btnrow">${btn(t('draft_due'), 'id="due"', 'btn primary')}</div>` : '') + '<div id="rl" class="list"></div>';
    const r = await api('/api/reminders');
    $('#rl').innerHTML = r.map(x => `<div class="item">${row(esc(x.customer_name), `<span class="pill ${statusPill(x.status)}">${esc(x.status)}</span>`)}${meta(`${esc(x.tier)} · ${x.days_overdue}d · ${fmt(x.amount_due)} · ${day(x.created_at)}`)}<span class="quote">${esc(x.message)}</span>${x.status === 'drafted' && can('reminders:send') ? `<div class="btnrow"><button class="btn primary" data-send="${esc(x.reminder_id)}">${esc(t('approve_send'))}</button><button class="btn" data-discard="${esc(x.reminder_id)}">${esc(t('discard'))}</button></div>` : ''}</div>`).join('') || empty();
    $('#rl').onclick = async e => { const s = e.target.closest('[data-send]'), d = e.target.closest('[data-discard]'); const b = s || d; if (!b) return; b.disabled = true; try { await post(`/api/reminders/${s ? s.dataset.send : d.dataset.discard}/${s ? 'send' : 'discard'}`); toast(s ? t('sent') : t('discard')); render(); } catch (err) { toast(err.message); b.disabled = false; } };
    $('#due')?.addEventListener('click', async () => { try { const x = await post('/api/reminders/due?min_days=1'); toast(`Drafted ${x.length}`); render(); } catch (e) { toast(e.message); } });
  };
  V.promises = async () => {
    view.innerHTML = h1(t('promises')) + '<div id="pl" class="list"></div>';
    const p = await api('/api/promises');
    $('#pl').innerHTML = p.slice().reverse().map(x => `<a class="item tap" href="#customer/${esc(x.customer_id)}">${row(esc(x.customer_name), `<span class="pill ${x.kept ? 'good' : x.broken ? 'crit' : x.kept === null ? '' : 'warn'}">${x.kept ? t('kept') : x.broken ? t('broken') : x.kept === null ? 'older' : t('open')}</span>`)}<div class="row">${meta(`by ${esc(x.promised_date)} · logged ${day(x.created_at)}`)}<b class="num">${fmt(x.amount)}</b></div></a>`).join('') || empty();
  };
  V.outbox = async () => {
    view.innerHTML = h1(t('outbox'), t('loading')) + '<div id="ol" class="list"></div>';
    const o = await api('/api/outbox');
    $('.sub').innerHTML = o.channel === 'outbox' ? 'No WhatsApp channel configured — send each with one tap, then mark sent.' : `Delivered automatically via ${esc(o.channel)}.`;
    $('#ol').innerHTML = (o.channel === 'outbox' && o.rows.some(r => r.status === 'queued') ? '' : '') + o.rows.map(r => `<div class="item">${row(esc(r.to_phone), `<span class="pill ${statusPill(r.status)}">${esc(r.status)}</span>`)}<span class="quote">${esc(r.text)}</span>${meta(`${esc(r.ref)} · ${when(r.created_at)}${r.error ? ' · <span class="crit">' + esc(r.error) + '</span>' : ''}`)}${r.status !== 'sent' ? `<div class="btnrow">${r.wa_link ? `<a class="btn primary" href="${esc(r.wa_link)}" target="_blank" rel="noopener">${esc(t('open_whatsapp'))}</a>` : ''}${r.status === 'failed' && o.channel !== 'outbox' ? `<button class="btn" data-retry="${esc(r.msg_id)}">Retry</button>` : ''}<button class="btn" data-sent="${esc(r.msg_id)}">${esc(t('mark_sent'))}</button></div>` : ''}</div>`).join('') || empty();
    $('#ol').onclick = async e => { const b = e.target.closest('[data-sent]'), r = e.target.closest('[data-retry]'); if (!b && !r) return; await post(b ? `/api/outbox/${b.dataset.sent}/sent` : `/api/outbox/${r.dataset.retry}/retry`); render(); };
  };
  V.doc = async id => {
    const d = await api('/api/documents/' + id); const e = d.entry;
    view.innerHTML = h1(`${d.kind} ${e.entry_id}`, `${esc(d.customer.name)} · ${day(e.created_at)}`, 'customer/' + e.customer_id) +
      `<div class="card"><div class="kv">${d.lines.map(l => `<b>${esc(l.name)}</b><span>${l.qty} ${esc(l.unit)} × ${fmt(l.unit_price)} = <b class="num">${fmt(l.total)}</b></span>`).join('')}<b>${esc(d.kind)}</b><span class="num"><b>${fmt(d.amount)}</b></span>${e.method ? `<b>${esc(t('method'))}</b><span>${esc(e.method)}</span>` : ''}${e.ref ? `<b>${esc(t('reference'))}</b><span>${esc(e.ref)}</span>` : ''}${e.due_date ? `<b>Due</b><span>${esc(e.due_date)}</span>` : ''}<b>${esc(t('balance'))}</b><span class="num">${fmt(d.balance_after)}</span></div></div>
      <div class="btnrow" style="margin-top:12px">${d.wa_link ? `<a class="btn primary" href="${esc(d.wa_link)}" target="_blank" rel="noopener">${esc(t('open_whatsapp'))}</a>` : ''}<button class="btn" id="pdf">${esc(t('pdf'))}</button><button class="btn" id="html">${esc(t('print'))}</button><button class="btn" id="copy">${esc(t('copied')).replace(t('copied'), 'Copy link')}</button></div>
      <p class="hint" style="margin-top:10px;word-break:break-all">${esc(d.public_url)}</p>`;
    $('#pdf').onclick = () => download(`/api/documents/${id}/pdf`, id + '.pdf'); $('#html').onclick = () => openAuthed(`/api/documents/${id}/html`);
    $('#copy').onclick = async () => { try { await navigator.clipboard.writeText(d.public_url); toast(t('copied')); } catch { toast(d.public_url, 6000); } };
  };

  // ================================================================ audit, staff, setup, settings, more
  V.audit = async () => {
    view.innerHTML = h1(t('audit'), t('every_write')) + '<div id="al" class="list"></div>';
    const a = await api('/api/audit');
    $('#al').innerHTML = a.map(x => item(row(esc(x.action.replace(/_/g, ' ')), `<span class="pill ${x.approved_by ? 'good' : ''}">${x.approved_by ? esc(x.approved_by) : 'auto'}</span>`) + meta(`${esc(x.actor)}${x.user ? ' · ' + esc(x.user) : ''} · ${esc(x.entity)} ${esc(x.entity_id)} · ${when(x.created_at)}`))).join('') || empty();
  };
  V.staff = async () => {
    view.innerHTML = h1(t('staff')) + `<div class="btnrow">${btn(t('add_staff'), 'id="add"', 'btn primary')}</div><div id="sl" class="list"></div>`;
    const s = await api('/api/staff');
    $('#sl').innerHTML = s.map(u => `<div class="item" data-u="${esc(u.user_id)}">${row(`${esc(u.name)}${u.user_id === state.me.user_id ? ' <span class="hint">(you)</span>' : ''}`, `<span class="pill ${u.active ? (u.locked ? 'crit' : 'acc') : ''}">${u.active ? (u.locked ? 'locked' : esc(u.role)) : t('inactive')}</span>`)}${meta(`${esc(u.phone)} · ${esc(u.role_title)}${u.last_login ? ' · last ' + when(u.last_login) : ''}`)}<div class="btnrow"><button class="btn" data-edit>${esc(t('edit'))}</button><button class="btn" data-pin>PIN</button><button class="btn" data-out>${esc(t('signout'))}</button></div></div>`).join('');
    $('#add').onclick = () => sheet(t('add_staff'), field(t('name'), 'name', 'required') + field(t('phone'), 'phone', 'required inputmode="tel"', 'tel') + select(t('role'), 'role', [['clerk', 'Clerk (munshi)'], ['salesman', 'Salesman'], ['driver', 'Driver'], ['owner', 'Owner']]) + field(t('choose_pin'), 'pin', 'required minlength="4" maxlength="6" inputmode="numeric"', 'password'), async d => { await post('/api/staff', d); toast(t('saved')); render(); });
    $('#sl').onclick = e => {
      const box = e.target.closest('[data-u]'); if (!box) return; const u = s.find(x => x.user_id === box.dataset.u);
      if (e.target.closest('[data-edit]')) sheet(t('edit'), field(t('name'), 'name', 'required', 'text', u.name) + select(t('role'), 'role', [['clerk', 'Clerk (munshi)'], ['salesman', 'Salesman'], ['driver', 'Driver'], ['owner', 'Owner']], u.role) + `<label class="chk"><input type="checkbox" name="active" ${u.active ? 'checked' : ''}> ${esc(t('active'))}</label>`, async d => { await patch('/api/staff/' + u.user_id, { name: d.name, role: d.role, active: !!d.active }); toast(t('saved')); render(); });
      if (e.target.closest('[data-pin]')) sheet(`PIN · ${u.name}`, field(t('choose_pin'), 'pin', 'required minlength="4" maxlength="6" inputmode="numeric"', 'password'), async d => { await post(`/api/staff/${u.user_id}/pin`, { pin: d.pin }); toast(t('saved')); });
      if (e.target.closest('[data-out]')) confirmSheet(t('signout'), `Sign ${u.name} out of every phone?`, async () => { const r = await post(`/api/staff/${u.user_id}/signout`); toast(`${r.sessions_revoked} session(s) ended`); });
    };
  };
  V.setup = async () => {
    view.innerHTML = h1(t('setup'), t('loading'));
    const [whs, routes, vehicles, settings, customers] = await Promise.all([api('/api/warehouses'), api('/api/routes'), api('/api/vehicles'), api('/api/settings'), api('/api/customers')]);
    view.innerHTML = h1(t('setup'), esc(settings.business_name)) +
      `<div class="card stack"><b>${esc(t('business'))}</b>${btn(t('edit'), 'id="biz"')}</div>
      <h2>${esc(t('godown'))}</h2><div class="list">${whs.map(w => item(row(esc(w.name), w.default ? '<span class="pill acc">default</span>' : `<button class="btn sm" data-def="${esc(w.warehouse_id)}">make default</button>`) + meta(esc(w.warehouse_id)))).join('')}</div><div class="btnrow" style="margin-top:8px">${btn(t('add_godown'), 'id="addW"')}</div>
      <h2>${esc(t('route'))}</h2><div class="list">${routes.map(r => item(row(esc(r.name), `<button class="btn sm" data-route="${esc(r.route_id)}">${esc(t('edit'))}</button>`) + meta(`${esc(r.route_id)} · ${esc(r.warehouse_id)} · ${r.stop_customer_ids.length} stops: ${r.stop_customer_ids.map(id => esc(customers.find(c => c.customer_id === id)?.name || id)).join(' → ')}`))).join('') || empty()}</div><div class="btnrow" style="margin-top:8px">${btn(t('add_route'), 'id="addR"')}</div>
      <h2>${esc(t('vehicle'))}</h2><div class="list">${vehicles.map(v => item(row(esc(v.plate), `<button class="btn sm danger" data-delv="${esc(v.vehicle_id)}" aria-label="Remove vehicle ${esc(v.plate)}">✕</button>`) + meta(`${esc(v.vehicle_id)} · ${esc(v.capacity_class)} · ${v.capacity_units} units`))).join('') || empty()}</div><div class="btnrow" style="margin-top:8px">${btn(t('add_vehicle'), 'id="addV"')}</div>
      <h2>${esc(t('import_excel'))}</h2><div class="card stack"><p class="hint" style="margin:0">Customers, products, stock and opening balances in one workbook.</p><div class="btnrow">${btn(t('download_template'), 'id="tpl"')}<label class="btn primary">${esc(t('import_excel'))}<input type="file" id="imp" accept=".xlsx" hidden></label></div><div id="impRes" class="hint"></div></div>`;
    $('#biz').onclick = () => sheet(t('business'), field(t('business_name'), 'business_name', 'required', 'text', settings.business_name) + field(t('city'), 'city', '', 'text', settings.city) + field(t('phone'), 'phone', '', 'tel', settings.phone) + field("Owner's WhatsApp (for the 8pm digest)", 'owner_phone', '', 'tel', settings.owner_phone) + field('Digest time', 'digest_time', 'pattern="[0-2][0-9]:[0-5][0-9]"', 'time', settings.digest_time) + field('Default credit days', 'credit_days', 'min="0"', 'number', settings.credit_days) + field('Invoice prefix', 'invoice_prefix', 'maxlength="6"', 'text', settings.invoice_prefix) + field('Orders above this need the owner (Rs)', 'big_order_limit', 'min="0"', 'number', settings.big_order_limit),
      async d => { await patch('/api/settings', { ...d, credit_days: Number(d.credit_days), big_order_limit: Number(d.big_order_limit) }); state.me = await api('/api/me'); M.store.set('me', state.me); toast(t('saved')); render(); });
    $('#addW').onclick = () => sheet(t('add_godown'), field(t('name'), 'name', 'required') + field('ID (optional)', 'warehouse_id', 'placeholder="WH-CITY"'), async d => { await post('/api/warehouses', d); toast(t('saved')); render(); });
    $$('[data-def]').forEach(b => b.onclick = async () => { await patch('/api/settings', { default_warehouse: b.dataset.def }); render(); });
    const routeForm = r => sheet(r ? t('edit') : t('add_route'), field(t('name'), 'name', 'required', 'text', r?.name || '') + (r ? '' : field('ID (optional)', 'route_id', 'placeholder="R-NORTH"')) + select(t('godown'), 'warehouse_id', whs.map(w => [w.warehouse_id, w.name]), r?.warehouse_id) + `<b>Stops, in order</b><p class="hint">Tick customers; order is the order you tick.</p><div class="list" id="stopPick">${customers.map(c => `<label class="item chk"><input type="checkbox" name="c_${esc(c.customer_id)}" ${r?.stop_customer_ids.includes(c.customer_id) ? 'checked' : ''}> <span>${esc(c.name)} <span class="hint">${esc(c.route_id || '')}</span></span></label>`).join('')}</div>`,
      async (d, form) => { const order = form.__order || (r?.stop_customer_ids || []); const ids = [...order.filter(id => d['c_' + id]), ...customers.map(c => c.customer_id).filter(id => d['c_' + id] && !order.includes(id))]; await post('/api/routes', { route_id: r?.route_id || d.route_id || '', name: d.name, warehouse_id: d.warehouse_id, stop_customer_ids: ids }); toast(t('saved')); render(); });
    $('#addR').onclick = () => { const s = routeForm(); s.form.__order = []; s.form.addEventListener('change', e => { if (e.target.type === 'checkbox') { const id = e.target.name.slice(2); s.form.__order = s.form.__order.filter(x => x !== id); if (e.target.checked) s.form.__order.push(id); } }); };
    $$('[data-route]').forEach(b => b.onclick = () => { const r = routes.find(x => x.route_id === b.dataset.route); const s = routeForm(r); s.form.__order = [...r.stop_customer_ids]; s.form.addEventListener('change', e => { if (e.target.type === 'checkbox') { const id = e.target.name.slice(2); s.form.__order = s.form.__order.filter(x => x !== id); if (e.target.checked) s.form.__order.push(id); } }); });
    $('#addV').onclick = () => sheet(t('add_vehicle'), field(t('plate'), 'plate', 'required') + select('Size', 'capacity_class', [['small', 'small'], ['medium', 'medium'], ['large', 'large']]) + field(`${t('capacity')} (load units, e.g. bags)`, 'capacity_units', 'required min="1"', 'number', 120), async d => { await post('/api/vehicles', { ...d, capacity_units: Number(d.capacity_units) }); toast(t('saved')); render(); });
    $$('[data-delv]').forEach(b => b.onclick = () => confirmSheet(t('vehicle'), 'Remove this vehicle?', async () => { await del('/api/vehicles/' + b.dataset.delv); render(); }));
    $('#tpl').onclick = () => download('/api/import/template.xlsx', 'munshi-import-template.xlsx');
    $('#imp').onchange = async e => { const f = e.target.files[0]; if (!f) return; const fd = new FormData(); fd.append('file', f); $('#impRes').textContent = t('loading'); try { const r = await api('/api/import', { method: 'POST', body: fd }); $('#impRes').innerHTML = `Imported: ${r.customers} customers, ${r.products} products, ${r.stock} stock rows, ${r.suppliers} suppliers, ${r.opening_balances} opening balances.${r.errors.length ? `<br><span class="crit">${r.errors.length} row(s) skipped:</span><br>${r.errors.map(esc).join('<br>')}` : ''}`; state.me = await api('/api/me'); M.store.set('me', state.me); } catch (err) { $('#impRes').textContent = err.message; } };
  };
  V.settings = async () => {
    const me = state.me;
    view.innerHTML = h1(t('settings')) + `<div class="stack">
      <div class="card kv"><b>${esc(t('signed_in_as'))}</b><span>${esc(me.name)} · ${esc(me.role_title)}</span><b>${esc(t('business'))}</b><span>${esc(me.business.name)}${me.business.demo ? ' <span class="pill">demo</span>' : ''}</span><b>${esc(t('model'))}</b><span>${esc(me.llm)}${me.llm === 'stub' ? ' <span class="hint">(offline rules — set LLM_PROVIDER=groq for a real model)</span>' : ''}</span><b>${esc(t('voice'))}</b><span>${me.voice ? 'on' : 'off <span class="hint">(needs GROQ_API_KEY)</span>'}</span><b>${esc(t('channel'))}</b><span>${esc(me.channel)}</span></div>
      <div class="card stack"><b>${esc(t('language'))}</b><div class="btnrow"><button class="btn ${state.lang === 'en' ? 'primary' : ''}" data-lang="en" aria-pressed="${state.lang === 'en'}">English</button><button class="btn ${state.lang === 'ur' ? 'primary' : ''}" data-lang="ur" aria-pressed="${state.lang === 'ur'}">اردو</button></div></div>
      <div class="card stack"><b>${esc(t('theme'))}</b><div class="btnrow">${['light', 'dark'].map(th => `<button type="button" class="btn ${state.theme === th ? 'primary' : ''}" data-theme-pick="${th}" aria-pressed="${state.theme === th}">${esc(t('theme_' + th))}</button>`).join('')}</div></div>
      ${btn(t('change_pin'), 'id="pin"')}
      ${can('export') ? btn(t('export_excel'), 'id="exp"') : ''}${can('backup') && !me.business.demo ? btn(t('backup'), 'id="bak"') : ''}
      <button class="btn" id="install" hidden>${esc(t('install'))}</button>
      ${window.Capacitor?.isNativePlatform?.() ? `<button class="btn" id="server">Change server</button>` : ''}
      <button class="btn danger" id="out">${esc(t('signout'))}</button>
      <p class="hint">Munshi ${esc((await api('/api/config')).version)} · <a class="link" href="https://github.com/azamali992/munshi" target="_blank" rel="noopener">source</a></p></div>`;
    $$('[data-lang]').forEach(b => b.onclick = () => setLang(b.dataset.lang));
    $$('[data-theme-pick]').forEach(b => b.onclick = () => M.setTheme(b.dataset.themePick));
    $('#out').onclick = () => signOut();
    $('#server')?.addEventListener('click', () => { location.href = 'https://localhost/?reset=1'; });
    $('#pin').onclick = () => sheet(t('change_pin'), field('Current PIN', 'old_pin', 'required', 'password') + field(t('choose_pin'), 'new_pin', 'required minlength="4" maxlength="6"', 'password'), async d => { await post('/api/me/pin', d); toast('PIN changed — sign in again'); signOut(true); });
    $('#exp')?.addEventListener('click', () => download('/api/export.xlsx', `munshi-${today()}.xlsx`));
    $('#bak')?.addEventListener('click', () => download('/api/backup', `munshi-backup-${today()}.db`));
    if (window.__installPrompt) { $('#install').hidden = false; $('#install').onclick = () => window.__installPrompt.prompt(); }
  };
  V.help = () => {
    const R = role();
    const cmds = { owner: ['Profit this month', 'Kaun kitna baqi hai?', 'Credit note Rana Brothers 5000 damaged bags', 'Pay Fauji 100000 by bank', 'Restock WH-MULTAN 100 urea received', 'Slow stock 30 days', 'Digest'],
      clerk: ['Chaudhry Farms ko 20 urea aur 5 dap bhej do', 'Confirm ORD-…', 'Allocate ORD-… at WH-…', 'Suggest dispatch for today', 'Approve DSP-…', 'DSP-… driver handed 45000', 'Chaudhry Farms paid 20000 jazzcash', 'Expense diesel 5000', 'Received 100 urea from Fauji at 3600 bill FF-1', 'Remind everyone over 30 days', 'Haji Sons promise 50000 by 2026-09-20', 'Aaj ka cashbook'],
      salesman: ['Rana Brothers ko 5 dap bhej do', 'Rana Brothers ka khata', 'Urea stock kitna hai', 'Haji Sons promise 50000 by 2026-09-20'],
      driver: ['Stops for DSP-…', 'Close STP-… delivered all cash 50000 otp 1234'] }[R] || [];
    const steps = { owner: ['Today shows the numbers; tap a tile to drill in.', 'Approvals: money and stock wait for you. Approve or reject with a reason.', 'Reports for sales, profit, collections, cashbook, stock value.', 'Staff: add people with a PIN; sign a lost phone out in one tap.', 'Setup: godowns, routes (stop order), vehicles (capacity), Excel import.', 'Settings → Backup: your whole business in one file.'],
      clerk: ['Desk shows the queue: drafts → confirm, confirmed → allocate, allocated → plan, vans → hand-in.', 'New order by form, or say it in chat. Confirm when the customer says yes.', 'Suggest today\'s plan, tick, create. Approve loading — codes go to customers.', 'When the driver returns: open the plan → Record hand-in. A shortfall names the stop.', 'Record payments at the counter, expenses, stock received from suppliers.', 'Khata → Draft for everyone overdue → Approve & send.'],
      salesman: ['Book order: pick the customer (balance and limit shown), add lines, save. The office confirms.', 'Customers: tap for khata and statement.', 'Log a promise to pay from the customer page; it shows on the office\'s khata.'],
      driver: ['Stops: today\'s route in order, with address and phone.', 'Tap a stop: delivered, returned, cash, the customer\'s 4-digit code, a note if something was short.', 'No signal? It saves on the phone and syncs itself. The badge shows what\'s queued.'] }[R] || [];
    view.innerHTML = h1('Help', esc(state.me?.role_title || '')) + `<h2>What to do</h2><div class="list">${steps.map((x, i) => item(`<span class="t">${i + 1}. ${esc(x)}</span>`)).join('')}</div>
      <h2>Things you can say in chat</h2><div class="chips" style="flex-wrap:wrap">${cmds.map(q => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join('')}</div>
      <p class="hint">Urdu, English or both. IDs like ORD-…, DSP-…, STP-… come from the screens. Nothing changes stock or money until someone approves.</p>
      <p class="hint"><a class="link" href="https://github.com/azamali992/munshi/blob/main/docs/user-guide.md" target="_blank" rel="noopener">Full user guide (English / اردو) ›</a></p>`;
    $$('[data-q]').forEach(b => b.onclick = () => prefill(b.dataset.q));
  };

  V.more = () => {
    const labels = { orders: [t('orders'), 'drafts, confirmed, dispatched, delivered'], dispatch: [t('dispatch'), 'plans, vehicles, stops'], driver: [t('driver_mode'), 'close stops with the customer code'], stock: [t('stock'), 'by godown, movement history'], customers: [t('customers'), 'tiers, limits, routes'], products: [t('products'), 'prices, cost, aliases'], suppliers: [t('suppliers'), 'what we owe'], purchases: [t('purchases'), 'stock received'], expenses: [t('expenses'), 'fuel, salaries, rent'], reports: [t('reports'), 'sales, profit, collections, cashbook'], reminders: [t('reminders'), 'drafted and sent'], outbox: [t('outbox'), 'delivery codes, invoices, receipts'], promises: [t('promises'), 'kept and broken'], notifications: [t('notifications'), ''], audit: [t('audit'), 'who approved what'], staff: [t('staff'), 'people and PINs'], setup: [t('setup'), 'godowns, routes, vehicles, import'], settings: [t('settings'), 'language, PIN, export, backup'], khata: [t('khata'), 'who owes what'], help: ['Help', 'what to do, what to say'] };
    view.innerHTML = h1(t('more')) + `<div class="list more">${(MORE[role()] || MORE.clerk).map(k => `<a class="item tap" href="#${k}"><span><span class="t">${esc(labels[k][0])}</span><br><span class="m">${esc(labels[k][1])}</span></span><span class="muted">›</span></a>`).join('')}</div>`;
  };

  function setupMic(btn, msgs) {
    let rec, chunks = [];
    const start = async () => { try { const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); rec = new MediaRecorder(stream); chunks = []; rec.ondataavailable = e => chunks.push(e.data); rec.onstop = async () => { stream.getTracks().forEach(x => x.stop()); const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' }); const fd = new FormData(); fd.append('audio', blob, 'note.webm'); msgs.insertAdjacentHTML('beforeend', '<div class="msg me">🎙 …</div>'); try { const r = await api('/api/voice?thread_id=' + state.thread, { method: 'POST', body: fd }); msgs.lastElementChild.textContent = '🎙 ' + r.transcript; msgs.insertAdjacentHTML('beforeend', r.pending ? renderApproval(r.pending) : renderMsg({ role: 'munshi', text: r.text, meta: { specialist: r.specialist } })); } catch (err) { toast(err.message); } refreshBadge(); }; rec.start(); btn.classList.add('rec'); } catch (e) { toast('Microphone not available'); } };
    const stop = () => { if (rec && rec.state === 'recording') rec.stop(); btn.classList.remove('rec'); };
    btn.addEventListener('pointerdown', start); btn.addEventListener('pointerup', stop); btn.addEventListener('pointerleave', stop);
  }
})();
