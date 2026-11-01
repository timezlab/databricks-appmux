"""Spikes 6 and 7, runs inside the sandbox namespaces (user, mount, net, pid) that egress_stack.py creates with `unshare`.
Not part of appmux.

Builds the veth pair with the helpers of the spike 2 prototype (`sandbox_child.py`: rtnetlink, AF_PACKET handed to the parent over
SCM_RIGHTS), then serves JSON-line commands on a unix socket until told to stop, so the driver can run one network test at a time
and flip the pump's rules between them. Commands that need root (raw sockets, crafted fragments) work only before `drop_caps`;
everything else runs as the hostile child would, with no capability.

usage: egress_child.py <scratch-dir>      (the directory holds config.json; the driver and the child meet there)
config.json may name an `extra_ops` file: its COMMANDS join this file's (spike 7 adds its load, fork and build commands that way).
Standard library only. Needs `sandbox_child.py` of probe-archive/appmux-probe2 next to this file or in that archive directory.
"""
from __future__ import annotations

import errno
import importlib.util
import json
import os
import random
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))  # `python -I` is not used here: the child needs its sibling module
for candidate in (HERE, os.path.join(HERE, "..", "..", "probe-archive", "appmux-probe2")):
    sys.path.insert(0, candidate)
import sandbox_child as base  # helpers only: its main() does not run on import

SCRATCH = sys.argv[1]
ATTACH_TIMEOUT_S = 60
CONNECT_TIMEOUT_S = 3.0
UDP_REPLY_TIMEOUT_S = 1.5
DNS_QUERY_ID = 0x5A5A
RAW_PROTOCOL_UDP = 17
IP_HEADER_BYTES = 20
FRAGMENT_UNIT_BYTES = 8
MORE_FRAGMENTS = 0x2000
IP_OPTION_NOP_X4 = b"\x01\x01\x01\x01"
background: dict[str, dict] = {}


def classify(exc: BaseException) -> str:
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, OSError) and exc.errno:
        return errno.errorcode.get(exc.errno, str(exc.errno))
    return f"{type(exc).__name__}"


def tcp_echo_once(host: str, port: int, timeout: float, family: int = socket.AF_INET, echo: bool = True) -> dict:
    """Connect, and when `echo` is set also send a line and wait for it to come back (the server is an echo)."""
    started = time.time()
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            reached = True
            if echo:
                sock.sendall(b"ping\n")
                reached = sock.recv(16) == b"ping\n"
        return {"ok": reached, "ms": round((time.time() - started) * 1000), "started": started}
    except OSError as exc:
        return {"ok": False, "error": classify(exc), "ms": round((time.time() - started) * 1000), "started": started}


def op_tcp_connect(host: str, port: int, timeout: float = CONNECT_TIMEOUT_S, echo: bool = True) -> dict:
    return tcp_echo_once(host, port, timeout, echo=echo)


def op_tcp_probe_start(name: str, host: str, port: int, interval_s: float, timeout: float) -> dict:
    """Connect-and-echo in a loop; the timeline shows when a rule change starts to apply to new connections."""
    entry = {"stop": threading.Event(), "log": []}
    background[name] = entry

    def loop() -> None:
        while not entry["stop"].is_set():
            attempt = tcp_echo_once(host, port, timeout)
            entry["log"].append({key: attempt[key] for key in ("started", "ok", "ms", "error") if key in attempt})
            entry["stop"].wait(max(0.0, interval_s - attempt["ms"] / 1000))

    entry["thread"] = threading.Thread(target=loop, daemon=True)
    entry["thread"].start()
    return {"started": name}


def op_tcp_stream_start(name: str, host: str, port: int, interval_s: float) -> dict:
    """One long-lived connection that sends a numbered line every interval and records every echo with its time."""
    entry = {"stop": threading.Event(), "sent": [], "received": [], "error": None, "closed": False}
    background[name] = entry
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S)
    sock.settimeout(None)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    entry["sock"] = sock

    def send_loop() -> None:
        sequence = 0
        try:
            while not entry["stop"].is_set():
                sock.sendall(f"{sequence}\n".encode())
                entry["sent"].append((sequence, time.time()))
                sequence += 1
                entry["stop"].wait(interval_s)
        except OSError as exc:
            entry["error"] = f"send: {classify(exc)}"

    def receive_loop() -> None:
        pending = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    entry["closed"] = not entry["stop"].is_set()  # a close after our own stop is not the peer's doing
                    return
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    entry["received"].append((int(line), time.time()))
        except OSError as exc:
            if not entry["stop"].is_set():
                entry["error"] = f"recv: {classify(exc)}"

    entry["threads"] = [threading.Thread(target=send_loop, daemon=True), threading.Thread(target=receive_loop, daemon=True)]
    for thread in entry["threads"]:
        thread.start()
    return {"started": name}


