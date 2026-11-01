"""Spikes 6 and 7, runs on the probe app: the frame pump with a rule-engine stub. Not part of appmux.

Copies Ethernet frames between two inherited descriptors like the spike 2 pump and decides, per frame the child sends, whether it
leaves. The rules come from a JSON file that a poll thread reloads when it changes, so a rule change reaches open and new
connections alike with no restart. Standard library only.

usage: egress_pump.py <packet-fd> <slirp-fd> <rules-file> <stats-file>
  <packet-fd>  AF_PACKET socket bound to the child's veth peer, opened inside the child's network namespace
  <slirp-fd>   SOCK_SEQPACKET end of the socketpair whose other end is slirp4netns (--netns-type=tapfd)
rules file:
  {"ipv6": "drop" | "pass",                    non-IPv4 frames (ARP always passes)
   "fragments": "drop-if-port-rules" | "pass", IP fragments; "pass" checks ports on the first fragment only
   "virtual_subnet": "always-allow" | "rules", whether 10.0.2.0/24 skips the rules (the design: always allowed)
   "default": "allow" | "deny",
   "rules": [{"action": "allow" | "deny", "cidr": "10.0.2.2/32", "proto": "tcp" | "udp" | "any", "ports": [low, high] | null}]}
Rules are ordered and the first match wins. The stats file is rewritten every STATS_INTERVAL_S and on SIGTERM.
"""
from __future__ import annotations

import collections
import ipaddress
import json
import os
import signal
import socket
import sys
import threading
import time

PACKET_OUTGOING = 4  # frames this host sent itself; re-injecting them would loop
FRAME_BUFFER_BYTES = 70000
RELOAD_POLL_S = 0.01
STATS_INTERVAL_S = 0.25
RECENT_DROPS_KEPT = 60

ETHERTYPE_ARP, ETHERTYPE_IPV4, ETHERTYPE_IPV6 = b"\x08\x06", b"\x08\x00", b"\x86\xdd"
ETHERNET_HEADER_BYTES = 14
IPV4_MIN_HEADER_BYTES = 20
PROTOCOLS = {"tcp": 6, "udp": 17, "any": None}
FRAGMENT_BITS = 0x3FFF  # MF flag plus the 13-bit offset
FRAGMENT_OFFSET_BITS = 0x1FFF
VIRTUAL_SUBNET = ipaddress.ip_network("10.0.2.0/24")  # slirp's own gateway and DNS: the far end of the cable


class Ruleset:
    """Compiled rules; replaced as a whole on reload so a frame never sees half of an update."""

    def __init__(self, document: dict) -> None:
        self.ipv6 = document.get("ipv6", "drop")
        self.fragments = document.get("fragments", "drop-if-port-rules")
        self.virtual_subnet_allowed = document.get("virtual_subnet", "always-allow") == "always-allow"
        self.default_allows = document.get("default", "allow") == "allow"
        self.items: list[tuple[bool, int, int, int | None, tuple[int, int] | None]] = []
        for rule in document.get("rules", []):
            network = ipaddress.ip_network(rule["cidr"])
            ports = tuple(rule["ports"]) if rule.get("ports") else None
            self.items.append((rule["action"] == "allow", int(network.network_address), int(network.netmask),
                               PROTOCOLS[rule.get("proto", "any")], ports))
        self.has_port_rules = any(item[4] is not None for item in self.items)
        self.virtual_net = (int(VIRTUAL_SUBNET.network_address), int(VIRTUAL_SUBNET.netmask))


state = {"rules": Ruleset({}), "generation": 0}
counters: collections.Counter = collections.Counter()
rule_hits: collections.Counter = collections.Counter()
recent_drops: collections.deque = collections.deque(maxlen=RECENT_DROPS_KEPT)
reloads: list[dict] = []
traffic = {"to_slirp_frames": 0, "to_slirp_bytes": 0, "to_child_frames": 0, "to_child_bytes": 0, "errors": []}
started_at = time.time()


