"""Spike 6, runs on the probe app: a TCP listener on the container's own address, for the neighbour App's reverse test. Not part of appmux.

usage: reverse_listener.py <seconds> <port> <result-file>
Writes the container's address to <result-file>.ip at start (the driver reads it; it is never printed) and the number of
connections that reached the listener to <result-file> at the end.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import egress_stack as es

duration_s, port, result_path = float(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
servers = es.EchoServers([port], [])
with open(result_path + ".ip", "w") as handle:
    handle.write(es.container_ip())
time.sleep(duration_s)
with open(result_path, "w") as handle:
    json.dump({"connections_that_reached_the_listener": servers.snapshot()["tcp_connections"][port]}, handle)
