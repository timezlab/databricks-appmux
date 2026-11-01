"""Spike 7, runs inside the sandbox: the load, fork, disk and build commands that egress_child.py loads from config.json.
Not part of appmux.

Everything here runs as the hostile child would: root of its own user namespace, no capability once the driver has called
`drop_caps`, and only the pump and slirp4netns between it and the network. Commands that need a capability (`mount_tmpfs`) run
before `drop_caps`. Standard library only.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import resource
import signal
import socket
import ssl
import struct
import subprocess
import sys
import time

import sandbox_child as base  # the spike 2 helpers egress_child.py already put on sys.path

BULK_CHUNK = b"\0" * 65536
BULK_HEADER = struct.Struct(">cQ")  # direction D (server sends) or U (client sends), then the byte count
BULK_RECEIVE_BYTES = 1 << 20
NETWORK_TIMEOUT_S = 60
BUILD_TIMEOUT_S = 300
BUILD_OUTPUT_TAIL_CHARS = 700
FILL_CHUNK = b"\xa5" * (1 << 20)
SYSCALL_NUMBERS_X86_64 = {"sched_setaffinity": 203}  # asm/unistd_64.h
AUDIT_ARCH_X86_64 = 0xC000003E
SECCOMP_RET_ALLOW, SECCOMP_RET_KILL_PROCESS, SECCOMP_RET_ERRNO = 0x7FFF0000, 0x80000000, 0x00050000
PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 22, 2
BPF_LD_W_ABS, BPF_JEQ_K, BPF_RET_K = 0x20, 0x15, 0x06
background: dict[str, subprocess.Popen] = {}


def percentile(sorted_values: list[float], fraction: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * fraction))]


def summarize(latencies: list[float], seconds: float, failures: list[str]) -> dict:
    latencies.sort()
    out = {"messages": len(latencies), "seconds": round(seconds, 2), "per_second": round(len(latencies) / seconds),
           "failures": len(failures), "failure_kinds": sorted(set(failures))}
    if latencies:
        out["latency_ms"] = {"p50": round(percentile(latencies, 0.50) * 1000, 2), "p95": round(percentile(latencies, 0.95) * 1000, 2),
                             "p99": round(percentile(latencies, 0.99) * 1000, 2), "max": round(latencies[-1] * 1000, 2)}
    return out


async def echo_worker(host: str, port: int, message: bytes, deadline: float, latencies: list[float], failures: list[str]) -> None:
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        failures.append(type(exc).__name__)
        return
    try:
        while time.monotonic() < deadline:
            started = time.perf_counter()
            writer.write(message)
            await reader.readexactly(len(message))
            latencies.append(time.perf_counter() - started)
    except (OSError, asyncio.IncompleteReadError) as exc:
        failures.append(type(exc).__name__)
    finally:
        writer.close()


async def echo_load(host: str, port: int, connections: int, duration_s: float, message_bytes: int) -> dict:
    latencies: list[float] = []
    failures: list[str] = []
    message = b"x" * message_bytes
    started = time.monotonic()
    deadline = started + duration_s
    await asyncio.gather(*(echo_worker(host, port, message, deadline, latencies, failures) for _ in range(connections)))
    return summarize(latencies, time.monotonic() - started, failures)


def op_echo_load(host: str, port: int, connections: int, duration_s: float, message_bytes: int) -> dict:
    """`connections` ping-pong connections to an echo server for `duration_s` seconds, one message in flight on each."""
    return asyncio.run(echo_load(host, port, connections, duration_s, message_bytes))


def op_bulk(host: str, port: int, direction: str, megabytes: int) -> dict:
    """Move `megabytes` MiB through the pump; `download` is server to child, `upload` is child to server."""
    size = megabytes << 20
    buffer = bytearray(BULK_RECEIVE_BYTES)
    started = time.perf_counter()
    with socket.create_connection((host, port), timeout=NETWORK_TIMEOUT_S) as sock:
        sock.sendall(BULK_HEADER.pack(b"D" if direction == "download" else b"U", size))
        if direction == "download":
            remaining = size
            while remaining:
                received = sock.recv_into(buffer, min(BULK_RECEIVE_BYTES, remaining))
                if not received:
                    raise ConnectionError(f"the server closed with {remaining} bytes to go")
                remaining -= received
        else:
            sent = 0
            while sent < size:
                sent += sock.send(BULK_CHUNK[: min(len(BULK_CHUNK), size - sent)])
            sock.recv(1)  # the server's acknowledgement that it read everything
    seconds = time.perf_counter() - started
    return {"direction": direction, "megabytes": megabytes, "seconds": round(seconds, 2), "mb_per_s": round(megabytes / seconds, 1)}


def op_https_get(host: str, path: str, limit_mb: int) -> dict:
    """A real download from the Internet through the pump and slirp4netns: HTTPS, up to `limit_mb` MiB, bytes discarded."""
    limit = limit_mb << 20
    context = ssl.create_default_context()
    started = time.perf_counter()
    total = 0
    with socket.create_connection((host, 443), timeout=NETWORK_TIMEOUT_S) as raw, context.wrap_socket(raw, server_hostname=host) as sock:
        sock.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nAccept-Encoding: identity\r\nUser-Agent: appmux-spike-7\r\n\r\n".encode())
        first = sock.recv(BULK_RECEIVE_BYTES)
        status = first.split(b"\r\n", 1)[0].decode(errors="replace")
        total = len(first)
        while total < limit:
            chunk = sock.recv(BULK_RECEIVE_BYTES)
            if not chunk:
                break
            total += len(chunk)
    seconds = time.perf_counter() - started
    return {"status": status, "megabytes": round(total / (1 << 20), 1), "seconds": round(seconds, 2),
            "mb_per_s": round(total / (1 << 20) / seconds, 1)}


def op_spawn(name: str, argv: list[str], log: str) -> dict:
    """Start a long-running helper inside the sandbox (the child's relay and its trivial web app)."""
    with open(log, "ab") as handle:
        background[name] = subprocess.Popen(argv, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL)
    return {"name": name, "pid_in_sandbox": background[name].pid}


def op_stop_process(name: str) -> dict:
    process = background.pop(name)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    return {"name": name, "exit": process.returncode}


def op_hog(processes: int, seconds: float, widen: bool = False) -> dict:
    """A CPU hog: `processes` busy loops that stop by themselves after `seconds`. With `widen` each first tries to move itself onto
    every CPU, the way a hostile child would undo a pin set by the launcher."""
    for _ in range(processes):
        if os.fork() == 0:
            if widen:
                try:
                    os.sched_setaffinity(0, range(os.cpu_count() or 1))
                except OSError:
                    pass  # refused: the hog carries on where the launcher put it
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                pass
            os._exit(0)
    return {"hog_processes": processes}


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]