def decide(frame: bytes, rules: Ruleset) -> tuple[bool, str]:
    """(forward?, reason). Anything that does not parse is dropped: this is a safety path."""
    if len(frame) < ETHERNET_HEADER_BYTES:
        return False, "runt"
    ethertype = frame[12:14]
    if ethertype == ETHERTYPE_ARP:
        return True, "arp"
    if ethertype == ETHERTYPE_IPV6:
        return (True, "ipv6-pass") if rules.ipv6 == "pass" else (False, "ipv6")
    if ethertype != ETHERTYPE_IPV4:
        return False, "non-ip"
    if len(frame) < ETHERNET_HEADER_BYTES + IPV4_MIN_HEADER_BYTES or frame[14] >> 4 != 4:
        return False, "malformed"
    header_bytes = (frame[14] & 0x0F) * 4
    total_bytes = int.from_bytes(frame[16:18], "big")
    if header_bytes < IPV4_MIN_HEADER_BYTES or total_bytes < header_bytes or ETHERNET_HEADER_BYTES + total_bytes > len(frame):
        return False, "malformed"
    destination = int.from_bytes(frame[30:34], "big")
    if rules.virtual_subnet_allowed and destination & rules.virtual_net[1] == rules.virtual_net[0]:
        return True, "virtual-subnet"
    protocol = frame[23]
    flags_and_offset = int.from_bytes(frame[20:22], "big")
    if flags_and_offset & FRAGMENT_BITS and rules.has_port_rules and rules.fragments == "drop-if-port-rules":
        return False, "fragment"
    # Ports are only visible in the first fragment, and only for TCP and UDP.
    port_at = ETHERNET_HEADER_BYTES + header_bytes + 2
    can_read_port = (not flags_and_offset & FRAGMENT_OFFSET_BITS and protocol in (6, 17)
                     and ETHERNET_HEADER_BYTES + total_bytes >= port_at + 2)
    destination_port = int.from_bytes(frame[port_at:port_at + 2], "big") if can_read_port else None
    for index, (allow, network, mask, rule_protocol, ports) in enumerate(rules.items):
        if destination & mask != network:
            continue
        if rule_protocol is not None and rule_protocol != protocol:
            continue
        if ports is not None and (destination_port is None or not ports[0] <= destination_port <= ports[1]):
            continue
        return allow, f"rule:{index}:{'allow' if allow else 'deny'}"
    return rules.default_allows, f"default:{'allow' if rules.default_allows else 'deny'}"


def to_slirp(packet: socket.socket, slirp: socket.socket) -> None:
    while True:
        frame, address = packet.recvfrom(FRAME_BUFFER_BYTES)
        if address[2] == PACKET_OUTGOING:
            continue
        forward, reason = decide(frame, state["rules"])
        counters[reason] += 1
        if reason.startswith("rule:"):
            rule_hits[reason] += 1
        if not forward:
            recent_drops.append((round(time.time(), 3), reason, frame[23] if len(frame) > 23 else None,
                                 socket.inet_ntoa(frame[30:34]) if len(frame) >= 34 and frame[12:14] == ETHERTYPE_IPV4 else None))
            continue
        slirp.send(frame)
        traffic["to_slirp_frames"] += 1
        traffic["to_slirp_bytes"] += len(frame)


def to_child(packet: socket.socket, slirp: socket.socket) -> None:
    while True:
        frame = slirp.recv(FRAME_BUFFER_BYTES)
        if not frame:
            return
        packet.send(frame)
        traffic["to_child_frames"] += 1
        traffic["to_child_bytes"] += len(frame)


def run(direction: str, work, packet: socket.socket, slirp: socket.socket) -> None:
    try:
        work(packet, slirp)
    except OSError as exc:  # the peer closing is the normal way to stop
        traffic["errors"].append(f"{direction}: {type(exc).__name__}: {exc}")


def watch_rules(path: str) -> None:
    """Reload when the file is replaced or rewritten. A bad file keeps the previous rules and is recorded."""
    seen = None
    while True:
        try:
            stat = os.stat(path)
            signature = (stat.st_mtime_ns, stat.st_ino, stat.st_size)
            if signature != seen:
                seen = signature
                with open(path) as handle:
                    document = json.load(handle)
                state["rules"] = Ruleset(document)
                state["generation"] += 1
                reloads.append({"generation": state["generation"], "loaded_at": round(time.time(), 4), "ok": True})
        except (OSError, ValueError, KeyError) as exc:  # a half-written or invalid file: keep the old rules, record why
            reloads.append({"generation": state["generation"], "loaded_at": round(time.time(), 4), "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(RELOAD_POLL_S)


def snapshot() -> dict:
    return {"seconds": round(time.time() - started_at, 2), "generation": state["generation"], "decisions": dict(counters),
            "rule_hits": dict(rule_hits), "recent_drops": list(recent_drops), "reloads": list(reloads), **traffic}


def write_stats(path: str) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(snapshot(), handle)
    os.replace(temporary, path)


def main() -> None:
    packet = socket.socket(fileno=int(sys.argv[1]))
    slirp = socket.socket(fileno=int(sys.argv[2]))
    rules_path, stats_path = sys.argv[3], sys.argv[4]

    def report_and_exit(*_: object) -> None:
        write_stats(stats_path)
        os._exit(0)

    signal.signal(signal.SIGTERM, report_and_exit)
    threading.Thread(target=watch_rules, args=(rules_path,), daemon=True).start()
    workers = [threading.Thread(target=run, args=("to_slirp", to_slirp, packet, slirp), daemon=True),
               threading.Thread(target=run, args=("to_child", to_child, packet, slirp), daemon=True)]
    for worker in workers:
        worker.start()
    while workers[1].is_alive():
        write_stats(stats_path)
        time.sleep(STATS_INTERVAL_S)
    report_and_exit()


if __name__ == "__main__":
    main()
