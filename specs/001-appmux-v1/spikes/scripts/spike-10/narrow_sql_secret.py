"""Spike 10, runs on the probe app: can a service principal token narrowed to `sql` still read a secret, through the SQL `secret()`
function instead of the Secrets API?

usage: narrow_sql_secret.py <warehouse-id> <secret-scope> <secret-key> [<token-scope> ...]
  <secret-scope> is a scope the app's service principal has READ on. Each <token-scope> is a `scope` string for the token request
  (default: `sql`, then `all-apis`).
environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET. Tokens and secret values are never printed: the output
holds HTTP statuses and the statement state (the statement returns the length of the value, not the value).
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
BASIC = base64.b64encode(f"{os.environ['DATABRICKS_CLIENT_ID']}:{os.environ['DATABRICKS_CLIENT_SECRET']}".encode()).decode()
DEFAULT_TOKEN_SCOPES = ("sql", "all-apis")
STATEMENT_WAIT_S = 50
NOTE_CHARS = 120


def mint(scope: str) -> str:
    request = urllib.request.Request(
        HOST + "/oidc/v1/token", urllib.parse.urlencode({"grant_type": "client_credentials", "scope": scope}).encode(),
        {"Authorization": "Basic " + BASIC, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def secrets_api_status(token: str, secret_scope: str, secret_key: str) -> int:
    query = urllib.parse.urlencode({"scope": secret_scope, "key": secret_key})
    request = urllib.request.Request(HOST + "/api/2.0/secrets/get?" + query, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def sql_secret_length(token: str, warehouse_id: str, secret_scope: str, secret_key: str) -> str:
    text = f"SELECT length(secret('{secret_scope}', '{secret_key}'))"
    payload = json.dumps({"warehouse_id": warehouse_id, "statement": text, "wait_timeout": f"{STATEMENT_WAIT_S}s"}).encode()
    request = urllib.request.Request(
        HOST + "/api/2.0/sql/statements", payload, {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=STATEMENT_WAIT_S + 30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:NOTE_CHARS]}"
    state = body.get("status", {})
    note = f" {state['error']['message'][:NOTE_CHARS]}" if state.get("error") else ""
    return f"{state.get('state')}{note}"


def main() -> None:
    warehouse_id, secret_scope, secret_key = sys.argv[1:4]
    token_scopes = sys.argv[4:] or list(DEFAULT_TOKEN_SCOPES)
    for token_scope in token_scopes:
        token = mint(token_scope)
        print(f"token scope '{token_scope}'", flush=True)
        print(f"    secrets/get API    -> HTTP {secrets_api_status(token, secret_scope, secret_key)}", flush=True)
        print(f"    SQL secret() length -> {sql_secret_length(token, warehouse_id, secret_scope, secret_key)}", flush=True)


if __name__ == "__main__":
    main()
