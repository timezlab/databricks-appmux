"""Spike 16, runs on the probe app: for each resource type of the data model, which read-only call shows the attacher's access.

usage: attach_probe.py <targets.json>
  <targets.json> maps a resource type to the identifier its call needs (warehouse id, experiment id, endpoint name, job id,
  `catalog.schema.name`, connection name, Genie space id, app name). A type with no entry is skipped.
environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET (the app's SP, a principal with no grant on the
objects), OBO_TOKEN (the caller's user token, set by the harness). Tokens are read here and never printed.
Output: one JSON line per call with the HTTP status for each token and, on a refusal, the first 120 characters of the error text;
`level` is the caller's own permission level when the response carries one.
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
NOTE_CHARS = 120

# (type, label, path template with {target}, query template). `{target}` is URL-quoted where it is a path segment.
CALLS = [
    ("sql_warehouse", "warehouse get", "/api/2.0/sql/warehouses/{target}", {}),
    ("sql_warehouse", "permissions", "/api/2.0/permissions/warehouses/{target}", {}),
    ("mlflow_experiment", "experiment get", "/api/2.0/mlflow/experiments/get", {"experiment_id": "{target}"}),
    ("mlflow_experiment", "permissions", "/api/2.0/permissions/experiments/{target}", {}),
    ("serving_endpoint", "endpoint get", "/api/2.0/serving-endpoints/{target}", {}),
    ("job", "job get", "/api/2.1/jobs/get", {"job_id": "{target}"}),
    ("job", "permissions", "/api/2.0/permissions/jobs/{target}", {}),
    ("uc_table", "table get", "/api/2.1/unity-catalog/tables/{target}", {}),
    ("uc_volume", "volume get", "/api/2.1/unity-catalog/volumes/{target}", {}),
    ("uc_function", "function get", "/api/2.1/unity-catalog/functions/{target}", {}),
    ("uc_connection", "connection get", "/api/2.1/unity-catalog/connections/{target}", {}),
    ("genie_space", "space get", "/api/2.0/genie/spaces/{target}", {}),
    ("vector_search_index", "index get", "/api/2.0/vector-search/indexes/{target}", {}),
    ("databricks_app", "app get", "/api/2.0/apps/{target}", {}),
    ("uc_volume", "files directory", "/api/2.0/fs/directories/Volumes/{target_path}", {}),
]

# The same objects read through the SQL Statement Execution API (scope `sql`), as the caller: a route for types with no scope of their own.
SQL_CALLS = [
    ("sql_warehouse", "sql SELECT 1", "SELECT 1"),
    ("uc_table", "sql DESCRIBE TABLE", "DESCRIBE TABLE {target}"),
    ("uc_function", "sql DESCRIBE FUNCTION", "DESCRIBE FUNCTION {target}"),
    ("uc_volume", "sql LIST", "LIST '/Volumes/{target_path}'"),
    ("uc_connection", "sql DESCRIBE CONNECTION", "DESCRIBE CONNECTION {target}"),
]
LEVEL_KEYS = ("permission_level", "permission")
STATEMENT_WAIT_S = 50


def sp_token() -> str:
    basic = base64.b64encode(f"{os.environ['DATABRICKS_CLIENT_ID']}:{os.environ['DATABRICKS_CLIENT_SECRET']}".encode()).decode()
    request = urllib.request.Request(
        HOST + "/oidc/v1/token", b"grant_type=client_credentials&scope=all-apis",
        {"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["access_token"]


def get(token: str, path: str, query: dict) -> dict:
    url = HOST + path + ("?" + urllib.parse.urlencode(query) if query else "")
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read() or b"{}")
            level = next((body[key] for key in LEVEL_KEYS if isinstance(body.get(key), str)), None)
            return {"status": response.status, **({"level": level} if level else {})}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            note = str(json.loads(raw).get("message") or raw)
        except ValueError:
            note = raw
        return {"status": exc.code, "note": note[:NOTE_CHARS]}


def statement(token: str, warehouse_id: str, text: str) -> dict:
    payload = json.dumps({"warehouse_id": warehouse_id, "statement": text, "wait_timeout": f"{STATEMENT_WAIT_S}s"}).encode()
    request = urllib.request.Request(
        HOST + "/api/2.0/sql/statements", payload, {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=STATEMENT_WAIT_S + 30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return {"status": exc.code, "note": raw[:NOTE_CHARS]}
    state = body.get("status", {})
    return {"status": 200, "state": state.get("state"), **({"note": state["error"]["message"][:NOTE_CHARS]} if state.get("error") else {})}


def main() -> None:
    with open(sys.argv[1]) as handle:
        targets = json.load(handle)
    obo = os.environ.get("OBO_TOKEN")
    if not obo:
        sys.exit("no OBO_TOKEN in the environment")
    sp = sp_token()
    for kind, label, path, query in CALLS:
        if kind not in targets:
            continue
        target = targets[kind]
        quoted = urllib.parse.quote(target, safe="")
        filled_path = path.format(target=quoted, target_path=target.replace(".", "/"))
        filled_query = {key: value.format(target=target) for key, value in query.items()}
        print(json.dumps({"type": kind, "call": label, "obo": get(obo, filled_path, filled_query), "sp": get(sp, filled_path, filled_query)}), flush=True)
    warehouse = targets.get("sql_warehouse")
    for kind, label, text in SQL_CALLS:
        if kind in targets and warehouse:
            filled = text.format(target=targets[kind], target_path=targets[kind].replace(".", "/"))
            print(json.dumps({"type": kind, "call": label, "obo": statement(obo, warehouse, filled), "sp": statement(sp, warehouse, filled)}), flush=True)


if __name__ == "__main__":
    main()
