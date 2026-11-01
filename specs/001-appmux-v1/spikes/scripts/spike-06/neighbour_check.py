"""Spike 6, runs on the owner's machine: the two controls for the neighbour-pod test, saved as JSON.

1. The neighbour App connects to its own pod address (its echo port and its HTTP port): a silent miss elsewhere is then the network.
2. The neighbour App connects to a listener on the probe container (the reverse direction); the listener counts what arrives.

Addresses are read into variables and never printed or written; the output names only results. Needs the CLI logged in to the
Free Edition workspace and a way to run a shell command on the probe (`SPIKE_SHELL`, a script that takes the command as its argument).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

LISTENER_PORT = 47120
LISTENER_SECONDS = 40
NEIGHBOUR_APP = "appmux-spike-6-neighbour"
REQUEST_TIMEOUT_S = 40
PROBE_PYTHON = "/app/python/source_code/.venv/bin/python"


def cli(*args: str) -> dict:
    return json.loads(subprocess.run(["databricks", *args, "-o", "json"], capture_output=True, text=True, check=True).stdout)


def probe_shell(command: str) -> str:
    done = subprocess.run([os.environ["SPIKE_SHELL"], command], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)["stdout"]


def neighbour_get(base_url: str, token: str, path: str) -> dict:
    request = urllib.request.Request(base_url + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        return json.load(response)


def main() -> None:
    result_path = sys.argv[1]
    probe_shell(f"cd /tmp/spike6 && rm -f reverse.json reverse.json.ip && "
                f"(nohup {PROBE_PYTHON} reverse_listener.py {LISTENER_SECONDS} {LISTENER_PORT} /tmp/spike6/reverse.json >/dev/null 2>&1 &); sleep 1")
    probe_ip = probe_shell("cat /tmp/spike6/reverse.json.ip").strip()
    app = cli("apps", "get", NEIGHBOUR_APP)
    token = cli("auth", "token")["access_token"]
    own = neighbour_get(app["url"], token, "/selftest")
    reverse = neighbour_get(app["url"], token, f"/reach?host={probe_ip}&port={LISTENER_PORT}")
    time.sleep(LISTENER_SECONDS)  # the listener writes its count when its time is up
    count = json.loads(probe_shell("cat /tmp/spike6/reverse.json"))
    result = {"date": time.strftime("%Y-%m-%d"),
              "neighbour_to_its_own_pod_address": own,
              "neighbour_to_the_probe_listener": reverse,
              "connections_the_probe_listener_counted": count}
    text = json.dumps(result, indent=2).replace(probe_ip, "<container-ip>")
    with open(result_path, "w") as handle:
        handle.write(text)
    print("wrote", result_path)


if __name__ == "__main__":
    main()
