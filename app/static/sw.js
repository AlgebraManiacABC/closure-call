// Safety Call service worker: shows escalation alerts and opens the decision page when tapped.
self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "Safety Call", {
    body: data.body || "A recommendation needs your decision.",
    tag: data.tag || "closure-call",
    renotify: true,
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    data: { url: data.url || "/" }
  }));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = new URL(event.notification.data && event.notification.data.url || "/", self.location.origin).href;
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (wins) {
    for (var i = 0; i < wins.length; i++) {
      if ("focus" in wins[i]) { wins[i].navigate(url); return wins[i].focus(); }
    }
    return clients.openWindow(url);
  }));
});
