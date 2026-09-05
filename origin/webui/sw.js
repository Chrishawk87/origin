/* Origin Compliance — minimal service worker.
 * Purpose: make the site installable as a phone/desktop app (PWA). It is
 * deliberately conservative — it NEVER caches HTML pages or API responses, so
 * an installed app always shows live data and never a stale login/screen.
 * Only the static app icons + manifest are cached (they rarely change). */
const CACHE = 'origin-v1';
const ASSETS = [
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable-512.png',
  '/apple-touch-icon.png',
  '/manifest.webmanifest'
];

self.addEventListener('install', function (e) {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(function (c) {
    return c.addAll(ASSETS).catch(function () {});
  }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil((async function () {
    const keys = await caches.keys();
    await Promise.all(keys.filter(function (k) { return k !== CACHE; })
                          .map(function (k) { return caches.delete(k); }));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', function (e) {
  const req = e.request;
  if (req.method !== 'GET') return;
  let url;
  try { url = new URL(req.url); } catch (_) { return; }
  // Always go to the network for pages and any API call — never serve stale.
  if (req.mode === 'navigate') return;
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/portal/api')) return;
  // Cache-first only for our own static icons/manifest.
  if (ASSETS.indexOf(url.pathname) !== -1) {
    e.respondWith(caches.match(req).then(function (r) { return r || fetch(req); }));
  }
});
