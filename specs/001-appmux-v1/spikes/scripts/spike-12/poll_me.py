"""Spike 12, runs on the probe app: poll SCIM `Me` with the caller's own token and print every change of the group list.

usage: poll_me.py <max-seconds> <interval-seconds>   (environment: DATABRICKS_HOST, OBO_TOKEN from the harness)
Each line: seconds since the start, then the groups as name:type. Only the appmux-spike-12 groups are named; other groups are counted.
"""
import json
import os
import sys
import time
import urllib.request

host = os.environ["DATABRICKS_HOST"]
host = host if host.startswith("http") else "https://" + host
token = os.environ["OBO_TOKEN"]
max_s, interval_s = float(sys.argv[1]), float(sys.argv[2])
start = time.time()
print(f"start epoch {start:.1f}", flush=True)
last = None
while time.time() - start < max_s:
    request = urllib.request.Request(host + "/api/2.0/preview/scim/v2/Me", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=30) as response:
        groups = json.load(response).get("groups", [])
    named = sorted(f"{g['display'].removeprefix('appmux-spike-12-')}:{g['type']}" for g in groups if g["display"].startswith("appmux-spike-12-"))
    state = (named, len(groups) - len(named))
    if state != last:
        print(f"t+{time.time() - start:5.1f}s  spike groups {named}  other groups {state[1]}", flush=True)
        last = state
    time.sleep(interval_s)
print("end", f"t+{time.time() - start:.0f}s")
