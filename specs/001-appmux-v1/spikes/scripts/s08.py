"""Spike 8 client: Databricks Apps proxy behaviour against the spike harness.
Token is fetched inside this process from the CLI and never printed."""
import base64
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time

HOST = os.environ.get("SPIKE_HOST", "appmux-probe-<workspace-id>.aws.databricksapps.com")
OUT = {}


def token():
    r = subprocess.run(["databricks", "auth", "token", "-o", "json"], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["access_token"]


def req(method, path, headers=None, body=None, timeout=120, raw_path=False):
    h = {"Authorization": "Bearer " + token()}
    h.update(headers or {})
    c = http.client.HTTPSConnection(HOST, timeout=timeout)
    t0 = time.time()
    try:
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        return {"status": r.status, "secs": round(time.time() - t0, 2), "len": len(data), "headers": r.getheaders(), "body": data}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "secs": round(time.time() - t0, 2)}
    finally:
        c.close()


def lower(r):
    return {k.lower(): v for k, v in r.get("headers", [])}


def short(r, show_body=False):
    d = {k: v for k, v in r.items() if k not in ("headers", "body")}
    if show_body and "body" in r:
        d["body"] = r["body"][:300].decode("utf-8", "replace")
    return d


def echo_headers(r):
    try:
        d = json.loads(r["body"])
        return {"raw_path": d["raw_path"], "headers": {k: v for k, v in d["headers"]}}
    except Exception:
        return None


def t_spoof():
    r = req("GET", "/echo/spoof", {
        "X-Forwarded-Email": "evil@example.com", "X-Forwarded-User": "evil", "X-Forwarded-Access-Token": "AAAAAA-fake",
        "X-Forwarded-For": "6.6.6.6", "X-Real-Ip": "6.6.6.6", "X-Forwarded-Host": "evil.example", "Cookie": "foo=bar; baz=1",
        "X-Databricks-Org-Id": "1"})
    e = echo_headers(r)
    h = e["headers"]
    keep = ["X-Forwarded-Email", "X-Forwarded-User", "X-Forwarded-Access-Token", "X-Forwarded-For", "X-Real-Ip", "X-Forwarded-Host", "Cookie", "X-Databricks-Org-Id", "Authorization"]
    OUT["spoof"] = {k: h.get(k, "<absent>") for k in keep}
    OUT["spoof"]["status"] = r["status"]


def t_paths():
    res = {}
    cases = {
        "plain_sub": "/echo/a/b",
        "dotdot": "/echo/a/b/../c",
        "double_slash": "//echo/x",
        "mid_double_slash": "/echo//x",
        "encoded_dotdot": "/echo/%2e%2e/echo/x",
        "encoded_slash": "/echo/a%2Fb",
        "encoded_space_query": "/echo/a%20b?q=%20x&r=1&r=2",
        "trailing": "/echo/",
        "semicolon": "/echo/a;b=1",
        "unicode": "/echo/%C3%A9",
        "fragment_free_long_query": "/echo/q?" + "a=" + "x" * 4000,
    }
    for name, p in cases.items():
        r = req("GET", p)
        res[name] = {"status": r.get("status"), "location": lower(r).get("location"), "sent": p[:60], "app_saw": (echo_headers(r) or {}).get("raw_path", (r.get("body") or b"")[:120].decode("utf-8", "replace") if "body" in r else r.get("error"))}
        if isinstance(res[name]["app_saw"], str):
            res[name]["app_saw"] = res[name]["app_saw"][:120]
    OUT["paths"] = res


def t_methods_and_bodies():
    res = {}
    for m in ("POST", "PUT", "DELETE", "OPTIONS"):
        r = req(m, "/echo/m", {"Content-Type": "application/json"}, body=b"{}" if m != "OPTIONS" else None)
        res[m] = r.get("status", r.get("error"))
    OUT["methods"] = res


def t_sizes():
    res = {}
    for mb in (1, 10, 100):
        r = req("GET", f"/gen?mb={mb}", timeout=300)
        res[f"resp_{mb}MB"] = short(r)
    for mb in (1, 10, 100):
        body = b"y" * (mb * 1024 * 1024)
        r = req("POST", "/sink", {"Content-Type": "application/octet-stream"}, body=body, timeout=300)
        sh = short(r, True)
        res[f"req_{mb}MB"] = sh
    OUT["sizes"] = res


def t_headers():
    res = {}
    for kb in (4, 8, 16, 32, 64):
        r = req("GET", f"/hdr?bytes={kb * 1024}")
        res[f"resp_hdr_{kb}KB"] = {"status": r.get("status", r.get("error")), "x_big_len": len(lower(r).get("x-big", ""))}
    for kb in (4, 8, 16, 32, 64):
        r = req("GET", "/echo/big", {"X-Pad": "p" * (kb * 1024)})
        res[f"req_hdr_{kb}KB"] = {"status": r.get("status", r.get("error"))}
    OUT["headers"] = res


def t_cookie():
    r = req("GET", "/cookie")
    OUT["set_cookie"] = {"status": r["status"], "set_cookie_seen_by_client": [v for k, v in r["headers"] if k.lower() == "set-cookie"]}
    r = req("GET", "/redirect")
    OUT["redirect"] = {"status": r["status"], "location": lower(r).get("location")}


