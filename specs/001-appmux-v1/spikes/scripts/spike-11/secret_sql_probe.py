"""Spike 11, runs on the probe app: the attach check for a secret through the SQL `secret()` function, run as the caller.

`SELECT length(secret('<scope>', '<key>'))` runs on a SQL warehouse as the identity of the token, succeeds only when that identity
has READ on the scope, and returns the length of the value, never the value. The token needs only the `sql` OAuth scope.

usage: secret_sql_probe.py <warehouse-id> <scope> <key> [<scope> <key> ...]
environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET (the app's SP), OBO_TOKEN (the caller's user token,
set by the harness). Tokens are read here and never printed. Output: one JSON line per scope/key and token: statement state and
the first 160 characters of the error text.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # `python -I` leaves the script's directory off the path
from secrets_probe import HOST, sp_token

STATEMENT_WAIT_S = 50
NOTE_CHARS = 160


def check(token: str, warehouse_id: str, scope: str, key: str) -> dict:
    text = f"SELECT length(secret('{scope}', '{key}'))"
    payload = json.dumps({"warehouse_id": warehouse_id, "statement": text, "wait_timeout": f"{STATEMENT_WAIT_S}s"}).encode()
    request = urllib.request.Request(
        HOST + "/api/2.0/sql/statements", payload, {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=STATEMENT_WAIT_S + 30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        return {"http": exc.code, "note": exc.read().decode("utf-8", "replace")[:NOTE_CHARS]}
    state = body.get("status", {})
    result = {"http": 200, "state": state.get("state")}
    if state.get("error"):
        result["note"] = state["error"]["message"][:NOTE_CHARS]
    return result


def main() -> None:
    warehouse_id, pairs = sys.argv[1], sys.argv[2:]
    obo = os.environ.get("OBO_TOKEN")
    if not obo:
        sys.exit("no OBO_TOKEN in the environment")
    sp = sp_token()
    for scope, key in zip(pairs[::2], pairs[1::2]):
        print(json.dumps({"scope": scope, "key": key, "obo": check(obo, warehouse_id, scope, key), "sp": check(sp, warehouse_id, scope, key)}), flush=True)


if __name__ == "__main__":
    main()
