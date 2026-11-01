(async () => {
  const out = document.getElementById("out"); const parts = [];
  try { const r = await fetch(document.body.dataset.api); parts.push("api:" + (await r.json()).ok); } catch (e) { parts.push("api:fail"); }
  let wsurl = document.body.dataset.ws;
  if (wsurl === "REL") { const u = new URL("ws", location.href); u.protocol = u.protocol.replace("http", "ws"); wsurl = u.href; }
  if (wsurl) {
    await new Promise(res => { const s = new WebSocket(wsurl); s.onopen = () => s.send("hi"); s.onmessage = m => { parts.push("ws:" + m.data); res(); }; s.onerror = () => { parts.push("ws:fail"); res(); }; setTimeout(res, 5000); });
  }
  out.textContent = parts.join(" ");
})();
