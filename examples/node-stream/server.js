'use strict';

/*
 * node-stream: a small Node app with no dependency, for trying appmux as an app admin and as a
 * visitor.
 *
 * It is a standard Databricks App: nothing in it knows about appmux, and the same folder runs on a
 * standalone App. package.json is what makes it a Node app; app.yaml starts it with
 * `node server.js`. It listens on the port in DATABRICKS_APP_PORT, which a standalone App and
 * appmux both set. It uses only Node's own `http` module: appmux v1 has no npm install step, so an
 * app that needs `node_modules` is not a v1 example (see ../README.md).
 *
 * What it shows
 *   The page at `/` and `GET /api/whoami` report who is calling (the identity headers the proxy or
 *   appmux forwards), which release is running (VERSION below and APP_GREETING from the
 *   environment), when this process started and how many streams it has served. What the Python
 *   examples do not cover is a response that stays open: `GET /events` sends one Server-Sent Event
 *   per second for STREAM_TICKS seconds and then ends. The page loads its script from `app.js` and
 *   opens `events` with relative URLs on purpose: appmux never rewrites a response body, so open
 *   the app as `/<slug>/` with the trailing slash.
 *
 * HTTP paths
 *   GET /            200 HTML
 *   GET /app.js      200 the page's script
 *   GET /events      200 text/event-stream: STREAM_TICKS `data: {"n", "of", "at"}` events, then
 *                    `event: done`; the stream also ends when the client goes away
 *   GET /api/whoami  200 JSON, see describe()
 *   GET /healthz     200 {"status": "ok"}; appmux's health check asks `/` by default
 *   other methods    405; other paths 404
 *
 * The access token is reported as present or absent and never echoed.
 */

const http = require('node:http');

const VERSION = '1.0.0';
const STARTED_AT = new Date();
const STREAM_TICKS = 15;
const STREAM_INTERVAL_MS = 1000;

// Headers the proxy sets and appmux forwards (design 7.1). appmux drops any client copy of them.
// Node lower-cases header names.
const EMAIL_HEADER = 'x-forwarded-email';
const USER_HEADER = 'x-forwarded-user';
const USERNAME_HEADER = 'x-forwarded-preferred-username';
const PREFIX_HEADER = 'x-forwarded-prefix';
const REQUEST_ID_HEADER = 'x-request-id';
const OBO_HEADER = 'x-forwarded-access-token';
const ANONYMOUS = 'anonymous';

const LISTEN_HOST = process.env.NODE_APP_HOST || '0.0.0.0';

let streamsStarted = 0;

function listenPort() {
  const rawPort = process.env.DATABRICKS_APP_PORT;
  const port = Number.parseInt(rawPort, 10);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    // Fail loudly at startup: a missing port means the platform contract was not met.
    throw new Error(`DATABRICKS_APP_PORT must be a port number, got: ${rawPort}`);
  }
  return port;
}

function greeting() {
  return process.env.APP_GREETING || '(APP_GREETING is not set)';
}

