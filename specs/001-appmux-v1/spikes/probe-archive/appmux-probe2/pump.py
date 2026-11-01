"""Probe v2.1 frame pump. Not part of appmux.

Copies Ethernet frames between two inherited file descriptors (optionally
filtering what the child may send, argv[3] = JSON {"deny": [cidr], "allow": [cidr]}):
  argv[1]  AF_PACKET socket bound to the child's veth peer (opened inside the
           child's network namespace, handed over with SCM_RIGHTS)
  argv[2]  SOCK_SEQPACKET end of a socketpair whose other end is slirp4netns
           (--netns-type=tapfd), which reads and writes one frame per call
One blocking thread per direction: a single loop could deadlock when both
sockets fill at once. Prints a stats line on exit. Stdlib only.
"""
import ipaddress
import json
import os
import signal
import socket
import sys
import threading
import time

PACKET_OUTGOING = 4  # frames this host sent itself; re-injecting them would loop
FRAME_BUFFER_BYTES = 70000  # larger than the 65520-byte MTU scenario plus the Ethernet header

ETHERTYPE_ARP, ETHERTYPE_IPV4 = b"\x08\x06", b"\x08\x00"
MAX_SAMPLED_DESTINATIONS = 20

stats = {"to_slirp_frames": 0, "to_slirp_bytes": 0, "to_child_frames": 0, "to_child_bytes": 0, "errors": [],
         "filter": None, "dropped": {}, "dropped_destination_sample": []}
deny_ranges = []   # (network int, mask int)
allow_ranges = []


def cidr_ranges(cidrs):
    ranges = []
    for cidr in cidrs:
        net = ipaddress.ip_network(cidr)
        ranges.append((int(net.network_address), int(net.netmask)))
    return ranges


def drop_reason(frame):
    """None = forward. Applies only when a filter is configured; fails closed on anything unparseable."""
    if stats["filter"] is None:
        return None
    if len(frame) < 14:
        return "runt"
    ethertype = frame[12:14]
    if ethertype == ETHERTYPE_ARP:
        return None
    if ethertype != ETHERTYPE_IPV4:
        return "non-ipv4"
    if len(frame) < 34:
        return "short-ip"
    destination = int.from_bytes(frame[30:34], "big")
    if any(destination & mask == net for net, mask in allow_ranges):
        return None
    if any(destination & mask == net for net, mask in deny_ranges):
        if len(stats["dropped_destination_sample"]) < MAX_SAMPLED_DESTINATIONS:
            text = str(ipaddress.ip_address(destination))
            if text not in stats["dropped_destination_sample"]:
                stats["dropped_destination_sample"].append(text)
        return "denied-destination"
    return None


def to_slirp(packet, slirp):
    while True:
        frame, address = packet.recvfrom(FRAME_BUFFER_BYTES)
        if address[2] == PACKET_OUTGOING:
            continue
        reason = drop_reason(frame)
        if reason:
            stats["dropped"][reason] = stats["dropped"].get(reason, 0) + 1
            continue
        slirp.send(frame)
        stats["to_slirp_frames"] += 1
        stats["to_slirp_bytes"] += len(frame)


def to_child(packet, slirp):
    while True:
        frame = slirp.recv(FRAME_BUFFER_BYTES)
        if not frame:
            return
        packet.send(frame)
        stats["to_child_frames"] += 1
        stats["to_child_bytes"] += len(frame)


def run(direction, fn, packet, slirp):
    try:
        fn(packet, slirp)
    except OSError as e:  # the peer closing is the normal way to stop
        stats["errors"].append(f"{direction}: {type(e).__name__}: {e}")


def main():
    packet = socket.socket(fileno=int(sys.argv[1]))
    slirp = socket.socket(fileno=int(sys.argv[2]))
    if len(sys.argv) > 3:
        policy = json.loads(sys.argv[3])
        deny_ranges.extend(cidr_ranges(policy["deny"]))
        allow_ranges.extend(cidr_ranges(policy["allow"]))
        stats["filter"] = policy
    started = time.time()

    def report_and_exit(*_):
        stats["seconds"] = round(time.time() - started, 1)
        print("PUMP-STATS " + json.dumps(stats), flush=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, report_and_exit)
    threads = [
        threading.Thread(target=run, args=("to_slirp", to_slirp, packet, slirp), daemon=True),
        threading.Thread(target=run, args=("to_child", to_child, packet, slirp), daemon=True),
    ]
    for thread in threads:
        thread.start()
    threads[1].join()
    report_and_exit()


if __name__ == "__main__":
    main()
