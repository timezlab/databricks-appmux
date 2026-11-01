"""Spike 6, runs on the probe app: egress mechanics of the pump.

Questions of the spec and where they are answered: live reload for open and new connections (`reload_new`, `reload_open`), port
rules for TCP and UDP (`port_rules`), IP fragments with port rules (`fragments`), IPv6 (`ipv6`), neighbours on the container's
subnet (`neighbours`). Also checks the allow-list shape of the design, a trailing `deny 0.0.0.0/0` with the virtual subnet always
allowed (`allow_list`).

usage: egress_lab.py <slirp4netns-binary> <result-json> [<neighbour-ip> <neighbour-echo-port> <neighbour-http-port>]
  <neighbour-ip> is the address of a second App of the same workspace, started for this test: the container's subnet may hold
  other tenants, so it is never swept; the run only tries the one address that is known to be ours.
Runs as the app user (uid 1000) in the app environment's Python. Addresses are replaced by placeholders in the result file.
"""
from __future__ import annotations

import ipaddress
import json
import os
import platform
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import egress_stack as es

SCRATCH = "/dev/shm/spike6-lab"
TCP_OPEN, TCP_SHUT, TCP_OTHER = 47101, 47102, 47105
UDP_OPEN, UDP_SHUT = 47103, 47104
V6_LOOPBACK_PORT = 47110
LAB_PORTS = (TCP_OPEN, TCP_SHUT, TCP_OTHER, UDP_OPEN, UDP_SHUT, V6_LOOPBACK_PORT)  # the lab's own listeners, not services of the container
PYPI_HOST = "pypi.org"
PUBLIC_V6 = "2606:4700:4700::1111"
SLIRP_GATEWAY_V6 = "fd00::2"
PROBE_TIMEOUT_S = 1.0
PROBE_INTERVAL_S = 0.1
OPEN_STREAM_INTERVAL_S = 0.05
SETTLE_S = 0.3
RELOAD_BEFORE_S, RELOAD_AFTER_S = 2.0, 12.0
OPEN_CONNECTION_DENIED_S = (2.0, 6.0, 15.0, 30.0)
FRAGMENTED_UDP_BYTES = 4000
SMALL_UDP_BYTES = 1000


def document(rules: list[dict], **settings: object) -> dict:
    return {"ipv6": "drop", "fragments": "drop-if-port-rules", "virtual_subnet": "always-allow", "default": "allow", "rules": rules, **settings}


def rule(action: str, cidr: str, proto: str = "any", ports: tuple[int, int] | None = None) -> dict:
    return {"action": action, "cidr": cidr, "proto": proto, "ports": list(ports) if ports else None}


def outcome(result: dict) -> str:
    """One word for what a test did: ok, or the error that ended it."""
    if "command_error" in result:
        return "command-error: " + result["command_error"]
    if "replies" in result:
        replies = result["replies"]
        return "ok" if all(r.get("intact") for r in replies) else (replies[0].get("error") or "damaged")
    return "ok" if result.get("ok") else result.get("error", "failed")


class Lab:
    def __init__(self, slirp_binary: str, python: str) -> None:
        self.ip = es.container_ip()
        self.servers = es.EchoServers([TCP_OPEN, TCP_SHUT, TCP_OTHER], [UDP_OPEN, UDP_SHUT], [V6_LOOPBACK_PORT])
        self.stack = es.Stack(SCRATCH, slirp_binary, python, document([]))
        self.slirp_binary, self.python = slirp_binary, python
        self.generation = 1

    def apply(self, doc: dict) -> dict:
        """Write new rules, wait for the pump to load them, return the write time and the pump's load time."""
        written_at = self.stack.write_rules(doc)
        self.generation += 1
        time.sleep(SETTLE_S)
        loaded = [r for r in self.stack.pump_stats()["reloads"] if r["generation"] == self.generation and r["ok"]]
        return {"written_at": written_at, "loaded_at": loaded[0]["loaded_at"] if loaded else None}

    def decisions(self) -> dict:
        return self.stack.pump_stats()["decisions"]

    def check_tcp(self, port: int) -> str:
        return outcome(self.stack.call("tcp_connect", host=self.ip, port=port, timeout=PROBE_TIMEOUT_S))

    def check_udp(self, port: int, size: int = 64) -> str:
        return outcome(self.stack.call("udp_echo", host=self.ip, port=port, size=size, timeout=PROBE_TIMEOUT_S))

    def delta(self, before: dict) -> dict:
        after = self.decisions()
        return {key: after[key] - before.get(key, 0) for key in after if after[key] != before.get(key, 0)}


