"""Throwaway spike harness (spikes 8, 17 and the remote runner for 2, 6, 7, 10, 18).

Not part of appmux. Stdlib only. Endpoints:
  /echo/...        reflects what the platform proxy sent (secret-bearing header values are redacted to a length)
  /sse, /ws, /sleep, /gen, /sink, /cookie, /hdr   proxy behaviour probes (spike 8)
  /x/run, /x/sh, /x/put, /x/get   remote runner, restricted to the owner's e-mail header
"""
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
OWNER = os.environ.get("SPIKE_OWNER", "")
WORK = "/tmp/spike"
os.makedirs(WORK, exist_ok=True)
SECRET_HEADERS = {"authorization", "cookie", "x-forwarded-access-token", "x-csrf-token"}
STARTED = time.time()


def redact(name, value):
    if name.lower() in SECRET_HEADERS:
        return f"<redacted len={len(value)} prefix={value[:6]!r}>" if name.lower() != "cookie" else f"<cookie names={[c.split('=')[0].strip() for c in value.split(';')]}>"
    return value


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "spike"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---- helpers
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def send_json(self, obj, code=200, extra=None):
        data = json.dumps(obj, indent=1, sort_keys=True).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def owner_ok(self):
        email = self.headers.get("X-Forwarded-Email", "")
        return bool(OWNER) and email == OWNER

    # ---- dispatch
    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def do_OPTIONS(self):
        self.route()

    def route(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p.startswith("/x/"):
            return self.runner(p[3:], q)
        if p.startswith("/echo"):
            return self.echo(u, q)
        if p == "/sse":
            return self.sse(q)
        if p == "/ws":
            return self.ws()
        if p == "/sleep":
            time.sleep(float(q.get("s", "1")))
            return self.send_json({"slept": q.get("s", "1")})
        if p == "/gen":
            return self.gen(q)
        if p == "/sink":
            n = 0
            left = int(self.headers.get("Content-Length") or 0)
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                n += len(chunk)
                left -= len(chunk)
            return self.send_json({"received": n})
        if p == "/cookie":
            return self.send_json({"ok": 1}, extra=[
                ("Set-Cookie", "a=1; Path=/; Domain=example.com; HttpOnly"),
                ("Set-Cookie", "b=2; Path=/deep/path; SameSite=Lax"),
                ("Set-Cookie", "__Host-c=3; Path=/; Secure"),
            ])
        if p == "/hdr":
            n = int(q.get("bytes", "1000"))
            return self.send_json({"ok": 1}, extra=[("X-Big", "a" * n)])
        if p == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.send_json({"hello": "spike harness", "uptime_s": round(time.time() - STARTED), "path": self.path})

    # ---- spike 8 probes
    def echo(self, u, q):
        body = self.body()
        hdrs = [[k, redact(k, v)] for k, v in self.headers.items()]
        self.send_json({
            "method": self.command, "raw_path": self.path, "version": self.request_version,
            "client": self.client_address[0], "headers": hdrs, "body_len": len(body),
            "server_time": time.time(),
        })

    def sse(self, q):
        n, delay = int(q.get("n", "5")), float(q.get("delay", "1"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        t0 = time.time()
        for i in range(n):
            msg = f"data: {i} t={time.time() - t0:.2f}\n\n".encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(msg), msg))
            self.wfile.flush()
            time.sleep(delay)
        self.wfile.write(b"0\r\n\r\n")
        self.close_connection = True

    def gen(self, q):
        mb = int(q.get("mb", "1"))
        total = mb * 1024 * 1024
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(total))
        self.end_headers()
        block = b"x" * 65536
        sent = 0
        while sent < total:
            part = block[: min(65536, total - sent)]
            self.wfile.write(part)
            sent += len(part)

    def ws(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            return self.send_json({"error": "not a websocket upgrade", "upgrade": self.headers.get("Upgrade"), "connection": self.headers.get("Connection")}, 400)
        acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.wfile.flush()
        sock = self.connection
        sock.settimeout(400)
        try:
            while True:
                hdr = self._recv(sock, 2)
                b1, b2 = hdr[0], hdr[1]
                op, ln = b1 & 0x0F, b2 & 0x7F
                if ln == 126:
                    ln = struct.unpack(">H", self._recv(sock, 2))[0]
                elif ln == 127:
                    ln = struct.unpack(">Q", self._recv(sock, 8))[0]
                mask = self._recv(sock, 4) if b2 & 0x80 else b""
                data = self._recv(sock, ln) if ln else b""
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if op == 8:
                    sock.sendall(b"\x88\x00")
                    break
                if op == 9:
                    sock.sendall(b"\x8a" + bytes([len(data)]) + data)
                    continue
                if data == b"sleep5":
                    time.sleep(5)
                reply = b"echo:" + data
                if len(reply) < 126:
                    sock.sendall(b"\x81" + bytes([len(reply)]) + reply)
                else:
                    sock.sendall(b"\x81\x7e" + struct.pack(">H", len(reply)) + reply)
        except Exception as e:  # connection ended by the peer or by a platform timeout
            sys.stderr.write(f"ws ended: {type(e).__name__} {e}\n")
        self.close_connection = True

    @staticmethod
    def _recv(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    # ---- remote runner (owner only)
    def runner(self, what, q):
        if not self.owner_ok():
            return self.send_json({"error": "forbidden", "saw_email": bool(self.headers.get("X-Forwarded-Email"))}, 403)
        body = self.body()
        timeout = float(q.get("timeout", "60"))
        if what in ("run", "sh"):
            path = os.path.join(WORK, f"job-{int(time.time() * 1000)}." + ("py" if what == "run" else "sh"))
            with open(path, "wb") as fh:
                fh.write(body)
            cmd = ["python3", path] if what == "run" else ["bash", path]
            # spikes 1, 11, 12, 16: the job gets the caller's on-behalf-of token in OBO_TOKEN; it is scrubbed from the output
            obo = self.headers.get("X-Forwarded-Access-Token", "")
            env = dict(os.environ, OBO_TOKEN=obo)

            def scrub(raw):
                text = raw.decode("utf-8", "replace")
                return text.replace(obo, "<obo-token>") if obo else text
            t0 = time.time()
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=WORK, env=env)
                out = {"rc": r.returncode, "stdout": scrub(r.stdout), "stderr": scrub(r.stderr)}
            except subprocess.TimeoutExpired as e:
                out = {"rc": "timeout", "stdout": scrub(e.stdout or b""), "stderr": scrub(e.stderr or b"")}
            out["secs"] = round(time.time() - t0, 2)
            return self.send_json(out)
        if what == "put":
            dest = q["path"]
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(body)
            return self.send_json({"wrote": dest, "bytes": len(body)})
        if what == "get":
            with open(q["path"], "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return self.send_json({"error": "unknown"}, 404)


class S(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


if __name__ == "__main__":
    print(f"spike harness on :{PORT}, owner set={bool(OWNER)}", flush=True)
    S(("0.0.0.0", PORT), H).serve_forever()
