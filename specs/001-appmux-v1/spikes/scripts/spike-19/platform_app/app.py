"""Spike 19, runs as the Free Edition app `appmux-spike-19`: serve a Next.js static export under /_appmux/ with the hash-based CSP
the local half found necessary, and report what the platform did to it.

The export directory is `out/` when the platform's build produced one, else `export/` (shipped prebuilt). The CSP is computed per
HTML file, as served, from the SHA-256 of each executable inline script.
  GET /_appmux/...         the export (HTML gets the Content-Security-Policy header)
  GET /_appmux/api/status  {"state": "ok", "counter": n}, the endpoint the page polls
  GET /_appmux/_info       which directory is served, whether node_modules/ exists, and the NAMES (never values) of request headers
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_PATH = "/_appmux"
HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
CSP_COMMON = "default-src 'none'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
INLINE_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S)
EXPORT_DIR = (HERE / "out") if (HERE / "out" / "index.html").is_file() else (HERE / "export")
counter = {"n": 0}


def script_policy(html: str) -> str:
    sources = ["'self'"]
    for match in INLINE_SCRIPT.finditer(html):
        attrs = match.group("attrs")
        if "src=" in attrs or 'type="application/json"' in attrs:
            continue  # external scripts are covered by 'self'; JSON blocks are not executed
        digest = hashlib.sha256(match.group("body").encode()).digest()
        sources.append("'sha256-" + base64.b64encode(digest).decode() + "'")
    return CSP_COMMON + "; script-src " + " ".join(sources)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # http.server dispatches on this name
        path = self.path.split("?")[0]
        if path == BASE_PATH + "/api/status":
            counter["n"] += 1
            self._send(200, "application/json", json.dumps({"state": "ok", "counter": counter["n"]}).encode())
        elif path == BASE_PATH + "/_info":
            info = {
                "export_dir": EXPORT_DIR.name,
                "node_modules_present": (HERE / "node_modules").is_dir(),
                "out_present": (HERE / "out").is_dir(),
                "request_header_names": sorted(self.headers.keys()),
            }
            self._send(200, "application/json", json.dumps(info).encode())
        elif path.startswith(BASE_PATH + "/"):
            self._serve_file(path[len(BASE_PATH) + 1:] or "index.html")
        else:
            self._send(404, "text/plain", b"not found")

    def _serve_file(self, relative: str) -> None:
        target = (EXPORT_DIR / relative).resolve()
        if target.is_dir():
            target = target / "index.html"
        if not target.is_relative_to(EXPORT_DIR.resolve()) or not target.is_file():
            self._send(404, "text/plain", b"not found")
            return
        body = target.read_bytes()
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, kind, body, script_policy(body.decode()) if kind == "text/html" else None)

    def _send(self, status: int, kind: str, body: bytes, policy: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if policy:
            self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass


if __name__ == "__main__":
    print(f"SPIKE19 serving {EXPORT_DIR.name}/ on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
