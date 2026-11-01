"""Spike 13 variant A: reports which packages the platform installed and from where."""
import importlib
import importlib.metadata as md
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[sys.argv.index("--port") + 1])


def report():
    out = {"variant": "a-requirements-lines", "python": sys.version.split()[0], "executable": sys.executable}
    for dist, module in (("appmux-spike13-marker-q7k2x", "appmux_spike13_marker_q7k2x"), ("humanize", "humanize")):
        try:
            m = importlib.import_module(module)
            out[dist] = {"version": md.version(dist), "file": getattr(m, "__file__", None)}
        except Exception as e:  # recorded as evidence, not handled
            out[dist] = f"{type(e).__name__}: {e}"
    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(report()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


print("SPIKE13", json.dumps(report()), flush=True)
HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