function escapeHtml(value) {
  const entities = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(value).replace(/[&<>"']/g, (character) => entities[character]);
}

function describe(request) {
  const headers = request.headers;
  return {
    app: 'node-stream',
    version: VERSION,
    greeting: greeting(),
    node: process.version,
    started_at: STARTED_AT.toISOString(),
    caller: {
      email: headers[EMAIL_HEADER] ?? null,
      user: headers[USER_HEADER] ?? null,
      preferred_username: headers[USERNAME_HEADER] ?? null,
    },
    // Present only for an OBO-delegated app and a visitor allowed to pass it (design 7.1).
    access_token_header_present: OBO_HEADER in headers,
    prefix: {
      x_forwarded_prefix: headers[PREFIX_HEADER] ?? null,
      appmux_base_path: process.env.APPMUX_BASE_PATH ?? null,
    },
    request_id: headers[REQUEST_ID_HEADER] ?? null,
    streams_started: streamsStarted,
  };
}

const PAGE_SCRIPT = `'use strict';
const output = document.getElementById('ticks');
const button = document.getElementById('start');
let source = null;

function stop(message) {
  if (source) source.close();
  source = null;
  output.textContent += message + '\\n';
  button.disabled = false;
}

button.addEventListener('click', () => {
  button.disabled = true;
  output.textContent = '';
  // Relative on purpose: it resolves under /<slug>/ when the page was opened with the slash.
  source = new EventSource('events');
  source.onmessage = (event) => {
    const tick = JSON.parse(event.data);
    output.textContent += 'tick ' + tick.n + '/' + tick.of + ' at ' + tick.at + '\\n';
  };
  source.addEventListener('done', () => stop('stream finished'));
  // EventSource would reconnect by itself; a lost stream is shown instead.
  source.onerror = () => stop('connection lost');
});
`;

function pageHtml(request) {
  const headers = request.headers;
  const who = headers[EMAIL_HEADER] || ANONYMOUS;
  const tokenState = OBO_HEADER in headers ? 'present' : 'absent';
  const prefix = headers[PREFIX_HEADER] || 'none';
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>node-stream</title>
<style>body{font:16px/1.5 system-ui;max-width:42rem;margin:2rem auto;padding:0 1rem}
code,pre{background:#eee;padding:0 .25em}pre{padding:.5em;min-height:3rem}</style></head>
<body>
<h1>node-stream <small>v${VERSION}</small></h1>
<p>${escapeHtml(greeting())}</p>
<p>You are <b>${escapeHtml(who)}</b>. The access-token header is ${tokenState}.</p>
<p>Process started ${STARTED_AT.toISOString()} on Node ${escapeHtml(process.version)}.
Served under prefix <code>${escapeHtml(prefix)}</code>. Streams started since then:
<b>${streamsStarted}</b>.</p>
<p><button id="start">Start a ${STREAM_TICKS} second stream</button></p>
<pre id="ticks"></pre>
<ul>
<li><a href="api/whoami">api/whoami</a></li>
<li><a href="events">events</a> (the raw stream)</li>
<li><a href="healthz">healthz</a></li>
</ul>
<script src="app.js"></script>
</body></html>`;
}

function sendBody(response, statusCode, contentType, body) {
  response.writeHead(statusCode, {
    'Content-Type': contentType,
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-cache',
  });
  response.end(body);
}

function sendJson(response, statusCode, body) {
  sendBody(response, statusCode, 'application/json', JSON.stringify(body));
}

function streamTicks(response) {
  streamsStarted += 1;
  response.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
  });
  // Send the headers now so the caller sees the stream open before the first tick.
  response.flushHeaders();

  let tick = 0;
  const timer = setInterval(() => {
    tick += 1;
    const event = { n: tick, of: STREAM_TICKS, at: new Date().toISOString() };
    response.write(`data: ${JSON.stringify(event)}\n\n`);
    if (tick >= STREAM_TICKS) {
      clearInterval(timer);
      response.end('event: done\ndata: {}\n\n');
    }
  }, STREAM_INTERVAL_MS);
  // The client left, or a proxy cut the stream: stop writing to it. `close` on the response, not
  // the request: the request is already fully read for a GET, so its `close` says nothing about
  // the connection.
  response.on('close', () => clearInterval(timer));
}

function handle(request, response) {
  if (request.method !== 'GET') {
    response.setHeader('Allow', 'GET');
    sendJson(response, 405, { error: 'method not allowed' });
    return;
  }
  const { pathname } = new URL(request.url, 'http://localhost');
  switch (pathname) {
    case '/':
      sendBody(response, 200, 'text/html; charset=utf-8', pageHtml(request));
      return;
    case '/app.js':
      sendBody(response, 200, 'text/javascript; charset=utf-8', PAGE_SCRIPT);
      return;
    case '/events':
      streamTicks(response);
      return;
    case '/api/whoami':
      sendJson(response, 200, describe(request));
      return;
    case '/healthz':
      sendJson(response, 200, { status: 'ok' });
      return;
    default:
      sendJson(response, 404, { error: 'not found' });
  }
}

const server = http.createServer(handle);

// appmux stops a child with SIGTERM. An open stream would keep close() waiting, so the connections
// are ended first; the process then exits with 0.
function shutdown() {
  server.close(() => process.exit(0));
  server.closeAllConnections();
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);

server.listen(listenPort(), LISTEN_HOST);
