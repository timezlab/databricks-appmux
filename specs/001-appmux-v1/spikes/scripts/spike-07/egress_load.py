"""Spike 7, runs on the probe app: throughput, hogs, a bounded fork test, disk fill and a compiler build, all through the sandbox
of spike 6. Not part of appmux.

Sections (all by default; name some to run only those):
  ceiling      how much CPU time the container can use: 1 to 8 busy loops, and 4 of them for 30 s
  bulk         a bulk download and upload through the pump, and a real HTTPS download from the Internet
  connections  1, 10 and 100 ping-pong connections from the sandbox through the pump
  edge         1, 10 and 100 HTTP connections through an edge stand-in and the relay to a trivial child, with and without the sandbox,
               and 10 and 100 from two load generators to see whether one generator was the limit
  hog          the same edge load, closed loop and at a fixed rate, next to a sandbox that runs 4 busy loops: at nice 0, at nice 19,
               at nice 19 pinned to one CPU, and pinned with hogs that try to widen their own pin, with and without a seccomp filter
               that refuses `sched_setaffinity`; and whether the sandbox can undo nice and the pin from inside
  idle         1 and 10 idle sandboxes: CPU they use doing nothing, and the memory each one holds
  fork         a sandbox forks 2000 processes; the driver ends it by killing the namespace's init, once with the container's PID 1 as the
               orphans' parent and once with the driver as subreaper; and the same fork under RLIMIT_NPROC
  disk         a sandbox fills a size-capped tmpfs, then a host directory
  build        pip builds a source distribution with a C extension in the sandbox: offline, through the pump, with no egress, and with
               a resolv.conf that names slirp's resolver
  build_resolver  only the last build arm (egress allowed, resolv.conf names slirp's resolver)

usage: egress_load.py <slirp4netns-binary> <result-json> [section ...]
Needs /tmp/spike6 (spike 6 files and bin/slirp4netns) and this directory. Runs as the app user. Addresses are masked in the result.
"""
from __future__ import annotations

import ctypes
import io
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from typing import Callable

HERE = os.path.dirname(os.path.abspath(__file__))
SPIKE6_DIR = "/tmp/spike6"
sys.path.insert(0, SPIKE6_DIR)
import egress_stack as es

PYTHON = sys.executable
SCRATCH_PREFIX = "/dev/shm/spike7"
RUN_DIR = "/dev/shm/spike7-run"
HOST_DIR = "/tmp/spike7-hostdir"
BUILD_DIR = "/tmp/spike7-build"
ECHO_PORT, BULK_PORT = 47301, 47302
EDGE_PORT, EDGE_DIRECT_PORT, CHILD_PORT = 47310, 47311, 47320
BULK_MEGABYTES = 256
HTTPS_HOST, HTTPS_PATH, HTTPS_LIMIT_MB = "pypi.org", "/simple/", 64
ECHO_MESSAGE_BYTES = 1024
LOAD_SECONDS = 10.0
CONNECTION_COUNTS = (1, 10, 100)
LOAD_GENERATOR_ARMS = ((1, CONNECTION_COUNTS), (2, (10, 100)))  # (generator processes, connection counts)
VICTIM_CONNECTIONS = 10
VICTIM_RATE_PER_S = 500
HOG_PROCESSES = 4
HOG_CPU = 3
IDLE_STACK_COUNTS = (1, 10)
IDLE_SETTLE_S = 3.0
IDLE_SECONDS = 10.0
CEILING_ARMS = ((1, 6), (2, 6), (3, 6), (4, 6), (8, 6), (4, 30))  # (busy loops, seconds)
PR_SET_CHILD_SUBREAPER = 36  # linux/prctl.h
TEARDOWN_WAIT_S = 30
FORK_COUNT = 2000
FORK_HOLD_S = 12.0
NPROC_LIMIT = 300
TMPFS_MB = 64
HOST_FILL_MB = 1024
FILE_LIMIT_MB = 8
SETUPTOOLS_PIN = "setuptools==84.1.0"
BUILD_CALL_TIMEOUT_S = 330  # a little over the 300 s limit load_ops.py puts on pip itself
READY_TIMEOUT_S = 15
MEMINFO = "/proc/meminfo"


def rules(*items: dict, default: str = "allow") -> dict:
    return {"ipv6": "drop", "fragments": "drop-if-port-rules", "virtual_subnet": "always-allow", "default": default, "rules": list(items)}


