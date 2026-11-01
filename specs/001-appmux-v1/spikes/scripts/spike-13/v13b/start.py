"""Spike 13 variant B: standard-library bootstrap (design 13.1 step 5, fallback).

Reads the app's own service-principal credentials from the environment, downloads every
wheel in WHEEL_FOLDER (a workspace folder the SP can read) to /tmp, installs them
offline into /tmp with the environment's pip, then starts the app with that directory
ahead of site-packages. Only success or failure and counts are logged, never a token.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

WHEEL_DIR = "/tmp/appmux-wheels"
DEPS_DIR = "/tmp/appmux-deps"


def log(**fields):
    print("SPIKE13-BOOT", json.dumps(fields), flush=True)


def call(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    host = os.environ["DATABRICKS_HOST"]
    host = host if host.startswith("https://") else "https://" + host
    basic = base64.b64encode(f"{os.environ['DATABRICKS_CLIENT_ID']}:{os.environ['DATABRICKS_CLIENT_SECRET']}".encode()).decode()
    token = json.loads(call(
        f"{host}/oidc/v1/token",
        {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        b"grant_type=client_credentials&scope=all-apis",
    ))["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    folder = os.environ["WHEEL_FOLDER"]
    listing = json.loads(call(f"{host}/api/2.0/workspace/list?" + urllib.parse.urlencode({"path": folder}), auth))
    wheels = [o for o in listing.get("objects", []) if o["path"].endswith(".whl")]
    log(step="list", wheel_count=len(wheels), object_types=sorted({o.get("object_type", "?") for o in wheels}))
    os.makedirs(WHEEL_DIR, exist_ok=True)
    for o in wheels:
        q = urllib.parse.urlencode({"path": o["path"], "format": "AUTO", "direct_download": "true"})
        data = call(f"{host}/api/2.0/workspace/export?{q}", auth)
        with open(os.path.join(WHEEL_DIR, os.path.basename(o["path"])), "wb") as f:
            f.write(data)
    log(step="download", files=sorted(os.listdir(WHEEL_DIR)))
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-index", "--find-links", WHEEL_DIR, "--target", DEPS_DIR, os.environ["MARKER_REQUIREMENT"]],
        capture_output=True, text=True,
    )
    log(step="install", returncode=install.returncode, tail=install.stdout.strip().splitlines()[-1:] + install.stderr.strip().splitlines()[-2:])
    if install.returncode != 0:
        sys.exit(1)
    os.environ["PYTHONPATH"] = DEPS_DIR
    os.execv(sys.executable, [sys.executable, "app.py", "--port", os.environ["DATABRICKS_APP_PORT"]])


if __name__ == "__main__":
    main()
