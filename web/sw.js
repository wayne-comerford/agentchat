// agentchat service worker — minimal offline shell + cache-first for static assets.
// API responses are NEVER cached (we want live data when online, explicit error when offline).

const CACHE = "agentchat-v2";
const SHELL = ["/", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  // Don't cache API or SSE — those must be live.
  if (url.pathname.startsWith("/v1/")) return;
  if (e.request.method !== "GET") return;

  // Cache-first for the shell, network fallback.
  e.respondWith(
    caches.match(e.request).then((cached) => {
      if (cached) return cached;
      return fetch(e.request).then((res) => {
        // Only cache successful basic responses for same-origin static assets
        if (res.ok && res.status === 200 && url.origin === self.location.origin) {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone)).catch(() => {});
        }
        return res;
      }).catch(() => {
        // Offline fallback: return cached root or a minimal offline page
        return caches.match("/") || new Response("Offline", { status: 503 });
      });
    })
  );
});
