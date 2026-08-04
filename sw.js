// Cache-first for the shell so the app opens instantly and works with no
// network. The book itself lives in localStorage, never in this cache.
//
// Two faults this is written against, both found by audit rather than by use:
//
//   * `addAll` is all-or-nothing, so one asset failing to fetch meant nothing
//     at all was stored and the app would not open offline — while the install
//     reported success.
//   * a navigation to `?v=123` did not match the cached `./index.html`, because
//     `caches.match` compares the query string too. Opening the app offline
//     from a link carrying anything after the `?` simply failed.
const CACHE = "milkbook-v2";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon.svg", "./icon-180.png"];

async function ensureShell() {
  let cache;
  try { cache = await caches.open(CACHE); } catch (e) { return 0; }
  let have = 0;
  await Promise.all(SHELL.map(async (url) => {
    try {
      if (await cache.match(url)) { have++; return; }
      const res = await fetch(url, { cache: "reload" });
      if (!res || !res.ok) return;
      await cache.put(url, res);
      have++;
    } catch (e) { /* this one asset is simply not available yet */ }
  }));
  return have;
}

self.addEventListener("install", (e) => {
  e.waitUntil(ensureShell().then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(ensureShell)
      .then(() => self.clients.claim())
  );
});

const isNavigation = (req) =>
  req.mode === "navigate" || req.destination === "document" ||
  (req.headers.get("accept") || "").includes("text/html");

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;

  if (isNavigation(req)) {
    // Network first, so a corrected version of the page reaches an installed
    // copy; the cached shell is the fallback, matched without the query string
    // so `?v=` or a shared link still opens with no signal.
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put("./index.html", copy)).catch(() => {});
            e.waitUntil(ensureShell());
          }
          return res;
        })
        .catch(() => caches.match(req, { ignoreSearch: true })
          .then((hit) => hit || caches.match("./index.html")))
    );
    return;
  }

  e.respondWith(
    caches.match(req, { ignoreSearch: true }).then((hit) => {
      const live = fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          }
          return res;
        })
        .catch(() => hit);
      return hit || live;
    })
  );
});
