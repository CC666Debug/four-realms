// Keeps the app's own files on the phone so it opens instantly, even with no signal.
// The page and the station list are fetched fresh when online (so updates show up right away);
// the saved copies are only used when the network fails. Streams are never cached.
const CACHE = 'fr-v7';   // bump when cached files change (keep the 'fr-' start)
const FILES = ['./', 'index.html', 'stations.json', 'manifest.webmanifest', 'favicon.png', 'icon-192.png', 'icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      // Only this app's old caches: the four sister apps share this web address.
      .then(keys => Promise.all(keys.filter(k => k.startsWith('fr-') && k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || new URL(req.url).origin !== location.origin) return;
  const path = new URL(req.url).pathname;

  // Network first for the page and the station list, falling back to the saved copy.
  if (req.mode === 'navigate' || path.endsWith('/stations.json')) {
    const key = req.mode === 'navigate' ? './' : 'stations.json';
    e.respondWith(
      fetch(req, { cache: 'no-cache' })
        .then(res => {
          if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(key, copy)); }
          return res;
        })
        .catch(() => caches.match(key))
    );
    return;
  }

  // Icons and the manifest: saved copy first.
  e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
});
