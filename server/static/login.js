"use strict";
// Ключ или код приглашения приходит после # — браузер не отправляет эту часть ни серверу, ни тем,
// кто строит превью ссылок. Читаем её, сразу стираем из адресной строки и меняем на сессию.
// #k= — постоянная личная ссылка, #i= — одноразовая ссылка-приглашение.
(() => {
  const msg = document.getElementById("msg");
  const m = /(?:^#|&)([ki])=([A-Za-z0-9_-]{20,200})/.exec(location.hash);
  history.replaceState(null, "", "/login");
  if (!m) {
    msg.textContent = "В ссылке нет ключа. Откройте ссылку для входа целиком, вместе с частью после #.";
    return;
  }
  fetch("/api/login", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(m[1] === "i" ? {invite: m[2]} : {key: m[2]}),
    credentials: "same-origin",
    cache: "no-store"
  })
    .then(r => r.json().then(j => ({ok: r.ok, j}), () => ({ok: r.ok, j: {}})))
    .then(({ok, j}) => {
      if (ok) location.replace("/");
      else msg.textContent = (j.error || "Не получилось войти.") + (m[1] === "i" ? " Попросите новую ссылку у того, кто её выдал." : "");
    })
    .catch(() => { msg.textContent = "Нет связи с Пультом. Откройте ссылку ещё раз."; });
})();
