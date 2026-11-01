"""Spikes 6 and 7, runs on the probe app: build the no-TUN network stack of ADR 0003 and drive it. Not part of appmux.

`Stack` starts what the launcher of the design would start for one child: `unshare` (user, mount, net, pid) running
egress_child.py, slirp4netns on one end of a SEQPACKET socketpair, and egress_pump.py between that pair and the AF_PACKET socket
of the child's veth. `EchoServers` are the destinations the child talks to; they run in the driver, on the container's own address,
so the rules under test can name that address (the design's global list blocks it). Standard library only.
"""
from __future__ import annotations

import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNSHARE_FLAGS = ["--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "--kill-child"]
STAGE1_TIMEOUT_S = 15
SLIRP_READY_TIMEOUT_S = 12
SERVER_READY_TIMEOUT_S = 30
HANDOFF_TIMEOUT_S = 15
CALL_TIMEOUT_S = 120
CLOCK_TICKS_PER_SECOND = os.sysconf("SC_CLK_TCK")
UDP_ECHO_BUFFER_BYTES = 70000


def container_ip() -> str:
    """The address of the interface that carries the default route (a UDP connect sends nothing)."""
    with open("/proc/net/route") as handle:
        for line in handle.read().splitlines()[1:]:
            fields = line.split()
            if fields[1] == "00000000":
                gateway = socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.connect((gateway, 9))
                    return probe.getsockname()[0]
    raise RuntimeError("no default route in the container")


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as handle:
        fields = handle.read().rsplit(")", 1)[-1].split()
    return (int(fields[11]) + int(fields[12])) / CLOCK_TICKS_PER_SECOND


class EchoServers:
    """TCP and UDP echo on 0.0.0.0, with counters of what actually reached them."""

    def __init__(self, tcp_ports: list[int], udp_ports: list[int], v6_loopback_ports: list[int] = ()) -> None:
        self.counts = {"tcp_connections": {port: 0 for port in [*tcp_ports, *v6_loopback_ports]},
                       "udp_datagrams": {port: 0 for port in udp_ports}, "udp_bytes": {port: 0 for port in udp_ports}}
        self.sockets: list[socket.socket] = []
        for port in v6_loopback_ports:  # where the host side of slirp's IPv6 gateway address would land
            listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("::1", port))
            listener.listen(8)
            self.sockets.append(listener)
            threading.Thread(target=self._accept, args=(listener, port), daemon=True).start()
        for port in tcp_ports:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("0.0.0.0", port))
            listener.listen(64)
            self.sockets.append(listener)
            threading.Thread(target=self._accept, args=(listener, port), daemon=True).start()
        for port in udp_ports:
            datagram_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            datagram_socket.bind(("0.0.0.0", port))
            self.sockets.append(datagram_socket)
            threading.Thread(target=self._udp_echo, args=(datagram_socket, port), daemon=True).start()

    def _accept(self, listener: socket.socket, port: int) -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            self.counts["tcp_connections"][port] += 1
            threading.Thread(target=self._tcp_echo, args=(connection,), daemon=True).start()

    @staticmethod
    def _tcp_echo(connection: socket.socket) -> None:
        with connection:
            try:
                while True:
                    data = connection.recv(65536)
                    if not data:
                        return
                    connection.sendall(data)
            except OSError:
                return  # the peer reset or vanished: nothing to echo to

    def _udp_echo(self, datagram_socket: socket.socket, port: int) -> None:
        while True:
            try:
                data, peer = datagram_socket.recvfrom(UDP_ECHO_BUFFER_BYTES)
                self.counts["udp_datagrams"][port] += 1
                self.counts["udp_bytes"][port] += len(data)
                datagram_socket.sendto(data, peer)
            except OSError:
                return

    def snapshot(self) -> dict:
        return {kind: dict(values) for kind, values in self.counts.items()}

    def close(self) -> None:
        for sock in self.sockets:
            sock.close()