# ---- process and machine accounting ---------------------------------------------------------------------------------------------

def process_table() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, script name) for every process this user can see."""
    table = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as handle:
                parent = int(handle.read().rsplit(")", 1)[-1].split()[1])
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                words = handle.read().split(b"\0")
            script = next((os.path.basename(w.decode(errors="replace")) for w in words[1:] if w.endswith(b".py")), os.path.basename(words[0].decode(errors="replace")))
            table[int(entry)] = (parent, script)
        except (OSError, ValueError, IndexError):
            continue  # the process ended while it was read
    return table


def descendants(root: int) -> dict[int, str]:
    table = process_table()
    found = {root: table.get(root, (0, "unshare"))[1]}
    grew = True
    while grew:
        grew = False
        for pid, (parent, script) in table.items():
            if parent in found and pid not in found:
                found[pid] = script
                grew = True
    return found


def read_machine() -> dict[str, tuple[int, int]]:
    out = {}
    with open("/proc/stat") as handle:
        for line in handle:
            if line.startswith("cpu"):
                name, *fields = line.split()
                values = [int(v) for v in fields[:8]]  # user nice system idle iowait irq softirq steal
                out[name] = (sum(values), values[3] + values[4])
    return out


class Meter:
    """CPU used between start() and stop() by named groups of processes, and by the machine's CPUs (as a share of each CPU)."""

    def __init__(self, groups: dict[str, Callable[[], list[int]]]) -> None:
        self.groups = groups

    def _read(self) -> dict[str, dict[int, float]]:
        out = {}
        for label, pids in self.groups.items():
            out[label] = {}
            for pid in pids():
                try:
                    out[label][pid] = es.cpu_seconds(pid)
                except (OSError, IndexError, ValueError):
                    continue  # the process ended
        return out

    def start(self) -> None:
        self.before, self.machine_before, self.started = self._read(), read_machine(), time.monotonic()

    def stop(self) -> dict:
        after, machine_after, seconds = self._read(), read_machine(), time.monotonic() - self.started
        cpu = {label: round(sum(after[label][p] - self.before[label].get(p, 0.0) for p in after[label]) / seconds * 100) for label in after}
        busy = {name: round(100 * (1 - (machine_after[name][1] - self.machine_before[name][1]) / max(1, machine_after[name][0] - self.machine_before[name][0])))
                for name in machine_after}
        return {"cpu_percent_of_one_cpu": cpu, "machine_busy_percent": busy}


class Sampler(threading.Thread):
    """Polls a reading while a long command runs, to see the lowest value it reached."""

    def __init__(self, read: Callable[[], float], interval_s: float = 0.05) -> None:
        super().__init__(daemon=True)
        self.read, self.interval_s, self.values, self.done = read, interval_s, [read()], threading.Event()

    def run(self) -> None:
        while not self.done.is_set():
            self.values.append(self.read())
            time.sleep(self.interval_s)

    def finish(self) -> tuple[float, float]:
        self.done.set()
        self.join()
        return min(self.values), max(self.values)


def mem_available_mb() -> float:
    with open(MEMINFO) as handle:
        return int(next(line for line in handle if line.startswith("MemAvailable")).split()[1]) / 1024


def disk_free_mb(path: str) -> float:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize / (1 << 20)


def process_count() -> int:
    return sum(entry.isdigit() for entry in os.listdir("/proc"))


# ---- the stack and its helpers ----------------------------------------------------------------------------------------------------

