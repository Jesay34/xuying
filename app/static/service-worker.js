const CACHE_NAME = "xuying-shell-alpha60";
const SHELL_ASSETS = [
  "/",
  "/setup",
  "/rebuild",
  "/immich",
  "/static/style.css",
  "/static/modern.css",
  "/static/theme.js",
  "/static/app.js",
  "/static/setup.js",
  "/static/rebuild.js",
  "/static/immich.js",
  "/static/icons/icon-xuying-light-192.png",
  "/static/icons/icon-xuying-dark-192.png",
  "/static/icons/icon-xuying-light-512.png",
  "/static/icons/icon-xuying-dark-512.png",
  "/static/icons/icon-xuying-maskable-512.png",
  "/static/icons/apple-touch-icon-light-v2.png",
  "/static/icons/apple-touch-icon-dark-v2.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key.startsWith("xuying-shell-") && key !== CACHE_NAME)
        .map((key) => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() => caches.match(event.request).then(
        (cached) => cached || caches.match("/")
      ))
    );
    return;
  }

  // JS and CSS must be network-first.  A cache-first script can leave a new
  // HTML form talking to an old endpoint after a NAS image upgrade.
  if (url.pathname.endsWith(".js") || url.pathname.endsWith(".css")) {
    event.respondWith(
      fetch(event.request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      }).catch(() => caches.match(event.request))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fresh = fetch(event.request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return response;
      });
      return cached || fresh;
    })
  );
});