def op_stop(name: str) -> dict:
    entry = background.pop(name)
    entry["stop"].set()
    if "sock" in entry:
        try:
            entry["sock"].shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already reset or closed; the timeline is what matters
        entry["sock"].close()
    for thread in entry.get("threads", [entry.get("thread")]):
        if thread:
            thread.join(timeout=5)
    return {key: value for key, value in entry.items() if key in ("log", "sent", "received", "error", "closed")}


def op_udp_echo(host: str, port: int, size: int, count: int = 1, timeout: float = UDP_REPLY_TIMEOUT_S) -> dict:
    replies = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        for index in range(count):
            payload = (f"{index}:".encode() + b"u" * size)[:size]
            sock.sendto(payload, (host, port))
            try:
                data, _ = sock.recvfrom(70000)
                replies.append({"bytes": len(data), "intact": data == payload})
            except OSError as exc:
                replies.append({"error": classify(exc)})
    return {"sent": count, "size": size, "replies": replies}


def op_dns(name: str, server: str = "10.0.2.3") -> dict:
    """One raw A query to slirp's DNS; returns the addresses answered."""
    query = struct.pack("!HHHHHH", DNS_QUERY_ID, 0x0100, 1, 0, 0, 0)
    query += b"".join(bytes([len(part)]) + part.encode() for part in name.split(".")) + b"\0" + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET6 if ":" in server else socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        try:
            sock.sendto(query, (server, 53))
            data, _ = sock.recvfrom(2048)
        except OSError as exc:
            return {"ok": False, "error": classify(exc)}
    answers, offset = struct.unpack_from("!H", data, 6)[0], len(query)
    addresses = []
    for _ in range(answers):
        offset = offset + 2 if data[offset] & 0xC0 == 0xC0 else data.index(0, offset) + 1  # name: a pointer or labels ending in 0
        record_type, _, _, length = struct.unpack_from("!HHIH", data, offset)
        offset += 10
        if record_type == 1 and length == 4:
            addresses.append(socket.inet_ntoa(data[offset:offset + 4]))
        offset += length
    return {"ok": bool(addresses), "addresses": addresses}


def ip_packet(source: str, destination: str, identification: int, offset_units: int, more: bool, payload: bytes, options: bytes = b"") -> bytes:
    """IPv4 header for a raw socket with IP_HDRINCL: the kernel fills the total length and the header checksum."""
    words = 5 + len(options) // 4
    flags_and_offset = (MORE_FRAGMENTS if more else 0) | offset_units
    header = struct.pack("!BBHHHBBH4s4s", (4 << 4) | words, 0, 0, identification, flags_and_offset, 64, RAW_PROTOCOL_UDP, 0,
                         socket.inet_aton(source), socket.inet_aton(destination))
    return header + options + payload


