/* ------------------------------------------------------------------
   Service worker for the Oregon rockhound map.

   Two jobs:
   1. Keep a copy of the app itself (page, code, data) so it opens
      with no signal.
   2. Answer map-tile requests from the tiles you saved in the app,
      instead of going to the internet.

   When you change any file in the app, bump SHELL_VERSION by one.
   That is what tells phones to pick up the new copy.
------------------------------------------------------------------ */

const SHELL_VERSION = 3;
const SHELL_CACHE = "oregon-shell-v" + SHELL_VERSION;
const TILE_CACHE  = "oregon-tiles-v1";  // must match TILE_CACHE in index.html

// Everything the app needs to start with no connection.
const SHELL_FILES = [
  "./",
  "index.html",
  "manifest.json",
  "vendor/maplibre-gl.js",
  "vendor/maplibre-gl.css",
  "icon-192.png",
  "icon-512.png",
  "data/claims.geojson",
  "data/milo.geojson"
];

// Requests to these hosts are map tiles.
const TILE_HOSTS = ["basemap.nationalmap.gov"];

// ---- Install: download the app shell ------------------------------
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      // Added one at a time so a single missing file (a data file you
      // haven't fetched yet) doesn't fail the whole install.
      Promise.all(
        SHELL_FILES.map((file) =>
          cache.add(file).catch(() => {
            console.warn("Skipped (not found):", file);
          })
        )
      )
    ).then(() => self.skipWaiting())
  );
});

// ---- Activate: throw away older shell versions --------------------
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((n) => n.startsWith("oregon-shell-") && n !== SHELL_CACHE)
          .map((n) => caches.delete(n))
      )
    ).then(() => self.clients.claim())
  );
});

// ---- Fetch: decide where each request gets answered from ----------
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Map tiles: saved copy first, network only as a fallback.
  if (TILE_HOSTS.includes(url.hostname)) {
    event.respondWith(
      caches.open(TILE_CACHE).then((cache) =>
        cache.match(req).then((hit) => {
          if (hit) return hit;
          return fetch(req).catch(
            () =>
              new Response("", { status: 504, statusText: "Tile not saved" })
          );
        })
      )
    );
    return;
  }

  // Everything else that belongs to this app: saved copy first,
  // but quietly refresh it in the background when there is signal.
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.open(SHELL_CACHE).then((cache) =>
        cache.match(req).then((hit) => {
          const fresh = fetch(req)
            .then((res) => {
              if (res && res.ok) cache.put(req, res.clone());
              return res;
            })
            .catch(() => hit);
          return hit || fresh;
        })
      )
    );
  }
});