def reload_new(lab: Lab) -> dict:
    """A rule change versus connections that are opened while it happens."""
    lab.apply(document([]))
    lab.stack.call("tcp_probe_start", name="new", host=lab.ip, port=TCP_OPEN, interval_s=PROBE_INTERVAL_S, timeout=PROBE_TIMEOUT_S)
    time.sleep(1.5)
    deny = lab.apply(document([rule("deny", f"{lab.ip}/32", "tcp", (TCP_OPEN, TCP_OPEN))]))
    time.sleep(3.0)
    allow = lab.apply(document([]))
    time.sleep(2.0)
    log = lab.stack.call("stop", name="new")["log"]
    after_deny = [a for a in log if deny["loaded_at"] <= a["started"] < allow["written_at"]]
    after_allow = [a for a in log if a["started"] >= allow["loaded_at"]]
    before_deny = [a for a in log if a["started"] < deny["loaded_at"]]
    first_failure = next((a for a in after_deny if not a["ok"]), None)
    first_success = next((a for a in after_allow if a["ok"]), None)
    return {
        "attempts": len(log),
        "before_deny": {"attempts": len(before_deny), "ok": sum(a["ok"] for a in before_deny)},
        "deny_file_to_pump_ms": round((deny["loaded_at"] - deny["written_at"]) * 1000, 1),
        "attempts_started_while_denied": {"attempts": len(after_deny), "ok": sum(a["ok"] for a in after_deny),
                                          "errors": sorted({a.get("error", "") for a in after_deny if not a["ok"]})},
        "first_failing_attempt_started_after_load_ms": round((first_failure["started"] - deny["loaded_at"]) * 1000) if first_failure else None,
        "allow_file_to_pump_ms": round((allow["loaded_at"] - allow["written_at"]) * 1000, 1),
        "first_success_started_after_load_ms": round((first_success["started"] - allow["loaded_at"]) * 1000) if first_success else None,
    }


def reload_open(lab: Lab, denied_s: float, observe_after_s: float = RELOAD_AFTER_S) -> dict:
    """The same change versus one long-lived TCP connection that sends every 50 ms, denied for `denied_s` seconds."""
    lab.apply(document([]))
    lab.stack.call("tcp_stream_start", name="open", host=lab.ip, port=TCP_OPEN, interval_s=OPEN_STREAM_INTERVAL_S)
    time.sleep(RELOAD_BEFORE_S)
    deny = lab.apply(document([rule("deny", f"{lab.ip}/32", "tcp", (TCP_OPEN, TCP_OPEN))]))
    time.sleep(denied_s)
    allow = lab.apply(document([]))
    time.sleep(observe_after_s)
    result = lab.stack.call("stop", name="open")
    sent, received = result["sent"], result["received"]
    echoed = {sequence for sequence, _ in received}
    sent_at = dict(sent)
    latencies = [t - sent_at[sequence] for sequence, t in received if sequence in sent_at]
    during = [t for _, t in received if deny["loaded_at"] <= t < allow["loaded_at"]]
    after = [t for _, t in received if t >= allow["loaded_at"]]
    last_before = max((t for _, t in received if t < deny["loaded_at"]), default=None)
    return {
        "sent": len(sent), "echoed": len(echoed), "every_sent_line_echoed": echoed >= set(sent_at),
        "connection_error": result["error"], "connection_closed_by_peer": result["closed"],
        "deny_file_to_pump_ms": round((deny["loaded_at"] - deny["written_at"]) * 1000, 1),
        "echoes_received_while_denied": len(during),
        "last_echo_before_deny_ms": round((deny["loaded_at"] - last_before) * 1000) if last_before else None,
        "denied_for_s": round(allow["loaded_at"] - deny["loaded_at"], 2),
        "observed_after_allow_s": observe_after_s,
        "first_echo_after_allow_s": round(min(after) - allow["loaded_at"], 2) if after else None,
        "longest_echo_latency_s": round(max(latencies), 2) if latencies else None,
        "pump_drops_by_rule": {key: value for key, value in lab.stack.pump_stats()["rule_hits"].items() if key.endswith("deny")},
    }