class Stack:
    def __init__(self, scratch: str, slirp_binary: str, python: str, rules: dict, mtu: int = 1500, enable_ipv6: bool = False,
                 disable_host_loopback: bool = True, extra_ops: str | None = None, child_nice: int = 0,
                 child_cpus: tuple[int, ...] = ()) -> None:
        self.scratch, self.python = scratch, python
        self.extra_ops, self.child_nice, self.child_cpus = extra_ops, child_nice, child_cpus
        self.slirp_binary, self.mtu = slirp_binary, mtu
        self.enable_ipv6, self.disable_host_loopback = enable_ipv6, disable_host_loopback
        self.rules_path = os.path.join(scratch, "rules.json")
        self.stats_path = os.path.join(scratch, "pump-stats.json")
        self.processes: dict[str, subprocess.Popen] = {}
        self.stream = None
        shutil.rmtree(scratch, ignore_errors=True)
        os.makedirs(scratch)
        self.write_rules(rules)

    def _path(self, name: str) -> str:
        return os.path.join(self.scratch, name)

    def write_rules(self, document: dict) -> float:
        """Replace the rules file atomically; returns the time just before the replace."""
        temporary = self._path("rules.json.tmp")
        with open(temporary, "w") as handle:
            json.dump(document, handle)
        moment = time.time()
        os.replace(temporary, self.rules_path)
        return moment

    def _wait(self, name: str, timeout_s: float, process: subprocess.Popen) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if os.path.exists(self._path(name)):
                return
            if process.poll() is not None:
                raise RuntimeError(f"child exited ({process.returncode}) before {name}: {self._log('child.log')}")
            time.sleep(0.05)
        raise RuntimeError(f"timed out waiting for {name}: {self._log('child.log')}")

    def _log(self, name: str, limit: int = 800) -> str:
        try:
            with open(self._path(name), errors="replace") as handle:
                return handle.read()[-limit:]
        except OSError:
            return "(no log)"

    def _limit_cpu(self) -> None:
        """Runs in the forked launcher before `unshare`, so the whole sandbox inherits it. A raised nice value cannot be lowered back
        without a capability; whether the sandbox can undo the affinity is what spike 7 measures."""
        if self.child_nice:
            os.nice(self.child_nice)
        if self.child_cpus:
            os.sched_setaffinity(0, self.child_cpus)

    def start(self) -> None:
        with open(self._path("config.json"), "w") as handle:
            json.dump({"mtu": self.mtu, "extra_ops": self.extra_ops}, handle)
        handoff = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        handoff.bind(self._path("fd.sock"))
        handoff.listen(1)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.scratch, "LANG": "C.UTF-8"}
        with open(self._path("child.log"), "ab") as log:
            child = subprocess.Popen(["unshare", *UNSHARE_FLAGS, "--", self.python, os.path.join(HERE, "egress_child.py"), self.scratch],
                                     stdout=log, stderr=log, env=env, start_new_session=True,
                                     preexec_fn=self._limit_cpu if (self.child_nice or self.child_cpus) else None)
        self.processes["child"] = child
        self._wait("stage1.ready", STAGE1_TIMEOUT_S, child)
        handoff.settimeout(HANDOFF_TIMEOUT_S)
        connection, _ = handoff.accept()
        try:
            _, descriptors, _, _ = socket.recv_fds(connection, 64, 1)
        finally:
            connection.close()
            handoff.close()
        packet_fd = descriptors[0]
        slirp_end, pump_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        ready_read, ready_write = os.pipe()
        command = [self.slirp_binary, "--netns-type=tapfd", f"--ready-fd={ready_write}", f"--mtu={self.mtu}"]
        if self.disable_host_loopback:
            command.append("--disable-host-loopback")
        if self.enable_ipv6:
            command.append("--enable-ipv6")
        command.append(str(slirp_end.fileno()))
        with open(self._path("slirp.log"), "ab") as slirp_log, open(self._path("pump.log"), "ab") as pump_log:
            try:
                self.processes["slirp"] = subprocess.Popen(command, stdout=slirp_log, stderr=slirp_log,
                                                            pass_fds=(slirp_end.fileno(), ready_write), start_new_session=True)
                self.processes["pump"] = subprocess.Popen(
                    [self.python, os.path.join(HERE, "egress_pump.py"), str(packet_fd), str(pump_end.fileno()), self.rules_path, self.stats_path],
                    stdout=pump_log, stderr=pump_log, pass_fds=(packet_fd, pump_end.fileno()), start_new_session=True)
            finally:
                os.close(ready_write)
                slirp_end.close()
                pump_end.close()
                os.close(packet_fd)
        readable, _, _ = select.select([ready_read], [], [], SLIRP_READY_TIMEOUT_S)
        ready = bool(readable and os.read(ready_read, 8))
        os.close(ready_read)
        if not ready:
            raise RuntimeError("slirp4netns did not report ready: " + self._log("slirp.log"))
        open(self._path("attached"), "w").close()
        self._wait("server.ready", SERVER_READY_TIMEOUT_S, child)
        control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        control.settimeout(CALL_TIMEOUT_S)
        control.connect(self._path("cmd.sock"))
        self.stream = control.makefile("rwb")
        self.control = control

    def call(self, op: str, **args: object) -> dict:
        self.stream.write(json.dumps({"op": op, "args": args}).encode() + b"\n")
        self.stream.flush()
        response = json.loads(self.stream.readline())
        if not response["ok"]:
            return {"command_error": response["error"]}
        return response["result"]

    def pump_stats(self) -> dict:
        time.sleep(0.4)  # the pump rewrites its stats file every 0.25 s
        with open(self.stats_path) as handle:
            return json.load(handle)

    def pids(self) -> dict[str, int]:
        return {name: process.pid for name, process in self.processes.items()}

    def close(self) -> dict:
        final = {}
        try:
            if self.stream:
                self.stream.write(b'{"op": "quit"}\n')
                self.stream.flush()
                self.stream.close()
                self.control.close()
        except OSError:
            pass  # the child may already be gone; teardown continues
        pump = self.processes.get("pump")
        if pump and pump.poll() is None:
            pump.send_signal(signal.SIGTERM)  # the pump writes its final stats before it exits
            try:
                pump.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # killed below
        try:
            with open(self.stats_path) as handle:
                final = json.load(handle)
        except (OSError, ValueError):
            pass  # no stats file: the run failed before the pump started
        for process in self.processes.values():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass  # already gone
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # SIGKILL is pending; nothing more to do
        return final


def mask(text: str, replacements: dict[str, str]) -> str:
    for value, placeholder in replacements.items():
        text = text.replace(value, placeholder)
    return text


if __name__ == "__main__":
    sys.exit("egress_stack.py is a library: run egress_lab.py")