class Lab:
    def __init__(self, slirp_binary: str) -> None:
        self.slirp_binary, self.ip = slirp_binary, es.container_ip()
        self.stacks: list[es.Stack] = []
        self.processes: list[subprocess.Popen] = []
        os.makedirs(RUN_DIR, exist_ok=True)

    def stack(self, name: str, initial: dict | None = None, child_nice: int = 0, child_cpus: tuple[int, ...] = ()) -> es.Stack:
        stack = es.Stack(f"{SCRATCH_PREFIX}-{name}", self.slirp_binary, PYTHON, initial or rules(), extra_ops=os.path.join(HERE, "load_ops.py"),
                         child_nice=child_nice, child_cpus=child_cpus)
        stack.start()
        self.stacks.append(stack)
        return stack

    def spawn(self, argv: list[str], log: str) -> subprocess.Popen:
        with open(os.path.join(RUN_DIR, log), "ab") as handle:
            process = subprocess.Popen([PYTHON, *argv], stdout=handle, stderr=handle, start_new_session=True)
        self.processes.append(process)
        return process

    def wait_for(self, ready: Callable[[], bool], what: str) -> None:
        deadline = time.time() + READY_TIMEOUT_S
        while time.time() < deadline:
            if ready():
                return
            time.sleep(0.05)
        raise RuntimeError(f"{what} did not come up")

    def stop_stack(self, stack: es.Stack) -> None:
        stack.close()
        self.stacks.remove(stack)

    def stop_process(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
        process.wait(timeout=5)
        self.processes.remove(process)

    def close(self) -> None:
        for stack in list(self.stacks):
            stack.close()
        for process in list(self.processes):
            self.stop_process(process)


def port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def sandbox_groups(stack: es.Stack) -> dict[str, Callable[[], list[int]]]:
    def by(script: str) -> Callable[[], list[int]]:
        return lambda: [pid for pid, name in descendants(stack.processes["child"].pid).items() if name == script]
    return {"pump": lambda: [stack.processes["pump"].pid], "slirp4netns": lambda: [stack.processes["slirp"].pid],
            "sandbox_main": by("egress_child.py"), "sandbox_relay": by("relay.py"), "sandbox_app": by("child_http.py")}


def merge_generators(results: list[dict]) -> dict:
    """One result from the output of several generators: counts add up; each latency percentile is the worst one any generator saw."""
    if len(results) == 1:
        return {key: value for key, value in results[0].items() if key != "cpu_seconds"}
    merged = {"connections": sum(r["connections"] for r in results), "generators": len(results), "requests": sum(r["requests"] for r in results),
              "seconds": max(r["seconds"] for r in results), "per_second": sum(r["per_second"] for r in results),
              "failures": sum(r["failures"] for r in results), "failure_kinds": sorted({kind for r in results for kind in r["failure_kinds"]})}
    if "rate_per_s" in results[0]:
        merged["rate_per_s"] = sum(r["rate_per_s"] for r in results)
    if all("latency_ms" in r for r in results):
        merged["latency_ms_worst_generator"] = {key: max(r["latency_ms"][key] for r in results) for key in results[0]["latency_ms"]}
    return merged


def loadgen(port: int, connections: int, duration_s: float, extra_groups: dict[str, Callable[[], list[int]]], path: str = "/",
            generators: int = 1, rate_per_s: float = 0.0) -> dict:
    """Run `generators` load-generator processes, the connections and the rate split between them, and meter everything named in
    `extra_groups` around them. A generator is one single-threaded interpreter, so it can be the limit it is meant to measure; its CPU is
    the figure it reports itself, because the process is gone by the time the meter stops."""
    shares = [connections // generators + (index < connections % generators) for index in range(generators)]
    processes = [subprocess.Popen([PYTHON, os.path.join(HERE, "http_loadgen.py"), "127.0.0.1", str(port), str(share), str(duration_s), path,
                                   str(rate_per_s / generators)], stdout=subprocess.PIPE, text=True) for share in shares]
    meter = Meter(extra_groups)
    meter.start()
    results = [json.loads(process.communicate(timeout=duration_s + 60)[0]) for process in processes]
    measured = meter.stop()
    out = merge_generators(results)
    measured["cpu_percent_of_one_cpu"]["load_generator"] = round(sum(r["cpu_seconds"] for r in results) / max(r["seconds"] for r in results) * 100)
    out.update(measured)
    return out


def connections_label(count: int, generators: int) -> str:
    return f"{count} connections" + ("" if generators == 1 else f", {generators} load generators")


# ---- sections --------------------------------------------------------------------------------------------------------------------

def section_bulk(lab: Lab) -> dict:
    server = lab.spawn([os.path.join(HERE, "load_server.py"), str(ECHO_PORT), str(BULK_PORT)], "server.log")
    lab.wait_for(lambda: port_open(BULK_PORT), "load_server")
    stack = lab.stack("bulk")
    out = {}
    try:
        groups = {**sandbox_groups(stack), "load_server": lambda: [server.pid]}
        for direction in ("download", "upload"):
            meter = Meter(groups)
            meter.start()
            result = stack.call("bulk", host=lab.ip, port=BULK_PORT, direction=direction, megabytes=BULK_MEGABYTES)
            out[f"{direction} {BULK_MEGABYTES} MiB, local server"] = {**result, **meter.stop()}
        meter = Meter(groups)
        meter.start()
        result = stack.call("https_get", host=HTTPS_HOST, path=HTTPS_PATH, limit_mb=HTTPS_LIMIT_MB)
        out["HTTPS download from the Internet (up to 64 MiB of a public index page)"] = {**result, **meter.stop()}
        out["pump"] = {k: v for k, v in stack.pump_stats().items() if k in ("to_slirp_frames", "to_child_frames", "to_slirp_bytes", "to_child_bytes", "errors")}
    finally:
        lab.stop_stack(stack)
        lab.stop_process(server)
    return out


def section_connections(lab: Lab) -> dict:
    server = lab.spawn([os.path.join(HERE, "load_server.py"), str(ECHO_PORT), str(BULK_PORT)], "server.log")
    lab.wait_for(lambda: port_open(ECHO_PORT), "load_server")
    stack = lab.stack("connections")
    out = {}
    try:
        groups = {**sandbox_groups(stack), "load_server": lambda: [server.pid]}
        for count in CONNECTION_COUNTS:
            meter = Meter(groups)
            meter.start()
            result = stack.call("echo_load", host=lab.ip, port=ECHO_PORT, connections=count, duration_s=LOAD_SECONDS, message_bytes=ECHO_MESSAGE_BYTES)
            out[f"{count} connections, {ECHO_MESSAGE_BYTES}-byte ping-pong"] = {**result, **meter.stop()}
    finally:
        lab.stop_stack(stack)
        lab.stop_process(server)
    return out


class Inbound:
    """A trivial child and its relay inside a sandbox, and an edge stand-in outside it that reaches the relay's unix socket."""

    def __init__(self, lab: Lab, stack: es.Stack, tag: str) -> None:
        self.lab, self.stack = lab, stack
        stack.call("drop_caps")  # the relay and the child run as the hostile child would: no capability
        socket_path = os.path.join(RUN_DIR, f"relay-{tag}.sock")
        stack.call("spawn", name="app", argv=[PYTHON, os.path.join(HERE, "child_http.py"), str(CHILD_PORT)], log=os.path.join(RUN_DIR, f"app-{tag}.log"))
        stack.call("spawn", name="relay", argv=[PYTHON, os.path.join(HERE, "relay.py"), socket_path, str(CHILD_PORT)], log=os.path.join(RUN_DIR, f"relay-{tag}.log"))
        lab.wait_for(lambda: os.path.exists(socket_path), "relay socket")
        self.edge = lab.spawn([os.path.join(HERE, "edge_standin.py"), str(EDGE_PORT), socket_path], f"edge-{tag}.log")
        lab.wait_for(lambda: port_open(EDGE_PORT), "edge stand-in")

    def groups(self) -> dict[str, Callable[[], list[int]]]:
        return {**sandbox_groups(self.stack), "edge": lambda: [self.edge.pid]}

    def close(self) -> None:
        self.lab.stop_process(self.edge)


def section_edge(lab: Lab) -> dict:
    out = {}
    stack = lab.stack("edge")
    try:
        inbound = Inbound(lab, stack, "sandbox")
        try:
            for generators, counts in LOAD_GENERATOR_ARMS:
                for count in counts:
                    out[f"{connections_label(count, generators)}, edge to relay to child in the sandbox"] = loadgen(
                        EDGE_PORT, count, LOAD_SECONDS, inbound.groups(), generators=generators)
        finally:
            inbound.close()
    finally:
        lab.stop_stack(stack)
    socket_path = os.path.join(RUN_DIR, "direct.sock")
    app = lab.spawn([os.path.join(HERE, "child_http.py"), f"unix:{socket_path}"], "direct-app.log")
    lab.wait_for(lambda: os.path.exists(socket_path), "direct app socket")
    edge = lab.spawn([os.path.join(HERE, "edge_standin.py"), str(EDGE_PORT), socket_path], "direct-edge.log")
    lab.wait_for(lambda: port_open(EDGE_PORT), "direct edge")
    try:
        for generators, counts in LOAD_GENERATOR_ARMS:
            for count in counts:
                out[f"{connections_label(count, generators)}, edge to a child on a unix socket, no sandbox and no relay (baseline)"] = loadgen(
                    EDGE_PORT, count, LOAD_SECONDS, {"edge": lambda: [edge.pid], "app": lambda: [app.pid]}, generators=generators)
    finally:
        lab.stop_process(edge)
        lab.stop_process(app)
    return out


PINNED = {"child_nice": 19, "child_cpus": (HOG_CPU,)}
# (label, limits the launcher puts on the hog's sandbox or None for no hog, options of the hog call, system call to refuse or None)
HOG_ARMS = (("no hog", None, {}, None),
            ("hog at nice 0", {}, {}, None),
            ("hog at nice 19, set by the launcher", {"child_nice": 19}, {}, None),
            (f"hog at nice 19 and pinned to CPU {HOG_CPU}, set by the launcher", PINNED, {}, None),
            (f"hog at nice 19 and pinned to CPU {HOG_CPU}, each loop first widens its own pin", PINNED, {"widen": True}, None),
            (f"hog at nice 19 and pinned to CPU {HOG_CPU}, each loop first widens its own pin, sched_setaffinity refused by seccomp",
             PINNED, {"widen": True}, "sched_setaffinity"))


def section_hog(lab: Lab) -> dict:
    out = {}
    victim = lab.stack("victim")
    try:
        inbound = Inbound(lab, victim, "victim")
        try:
            for index, (label, limits, hog_options, refused_call) in enumerate(HOG_ARMS):
                hog = None
                try:
                    groups = inbound.groups()
                    if limits is not None:
                        hog = lab.stack(f"hog{index}", **limits)
                        hog.call("drop_caps")
                        if refused_call:
                            hog.call("deny_syscall", name=refused_call)
                        hog.call("hog", processes=HOG_PROCESSES, seconds=2 * LOAD_SECONDS + 6, **hog_options)  # outlasts both victim runs below
                        time.sleep(1.0)
                        groups = {**groups, "hog_processes": lambda stack=hog: list(descendants(stack.processes["child"].pid))}
                    out[f"{label}, closed loop"] = loadgen(EDGE_PORT, VICTIM_CONNECTIONS, LOAD_SECONDS, groups)
                    out[f"{label}, {VICTIM_RATE_PER_S} requests per second"] = loadgen(
                        EDGE_PORT, VICTIM_CONNECTIONS, LOAD_SECONDS, groups, rate_per_s=VICTIM_RATE_PER_S)
                finally:
                    if hog is not None:
                        lab.stop_stack(hog)
        finally:
            inbound.close()
    finally:
        lab.stop_stack(victim)
    for label, refused_call in (("can the sandbox undo nice 19 and the CPU pin from inside", None),
                                ("the same with sched_setaffinity refused by seccomp", "sched_setaffinity")):
        pinned = lab.stack("undo", **PINNED)
        try:
            pinned.call("drop_caps")
            if refused_call:
                pinned.call("deny_syscall", name=refused_call)
            out[label] = pinned.call("undo_limits")
        finally:
            lab.stop_stack(pinned)
    return out


def pss_mb(pids: list[int]) -> float:
    """Proportional set size: shared pages (the interpreter, libc) are divided between the processes that map them."""
    total_kb = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/smaps_rollup") as handle:
                total_kb += next(int(line.split()[1]) for line in handle if line.startswith("Pss:"))
        except (OSError, StopIteration):
            continue  # the process ended
    return total_kb / 1024


def section_idle(lab: Lab) -> dict:
    out = {}
    for count in IDLE_STACK_COUNTS:
        memory_before = mem_available_mb()
        stacks = [lab.stack(f"idle{count}-{index}") for index in range(count)]
        try:
            for stack in stacks:
                stack.call("drop_caps")
            time.sleep(IDLE_SETTLE_S)
            per_stack = [sandbox_groups(stack) for stack in stacks]
            groups = {label: (lambda label=label: [pid for found in per_stack for pid in found[label]()]) for label in per_stack[0]}
            held = {label: round(pss_mb(pids()), 1) for label, pids in groups.items()}
            meter = Meter(groups)
            meter.start()
            time.sleep(IDLE_SECONDS)
            measured = meter.stop()
            out[f"{count} idle sandboxes"] = {
                "pss_mb_by_part": held, "pss_mb_total": round(sum(held.values()), 1), "pss_mb_per_sandbox": round(sum(held.values()) / count, 1),
                "memory_available_dropped_mb": round(memory_before - mem_available_mb()),
                "cpu_percent_of_one_cpu_all_sandboxes": measured["cpu_percent_of_one_cpu"], "machine_busy_percent": measured["machine_busy_percent"]["cpu"]}
        finally:
            for stack in stacks:
                lab.stop_stack(stack)
    return out


def namespace_members(stack: es.Stack) -> list[int]:
    return [pid for pid in descendants(stack.processes["child"].pid) if pid != stack.processes["child"].pid]


def process_stat(pid: int) -> tuple[str, int] | None:
    """(kernel state letter, parent pid) of a process, or None when it does not exist."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            state, parent = handle.read().rsplit(")", 1)[-1].split()[:2]
        return state, int(parent)
    except (OSError, ValueError):
        return None


def is_running(pid: int) -> bool:
    stat = process_stat(pid)
    return stat is not None and stat[0] not in ("Z", "X")


def command_of(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm") as handle:
            return handle.read().strip()
    except OSError:
        return "(gone)"


def become_subreaper() -> None:
    """Orphans of this process's descendants are re-parented to it instead of to the container's PID 1."""
    if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_CHILD_SUBREAPER) failed")


def tear_down_namespace(stack: es.Stack, members: list[int], reap: bool) -> dict:
    """Kill the `unshare` process; `--kill-child` takes the namespace's init with it and the kernel kills everything in the namespace.
    A member counts as gone once it is a zombie, which holds no memory; zombies are counted on their own. With `reap` the driver is a
    subreaper, so the orphaned init is its child and it waits for it."""
    unshare_pid = stack.processes["child"].pid
    inits = [pid for pid, (parent, _) in process_table().items() if parent == unshare_pid]
    started = time.monotonic()
    os.kill(unshare_pid, signal.SIGKILL)
    reaped = 0
    while time.monotonic() - started < TEARDOWN_WAIT_S:
        if reap:
            for pid in list(inits):
                try:
                    if os.waitpid(pid, os.WNOHANG)[0] == pid:
                        inits.remove(pid)
                        reaped += 1
                except ChildProcessError:
                    continue  # not re-parented to this process yet; the next pass tries again
        if not any(is_running(pid) for pid in members):
            break
        time.sleep(0.005)
    seconds = time.monotonic() - started
    zombies = [pid for pid in members if (process_stat(pid) or ("", 0))[0] == "Z"]
    parents = sorted({command_of(process_stat(pid)[1]) for pid in zombies})
    return {"processes_in_the_namespace": len(members), "seconds_until_none_is_running": round(seconds, 3),
            "still_running": sum(is_running(pid) for pid in members), "zombies_left": len(zombies), "command_of_the_zombies_parent": parents,
            "reaped_by_the_driver": reaped}


def section_fork(lab: Lab) -> dict:
    out = {}
    baseline_processes, baseline_memory = process_count(), mem_available_mb()
    victim = lab.stack("victim")
    try:
        inbound = Inbound(lab, victim, "victim")
        try:
            out["victim before the fork test"] = loadgen(EDGE_PORT, VICTIM_CONNECTIONS, LOAD_SECONDS, inbound.groups())
            forker = lab.stack("forker")
            forker.call("drop_caps")
            victim_run: dict = {}
            thread = threading.Thread(target=lambda: victim_run.update(loadgen(EDGE_PORT, VICTIM_CONNECTIONS, LOAD_SECONDS, inbound.groups())))
            thread.start()
            memory = Sampler(mem_available_mb)
            memory.start()
            forked = forker.call("fork_to", count=FORK_COUNT, hold_s=FORK_HOLD_S)
            peak_processes = process_count()
            members = namespace_members(forker)
            low_memory, _ = memory.finish()
            thread.join()
            out["fork test"] = {**forked, "container_processes_before": baseline_processes, "container_processes_at_peak": peak_processes,
                                "memory_available_dropped_mb": round(baseline_memory - low_memory)}
            out["victim during the fork test"] = victim_run
            out["PID 1 of the container"] = command_of(1)
            out["teardown by killing the namespace's init, orphans go to the container's PID 1"] = tear_down_namespace(forker, members, reap=False)
            out["container_processes_after_teardown"] = process_count()
            lab.stop_stack(forker)

            become_subreaper()
            reaped = lab.stack("forker-reaped")
            reaped.call("drop_caps")
            reaped.call("fork_to", count=FORK_COUNT, hold_s=FORK_HOLD_S)
            out["teardown with the driver as subreaper"] = tear_down_namespace(reaped, namespace_members(reaped), reap=True)
            lab.stop_stack(reaped)

            limited = lab.stack("forker-nproc")
            limited.call("drop_caps")
            processes_outside = process_count()  # all of them belong to the same real user; if the limit counted them, created would be lower
            outcome = limited.call("fork_to", count=FORK_COUNT, hold_s=FORK_HOLD_S, nproc_limit=NPROC_LIMIT)
            outcome["processes_of_the_container_outside_the_sandbox"] = processes_outside
            outcome["processes_in_the_sandbox"] = len(namespace_members(limited))
            out[f"fork test with RLIMIT_NPROC {NPROC_LIMIT} set by the launcher"] = outcome
            lab.stop_stack(limited)
        finally:
            inbound.close()
    finally:
        lab.stop_stack(victim)
    return out


def section_disk(lab: Lab) -> dict:
    out = {}
    os.makedirs(HOST_DIR, exist_ok=True)
    stack = lab.stack("disk")
    try:
        target = os.path.join(HOST_DIR, "tmpfs")
        out["mount"] = stack.call("mount_tmpfs", target=target, size_mb=TMPFS_MB)
        stack.call("drop_caps")
        memory = Sampler(mem_available_mb)
        free_before_memory = mem_available_mb()
        memory.start()
        filled = stack.call("fill", directory=target, limit_mb=TMPFS_MB * 2)
        low_memory, _ = memory.finish()
        out[f"fill a {TMPFS_MB} MiB tmpfs with up to {TMPFS_MB * 2} MiB"] = {**filled, "memory_available_dropped_mb": round(free_before_memory - low_memory)}
        for label, file_limit in (("host directory, no limit", None), (f"host directory, RLIMIT_FSIZE {FILE_LIMIT_MB} MiB per file", FILE_LIMIT_MB)):
            disk = Sampler(lambda: disk_free_mb(HOST_DIR))
            free_before = disk_free_mb(HOST_DIR)
            disk.start()
            filled = stack.call("fill", directory=HOST_DIR, limit_mb=HOST_FILL_MB, file_limit_mb=file_limit)
            low, _ = disk.finish()
            out[f"fill the {label} with {HOST_FILL_MB} MiB"] = {**filled, "free_disk_dropped_mb": round(free_before - low)}
            time.sleep(0.2)
    finally:
        lab.stop_stack(stack)
        subprocess.run(["rm", "-rf", HOST_DIR], check=False)
    return out


def make_sdist(path: str) -> None:
    files = {
        "spike7_ext-1.0/PKG-INFO": "Metadata-Version: 2.1\nName: spike7-ext\nVersion: 1.0\n",
        "spike7_ext-1.0/pyproject.toml": f'[build-system]\nrequires = ["{SETUPTOOLS_PIN}"]\nbuild-backend = "setuptools.build_meta"\n',
        "spike7_ext-1.0/setup.py": 'from setuptools import Extension, setup\n\nsetup(name="spike7-ext", version="1.0", ext_modules=[Extension("spike7_ext", ["spike7_ext.c"])])\n',
        "spike7_ext-1.0/spike7_ext.c": (
            '#define PY_SSIZE_T_CLEAN\n#include <Python.h>\n\n'
            'static PyObject *add(PyObject *self, PyObject *args) {\n    long a, b;\n    if (!PyArg_ParseTuple(args, "ll", &a, &b)) return NULL;\n'
            '    return PyLong_FromLong(a + b);\n}\n\n'
            'static PyMethodDef methods[] = {{"add", add, METH_VARARGS, "add two integers"}, {NULL, NULL, 0, NULL}};\n'
            'static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "spike7_ext", NULL, -1, methods};\n'
            'PyMODINIT_FUNC PyInit_spike7_ext(void) { return PyModule_Create(&module); }\n'),
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def build_arms() -> list[tuple]:
    """(label, egress rules, build isolation, pip offline, resolv.conf names slirp's resolver)"""
    return [("no build isolation, setuptools from the environment, all egress denied", rules(default="deny"), False, True, False),
            (f"build isolation, {SETUPTOOLS_PIN} fetched through the pump", rules(), True, False, False),
            ("build isolation, all egress denied", rules(default="deny"), True, False, False),
            ("build isolation, all egress denied, resolv.conf names slirp's resolver", rules(default="deny"), True, False, True),
            (f"build isolation, {SETUPTOOLS_PIN} fetched through the pump, resolv.conf names slirp's resolver", rules(), True, False, True)]


def run_build_arms(lab: Lab, arms: list[tuple]) -> dict:
    out = {}
    subprocess.run(["rm", "-rf", BUILD_DIR], check=False)
    os.makedirs(BUILD_DIR)
    sdist = os.path.join(BUILD_DIR, "spike7_ext-1.0.tar.gz")
    make_sdist(sdist)
    out["compiler"] = subprocess.run(["gcc", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    for index, (label, egress, isolation, offline, slirp_resolver) in enumerate(arms):
        stack = lab.stack(f"build{index}", egress)
        stack.control.settimeout(BUILD_CALL_TIMEOUT_S)  # the denied-egress arms wait out pip's retries, longer than the default call timeout
        try:
            workdir = os.path.join(BUILD_DIR, f"work{index}")
            stack.call("mount_tmpfs", target=workdir, size_mb=256)
            if slirp_resolver:  # the design's sandbox has no host resolv.conf; the launcher writes one that names slirp's DNS
                resolv = os.path.join(stack.scratch, "resolv.conf")
                with open(resolv, "w") as handle:
                    handle.write("nameserver 10.0.2.3\n")
                stack.call("bind_file", source=resolv, target="/etc/resolv.conf")
            stack.call("drop_caps")
            meter = Meter(sandbox_groups(stack))
            meter.start()
            result = stack.call("build", sdist=sdist, target=os.path.join(workdir, "site"), workdir=workdir, isolation=isolation, offline=offline)
            out[label] = {**result, **meter.stop()}
        finally:
            lab.stop_stack(stack)
    subprocess.run(["rm", "-rf", BUILD_DIR], check=False)
    return out


def section_build(lab: Lab) -> dict:
    return run_build_arms(lab, build_arms())


def section_build_resolver(lab: Lab) -> dict:
    return run_build_arms(lab, build_arms()[-1:])


def section_ceiling(_lab: Lab) -> dict:
    out = {}
    for loops, seconds in CEILING_ARMS:
        argv = [PYTHON, os.path.join(HERE, "cpu_ceiling.py"), str(loops), str(seconds)]
        out[f"{loops} busy loops for {seconds} s"] = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True, timeout=seconds + 30).stdout)
    return out


SECTIONS = {"ceiling": section_ceiling, "bulk": section_bulk, "connections": section_connections, "edge": section_edge, "hog": section_hog, "fork": section_fork,
            "idle": section_idle, "disk": section_disk, "build": section_build, "build_resolver": section_build_resolver}


def main() -> None:
    slirp_binary, result_path = sys.argv[1], sys.argv[2]
    chosen = sys.argv[3:] or [name for name in SECTIONS if name != "build_resolver"]  # the last build arm is already part of `build`
    lab = Lab(slirp_binary)
    with open("/proc/stat") as handle:
        cpu_lines = sum(line.startswith("cpu") and line[3].isdigit() for line in handle)
    result: dict = {"date": time.strftime("%Y-%m-%d"), "kernel": platform.release(), "python": platform.python_version(), "cpus_visible": os.cpu_count(),
                    "cpu_lines_in_proc_stat": cpu_lines, "load_average_at_start": os.getloadavg()[0],
                    "memory_available_mb_at_start": round(mem_available_mb()), "slirp4netns": "v1.3.6 (sha256 checked against probe-archive)"}
    try:
        for name in chosen:
            started = time.time()
            try:
                result[name] = SECTIONS[name](lab)
            except Exception as exc:  # a section that fails is a finding; the other sections still run
                result[name] = {"error": f"{type(exc).__name__}: {exc}"}
            result.setdefault("section_seconds", {})[name] = round(time.time() - started)
            print(f"section {name} done", flush=True)
    finally:
        lab.close()
    text = es.mask(json.dumps(result, indent=2, default=str), {lab.ip: "<container-ip>"})
    with open(result_path, "w") as handle:
        handle.write(text)
    print("wrote result", flush=True)


if __name__ == "__main__":
    main()
