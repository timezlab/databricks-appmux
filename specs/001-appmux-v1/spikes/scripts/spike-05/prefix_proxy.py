"""Spike 5: stdlib prefix proxy. Serves /<slug>/x to the upstream as /x (or keeps the prefix when strip=False),
adds X-Forwarded-Prefix, counts requests that arrive outside /<slug>/ (they get 404 and are listed in `stray`).
Raw TCP relay so SSE and WebSocket upgrades stream through untouched."""
import socket
import threading


class PrefixProxy:
    def __init__(self, listen_port, upstream_port, slug="sales", strip=True):
        self.listen_port, self.upstream_port, self.slug, self.strip = listen_port, upstream_port, slug, strip
        self.prefix = "/" + slug
        self.stray = []
        self.forwarded = []
        self.upgrades = 0
        self.lock = threading.Lock()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", listen_port))
        self.srv.listen(64)
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.running = False
        try:
            self.srv.close()
        except OSError:
            pass

    def _accept(self):
        while self.running:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _pipe(src, dst):
        try:
            while True:
                d = src.recv(65536)
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _serve(self, c):
        try:
            while True:  # keep-alive loop on the client side; each request gets its own upstream connection
                buf = b""
                while b"\r\n\r\n" not in buf:
                    d = c.recv(65536)
                    if not d:
                        return
                    buf += d
                head, _, rest = buf.partition(b"\r\n\r\n")
                lines = head.decode("latin1").split("\r\n")
                method, target, version = lines[0].split(" ", 2)
                hdrs = [tuple(l.split(":", 1)) for l in lines[1:] if ":" in l]
                hmap = {k.strip().lower(): v.strip() for k, v in hdrs}
                path = target.split("?", 1)[0]
                if path != self.prefix and not path.startswith(self.prefix + "/"):
                    with self.lock:
                        self.stray.append(f"{method} {target}")
                    body = b"stray: outside prefix"
                    c.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: %d\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n%s" % (len(body), body))
                    return
                up_target = target if not self.strip else (target[len(self.prefix):] or "/")
                if up_target.startswith("?"):
                    up_target = "/" + up_target
                with self.lock:
                    self.forwarded.append(f"{method} {target} -> {up_target}")
                is_upgrade = hmap.get("upgrade", "").lower() == "websocket"
                clen = int(hmap.get("content-length", "0") or 0)
                body = rest
                while len(body) < clen:
                    body += c.recv(65536)
                extra, body = body[clen:], body[:clen]
                out = [f"{method} {up_target} {version}"]
                for k, v in hdrs:
                    if k.strip().lower() in ("connection", "proxy-connection", "keep-alive", "x-forwarded-prefix") and not is_upgrade:
                        continue
                    out.append(f"{k.strip()}: {v.strip()}")
                out.append(f"X-Forwarded-Prefix: {self.prefix}")
                if not is_upgrade:
                    out.append("Connection: close")
                up = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=30)
                up.settimeout(None)
                up.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin1") + body)
                if is_upgrade:
                    with self.lock:
                        self.upgrades += 1
                    t = threading.Thread(target=self._pipe, args=(c, up), daemon=True)
                    t.start()
                    self._pipe(up, c)
                    return
                while True:  # relay the response until upstream closes (Connection: close)
                    d = up.recv(65536)
                    if not d:
                        break
                    c.sendall(d)
                up.close()
                return  # one request per client connection keeps framing trivial
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass
