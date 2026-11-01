'use strict';

/*
 * Fixture node_app: a Node HTTP server with no dependency.
 *
 * A well-behaved Node child for the runtime-detection and start tests (design 5.4, spec US10).
 * package.json means the Node runtime; there is no app.yaml on purpose, so the default start
 * command `npm run start` (-> `node server.js`) is the one under test. The package has no
 * `dependencies`, so nothing is installed. Task T159 has it registered in `index` mode. Design
 * 5.3 requires that mode only for a Node app that has dependencies, because v1 ships no npm cache;
 * this app has none, so the build has nothing to fetch from the index.
 * It uses only Node's own `http` module and listens on the port in DATABRICKS_APP_PORT.
 *
 * HTTP paths
 *   GET /        200 {"fixture": "node_app", "status": "ok"}
 *   GET /report  200 {"fixture": "node_app", "node": "<process.version>", "port": <number>,
 *                     "base_path": "<APPMUX_BASE_PATH or null>",
 *                     "forwarded_prefix": "<X-Forwarded-Prefix of this request or null>"}
 *   anything else  404 {"fixture": "node_app", "error": "not found"}
 */

const http = require('node:http');

const LISTEN_HOST = process.env.NODE_APP_HOST || '0.0.0.0';

function listenPort() {
  const rawPort = process.env.DATABRICKS_APP_PORT;
  const port = Number.parseInt(rawPort, 10);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    // Fail loudly at startup: a missing port means the platform contract was not met.
    throw new Error(`DATABRICKS_APP_PORT must be a port number, got: ${rawPort}`);
  }
  return port;
}

function sendJson(response, statusCode, body) {
  const payload = JSON.stringify(body);
  response.writeHead(statusCode, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(payload),
  });
  response.end(payload);
}

const port = listenPort();

const server = http.createServer((request, response) => {
  const { pathname } = new URL(request.url, 'http://localhost');
  if (pathname === '/') {
    sendJson(response, 200, { fixture: 'node_app', status: 'ok' });
  } else if (pathname === '/report') {
    sendJson(response, 200, {
      fixture: 'node_app',
      node: process.version,
      port,
      base_path: process.env.APPMUX_BASE_PATH || null,
      forwarded_prefix: request.headers['x-forwarded-prefix'] || null,
    });
  } else {
    sendJson(response, 404, { fixture: 'node_app', error: 'not found' });
  }
});

server.listen(port, LISTEN_HOST, () => {
  process.stderr.write(`node_app listening on ${LISTEN_HOST}:${port}\n`);
});
