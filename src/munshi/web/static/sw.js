// Munshi service worker: app shell cached for instant, offline-capable opens;
// API calls go to the network and fall back to a clear offline message.
const SHELL = 'munshi-shell-v4';
const ASSETS = ['/', '/static/index.html', '/static/styles.css', '/static/theme.js', '/static/i18n.js', '/static/core.js', '/static/views.js', '/static/icon.svg', '/manifest.webmanifest'];
self.addEventListener('install', e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) {           // fonts: cache after first successful load
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request).then(res => { if (res.ok) { const copy = res.clone(); caches.open(SHELL).then(c => c.put(e.request, copy)); } return res; }).catch(() => hit)));
    return;
  }
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/i/')) {
    e.respondWith(fetch(e.request).catch(() => new Response(JSON.stringify({ detail: 'offline' }), { status: 503, headers: { 'Content-Type': 'application/json' } })));
    return;
  }
  // shell: network first (so updates land), cache fallback (so it opens offline)
  e.respondWith(fetch(e.request).then(res => { if (e.request.method === 'GET' && res.ok) { const copy = res.clone(); caches.open(SHELL).then(c => c.put(e.request, copy)); } return res; })
    .catch(() => caches.match(e.request, { ignoreSearch: true }).then(hit => hit || caches.match('/'))));
});