def port_rules(lab: Lab) -> dict:
    """Which rule shapes drop which protocol and port. Each check is one new connection or datagram."""
    network = f"{lab.ip}/32"
    cases = {
        "deny tcp 47102": [rule("deny", network, "tcp", (TCP_SHUT, TCP_SHUT))],
        "deny udp 47104": [rule("deny", network, "udp", (UDP_SHUT, UDP_SHUT))],
        "deny tcp range 47101-47102": [rule("deny", network, "tcp", (TCP_OPEN, TCP_SHUT))],
        "allow tcp 47101, then deny all of the address": [rule("allow", network, "tcp", (TCP_OPEN, TCP_OPEN)), rule("deny", network)],
        "deny all of the address, then allow tcp 47101": [rule("deny", network), rule("allow", network, "tcp", (TCP_OPEN, TCP_OPEN))],
    }
    out = {}
    for label, rules in cases.items():
        lab.apply(document(rules))
        before = lab.decisions()
        out[label] = {
            "tcp 47101": lab.check_tcp(TCP_OPEN), "tcp 47102": lab.check_tcp(TCP_SHUT), "tcp 47105": lab.check_tcp(TCP_OTHER),
            "udp 47103": lab.check_udp(UDP_OPEN), "udp 47104": lab.check_udp(UDP_SHUT),
            # nothing listens on TCP 47103: ECONNREFUSED means the SYN reached the host, a timeout means the pump dropped it
            "tcp to the UDP port 47103": lab.check_tcp(UDP_OPEN),
            "pump decisions": lab.delta(before),
        }
    return out


def fragments(lab: Lab) -> dict:
    """Fragmented UDP, before and after the capability drop. The pump's decision counters show which layer acted."""
    network = f"{lab.ip}/32"
    with_port_rule = [rule("deny", network, "udp", (UDP_SHUT, UDP_SHUT))]
    without_port_rule = [rule("deny", "192.0.2.0/24")]
    out: dict[str, dict] = {}

    def trial(label: str, steps: dict) -> None:
        before_pump, before_servers = lab.decisions(), lab.servers.snapshot()
        results = {name: fn() for name, fn in steps.items()}
        after_servers = lab.servers.snapshot()
        results["pump decisions"] = lab.delta(before_pump)
        results["datagrams that reached the echo server (udp 47103, 47104)"] = [
            after_servers["udp_datagrams"][UDP_OPEN] - before_servers["udp_datagrams"][UDP_OPEN],
            after_servers["udp_datagrams"][UDP_SHUT] - before_servers["udp_datagrams"][UDP_SHUT]]
        out[label] = results

    def raw(port: int, shape: str):
        return lambda: lab.stack.call("raw_datagram", destination=lab.ip, port=port, shape=shape)

    def udp(port: int, size: int):
        return lambda: lab.check_udp(port, size)

    lab.apply(document(with_port_rule))
    out["child before the capability drop"] = lab.stack.call("info")
    trial("A. port rule present, fragments dropped (the design)", {
        f"kernel-fragmented udp {FRAGMENTED_UDP_BYTES} B to 47103": udp(UDP_OPEN, FRAGMENTED_UDP_BYTES),
        f"unfragmented udp {SMALL_UDP_BYTES} B to 47103": udp(UDP_OPEN, SMALL_UDP_BYTES),
        "raw whole datagram to 47103": raw(UDP_OPEN, "whole"),
        "raw two fragments to 47103": raw(UDP_OPEN, "two-fragments"),
        "raw second fragment only to 47103": raw(UDP_OPEN, "second-only"),
        "raw datagram with IP options to 47103 (header 24 B)": raw(UDP_OPEN, "ip-options"),
        "raw datagram with IP options to the denied port 47104": raw(UDP_SHUT, "ip-options"),
    })
    lab.apply(document(with_port_rule, fragments="pass"))
    trial("B. port rule present, fragments passed (control: ports judged on the first fragment only)", {
        f"kernel-fragmented udp {FRAGMENTED_UDP_BYTES} B to 47103": udp(UDP_OPEN, FRAGMENTED_UDP_BYTES),
        f"kernel-fragmented udp {FRAGMENTED_UDP_BYTES} B to the denied port 47104": udp(UDP_SHUT, FRAGMENTED_UDP_BYTES),
        "raw two fragments to 47103": raw(UDP_OPEN, "two-fragments"),
        "raw two fragments to the denied port 47104": raw(UDP_SHUT, "two-fragments"),
        "raw second fragment only to 47103": raw(UDP_OPEN, "second-only"),
    })
    lab.apply(document(without_port_rule))
    trial("C. no port rule anywhere, fragments dropped only when port rules exist", {
        f"kernel-fragmented udp {FRAGMENTED_UDP_BYTES} B to 47103": udp(UDP_OPEN, FRAGMENTED_UDP_BYTES),
    })
    out["capabilities dropped"] = lab.stack.call("drop_caps")
    out["raw socket after the drop"] = lab.stack.call("raw_datagram", destination=lab.ip, port=UDP_OPEN, shape="whole")
    lab.apply(document(with_port_rule))
    trial("D. after the drop, port rule present, fragments dropped", {
        f"kernel-fragmented udp {FRAGMENTED_UDP_BYTES} B to 47103": udp(UDP_OPEN, FRAGMENTED_UDP_BYTES),
        f"unfragmented udp {SMALL_UDP_BYTES} B to 47103": udp(UDP_OPEN, SMALL_UDP_BYTES),
    })
    return out