class SockFprog(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]


def op_deny_syscall(name: str) -> dict:
    """Install a seccomp filter that fails the named system call with EPERM, for this process and everything it forks from now on.
    Call after `drop_caps`, which sets `no_new_privs` (installing a filter without it needs a capability)."""
    number = SYSCALL_NUMBERS_X86_64[name]
    program = (SockFilter * 7)(
        SockFilter(BPF_LD_W_ABS, 0, 0, 4),                         # load the architecture
        SockFilter(BPF_JEQ_K, 1, 0, AUDIT_ARCH_X86_64),            # x86-64: skip the next instruction
        SockFilter(BPF_RET_K, 0, 0, SECCOMP_RET_KILL_PROCESS),     # any other architecture: numbers differ, so refuse to guess
        SockFilter(BPF_LD_W_ABS, 0, 0, 0),                         # load the system call number
        SockFilter(BPF_JEQ_K, 0, 1, number),                       # the named call falls through to the deny, anything else skips to the allow
        SockFilter(BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.EPERM),
        SockFilter(BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(SockFprog(len(program), program)), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_SECCOMP) failed")
    return {"denied": name, "number": number}


def op_undo_limits() -> dict:
    """Try to undo, from inside, the CPU limits the launcher set before the sandbox started (nice, CPU affinity)."""
    out = {"nice_before": os.getpriority(os.PRIO_PROCESS, 0), "affinity_before": sorted(os.sched_getaffinity(0))}
    attempts = {"setpriority back to 0": lambda: os.setpriority(os.PRIO_PROCESS, 0, 0),
                "sched_setaffinity to every visible CPU": lambda: os.sched_setaffinity(0, range(os.cpu_count() or 1))}
    for name, attempt in attempts.items():
        try:
            attempt()
            out[name] = "allowed"
        except OSError as exc:
            out[name] = errno.errorcode.get(exc.errno, str(exc.errno))
    out["nice_after"], out["affinity_after"] = os.getpriority(os.PRIO_PROCESS, 0), sorted(os.sched_getaffinity(0))
    return out


def op_fork_to(count: int, hold_s: float, nproc_limit: int | None = None) -> dict:
    """The bounded fork test: fork until `count` sleeping children exist or fork fails; the driver ends it."""
    if nproc_limit:
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))
    created = 0
    stopped_by = "count reached"
    started = time.perf_counter()
    for _ in range(count):
        try:
            pid = os.fork()
        except OSError as exc:
            stopped_by = errno.errorcode.get(exc.errno, str(exc.errno))
            break
        if pid == 0:
            time.sleep(hold_s)
            os._exit(0)
        created += 1
    return {"created": created, "seconds": round(time.perf_counter() - started, 2), "stopped_by": stopped_by,
            "nproc_limit": nproc_limit, "pid_of_this_process_in_sandbox": os.getpid()}


