"""Spike 15, runs on the probe app: can appmux list and export a workspace folder with the presser's OBO token, which scope that
needs, and what the same token and the app's service principal cannot do.

usage: workspace_read_probe.py <owner-folder>
  <owner-folder> is a workspace folder the caller (the owner) can read and the SP has no grant on.
environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET (the app's SP), OBO_TOKEN (the caller's user token,
set by the harness). Tokens are read here and never printed; file contents are never printed: the output holds HTTP status,
error text, counts and byte lengths.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = "https://" + os.environ["DATABRICKS_HOST"].removeprefix("https://").rstrip("/")
FILE_TYPES = ("FILE", "NOTEBOOK")


def call(token: str, method: str, path: str, query: dict | None = None, payload: dict | None = None) -> tuple[int, bytes]:
    url = HOST + path + ("?" + urllib.parse.urlencode(query) if query else "")
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, body, headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def error_text(raw: bytes) -> str:
    try:
        body = json.loads(raw)
        return str(body.get("message") or body)[:200]
    except ValueError:
        return raw.decode("utf-8", "replace")[:200]


def read_folder(label: str, token: str, folder: str, known_file: str | None = None) -> str | None:
    """get-status, list, then export one file: what an Update needs. Returns the file exported (or `known_file`, tried directly)."""
    status, raw = call(token, "GET", "/api/2.0/workspace/get-status", {"path": folder})
    print(f"{label}: get-status HTTP {status}" + ("" if status == 200 else f": {error_text(raw)}"), flush=True)
    status, raw = call(token, "GET", "/api/2.0/workspace/list", {"path": folder})
    if status != 200:
        print(f"{label}: list HTTP {status}: {error_text(raw)}", flush=True)
        return None
    objects = json.loads(raw).get("objects", [])
    print(f"{label}: list HTTP 200, {len(objects)} objects, types {sorted({o['object_type'] for o in objects})}", flush=True)
    first = next((o["path"] for o in objects if o["object_type"] in FILE_TYPES), known_file)
    if first is None:
        print(f"{label}: no file to export in this folder", flush=True)
        return None
    status, raw = call(token, "GET", "/api/2.0/workspace/export", {"path": first, "direct_download": "true"})
    print(f"{label}: export HTTP {status}" + (f", {len(raw)} bytes" if status == 200 else f": {error_text(raw)}"), flush=True)
    return first


def sp_token() -> str:
    basic = base64.b64encode(f"{os.environ['DATABRICKS_CLIENT_ID']}:{os.environ['DATABRICKS_CLIENT_SECRET']}".encode()).decode()
    request = urllib.request.Request(
        HOST + "/oidc/v1/token", b"grant_type=client_credentials&scope=all-apis",
        {"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def main() -> None:
    owner_folder = sys.argv[1]
    sp_folder = "/Workspace/Users/" + os.environ["DATABRICKS_CLIENT_ID"]
    obo = os.environ.get("OBO_TOKEN")
    if not obo:
        sys.exit("no OBO_TOKEN in the environment")
    sp = sp_token()
    print("-- the caller's OBO token", flush=True)
    exported = read_folder("owner folder", obo, owner_folder)
    read_folder("SP's own folder", obo, sp_folder)
    status, raw = call(obo, "POST", "/api/2.0/workspace/mkdirs", payload={"path": owner_folder + "/appmux-spike-15-write-check"})
    print(f"write attempt (mkdirs) with the OBO token: HTTP {status}: {error_text(raw)}", flush=True)
    print("-- the app's service principal token (no grant on the owner's folder)", flush=True)
    read_folder("owner folder", sp, owner_folder, known_file=exported)
    read_folder("SP's own folder", sp, sp_folder)


if __name__ == "__main__":
    main()
