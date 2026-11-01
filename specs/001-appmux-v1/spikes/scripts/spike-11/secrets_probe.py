"""Spike 11, runs on the probe app: can an App read a Databricks secret value, with which identity, and which calls work.

usage: secrets_probe.py <scope-with-grant> <key> <expected-sha256> <scope-without-grant> <key> <expected-sha256>
environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET (the app's service principal), OBO_TOKEN (the
caller's user token, set by the harness). Tokens are read here and never printed; a secret value is never printed either:
the output holds the HTTP status, the error text, and whether the SHA-256 of the value equals the expected one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = "https://" + os.environ["DATABRICKS_HOST"].removeprefix("https://").rstrip("/")


def sp_token() -> str:
    basic = base64.b64encode(f"{os.environ['DATABRICKS_CLIENT_ID']}:{os.environ['DATABRICKS_CLIENT_SECRET']}".encode()).decode()
    request = urllib.request.Request(
        HOST + "/oidc/v1/token", b"grant_type=client_credentials&scope=all-apis",
        {"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def rest_get(token: str, path: str, **query) -> tuple[int, dict]:
    request = urllib.request.Request(HOST + path + "?" + urllib.parse.urlencode(query), headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(text)
        except ValueError:
            return exc.code, {"raw": text[:200]}


def describe(status: int, body: dict, expected_sha256: str | None = None) -> str:
    """One line that never contains the secret value."""
    parts = [f"HTTP {status}"]
    if "value" in body:
        value = base64.b64decode(body["value"])
        parts.append(f"value returned, {len(value)} bytes, sha256 {'equals' if hashlib.sha256(value).hexdigest() == expected_sha256 else 'differs from'} the expected")
    elif "keys" in body:
        parts.append("keys " + str([k["key"] for k in body["keys"]]))
    elif "scopes" in body:
        parts.append("scopes " + str(sorted(s["name"] for s in body["scopes"])))
    else:
        parts.append(json.dumps({k: str(v)[:160] for k, v in body.items()}))
    return ", ".join(parts)


def sdk_attempts(scope: str, key: str, expected_sha256: str) -> None:
    from databricks.sdk import WorkspaceClient  # the app environment's own copy; nothing is installed

    client = WorkspaceClient()  # reads the same service principal from the environment
    for label, call in (
        ("sdk dbutils.secrets.get", lambda: client.dbutils.secrets.get(scope, key)),
        ("sdk secrets.get_secret", lambda: client.secrets.get_secret(scope, key).value),
    ):
        try:
            value = call()
            raw = base64.b64decode(value) if label.endswith("get_secret") else value.encode()
            print(f"{label}: value returned, {len(raw)} bytes, sha256 {'equals' if hashlib.sha256(raw).hexdigest() == expected_sha256 else 'differs from'} the expected", flush=True)
        except Exception as exc:  # the type and message are the measurement
            print(f"{label}: {type(exc).__name__}: {str(exc)[:200]}", flush=True)


def main() -> None:
    scope, key, expected, other_scope, other_key, other_expected = sys.argv[1:7]
    sp = sp_token()
    obo = os.environ.get("OBO_TOKEN")
    print("-- service principal token, scope with READ", flush=True)
    print("get   :", describe(*rest_get(sp, "/api/2.0/secrets/get", scope=scope, key=key), expected), flush=True)
    print("list  :", describe(*rest_get(sp, "/api/2.0/secrets/list", scope=scope)), flush=True)
    print("scopes:", describe(*rest_get(sp, "/api/2.0/secrets/scopes/list")), flush=True)
    sdk_attempts(scope, key, expected)
    print("-- service principal token, second scope", flush=True)
    print("get   :", describe(*rest_get(sp, "/api/2.0/secrets/get", scope=other_scope, key=other_key), other_expected), flush=True)
    print("list  :", describe(*rest_get(sp, "/api/2.0/secrets/list", scope=other_scope)), flush=True)
    print("-- the caller's OBO token", flush=True)
    if obo:
        print("list  :", describe(*rest_get(obo, "/api/2.0/secrets/list", scope=scope)), flush=True)
        print("get   :", describe(*rest_get(obo, "/api/2.0/secrets/get", scope=scope, key=key), expected), flush=True)
    else:
        print("no OBO_TOKEN in the environment", flush=True)


if __name__ == "__main__":
    main()
