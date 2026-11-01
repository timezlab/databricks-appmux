"""Spike 4, runs on the probe app as its service principal: write, read back and delete files of growing size
in a workspace folder and in a Unity Catalog volume.

usage:
  store_probe.py setup                  create the SP folder and the throwaway volume (both are tried; each result is printed)
  store_probe.py run <workspace|volume> <size-mb>[,<size-mb>...]
  store_probe.py small <workspace|volume> <count> <bytes> <threads>   many small files named by their sha256, then list and delete them
  store_probe.py teardown               delete the folder and the volume
environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET (read here, never printed)
Each operation prints one JSON line: target, op, size, HTTP status, seconds, MB/s, and whether the bytes read back match.
"""
from __future__ import annotations

import base64
import collections
import concurrent.futures
import hashlib
import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse

HOST = os.environ["DATABRICKS_HOST"].removeprefix("https://").rstrip("/")
CLIENT_ID = os.environ["DATABRICKS_CLIENT_ID"]
CLIENT_SECRET = os.environ["DATABRICKS_CLIENT_SECRET"]
WORK_DIR = "/tmp/spike4"
FOLDER = f"/Workspace/Users/{CLIENT_ID}/appmux-store"
VOLUME = ("workspace", "default", "appmux_spike_4_store")
VOLUME_ROOT = "/Volumes/" + "/".join(VOLUME)
BLOCK_BYTES = 1 << 20
OK_STATUSES = (200, 204)  # the Files API answers 204 to a write and a delete
SSL_CONTEXT = ssl.create_default_context()


def _token() -> str:
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    connection = http.client.HTTPSConnection(HOST, context=SSL_CONTEXT, timeout=60)
    connection.request(
        "POST", "/oidc/v1/token", "grant_type=client_credentials&scope=all-apis",
        {"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"},
    )
    response = connection.getresponse()
    body = json.loads(response.read())
    connection.close()
    return body["access_token"]


TOKEN = _token()


def call(method: str, path: str, body=None, length: int | None = None, sink=None, content_type: str | None = None):
    """Return (status, seconds, bytes_read, body_text). `body` is bytes or a file; `sink` receives response chunks."""
    headers = {"Authorization": "Bearer " + TOKEN}
    if length is not None:
        headers["Content-Length"] = str(length)
    if content_type:
        headers["Content-Type"] = content_type
    connection = http.client.HTTPSConnection(HOST, context=SSL_CONTEXT, timeout=900)
    started = time.time()
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        # the measurement itself: the service closes an oversized upload instead of answering, so status 0 means "no HTTP answer"
        connection.close()
        return 0, time.time() - started, 0, f"{type(exc).__name__}: {exc}"
    read = 0
    text = b""
    while chunk := response.read(BLOCK_BYTES):
        read += len(chunk)
        if sink is not None and response.status in OK_STATUSES:
            sink.update(chunk)
        elif len(text) < 300:
            text += chunk[:300]
    seconds = time.time() - started
    connection.close()
    return response.status, seconds, read, text.decode("utf-8", "replace")


def make_file(size_mb: int) -> tuple[str, str]:
    os.makedirs(WORK_DIR, exist_ok=True)
    path = f"{WORK_DIR}/blob-{size_mb}.bin"
    digest = hashlib.sha256()
    with open(path, "wb") as handle:
        for _ in range(size_mb):
            block = os.urandom(BLOCK_BYTES)
            digest.update(block)
            handle.write(block)
    return path, digest.hexdigest()


def report(target: str, op: str, size_mb: int | None, status: int, seconds: float, extra: dict | None = None) -> None:
    line = {"target": target, "op": op, "size_mb": size_mb, "status": status, "seconds": round(seconds, 1)}
    if size_mb and status in OK_STATUSES and op in ("write", "read"):
        line["mb_per_s"] = round(size_mb / max(seconds, 0.001), 1)
    line.update(extra or {})
    print(json.dumps(line), flush=True)


