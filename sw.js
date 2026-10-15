// Service worker mínimo: rede primeiro (app sempre atualizado), cache como reserva
const CACHE = "dc-v2";
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => self.clients.claim());
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    // pagina (HTML) sempre revalidada no servidor: evita o PC ficar preso numa versao antiga
    fetch(e.request.mode === "navigate" ? new Request(e.request.url, {cache: "no-cache"}) : e.request).then(r => {
      const cp = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, cp)).catch(()=>{});
      return r;
    }).catch(() => caches.match(e.request))
  );
});
