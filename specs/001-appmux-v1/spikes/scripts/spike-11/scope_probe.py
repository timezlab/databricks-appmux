"""Spike 11, runs on the probe app as its service principal: what the SP can do with a scope it was never granted, and whether
it can create a scope of its own (the SP is not a workspace admin).

usage: scope_probe.py <scope-never-granted> <key> <scope-to-create>
environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET (read here, never printed).
The output holds HTTP statuses and error text only; the value written into the SP's own scope is random and never printed.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # `python -I` leaves the script's directory off the path
from secrets_probe import HOST, describe, rest_get, sp_token


def rest_post(token: str, path: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        HOST + path, json.dumps(payload).encode(), {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(text)
        except ValueError:
            return exc.code, {"raw": text[:200]}


def main() -> None:
    never_granted, key, own_scope = sys.argv[1:4]
    token = sp_token()
    print("-- scope the SP was never granted", flush=True)
    print("list  :", describe(*rest_get(token, "/api/2.0/secrets/list", scope=never_granted)), flush=True)
    print("get   :", describe(*rest_get(token, "/api/2.0/secrets/get", scope=never_granted, key=key)), flush=True)
    print("-- a scope the SP creates itself", flush=True)
    print("create:", describe(*rest_post(token, "/api/2.0/secrets/scopes/create", {"scope": own_scope})), flush=True)
    print("put   :", describe(*rest_post(token, "/api/2.0/secrets/put", {"scope": own_scope, "key": "own", "string_value": secrets.token_urlsafe(24)})), flush=True)
    print("list  :", describe(*rest_get(token, "/api/2.0/secrets/list", scope=own_scope)), flush=True)
    print("delete:", describe(*rest_post(token, "/api/2.0/secrets/scopes/delete", {"scope": own_scope})), flush=True)


if __name__ == "__main__":
    main()