def allow_list(lab: Lab, masks: dict) -> dict:
    """The allow-list shape of design 6.3: allow rules then a trailing `deny 0.0.0.0/0`."""
    network = f"{lab.ip}/32"
    allow_rules = [rule("allow", network, "tcp", (TCP_OPEN, TCP_OPEN)), rule("deny", "0.0.0.0/0")]
    out = {}
    lab.apply(document([]))
    resolved = lab.stack.call("dns", name=PYPI_HOST)
    pypi_ip = resolved.get("addresses", [None])[0]
    if pypi_ip:
        masks[pypi_ip] = "<pypi-ip>"
    out["default allow: dns " + PYPI_HOST] = outcome(resolved)
    if pypi_ip:
        out["default allow: tcp to pypi 443"] = outcome(lab.stack.call("tcp_connect", host=pypi_ip, port=443, timeout=PROBE_TIMEOUT_S * 3, echo=False))
    for label, setting in (("virtual subnet always allowed", "always-allow"), ("virtual subnet subject to the rules", "rules")):
        lab.apply(document(allow_rules, virtual_subnet=setting))
        out[label] = {
            "dns " + PYPI_HOST: outcome(lab.stack.call("dns", name=PYPI_HOST)),
            "tcp 47101 (allowed)": lab.check_tcp(TCP_OPEN), "tcp 47102": lab.check_tcp(TCP_SHUT), "udp 47103": lab.check_udp(UDP_OPEN),
            "tcp to pypi 443": outcome(lab.stack.call("tcp_connect", host=pypi_ip, port=443, timeout=PROBE_TIMEOUT_S, echo=False)) if pypi_ip else "no address",
        }
    return out


def ipv6(lab: Lab) -> dict:
    """Design arm: frames other than IPv4 and ARP are dropped and slirp has IPv6 off. Control arm: neither layer stops it."""
    lab.apply(document([]))
    before = lab.decisions()
    design = lab.stack.call("ipv6", public_host=PUBLIC_V6, gateway_host=SLIRP_GATEWAY_V6, port=V6_LOOPBACK_PORT)
    out = {"design arm (pump drops IPv6, slirp without --enable-ipv6)": design, "pump decisions in the design arm": lab.delta(before)}
    return out


def ipv6_control(slirp_binary: str, python: str) -> dict:
    stack = es.Stack(SCRATCH + "-v6", slirp_binary, python, document([], ipv6="pass"), enable_ipv6=True)
    try:
        stack.start()
        result = stack.call("ipv6", public_host=PUBLIC_V6, gateway_host=SLIRP_GATEWAY_V6, port=V6_LOOPBACK_PORT)
        result["pump decisions"] = {k: v for k, v in stack.pump_stats()["decisions"].items() if "ipv6" in k or k.startswith("default")}
        return result
    finally:
        stack.close()


