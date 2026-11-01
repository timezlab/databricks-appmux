"""Spike 10, runs on the probe app: does the service principal's token request accept a scope narrower than `all-apis`, and what
does a narrowed token still reach?

usage: narrow_scope.py <secret-scope> <secret-key> [<scope-string> ...]   (a scope string of `-` means: omit the parameter)
environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET. Tokens and secret values are never printed.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HOST = "https://" + os.environ["DATABRICKS_HOST"].removeprefix("https://").rstrip("/")
BASIC = base64.b64encode(f"{os.environ['DATABRICKS_CLIENT_ID']}:{os.environ['DATABRICKS_CLIENT_SECRET']}".encode()).decode()


def mint(scope: str) -> tuple[int, dict]:
    form = {"grant_type": "client_credentials"}
    if scope != "-":
        form["scope"] = scope
    request = urllib.request.Request(
        HOST + "/oidc/v1/token", urllib.parse.urlencode(form).encode(),
        {"Authorization": "Basic " + BASIC, "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def status_of(token: str, path: str, **query) -> str:
    request = urllib.request.Request(HOST + path + ("?" + urllib.parse.urlencode(query) if query else ""), headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            text = json.loads(text).get("message", "")
        except ValueError:
            pass  # not JSON: the first characters of the body are the message
        return f"HTTP {exc.code} {text[:110]}"


if __name__ == "__main__":
    secret_scope, secret_key, *scopes = sys.argv[1:]
    for scope in scopes:
        status, body = mint(scope)
        if status != 200:
            print(f"scope {scope!r}: token request HTTP {status} {json.dumps({k: str(v)[:140] for k, v in body.items()})}", flush=True)
            continue
        token = body["access_token"]
        print(f"scope {scope!r}: token granted, scope in the answer {body.get('scope')!r}, expires_in {body.get('expires_in')}", flush=True)
        print(f"    secrets/get      -> {status_of(token, '/api/2.0/secrets/get', scope=secret_scope, key=secret_key)}", flush=True)
        print(f"    scim Me          -> {status_of(token, '/api/2.0/preview/scim/v2/Me')}", flush=True)
        print(f"    apps list        -> {status_of(token, '/api/2.0/apps')}", flush=True)
        print(f"    warehouses list  -> {status_of(token, '/api/2.0/sql/warehouses')}", flush=True)
