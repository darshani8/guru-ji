/*
 * Service worker for the Guru Ji assistant. It makes the page installable and
 * lets it open without a connection; it never touches the API.
 *
 * Only the page and its static files are handled, always network first so
 * a deploy is picked up on the next load. The cached copy is used only when
 * the network fails. API calls, sign-in, voice sockets and anything from
 * another origin pass straight through and are never stored, so no answer,
 * record or token ends up in the cache.
 */
'use strict';

const CACHE = 'guru-ji-shell-v1';
const SHELL = [
  '/',
  '/app.js',
  '/manifest.json',
  '/shared/styles.css',
  '/shared/auth.js',
  '/shared/pwa.js',
  '/icons/icon.svg',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

// The page is always stored under `/`, without its query: the sign-in
// callback arrives as `/?code=…`, and the code must never be a cache key.
const PAGE_KEY = '/';

async function networkFirst(request, key) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok && response.type === 'basic') await cache.put(key, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(key);
    if (cached) return cached;
    if (request.mode === 'navigate') {
      return new Response(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        + '<title>Guru Ji — Offline</title><p>Guru Ji needs an internet connection. Reconnect and reload.</p>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } },
      );
    }
    throw error;
  }
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === 'navigate' && (url.pathname === '/' || url.pathname === '/index.html')) {
    event.respondWith(networkFirst(request, PAGE_KEY));
    return;
  }
  if (SHELL.includes(url.pathname)) {
    event.respondWith(networkFirst(request, url.pathname));
  }
});
