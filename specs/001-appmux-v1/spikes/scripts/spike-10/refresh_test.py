"""Spike 10, runs on a throwaway app: keep an unmodified Databricks SDK authenticated across a 1-hour service principal
token expiry in children that never hold the client secret.

The supervisor (this process) holds the client id and secret, mints tokens, and offers them two ways:
  A  a local metadata service: the child gets DATABRICKS_METADATA_SERVICE_URL (the SDK's `metadata-service` auth, default chain)
  B  a token command: the child gets DATABRICKS_AUTH_TYPE=databricks-cli and DATABRICKS_CLI_PATH=<stub that prints the token file>
  C  control: the child gets the token minted at start in DATABRICKS_TOKEN and nothing else
Each child calls `WorkspaceClient().current_user.me()` every INTERVAL_S seconds. The supervisor re-mints when the cached
token has less than REFRESH_MARGIN_S left (environment, default 600; ADVERTISED_LIFETIME_S shortens the life it advertises, for a dry run). Tokens are never printed: the log holds the first 8 hex digits of the token's SHA-256.

usage: refresh_test.py <work-dir> <total-seconds> <interval-seconds>      (environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "https://" + os.environ["DATABRICKS_HOST"].removeprefix("https://").rstrip("/")
BASIC = base64.b64encode(f"{os.environ['DATABRICKS_CLIENT_ID']}:{os.environ['DATABRICKS_CLIENT_SECRET']}".encode()).decode()
TOKEN_SCOPE = "all-apis"
REFRESH_MARGIN_S = int(os.environ.get("REFRESH_MARGIN_S", "600"))
# test-only: advertise a shorter life than the real 3600 s so the SDK's refresh path runs in minutes (the dry run), never in the real run
ADVERTISED_LIFETIME_S = os.environ.get("ADVERTISED_LIFETIME_S")
PORT = 18710
PYTHON = sys.executable

_lock = threading.Lock()
_token: dict = {}
started = time.time()


def log(message: str) -> None:
    print(f"t+{time.time() - started:6.0f}s  {message}", flush=True)


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def mint() -> dict:
    request = urllib.request.Request(
        HOST + "/oidc/v1/token", urllib.parse.urlencode({"grant_type": "client_credentials", "scope": TOKEN_SCOPE}).encode(),
        {"Authorization": "Basic " + BASIC, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = json.load(response)
    lifetime_s = int(ADVERTISED_LIFETIME_S or body["expires_in"])
    token = {"access_token": body["access_token"], "token_type": body["token_type"], "expires_on": int(time.time()) + lifetime_s}
    log(f"supervisor minted a token {fingerprint(token['access_token'])}, real life {body['expires_in']} s, advertised {lifetime_s} s")
    return token


def current() -> dict:
    """The cached token, re-minted when less than REFRESH_MARGIN_S is left."""
    global _token
    with _lock:
        if not _token or _token["expires_on"] - time.time() < REFRESH_MARGIN_S:
            _token = mint()
        return dict(_token)


class MetadataService(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # http.server dispatches on this name
        token = current()
        payload = json.dumps({k: token[k] for k in ("access_token", "token_type", "expires_on")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        log(f"metadata service served {fingerprint(token['access_token'])} to {self.client_address[1]} (version header {self.headers.get('X-Databricks-Metadata-Version')})")

    def log_message(self, fmt: str, *args) -> None:
        pass


def write_token_file(path: str) -> None:
    """B: keep the file the stub prints current; the SDK reads `expiry` as local naive time."""
    last = None
    while True:
        token = current()
        if token["access_token"] != last:
            expiry = datetime.fromtimestamp(token["expires_on"]).strftime("%Y-%m-%dT%H:%M:%S")
            temporary = path + ".tmp"
            with open(temporary, "w") as handle:
                json.dump({"access_token": token["access_token"], "token_type": token["token_type"], "expiry": expiry}, handle)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            log(f"token file rewritten with {fingerprint(token['access_token'])}")
            last = token["access_token"]
        time.sleep(30)


CHILD = r'''
import hashlib, sys, time
from databricks.sdk import WorkspaceClient
label, interval, total = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
start = time.time()
client = WorkspaceClient()
while True:
    elapsed = time.time() - start
    try:
        client.current_user.me()
        header = client.config.authenticate().get("Authorization", "")
        print(f"{label} t+{elapsed:5.0f}s  call ok, token {hashlib.sha256(header.removeprefix('Bearer ').encode()).hexdigest()[:8]}", flush=True)
    except Exception as exc:  # the type and message are the measurement
        print(f"{label} t+{elapsed:5.0f}s  call FAILED {type(exc).__name__}: {str(exc)[:140]}", flush=True)
    if elapsed + interval > total:
        break
    time.sleep(interval)
'''


def child_env(extra: dict) -> dict:
    """No client id, secret or token inherited: the child holds what `extra` gives it and nothing else."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TZ")}
    env["DATABRICKS_HOST"] = HOST
    env.update(extra)
    return env


def main(work_dir: str, total_s: float, interval_s: float) -> None:
    os.makedirs(work_dir, exist_ok=True)
    token_file = os.path.join(work_dir, "token.json")
    stub = os.path.join(work_dir, "token-command.sh")
    with open(stub, "w") as handle:
        handle.write(f"#!/bin/sh\ncat {token_file}\n")
    os.chmod(stub, 0o700)

    first = current()
    threading.Thread(target=write_token_file, args=(token_file,), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), MetadataService)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while not os.path.exists(token_file):
        time.sleep(0.2)

    children = {
        "A-metadata": {"DATABRICKS_METADATA_SERVICE_URL": f"http://127.0.0.1:{PORT}/token"},
        "B-command ": {"DATABRICKS_AUTH_TYPE": "databricks-cli", "DATABRICKS_CLI_PATH": stub},
        "C-static  ": {"DATABRICKS_TOKEN": first["access_token"]},
    }
    processes = [
        subprocess.Popen([PYTHON, "-I", "-c", CHILD, label, str(interval_s), str(total_s)], env=child_env(extra), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for label, extra in children.items()
    ]

    def pump(process: subprocess.Popen) -> None:
        for line in process.stdout:
            log("child " + line.rstrip())

    pumps = [threading.Thread(target=pump, args=(p,)) for p in processes]
    for thread in pumps:
        thread.start()
    for process in processes:
        process.wait()
    for thread in pumps:
        thread.join()
    log("all children finished")


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))
