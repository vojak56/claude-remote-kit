// Service worker of the web monitor: shows "your turn" push notifications and opens the monitor on tap.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: 'Claude monitor', body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || 'Claude monitor', {
    body: d.body || '',
    tag: d.tag || undefined,
    renotify: Boolean(d.tag),
    icon: 'icon-192.png',
    badge: 'icon-192.png',
    data: { url: d.url || './' },
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of windows) { if ('focus' in w) return w.focus(); }
    return self.clients.openWindow(e.notification.data.url || './');
  })());
});
