"""Spike 17 probe: logs argv and the environment *names* the platform delivers (values only for benign names)."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

BENIGN = {"DATABRICKS_APP_PORT", "DATABRICKS_APP_NAME", "DATABRICKS_HOST", "DATABRICKS_WORKSPACE_ID",
          "PORT", "HOME", "USER", "PWD", "LANG", "TZ", "PYTHONUNBUFFERED", "SPIKE_VARIANT", "MISSING_RES", "LITERAL_ENV"}
env = {}
for k, v in sorted(os.environ.items()):
    env[k] = v if k in BENIGN else f"<{len(v)} chars>"
port_arg = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else None
print("SPIKE17", json.dumps({"variant": os.environ.get("SPIKE_VARIANT"), "argv": sys.argv, "port_arg": port_arg, "env": env}), flush=True)
port = int(port_arg) if port_arg and port_arg.isdigit() else int(os.environ["DATABRICKS_APP_PORT"])
print("SPIKE17 listening on", port, "(port_arg_numeric=%s)" % bool(port_arg and port_arg.isdigit()), flush=True)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", port), H).serve_forever()