def _encoded(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def target_paths(target: str, name: str) -> dict:
    if target == "workspace":
        return {
            "put": f"/api/2.0/workspace-files/import-file{_encoded(FOLDER)}/{name}?overwrite=true",
            "get": f"/api/2.0/workspace/export?path={urllib.parse.quote(FOLDER + '/' + name, safe='')}&direct_download=true",
        }
    files_path = f"/api/2.0/fs/files{_encoded(VOLUME_ROOT)}/{name}"
    return {"put": files_path + "?overwrite=true", "get": files_path, "delete": files_path}


def delete(target: str, name: str) -> tuple[int, float, str]:
    if target == "workspace":
        status, seconds, _, text = call(
            "POST", "/api/2.0/workspace/delete", json.dumps({"path": f"{FOLDER}/{name}"}).encode(), content_type="application/json")
    else:
        status, seconds, _, text = call("DELETE", target_paths(target, name)["delete"])
    return status, seconds, text


def run(target: str, sizes_mb: list[int]) -> None:
    for size_mb in sizes_mb:
        local, expected = make_file(size_mb)
        name = f"blob-{size_mb}.bin"
        urls = target_paths(target, name)
        with open(local, "rb") as handle:
            status, seconds, _, text = call("PUT" if target == "volume" else "POST", urls["put"], handle, os.path.getsize(local))
        report(target, "write", size_mb, status, seconds, {} if status in OK_STATUSES else {"error": text[:200]})
        os.remove(local)
        if status not in OK_STATUSES:
            continue
        digest = hashlib.sha256()
        status, seconds, read, text = call("GET", urls["get"], sink=digest)
        report(target, "read", size_mb, status, seconds, {"bytes_read": read, "match": digest.hexdigest() == expected} if status in OK_STATUSES else {"error": text[:200]})
        status, seconds, text = delete(target, name)
        report(target, "delete", size_mb, status, seconds, {} if status in OK_STATUSES else {"error": text[:200]})


def small(target: str, count: int, size_bytes: int, threads: int) -> None:
    """The content store holds one file per distinct digest, so a source tree is many small calls, not one large one."""
    folder = "small"
    if target == "workspace":
        call("POST", "/api/2.0/workspace/mkdirs", json.dumps({"path": f"{FOLDER}/{folder}"}).encode(), content_type="application/json")
    blobs = {}
    for _ in range(count):
        data = os.urandom(size_bytes)
        blobs[hashlib.sha256(data).hexdigest()] = data

    def write(digest: str) -> int:
        name = f"{folder}/{digest}"
        return call("PUT" if target == "volume" else "POST", target_paths(target, name)["put"], blobs[digest], size_bytes)[0]

    def read(digest: str) -> int:
        sink = hashlib.sha256()
        status = call("GET", target_paths(target, f"{folder}/{digest}")["get"], sink=sink)[0]
        return status if sink.hexdigest() == digest or status != 200 else -1  # -1: answered 200 with other bytes

    def remove(digest: str) -> int:
        return delete(target, f"{folder}/{digest}")[0]

    for op, action in (("write", write), ("read", read), ("delete", remove)):
        started = time.time()
        with concurrent.futures.ThreadPoolExecutor(threads) as pool:
            results = list(pool.map(action, blobs))
        seconds = time.time() - started
        report(target, f"small-{op}", None, 200, seconds, {
            "files": count, "bytes_each": size_bytes, "threads": threads,
            "statuses": dict(collections.Counter(results)), "files_per_s": round(count / seconds, 1),
        })


def setup() -> None:
    status, seconds, _, text = call(
        "POST", "/api/2.0/workspace/mkdirs", json.dumps({"path": FOLDER}).encode(), content_type="application/json")
    report("workspace", "mkdirs", None, status, seconds, {} if status in OK_STATUSES else {"error": text[:200]})
    payload = {"catalog_name": VOLUME[0], "schema_name": VOLUME[1], "name": VOLUME[2], "volume_type": "MANAGED"}
    status, seconds, _, text = call("POST", "/api/2.1/unity-catalog/volumes", json.dumps(payload).encode(), content_type="application/json")
    report("volume", "create", None, status, seconds, {} if status in OK_STATUSES else {"error": text[:200]})


def teardown() -> None:
    status, seconds, _, text = call(
        "POST", "/api/2.0/workspace/delete", json.dumps({"path": FOLDER, "recursive": True}).encode(), content_type="application/json")
    report("workspace", "delete-folder", None, status, seconds, {} if status in OK_STATUSES else {"error": text[:200]})
    status, seconds, _, text = call("DELETE", "/api/2.1/unity-catalog/volumes/" + ".".join(VOLUME))
    report("volume", "delete", None, status, seconds, {} if status in OK_STATUSES else {"error": text[:200]})


if __name__ == "__main__":
    command = sys.argv[1]
    if command == "setup":
        setup()
    elif command == "run":
        run(sys.argv[2], [int(size) for size in sys.argv[3].split(",")])
    elif command == "small":
        small(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
    elif command == "teardown":
        teardown()
    else:
        sys.exit(__doc__)
