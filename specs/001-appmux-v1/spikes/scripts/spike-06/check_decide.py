"""Spike 6, runs anywhere (no namespaces needed): assertions on the pump's decide() for the rule shapes the lab exercises.
Run from this directory: python3 -I check_decide.py. 198.51.100.7 is a documentation address standing in for the container.
"""
import os
import socket
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import egress_pump as p

def frame(dst, proto=17, dport=1000, flags_offset=0, ihl=5, payload=b"x" * 16, ethertype=b"\x08\x00"):
    l4 = struct.pack("!HH", 40000, dport) + payload if proto in (6, 17) else payload
    header = struct.pack("!BBHHHBBH4s4s", (4 << 4) | ihl, 0, ihl * 4 + len(l4), 1, flags_offset, 64, proto, 0,
                         socket.inet_aton("10.0.2.100"), socket.inet_aton(dst)) + b"\x01" * ((ihl - 5) * 4)
    return b"\xff" * 6 + b"\xaa" * 6 + ethertype + header + l4

def rs(**kw):
    base = {"ipv6": "drop", "fragments": "drop-if-port-rules", "default": "allow", "rules": []}
    base.update(kw)
    return p.Ruleset(base)

C = "198.51.100.7"
r = rs(rules=[{"action": "deny", "cidr": C + "/32", "proto": "tcp", "ports": [47101, 47102]}])
assert p.decide(frame(C, 6, 47101), r) == (False, "rule:0:deny")
assert p.decide(frame(C, 6, 47102), r) == (False, "rule:0:deny")
assert p.decide(frame(C, 6, 47103), r) == (True, "default:allow")
assert p.decide(frame(C, 17, 47101), r) == (True, "default:allow"), "protocol-specific rule must not match udp"
assert p.decide(frame("8.8.8.8", 6, 47101), r)[0]
# IP options shift the ports
assert p.decide(frame(C, 6, 47101, ihl=6), r) == (False, "rule:0:deny")
# fragments: dropped when port rules exist, otherwise judged by CIDR only
assert p.decide(frame(C, 17, 9, flags_offset=0x2000), r) == (False, "fragment")
assert p.decide(frame(C, 17, 9, flags_offset=0x0002), r) == (False, "fragment")
r_pass = rs(fragments="pass", rules=r.items and [{"action": "deny", "cidr": C + "/32", "proto": "udp", "ports": [47104, 47104]}])
assert p.decide(frame(C, 17, 47104, flags_offset=0x2000), r_pass) == (False, "rule:0:deny"), "first fragment still judged by port"
assert p.decide(frame(C, 17, 47104, flags_offset=0x0002), r_pass)[0], "non-first fragment has no port: passes in the naive mode"
r_cidr = rs(rules=[{"action": "deny", "cidr": C + "/32"}])
assert p.decide(frame(C, 17, 9, flags_offset=0x2000), r_cidr) == (False, "rule:0:deny"), "no port rules: fragments judged by CIDR"
# allow-list with a trailing deny; the virtual subnet stays open
r_allow = rs(rules=[{"action": "allow", "cidr": C + "/32", "proto": "tcp", "ports": [47101, 47101]}, {"action": "deny", "cidr": "0.0.0.0/0"}])
assert p.decide(frame("10.0.2.3", 17, 53), r_allow) == (True, "virtual-subnet")
assert p.decide(frame(C, 6, 47101), r_allow) == (True, "rule:0:allow")
assert p.decide(frame(C, 6, 47102), r_allow) == (False, "rule:1:deny")
assert p.decide(frame("1.1.1.1", 6, 443), r_allow) == (False, "rule:1:deny")
r_nov = rs(virtual_subnet="rules", rules=r_allow.items and [{"action": "deny", "cidr": "0.0.0.0/0"}])
assert p.decide(frame("10.0.2.3", 17, 53), r_nov) == (False, "rule:0:deny")
# ipv6 / non-ip / arp / malformed
assert p.decide(b"\xff" * 12 + b"\x86\xdd" + b"\0" * 40, r) == (False, "ipv6")
assert p.decide(b"\xff" * 12 + b"\x86\xdd" + b"\0" * 40, rs(ipv6="pass")) == (True, "ipv6-pass")
assert p.decide(b"\xff" * 12 + b"\x08\x06" + b"\0" * 28, r) == (True, "arp")
assert p.decide(b"\xff" * 12 + b"\x81\x00" + b"\0" * 40, r) == (False, "non-ip")
assert p.decide(frame(C, 6, 1)[:-6], r) == (False, "malformed"), "truncated frame"
assert p.decide(b"\0" * 5, r) == (False, "runt")
print("decide: all assertions hold")
