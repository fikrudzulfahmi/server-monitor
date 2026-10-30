// Service worker minimal: cukup agar aplikasi bisa dipasang (installable).
// Tidak menyimpan cache agar data monitoring selalu segar.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.mode === 'navigate') {
    e.respondWith(fetch(e.request).catch(() => new Response(
      '<h1>Offline</h1><p>Server tidak terjangkau.</p>',
      { headers: { 'Content-Type': 'text/html; charset=utf-8' } })));
  }
});
