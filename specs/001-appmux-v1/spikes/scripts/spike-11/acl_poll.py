"""Spike 11, runs on the probe app as its service principal: after a scope's READ grant for the SP is added or removed, how the
answers of `secrets/get` and `secrets/list` change over time.

usage: acl_poll.py <scope> <key> <interval-seconds> <count>
environment: DATABRICKS_HOST, DATABRICKS_CLIENT_ID, DATABRICKS_CLIENT_SECRET (read here, never printed).
Prints one line per call (elapsed seconds, HTTP status of get and of list); a secret value is never read into the output.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # `python -I` leaves the script's directory off the path
from secrets_probe import rest_get, sp_token


def main() -> None:
    scope, key, interval_s, count = sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
    token = sp_token()
    started = time.time()
    for _ in range(count):
        get_status = rest_get(token, "/api/2.0/secrets/get", scope=scope, key=key)[0]
        list_status = rest_get(token, "/api/2.0/secrets/list", scope=scope)[0]
        print(f"t+{time.time() - started:5.1f}s  get {get_status}  list {list_status}", flush=True)
        time.sleep(interval_s)


if __name__ == "__main__":
    main()