def op_mount_tmpfs(target: str, size_mb: int) -> dict:
    """Needs the capability of the sandbox's own user namespace: call before `drop_caps`."""
    os.makedirs(target, exist_ok=True)
    base.mount("tmpfs", target, "tmpfs", base.MS_NOSUID | base.MS_NODEV, f"size={size_mb}m,mode=1777")
    return {"mounted": target, "size_mb": size_mb}


def op_bind_file(source: str, target: str) -> dict:
    """Bind-mount `source` over the existing file `target` (the sandbox's own `/etc/resolv.conf`). Call before `drop_caps`."""
    base.mount(source, target, None, base.MS_BIND)
    return {"bound": target}


def op_fill(directory: str, limit_mb: int, file_limit_mb: int | None = None) -> dict:
    """Write up to `limit_mb` MiB into `directory`, one file after another; stop at the first error other than a full file.
    Files are removed afterwards. `file_limit_mb` sets RLIMIT_FSIZE: a write past it fails with EFBIG and the next file starts."""
    if file_limit_mb:
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit_mb << 20, file_limit_mb << 20))
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # the write then fails with EFBIG instead of killing this process
    limit = limit_mb << 20
    written = 0
    stopped_by = "limit reached"
    paths: list[str] = []
    started = time.perf_counter()
    try:
        while written < limit and stopped_by == "limit reached":
            paths.append(os.path.join(directory, f"fill-{len(paths)}.bin"))
            with open(paths[-1], "wb", buffering=0) as handle:
                try:
                    while written < limit:
                        written += handle.write(FILL_CHUNK)
                except OSError as exc:
                    if exc.errno != errno.EFBIG:
                        stopped_by = errno.errorcode.get(exc.errno, str(exc.errno))
    finally:
        seconds = time.perf_counter() - started
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass  # nothing to remove: the open failed before the file existed
    return {"written_mb": written >> 20, "seconds": round(seconds, 2), "stopped_by": stopped_by, "files": len(paths)}


def op_build(sdist: str, target: str, workdir: str, isolation: bool, offline: bool) -> dict:
    """`pip install` of a source distribution that has a C extension, the way a build step in the sandbox would run it."""
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": workdir, "TMPDIR": workdir, "PIP_CACHE_DIR": os.path.join(workdir, "pip-cache"),
                   "PIP_DISABLE_PIP_VERSION_CHECK": "1", "LANG": "C.UTF-8"}
    command = [sys.executable, "-m", "pip", "install", "--no-deps", "--retries", "1", "--timeout", "5", "--target", target]
    if not isolation:
        command.append("--no-build-isolation")
    if offline:
        command.append("--no-index")
    command.append(sdist)
    started = time.perf_counter()
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S, env=environment)
        exit_code, output = done.returncode, (done.stdout + done.stderr)[-BUILD_OUTPUT_TAIL_CHARS:]
    except subprocess.TimeoutExpired:
        exit_code, output = None, "timed out"
    seconds = time.perf_counter() - started
    imported = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {target!r}); import spike7_ext; print(spike7_ext.add(2, 3))"],
                              capture_output=True, text=True, env=environment)
    return {"exit_code": exit_code, "seconds": round(seconds, 1), "import_add_2_3": imported.stdout.strip() or imported.stderr.strip()[-200:],
            "output_tail": output}


def op_reap() -> dict:
    """Collect exited children (this process is pid 1 of its pid namespace)."""
    reaped = 0
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if not pid:
            break
        reaped += 1
    return {"reaped": reaped}


COMMANDS = {"echo_load": op_echo_load, "bulk": op_bulk, "https_get": op_https_get, "spawn": op_spawn, "stop_process": op_stop_process,
            "hog": op_hog, "deny_syscall": op_deny_syscall, "undo_limits": op_undo_limits, "fork_to": op_fork_to, "mount_tmpfs": op_mount_tmpfs, "bind_file": op_bind_file, "fill": op_fill, "build": op_build, "reap": op_reap}
