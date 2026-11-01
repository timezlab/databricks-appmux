const express = require("express");
const http = require("http");
const { WebSocketServer } = require("ws");
const app = express();
app.use("/static", express.static(__dirname + "/public"));
app.get("/api/ping", (req, res) => res.json({ ok: true }));
// relative links: works under any prefix
app.get("/", (req, res) => res.send('<!doctype html><body data-api="api/ping" data-ws="REL"><div id="out">loading</div><script src="static/app.js"></script></body>'));
// absolute links: the typical hard-coded case
app.get("/hard", (req, res) => res.send('<!doctype html><body data-api="/api/ping" data-ws=""><div id="out">loading</div><script src="/static/app.js"></script></body>'));
const server = http.createServer(app);
const wss = new WebSocketServer({ server, path: "/ws" });
wss.on("connection", (s) => s.on("message", (m) => s.send("echo:" + m)));
server.listen(Number(process.env.PORT), "127.0.0.1");