def t_sse():
    tk = token()
    c = http.client.HTTPSConnection(HOST, timeout=60)
    t0 = time.time()
    c.request("GET", "/sse?n=6&delay=1", headers={"Authorization": "Bearer " + tk})
    r = c.getresponse()
    arrivals = []
    while True:
        line = r.readline()
        if not line:
            break
        if line.startswith(b"data:"):
            arrivals.append(round(time.time() - t0, 2))
    OUT["sse"] = {"status": r.status, "arrival_secs": arrivals, "transfer_encoding": r.getheader("Transfer-Encoding"), "streamed": len(arrivals) >= 2 and arrivals[-1] - arrivals[0] > 3}
    c.close()


def t_idle(sec):
    r = req("GET", f"/sleep?s={sec}", timeout=sec + 120)
    OUT.setdefault("idle", {})[str(sec)] = {k: v for k, v in short(r, True).items()}


# -- websocket client
def ws_connect(path="/ws"):
    ctx = ssl.create_default_context()
    raw = socket.create_connection((HOST, 443), timeout=30)
    s = ctx.wrap_socket(raw, server_hostname=HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nAuthorization: Bearer {token()}\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    return s, head.decode("latin1").split("\r\n")[0], rest


def ws_send(s, payload):
    mask = os.urandom(4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    hdr = b"\x81" + (bytes([0x80 | n]) if n < 126 else b"\xfe" + n.to_bytes(2, "big")) + mask
    s.sendall(hdr + data)


def ws_recv(s, timeout):
    s.settimeout(timeout)
    h = s.recv(2)
    if len(h) < 2:
        return None
    ln = h[1] & 0x7F
    if ln == 126:
        ln = int.from_bytes(s.recv(2), "big")
    data = b""
    while len(data) < ln:
        data += s.recv(ln - len(data))
    return h[0] & 0x0F, data


def t_ws(idle_secs=(30, 120)):
    res = {}
    s, status, rest = ws_connect()
    res["handshake"] = status
    if "101" in status:
        ws_send(s, b"hello")
        res["echo"] = str(ws_recv(s, 20))
        for idle in idle_secs:
            time.sleep(idle)
            try:
                ws_send(s, b"after-idle-%d" % idle)
                res[f"after_idle_{idle}s"] = str(ws_recv(s, 20))
            except Exception as e:
                res[f"after_idle_{idle}s"] = f"{type(e).__name__}: {e}"
                break
        try:
            ws_send(s, b"x" * 100000 if False else b"x" * 60000)
            res["frame_60KB"] = str(ws_recv(s, 20))[:60]
        except Exception as e:
            res["frame_60KB"] = f"{type(e).__name__}: {e}"
    s.close()
    OUT["websocket"] = res


def t_sse_long():
    tk = token()
    c = http.client.HTTPSConnection(HOST, timeout=120)
    t0 = time.time()
    c.request("GET", "/sse?n=36&delay=10", headers={"Authorization": "Bearer " + tk})
    r = c.getresponse()
    arrivals, err = [], None
    try:
        while True:
            line = r.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                arrivals.append(round(time.time() - t0))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    OUT["sse_360s_every_10s"] = {"status": r.status, "events": len(arrivals), "last_event_at_s": arrivals[-1] if arrivals else None, "ended_at_s": round(time.time() - t0), "error": err}
    c.close()


def t_noauth():
    c = http.client.HTTPSConnection(HOST, timeout=30)
    c.request("GET", "/echo/x")
    r = c.getresponse()
    r.read()
    OUT["no_auth"] = {"status": r.status, "has_location": bool(r.getheader("Location"))}
    c = http.client.HTTPSConnection(HOST, timeout=30)
    c.request("GET", "/echo/x", headers={"Authorization": "Bearer not-a-token"})
    r = c.getresponse()
    OUT["bad_bearer"] = {"status": r.status, "body": r.read()[:150].decode("utf-8", "replace")}
    c = http.client.HTTPSConnection(HOST, timeout=30)
    c.request("GET", "/x/sh", headers={"Authorization": "Bearer not-a-token", "X-Forwarded-Email": "<owner-email>"})
    r = c.getresponse()
    OUT["spoof_owner_without_auth"] = {"status": r.status, "has_location": bool(r.getheader("Location"))}


def t_x_spoof():
    r = req("POST", "/x/sh", {"X-Forwarded-Email": "evil@example.com"}, body=b"echo hi")
    OUT["exec_gate_with_spoofed_email"] = {"status": r.get("status"), "body": r.get("body", b"")[:120].decode("utf-8", "replace")}
    r = req("POST", "/x/sh", None, body=b"echo hi")
    OUT["exec_gate_real_email"] = {"status": r.get("status"), "body": r.get("body", b"")[:160].decode("utf-8", "replace")}


TESTS = {"spoof": t_spoof, "paths": t_paths, "methods": t_methods_and_bodies, "sizes": t_sizes, "headers": t_headers,
         "cookie": t_cookie, "sse": t_sse, "noauth": t_noauth, "xspoof": t_x_spoof,
         "idle30": lambda: t_idle(30), "idle60": lambda: t_idle(60), "idle120": lambda: t_idle(120), "idle300": lambda: t_idle(300),
         "ws": lambda: t_ws((30, 120)), "ssel": t_sse_long, "ws330": lambda: t_ws((330,)), "wsquick": lambda: t_ws(())}

if __name__ == "__main__":
    names = sys.argv[1:] or ["spoof", "paths", "methods", "headers", "cookie", "sse", "noauth", "xspoof"]
    threads = []
    for n in names:
        def run(n=n):
            try:
                TESTS[n]()
            except Exception as e:
                OUT[n] = f"TEST ERROR {type(e).__name__}: {e}"
        t = threading.Thread(target=run)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    print(json.dumps(OUT, indent=1, sort_keys=True, default=str))
