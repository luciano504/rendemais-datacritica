// Service worker mínimo: rede primeiro (app sempre atualizado), cache como reserva
const CACHE = "dc-v1";
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => self.clients.claim());
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request).then(r => {
      const cp = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, cp)).catch(()=>{});
      return r;
    }).catch(() => caches.match(e.request))
  );
});
