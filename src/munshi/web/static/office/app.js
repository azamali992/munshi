/* Office console shell: sign-in (the same phone + PIN, the same session as the phone app), the role gate
   (owner and clerk only), the left nav and a hash router. Each screen is its own module. */
import { $, $$, api, esc, post, signOut, state, store, toast } from './lib.js';
import products from './products.js';
import inventory from './inventory.js';
import clients from './clients.js';
import { godowns, suppliers } from './suppliers.js';
import importExport from './import.js';

const NAV = [
  ['products', 'Products & prices', '▦'],
  ['inventory', 'Inventory', '▤'],
  ['clients', 'Clients', '☰'],
  ['suppliers', 'Suppliers', '⇲'],
  ['godowns', 'Godowns', '⌂'],
  ['import', 'Import / export', '⇅'],
];
const SCREENS = { products, inventory, clients, suppliers, godowns, import: importExport };
const OFFICE_ROLES = ['owner', 'clerk'];
const root = $('#root');

state.onSignedOut = () => { location.hash = ''; render(); };

// ---------------------------------------------------------------- sign-in
async function signInView() {
  let cfg = { demo: false, demo_users: [] }; try { cfg = await api('/api/config'); } catch { }
  root.innerHTML = `<div class="o-center"><form class="o-login" id="lf">
      <div class="o-brand" style="padding:0"><span class="brand-mark"></span>Munshi <span class="muted" style="font-weight:600">· Office</span></div>
      <h1>Sign in</h1><p class="sub" style="margin:0">The same phone number and PIN as the phone app.</p>
      <label class="f">Phone<input class="input" name="phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="0300-1234567" required></label>
      <label class="f">PIN<input class="input" name="pin" type="password" inputmode="numeric" autocomplete="current-password" maxlength="6" required></label>
      <p class="formerr" role="alert"></p>
      <button class="btn primary" type="submit">Sign in</button>
      ${cfg.demo ? `<p class="hint" style="margin:4px 0 0">Demo accounts</p><div class="chips" style="flex-wrap:wrap">${cfg.demo_users.map(u => `<button type="button" class="chip" data-phone="${esc(u.phone)}" data-pin="${esc(u.pin)}">${esc(u.role)} · ${esc(u.name.split(' ')[0])}</button>`).join('')}</div>` : ''}
      <p class="hint" style="margin:0"><a class="link" href="/">Open the phone app instead ›</a></p></form></div>`;
  const f = $('#lf');
  $$('[data-phone]', f).forEach(b => b.onclick = () => { f.phone.value = b.dataset.phone; f.pin.value = b.dataset.pin; f.requestSubmit(); });
  f.onsubmit = async e => {
    e.preventDefault(); $('.formerr', f).textContent = '';
    try {
      const r = await post('/api/session', { phone: f.phone.value, pin: f.pin.value, device: 'office · ' + navigator.userAgent.slice(0, 50) });
      state.token = r.token; state.me = r.me; store.set('token', r.token); store.set('me', r.me);
      if (!location.hash || location.hash === '#') location.hash = '#/products'; else render();
    } catch (err) { $('.formerr', f).textContent = err.message; }
  };
}

// ---------------------------------------------------------------- salesman / driver
function gateView() {
  root.innerHTML = `<div class="o-center"><div class="o-login">
    <div class="o-brand" style="padding:0"><span class="brand-mark"></span>Munshi <span class="muted" style="font-weight:600">· Office</span></div>
    <h1>Use the phone app</h1>
    <p style="margin:0">The office console is for the owner and the clerk. As ${esc(state.me.role_title || state.me.role)}, everything you need
      -- ${state.me.role === 'driver' ? 'your stops, delivery codes and cash' : 'booking orders, your customers and their khata'} -- is in the phone app.</p>
    <a class="btn primary" href="/">Open the phone app</a>
    <button class="btn ghost" id="so">Sign out (${esc(state.me.name)})</button></div></div>`;
  $('#so').onclick = () => signOut();
}

// ---------------------------------------------------------------- shell + router
function shell() {
  if ($('.o-shell', root)) return;
  const me = state.me;
  root.innerHTML = `<div class="o-shell">
    <nav class="o-nav" aria-label="Office">
      <div class="o-brand"><span class="brand-mark"></span>Munshi</div>
      <div class="o-biz">${esc(me.business.name)} · office</div>
      ${NAV.map(([k, label, ico]) => `<a href="#/${k}" data-nav="${k}"><span class="ico" aria-hidden="true">${ico}</span>${esc(label)}</a>`).join('')}
      <div class="grow"></div>
      <a href="/" title="The chat-first phone app"><span class="ico" aria-hidden="true">✎</span>Phone app</a>
      <a href="#" id="themeT"><span class="ico" aria-hidden="true">◐</span>${store.get('theme', 'light') === 'dark' ? 'Light theme' : 'Dark theme'}</a>
      <a href="#" id="signout"><span class="ico" aria-hidden="true">⎋</span>Sign out</a>
      <div class="o-me"><b>${esc(me.name)}</b>${esc(me.role_title || me.role)}</div>
    </nav>
    <main class="o-main"><header class="o-top" id="top"></header><div class="o-page" id="page"></div></main></div>`;
  $('#signout').onclick = e => { e.preventDefault(); signOut(); };
  $('#themeT').onclick = e => {
    e.preventDefault(); const next = store.get('theme', 'light') === 'dark' ? 'light' : 'dark'; store.set('theme', next); window.munshiApplyTheme?.(next);
    e.currentTarget.lastChild.textContent = next === 'dark' ? 'Light theme' : 'Dark theme';
  };
}

export const view = {
  header(title, sub = '', actions = '') {
    $('#top').innerHTML = `<div><h1>${esc(title)}</h1>${sub ? `<p class="sub">${sub}</p>` : ''}</div><div class="grow"></div><div class="o-bar">${actions}</div>`;
  },
  get page() { return $('#page'); },
  get top() { return $('#top'); },
};

let chain = Promise.resolve();
function render() { chain = chain.then(_render, _render); return chain; }
async function _render() {
  $('#drawer').hidden = true; $('#drawer').innerHTML = '';
  if (!state.token) return signInView();
  if (!state.me || !state.me.permissions) {
    try { state.me = await api('/api/me'); store.set('me', state.me); }
    catch (e) { if (!state.token) return; root.innerHTML = `<div class="o-center"><p class="formerr">${esc(e.message)}</p></div>`; return; }
  }
  if (!OFFICE_ROLES.includes(state.me.role)) return gateView();
  shell();
  const [name, ...rest] = (location.hash.replace(/^#\/?/, '') || 'products').split('/');
  const screen = SCREENS[name];
  if (!screen) { location.hash = '#/products'; return; }
  $$('[data-nav]').forEach(a => { const on = a.dataset.nav === name; a.classList.toggle('active', on); on ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current'); });
  document.title = `${NAV.find(n => n[0] === name)[1]} · Munshi Office`;
  view.page.innerHTML = '<p class="muted">Loading…</p>'; view.header(NAV.find(n => n[0] === name)[1]);
  window.scrollTo(0, 0);
  try { await screen(view, rest.map(decodeURIComponent)); }
  catch (e) { view.page.innerHTML = `<p class="o-note crit">${esc(e.message)}</p>`; if (e.status !== 401) console.warn('office screen failed:', e.message); }
}
export const rerender = render;

window.addEventListener('hashchange', render);
window.addEventListener('unhandledrejection', e => { toast(e.reason?.message || 'Something went wrong', 5000); });
render();