def op_raw_datagram(destination: str, port: int, shape: str) -> dict:
    """Send one hand-built UDP datagram to an echo server and report whether the echo came back (needs CAP_NET_RAW).

    shape: "whole" one packet; "two-fragments" the datagram split after 16 bytes; "second-only" only the non-first fragment;
    "ip-options" one packet whose header is 24 bytes, which moves the UDP header.
    """
    reply_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reply_socket.bind((base.CHILD_IP, 0))
    reply_socket.settimeout(UDP_REPLY_TIMEOUT_S)
    source_port = reply_socket.getsockname()[1]
    body = b"raw-datagram-" + b"r" * 19  # 32 bytes
    datagram = struct.pack("!HHHH", source_port, port, 8 + len(body), 0) + body  # UDP checksum 0 = not used (IPv4)
    identification = random.randint(1, 65000)
    raw = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
    raw.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
    try:
        if shape == "whole":
            packets = [ip_packet(base.CHILD_IP, destination, identification, 0, False, datagram)]
        elif shape == "ip-options":
            packets = [ip_packet(base.CHILD_IP, destination, identification, 0, False, datagram, IP_OPTION_NOP_X4)]
        else:
            split = 2 * FRAGMENT_UNIT_BYTES
            first = ip_packet(base.CHILD_IP, destination, identification, 0, True, datagram[:split])
            second = ip_packet(base.CHILD_IP, destination, identification, split // FRAGMENT_UNIT_BYTES, False, datagram[split:])
            packets = [second] if shape == "second-only" else [first, second]
        for packet in packets:
            raw.sendto(packet, (destination, 0))
        try:
            data, _ = reply_socket.recvfrom(2048)
            return {"packets_sent": len(packets), "echo": data == body}
        except OSError as exc:
            return {"packets_sent": len(packets), "echo": False, "error": classify(exc)}
    finally:
        raw.close()
        reply_socket.close()


def route_lookup_v6(host: str) -> dict:
    """Connecting a UDP socket sends nothing but makes the kernel pick a route and a source address."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
            sock.connect((host, 9))
            return {"ok": True, "source_prefix": sock.getsockname()[0][:6]}
    except OSError as exc:
        return {"ok": False, "error": classify(exc)}


def op_ipv6(public_host: str, gateway_host: str, port: int, settle_s: float = 3.0) -> dict:
    """What the child sees of IPv6 and what its attempts do."""
    time.sleep(settle_s)  # router advertisements, if any slirp sends, arrive in the first seconds
    out = {"if_inet6": base.read_text("/proc/net/if_inet6", 2000).split("\n")[:8],
           # destination, prefix length, next hop, metric, flags, interface of every route (flags 0200 = reject)
           "ipv6_route": [[f[0], f[1], f[4], f[5], f[8], f[9]] for f in (l.split() for l in base.read_text("/proc/net/ipv6_route", 8000).splitlines())]}
    for label, host in (("public", public_host), ("slirp_gateway", gateway_host)):
        out[label] = tcp_echo_once(host, port, CONNECT_TIMEOUT_S, socket.AF_INET6, echo=False)
        out[label + " (route lookup)"] = route_lookup_v6(host)
    out["sysctl"] = {name: base.read_text(f"/proc/sys/net/ipv6/conf/{name}", 20).strip()
                     for name in ("all/disable_ipv6", "eth0/disable_ipv6", "eth0/accept_ra", "eth0/forwarding", "eth0/autoconf")}
    out["dns_over_ipv6_to_slirp"] = op_dns("pypi.org", server="fd00::3")
    return out


def op_info() -> dict:
    status = {line.split(":")[0]: line.split(":")[1].strip() for line in base.read_text("/proc/self/status", 4000).splitlines() if ":" in line}
    return {"uid": os.getuid(), "cap_eff": status.get("CapEff"), "interface_ipv4": base.interface_ipv4(base.CHILD_IFACE),
            "default_route": base.default_route(), "pid": os.getpid()}


def op_drop_caps() -> dict:
    base.drop_capabilities()
    return op_info()


COMMANDS = {
    "tcp_connect": op_tcp_connect, "tcp_probe_start": op_tcp_probe_start, "tcp_stream_start": op_tcp_stream_start, "stop": op_stop,
    "udp_echo": op_udp_echo, "dns": op_dns, "raw_datagram": op_raw_datagram, "ipv6": op_ipv6, "info": op_info, "drop_caps": op_drop_caps,
}


def serve(listener: socket.socket) -> None:
    connection, _ = listener.accept()
    with connection, connection.makefile("rwb") as stream:
        for line in stream:
            request = json.loads(line)
            if request["op"] == "quit":
                return
            try:
                response = {"ok": True, "result": COMMANDS[request["op"]](**request.get("args", {}))}
            except Exception as exc:  # a failed command is a finding for the driver, not a reason to stop serving
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            stream.write(json.dumps(response).encode() + b"\n")
            stream.flush()


def wait_for_file(name: str, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(os.path.join(SCRATCH, name)):
            return True
        time.sleep(0.05)
    return False


def load_extra_ops(path: str) -> dict:
    spec = importlib.util.spec_from_file_location("extra_ops", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.COMMANDS


def main() -> None:
    with open(os.path.join(SCRATCH, "config.json")) as handle:
        config = json.load(handle)
    if config.get("extra_ops"):
        COMMANDS.update(load_extra_ops(config["extra_ops"]))
    base.set_loopback_up()
    base.setup_veth(config)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(os.path.join(SCRATCH, "cmd.sock"))
    listener.listen(1)
    open(os.path.join(SCRATCH, "stage1.ready"), "w").close()
    if not wait_for_file("attached", ATTACH_TIMEOUT_S):
        sys.exit("the driver never attached the NAT")
    base.wait_for_network()
    open(os.path.join(SCRATCH, "server.ready"), "w").close()
    serve(listener)


if __name__ == "__main__":
    main()