def neighbours(lab: Lab, neighbour_ip: str, neighbour_ports: tuple[tuple[int, bool], ...], masks: dict) -> dict:
    """Passive facts, then the one neighbour that is ours: from the plain container, from the sandbox, and with a subnet rule.

    neighbour_ports: (port, echo) pairs; `echo` says the listener there is an echo server, otherwise only the connect is tried.
    """
    routes = []
    with open("/proc/net/route") as handle:
        for line in handle.read().splitlines()[1:]:
            fields = line.split()
            routes.append({"interface": fields[0], "destination": socket.inet_ntoa(struct.pack("<L", int(fields[1], 16))),
                           "gateway": socket.inet_ntoa(struct.pack("<L", int(fields[2], 16))),
                           "prefix_length": bin(int(fields[7], 16)).count("1")})
    with open("/proc/net/arp") as handle:
        arp_rows = len(handle.read().splitlines()) - 1
    subnet = next(r for r in routes if r["destination"] != "0.0.0.0" and r["prefix_length"] < 32 and r["gateway"] == "0.0.0.0")
    cidr = f"{subnet['destination']}/{subnet['prefix_length']}"
    masks[subnet["destination"]] = "<pod-subnet>"
    out = {"routes": routes, "arp_table_rows": arp_rows, "container_subnet_prefix": subnet["prefix_length"],
           "addresses_in_that_subnet": 2 ** (32 - subnet["prefix_length"]) - 2,
           "neighbour_inside_the_container_subnet": ipaddress.ip_address(neighbour_ip) in ipaddress.ip_network(cidr)}
    for port, echo in neighbour_ports:
        started = time.time()
        try:
            with socket.create_connection((neighbour_ip, port), timeout=3):
                reached = "connected"
        except OSError as exc:
            reached = type(exc).__name__ + (f" errno {exc.errno}" if exc.errno else "")
        out[f"plain container to the neighbour port {port}"] = {"result": reached, "ms": round((time.time() - started) * 1000)}
    for label, rules in (("sandbox, no rule", []), ("sandbox, deny the subnet", [rule("deny", cidr)]),
                         ("sandbox, deny only the neighbour address", [rule("deny", f"{neighbour_ip}/32")])):
        lab.apply(document(rules))
        out[label] = {f"port {port}": outcome(lab.stack.call("tcp_connect", host=neighbour_ip, port=port, timeout=PROBE_TIMEOUT_S * 2, echo=echo))
                      for port, echo in neighbour_ports}
    return out


def main() -> None:
    slirp_binary, result_path = sys.argv[1], sys.argv[2]
    neighbour = sys.argv[3:6]
    python = sys.executable
    lab = Lab(slirp_binary, python)
    gateway = next(line.split()[2] for line in open("/proc/net/route").read().splitlines()[1:] if line.split()[1] == "00000000")
    masks ={lab.ip: "<container-ip>", socket.inet_ntoa(struct.pack("<L", int(gateway, 16))): "<gateway-ip>"}
    if neighbour:
        masks[neighbour[0]] = "<neighbour-ip>"
    result: dict = {"date": time.strftime("%Y-%m-%d"), "kernel": platform.release(), "python": platform.python_version(),
                    "cpus_visible": os.cpu_count(), "slirp4netns": "v1.3.6 (sha256 checked against probe-archive)"}
    started = time.time()
    try:
        lab.stack.start()
        result["stack_start_s"] = round(time.time() - started, 2)
        result["baseline"] = {"tcp": lab.check_tcp(TCP_OPEN), "udp": lab.check_udp(UDP_OPEN), "child": lab.stack.call("info")}
        result["fragments"] = fragments(lab)
        result["reload_new_connections"] = reload_new(lab)
        result["reload_open_connection"] = {f"denied {seconds:g} s": reload_open(lab, seconds) for seconds in OPEN_CONNECTION_DENIED_S}
        result["port_rules"] = port_rules(lab)
        result["allow_list"] = allow_list(lab, masks)
        result["ipv6"] = ipv6(lab)
        if neighbour:
            result["neighbours"] = neighbours(lab, neighbour[0], ((int(neighbour[1]), True), (int(neighbour[2]), False)), masks)
    finally:
        final = lab.stack.close()
    result["pump_totals"] = {key: final.get(key) for key in ("decisions", "rule_hits", "to_slirp_frames", "to_child_frames", "errors")}
    try:
        result["ipv6_control_arm"] = ipv6_control(slirp_binary, python)
    except Exception as exc:  # the control arm is optional evidence: record why it did not run
        result["ipv6_control_arm"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        lab.servers.close()
    text = es.mask(json.dumps(result, indent=2, default=str), masks)
    with open(result_path, "w") as handle:
        handle.write(text)
    print(text)


if __name__ == "__main__":
    main()
