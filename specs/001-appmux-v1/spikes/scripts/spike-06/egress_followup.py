"""Spike 6 follow-up, runs on the probe app: what runs 6a and 6b left open. Not part of appmux.

reload_long    An open TCP connection denied for 30 s did not recover within the 12 s that run 6b watched after the allow: watch 90 s.
ipv6_control   The control arm (pump passes IPv6, slirp4netns has `--enable-ipv6`) got an address and a default route but every
               connect returned ENETUNREACH: add a route lookup per destination and the interface's IPv6 sysctls.
own_address    Which of the container's own listening ports a sandbox can reach on the container's address with no rule, and with
               the design's `deny <container address>/32`. The edge listens on all addresses, so this is where a child could
               talk to the edge directly and to the platform's own services.

usage: egress_followup.py <slirp4netns-binary> <result-json> [section ...]     (all sections by default)
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import egress_lab as lab_module
import egress_stack as es

LONG_DENY_S = 30.0
LONG_OBSERVE_S = 90.0


def listening_ports(own_address: str) -> list[int]:
    """TCP ports in LISTEN state on every address or on `own_address`."""
    own_hex = struct.pack("<L", int.from_bytes(socket.inet_aton(own_address), "big")).hex().upper()
    ports = set()
    with open("/proc/net/tcp") as handle:
        for line in handle.read().splitlines()[1:]:
            fields = line.split()
            address, port = fields[1].split(":")
            if fields[3] == "0A" and address in ("00000000", own_hex):
                ports.add(int(port, 16))
    return sorted(ports)


def own_address(lab: lab_module.Lab) -> dict:
    ports = [port for port in listening_ports(lab.ip) if port not in lab_module.LAB_PORTS]
    out = {"listening_ports_on_the_container_address": ports}
    for label, doc in (("no rule", lab_module.document([])),
                       ("the design's deny of the container address", lab_module.document([lab_module.rule("deny", f"{lab.ip}/32")]))):
        lab.apply(doc)
        reached = [port for port in ports if lab.stack.call("tcp_connect", host=lab.ip, port=port, timeout=1.0, echo=False).get("ok")]
        out[f"sandbox, {label}"] = {"connected": reached, "not_connected": [port for port in ports if port not in reached]}
    return out


def main() -> None:
    slirp_binary, result_path = sys.argv[1], sys.argv[2]
    chosen = sys.argv[3:] or ["reload_long", "ipv6_control", "own_address"]
    python = sys.executable
    lab = lab_module.Lab(slirp_binary, python)
    result: dict = {"date": time.strftime("%Y-%m-%d")}
    try:
        if "reload_long" in chosen or "own_address" in chosen:
            try:
                lab.stack.start()
                if "reload_long" in chosen:
                    result["reload_open_connection"] = {f"denied {LONG_DENY_S:g} s": lab_module.reload_open(lab, LONG_DENY_S, LONG_OBSERVE_S)}
                if "own_address" in chosen:
                    result["own_address"] = own_address(lab)
            finally:
                lab.stack.close()
        if "ipv6_control" in chosen:
            try:
                result["ipv6_control_arm"] = lab_module.ipv6_control(slirp_binary, python)
            except Exception as exc:  # the control arm is optional evidence: record why it did not run
                result["ipv6_control_arm"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        lab.servers.close()
    text = es.mask(json.dumps(result, indent=2, default=str), {lab.ip: "<container-ip>"})
    with open(result_path, "w") as handle:
        handle.write(text)
    print(text)


if __name__ == "__main__":
    main()
