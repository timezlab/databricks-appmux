"""Spike 19, local: serve a Next.js static export under a base path from the standard library with a strict
Content-Security-Policy, load it in headless Chromium, and record every CSP violation and whether the page rendered and polled.

usage: check_csp.py <work-dir> <chromium-headless-shell>
  <work-dir> is the directory build.sh produced; each of app-router/out and pages-router/out is tried under three policies:
    open     no Content-Security-Policy header (control)
    strict   script-src 'self'
    hashes   script-src 'self' plus the SHA-256 of every inline <script> of the page being served, computed from the file when served
Output: one JSON line per combination. Needs `playwright` for Python.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_PATH = "/_appmux"
CSP_COMMON = "default-src 'none'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
INLINE_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.S)
MIN_POLLS = 4
WAIT_S = 10


def inline_script_hashes(html: str) -> list[str]:
    """CSP source expressions for the inline scripts the browser would execute (JSON data blocks are not executed)."""
    sources = []
    for match in INLINE_SCRIPT.finditer(html):
        attrs = match.group("attrs")
        if "src=" in attrs or 'type="application/json"' in attrs:
            continue
        digest = hashlib.sha256(match.group("body").encode()).digest()
        sources.append("'sha256-" + base64.b64encode(digest).decode() + "'")
    return sources


def make_handler(export_dir: Path, mode: str):
    counter = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server dispatches on this name
            path = self.path.split("?")[0]
            if path == BASE_PATH + "/api/status":
                counter["n"] += 1
                self._send(200, "application/json", json.dumps({"state": "ok", "counter": counter["n"]}).encode(), None)
                return
            if not path.startswith(BASE_PATH + "/"):
                self._send(404, "text/plain", b"not found", None)
                return
            relative = path[len(BASE_PATH) + 1:] or "index.html"
            target = (export_dir / relative).resolve()
            if target.is_dir():
                target = target / "index.html"
            if not target.is_relative_to(export_dir.resolve()) or not target.is_file():
                self._send(404, "text/plain", b"not found", None)
                return
            body = target.read_bytes()
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            policy = None
            if kind == "text/html" and mode != "open":
                scripts = ["'self'"] + (inline_script_hashes(body.decode()) if mode == "hashes" else [])
                policy = CSP_COMMON + "; script-src " + " ".join(scripts)
            self._send(200, kind, body, policy)

        def _send(self, status: int, kind: str, body: bytes, policy: str | None) -> None:
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

    return Handler


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def trial(browser, export_dir: Path, mode: str) -> dict:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(export_dir, mode))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page = browser.new_context().new_page()
    page.add_init_script(
        "window.__csp = []; document.addEventListener('securitypolicyviolation', e => window.__csp.push("
        "{directive: e.violatedDirective, blocked: String(e.blockedURI).slice(0, 60), sample: (e.sample || '').slice(0, 50)}));"
    )
    failures = []
    page.on("requestfailed", lambda r: failures.append(r.url.replace(f"http://127.0.0.1:{port}", "")[:80]))
    result = {"policy": mode}
    try:
        page.goto(f"http://127.0.0.1:{port}{BASE_PATH}/", timeout=20000)
        deadline = time.time() + WAIT_S
        polls = 0
        while time.time() < deadline:
            text = page.inner_text("#polls") if page.query_selector("#polls") else "0"
            polls = int(text) if text.isdigit() else 0
            if polls >= MIN_POLLS:
                break
            time.sleep(0.25)
        result.update(rendered=page.query_selector("#status") is not None, polls_seen=polls, status_text=page.inner_text("#status") if page.query_selector("#status") else None)
    except Exception as exc:  # the failure of the page is the measurement
        result.update(rendered=False, error=f"{type(exc).__name__}: {str(exc)[:120]}")
    violations = page.evaluate("window.__csp")
    result["csp_violations"] = len(violations)
    result["violation_examples"] = violations[:3]
    result["failed_requests"] = failures[:3]
    server.shutdown()
    return result


if __name__ == "__main__":
    work = Path(sys.argv[1])
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=sys.argv[2], headless=True, args=["--no-sandbox"])
        for router in ("app-router", "pages-router"):
            for mode in ("open", "strict", "hashes"):
                print(json.dumps({"router": router, **trial(browser, work / router / "out", mode)}), flush=True)
        browser.close()
