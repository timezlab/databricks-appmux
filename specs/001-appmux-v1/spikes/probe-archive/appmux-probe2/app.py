"""Throwaway probe v2.1: can option B work on a Databricks App container without /dev/net/tun?

Option B = every child gets its own network namespace; user-mode NAT gives it
outbound access, and appmux reaches it through a unix-socket relay. Probe v2
showed the TAP-based tools (slirp4netns, pasta) cannot start because the
container has no /dev/net/tun. Not part of appmux.

v2.1 measures two things. "offline": the sandbox without any NAT (pid ns, tmpfs
/proc and /tmp, capability drop, the relay), so those results no longer depend
on the network. "veth": the no-TUN rescue path (B'): a veth pair inside the
child's namespace, an AF_PACKET socket on its peer end handed to appmux, a
frame pump, and slirp4netns in --netns-type=tapfd mode reading frames from a
SOCK_SEQPACKET socketpair. A "control" run of the same battery in the plain
container is the baseline.

Safety: the report holds env var NAMES only, strings go through deep_scrub(),
credential-bearing headers are redacted, and /databricks/runtime-secrets is
only stat'ed/opened, never read. Stdlib only.
"""
import copy
import ctypes
import hashlib
import html
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BIN_SOURCE_DIR = os.path.join(APP_DIR, "bin")
CHILD_SCRIPT = os.path.join(APP_DIR, "sandbox_child.py")
PUMP_SCRIPT = os.path.join(APP_DIR, "pump.py")
PYTHON = os.path.abspath(sys.executable)
SCRATCH_ROOT = "/dev/shm"
SANDBOX_APP_PORT = 9999
BOOT_ID = str(uuid.uuid4())
PR_SET_DUMPABLE = 4
COMMAND_TIMEOUT_SECONDS = 15
LAUNCH_TIMEOUT_SECONDS = 10
ATTACH_READY_TIMEOUT_SECONDS = 12
HANDOFF_TIMEOUT_SECONDS = 15
SERVER_READY_TIMEOUT_SECONDS = 90
CHILD_RESULT_TIMEOUT_SECONDS = 330
CONTROL_TIMEOUT_SECONDS = 330
PING_SAMPLES = 100
BULK_MEGABYTES = 64
CLOCK_TICKS_PER_SECOND = os.sysconf("SC_CLK_TCK")
RUNTIME_CFG_PATH = "/databricks/runtime-secrets/runtime.cfg"
V1_MARKERS = (
    os.path.join(tempfile.gettempdir(), "appmux-probe-marker.json"),
    os.path.join(os.getcwd(), "appmux-probe-marker.json"),
)
V2_MARKERS = (
    os.path.join(tempfile.gettempdir(), "appmux-probe2-marker.json"),
    os.path.join(os.getcwd(), "appmux-probe2-marker.json"),
)
SENSITIVE_HEADER = re.compile(r"(?i)token|authorization|cookie|secret")
SCRUB_PATTERNS = (
    (re.compile(r"(?i)((?:secret|token|password|passwd|api[_-]?key|authorization)[\"'=: ]+)[^\s\"',}]+"), r"\1<redacted>"),
    (re.compile(r"dapi[0-9a-f]{16,}"), "<redacted-pat>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"), "<redacted-jwt>"),
    (re.compile(r"(://)[^/@\s]+:[^/@\s]+@"), r"\1<redacted>@"),
)
# Most capable launcher first; the report records which variant the platform accepted.
UNSHARE_VARIANTS = (
    ("pidns+kill-child", ["--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "--kill-child"]),
    ("pidns", ["--user", "--map-root-user", "--net", "--mount", "--pid", "--fork"]),
    ("no-pidns", ["--user", "--map-root-user", "--net", "--mount"]),
)
SCENARIOS = (
    {"name": "veth-slirp-hardened", "network": "veth", "hardened": True, "full": True, "mtu": 65520},
    {"name": "veth-slirp-filtered", "network": "veth", "hardened": True, "full": True, "mtu": 65520, "filter": True},
)
# Destinations the pump drops for the "filtered" scenario; 10.0.2.0/24 is slirp's own gateway/DNS and stays allowed.
PRIVATE_RANGES = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"]
SLIRP_SUBNET = "10.0.2.0/24"

REPORT = {"boot_id": BOOT_ID, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "finished": False, "checks": {}}
REPORT_LOCK = threading.Lock()
BIN_PATHS = {}  # binary name -> verified, executable path; filled by check_binaries


def sh(cmd, timeout=COMMAND_TIMEOUT_SECONDS):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"rc": None, "out": "binary not found"}
    except subprocess.TimeoutExpired:
        return {"rc": None, "out": f"timeout after {timeout}s"}
    except OSError as e:
        return {"rc": None, "out": f"{type(e).__name__}: {e}"}
    return {"rc": proc.returncode, "out": (proc.stdout + proc.stderr).strip()[-600:]}


def read(path, limit=2000):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(limit).strip()
    except OSError as e:
        return f"unreadable: {type(e).__name__}: {e}"


def tail(path, limit=1500):
    return read(path, 1 << 20)[-limit:]


def deep_scrub(value):
    if isinstance(value, str):
        for pattern, replacement in SCRUB_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {deep_scrub(k): deep_scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_scrub(v) for v in value]
    return value


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tcp_listeners():
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in read(path, 400000).splitlines()[1:]:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "0A":
                ports.add(int(parts[1].rsplit(":", 1)[1], 16))
    return sorted(ports)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for(path, timeout, proc=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        if proc is not None and proc.poll() is not None:
            return os.path.exists(path)
        time.sleep(0.1)
    return False


# --- platform facts ---

def check_platform():
    out = {}
    try:
        st = os.stat("/dev/net/tun")
        out["tun_stat"] = {"mode": oct(st.st_mode & 0o7777), "rdev": [os.major(st.st_rdev), os.minor(st.st_rdev)]}
        fd = os.open("/dev/net/tun", os.O_RDWR)
        os.close(fd)
        out["tun_open"] = "ok"
    except OSError as e:
        out["tun_open"] = f"{type(e).__name__}: {e}"
    try:
        st = os.stat(RUNTIME_CFG_PATH)
        with open(RUNTIME_CFG_PATH, "rb"):
            pass  # open only; content is never read
        out["runtime_cfg"] = {"exists": True, "size": st.st_size, "mode": oct(st.st_mode & 0o777), "open_ok": True}
    except OSError as e:
        out["runtime_cfg"] = {"exists": os.path.exists(RUNTIME_CFG_PATH), "error": f"{type(e).__name__}: {e}"}
    sleeper = subprocess.Popen(["sleep", "20"])
    try:
        adj = {}
        for value in ("500", "-100"):
            try:
                with open(f"/proc/{sleeper.pid}/oom_score_adj", "w") as f:
                    f.write(value)
                adj[value] = f"written, reads back {read(f'/proc/{sleeper.pid}/oom_score_adj', 10)}"
            except OSError as e:
                adj[value] = f"{type(e).__name__}: {e}"
        out["oom_score_adj"] = adj
    finally:
        sleeper.kill()
        sleeper.wait()
    mounts = [l.split() for l in read("/proc/mounts", 200000).splitlines()]
    interesting = ("/tmp", "/dev/shm", "/var/tmp", os.getcwd())
    out["mount_options"] = {m[1]: m[3] for m in mounts if len(m) > 3 and m[1] in interesting}
    out["resolv_conf"] = [l for l in read("/etc/resolv.conf", 2000).splitlines() if l.startswith(("nameserver", "search"))]
    out["util_linux_unshare"] = sh(["unshare", "--version"])
    out["userns_sysctl"] = {p: read(p, 30) for p in ("/proc/sys/user/max_user_namespaces", "/proc/sys/user/max_net_namespaces", "/proc/sys/net/ipv4/ping_group_range")}
    return out


def check_binaries():
    checksums = {}
    for line in read(os.path.join(BIN_SOURCE_DIR, "SHA256SUMS"), 4000).splitlines():
        parts = line.split()
        if len(parts) == 2:
            checksums[parts[1]] = parts[0]
    if not checksums:
        return {"error": f"no bin/SHA256SUMS in {BIN_SOURCE_DIR}; scenarios will be skipped"}
    result = {"expected": sorted(checksums), "tried": {}}
    for base in dict.fromkeys((tempfile.gettempdir(), os.getcwd(), "/var/tmp", "/dev/shm")):
        entry = {}
        try:
            dest = tempfile.mkdtemp(prefix="appmux-bin-", dir=base)
        except OSError as e:
            result["tried"][base] = {"error": f"{type(e).__name__}: {e}"}
            continue
        usable = {}
        for name, digest in checksums.items():
            target = os.path.join(dest, name)
            shutil.copyfile(os.path.join(BIN_SOURCE_DIR, name), target)
            os.chmod(target, 0o755)
            version = sh([target, "--version"])
            entry[name] = {"sha256_ok": sha256_of(target) == digest, "version": version}
            if version["rc"] == 0 and entry[name]["sha256_ok"]:
                usable[name] = target
        result["tried"][base] = entry
        if usable:
            BIN_PATHS.update(usable)
            result["chosen"] = {"dir": dest, "usable": sorted(usable)}
            break
        shutil.rmtree(dest, ignore_errors=True)  # best-effort: this directory did not allow exec
    return result


def check_persistence():
    out = {}
    for label, markers in (("v1", V1_MARKERS), ("v2", V2_MARKERS)):
        out[label] = {}
        for path in markers:
            if os.path.exists(path):
                out[label][path] = read(path, 200)
            else:
                out[label][path] = None
    for path in V2_MARKERS:
        try:
            with open(path, "w") as f:
                json.dump({"boot_id": BOOT_ID, "written_at": time.time()}, f)
        except OSError as e:
            out.setdefault("write_errors", {})[path] = f"{type(e).__name__}: {e}"
    return out


# --- sandbox scenarios ---

def container_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))  # UDP connect sends nothing; it only selects the outgoing interface
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def platform_gateway():
    for line in read("/proc/net/route", 20000).splitlines()[1:]:
        fields = line.split()
        if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 2:
            return socket.inet_ntoa(bytes.fromhex(fields[2])[::-1])
    return ""


def host_nameserver():
    for line in read("/etc/resolv.conf", 2000).splitlines():
        if line.startswith("nameserver"):
            return line.split()[1]
    return ""


def base_config(scratch, sibling_dir, decoy_tcp_port, decoy_unix, sibling_pid, scenario):
    return {
        "scenario": scenario,
        "scratch": scratch,
        "python": PYTHON,
        "app_port": SANDBOX_APP_PORT,
        "appmux_pid": os.getpid(),
        "sibling_pid": sibling_pid,
        "sibling_dir": sibling_dir,
        "decoy_tcp_port": decoy_tcp_port,
        "decoy_unix": decoy_unix,
        "host_ports": tcp_listeners(),
        "databricks_host": os.environ.get("DATABRICKS_HOST", ""),
        "do_numpy": bool(scenario.get("full")),
        "network": scenario.get("network", "host"),
        "mtu": scenario.get("mtu", 1500),
        "appmux_http_port": PORT,
        "container_ip": container_ip(),
        "platform_gateway": platform_gateway(),
        "host_nameserver": host_nameserver(),
        "resolv_bind": scenario.get("network") == "veth",
    }


class Decoys:
    """A stand-in for a sibling child: a TCP port, a unix socket and a sleeping process."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="probe-sibling-")
        with open(os.path.join(self.dir, "canary.txt"), "w") as f:
            f.write("sibling-canary")
        self.tcp = socket.socket()
        self.tcp.bind(("127.0.0.1", 0))
        self.tcp.listen(8)
        self.tcp_port = self.tcp.getsockname()[1]
        self.unix_path = os.path.join(self.dir, "decoy.sock")
        self.unix = socket.socket(socket.AF_UNIX)
        self.unix.bind(self.unix_path)
        os.chmod(self.unix_path, 0o600)
        self.unix.listen(8)
        self.sleeper = subprocess.Popen(["sleep", "600"], env={"PROBE_SIBLING_MARKER": "x", "PATH": os.environ.get("PATH", "")})

    def close(self):
        self.tcp.close()
        self.unix.close()
        self.sleeper.kill()
        self.sleeper.wait()
        shutil.rmtree(self.dir, ignore_errors=True)  # best-effort cleanup of a temp dir


def child_env(scratch):
    # appmux would hand a child a scrubbed env; the probe does the same.
    return {"PATH": os.environ.get("PATH", ""), "HOME": scratch, "LANG": "C.UTF-8"}


def launch_sandbox(scratch):
    attempts = []
    for label, flags in UNSHARE_VARIANTS:
        stage1 = os.path.join(scratch, "stage1.ready")
        if os.path.exists(stage1):
            os.remove(stage1)
        log = open(os.path.join(scratch, "child.log"), "ab")
        try:
            proc = subprocess.Popen(
                ["unshare", *flags, "--", PYTHON, CHILD_SCRIPT, scratch],
                stdout=log, stderr=log, env=child_env(scratch), start_new_session=True,
            )
        finally:
            log.close()
        if wait_for(stage1, LAUNCH_TIMEOUT_SECONDS, proc):
            attempts.append({"variant": label, "ok": True})
            return proc, label, attempts
        attempts.append({"variant": label, "ok": False, "rc": proc.poll(), "log": tail(os.path.join(scratch, "child.log"), 400)})
        kill_group(proc)
    return None, None, attempts


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # already gone
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass  # SIGKILL pending; nothing more to do


def receive_packet_fd(listener):
    """The sandbox opens an AF_PACKET socket in its own netns and sends it here (SCM_RIGHTS)."""
    listener.settimeout(HANDOFF_TIMEOUT_SECONDS)
    connection, _ = listener.accept()
    try:
        _, fds, _, _ = socket.recv_fds(connection, 64, 1)
    finally:
        connection.close()
    if not fds:
        raise RuntimeError("sandbox connected but sent no file descriptor")
    return fds[0]


def start_nat_and_pump(scenario, scratch, packet_fd):
    """slirp4netns reads frames from one end of a SEQPACKET socketpair; pump.py bridges the other end to the veth."""
    slirp_end, pump_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    ready_read, ready_write = os.pipe()
    cmd = [BIN_PATHS["slirp4netns"], "--netns-type=tapfd", f"--ready-fd={ready_write}", f"--mtu={scenario['mtu']}"]
    if scenario["hardened"]:
        cmd.append("--disable-host-loopback")
    cmd.append(str(slirp_end.fileno()))
    slirp_log = open(os.path.join(scratch, "slirp.log"), "ab")
    pump_log = open(os.path.join(scratch, "pump.log"), "ab")
    info = {"cmd": [os.path.basename(cmd[0])] + cmd[1:]}
    slirp = pump = None
    try:
        slirp = subprocess.Popen(cmd, stdout=slirp_log, stderr=slirp_log, pass_fds=(slirp_end.fileno(), ready_write), start_new_session=True)
        pump_cmd = [PYTHON, PUMP_SCRIPT, str(packet_fd), str(pump_end.fileno())]
        if scenario.get("filter"):
            pump_cmd.append(json.dumps({"deny": PRIVATE_RANGES + [f"{container_ip()}/32"], "allow": [SLIRP_SUBNET]}))
        info["pump_cmd_args"] = pump_cmd[4:]
        pump = subprocess.Popen(pump_cmd, stdout=pump_log, stderr=pump_log, pass_fds=(packet_fd, pump_end.fileno()), start_new_session=True)
    finally:
        os.close(ready_write)
        slirp_end.close()
        pump_end.close()
        os.close(packet_fd)
        slirp_log.close()
        pump_log.close()
    readable, _, _ = select.select([ready_read], [], [], ATTACH_READY_TIMEOUT_SECONDS)
    info["ready"] = bool(readable and os.read(ready_read, 8))
    os.close(ready_read)
    info["slirp_log"] = tail(os.path.join(scratch, "slirp.log"), 600)
    return slirp, pump, info


def process_cost(pid):
    stat = read(f"/proc/{pid}/stat", 1000)
    fields = stat.rsplit(")", 1)[-1].split()
    cpu_seconds = (int(fields[11]) + int(fields[12])) / CLOCK_TICKS_PER_SECOND if len(fields) > 12 else None
    rss = [l for l in read(f"/proc/{pid}/status", 4000).splitlines() if l.startswith(("VmRSS", "VmHWM"))]
    return {"cpu_seconds": cpu_seconds, "memory": rss}


def http_get(open_socket, request_path, timeout=30):
    sock = open_socket()
    sock.settimeout(timeout)
    started = time.perf_counter()
    sock.sendall(f"GET {request_path} HTTP/1.0\r\nHost: probe\r\n\r\n".encode())
    head = b""
    total = 0
    while True:
        chunk = sock.recv(1 << 16)
        if not chunk:
            break
        if len(head) < 32:
            head += chunk[:32]
        total += len(chunk)
    sock.close()
    status = int(head.split()[1]) if head.startswith(b"HTTP/") else None
    return status, total, time.perf_counter() - started


def measure_path(open_socket):
    out = {}
    try:
        samples = []
        for _ in range(PING_SAMPLES):
            status, _, seconds = http_get(open_socket, "/ping", 10)
            if status != 200:
                raise RuntimeError(f"ping status {status}")
            samples.append(seconds * 1000)
        samples.sort()
        out["ping_ms"] = {"p50": round(samples[len(samples) // 2], 2), "p95": round(samples[int(len(samples) * 0.95) - 1], 2), "max": round(samples[-1], 2), "n": len(samples)}
        status, total, seconds = http_get(open_socket, f"/bulk?mb={BULK_MEGABYTES}", 60)
        out["bulk"] = {"status": status, "megabytes": round(total / (1 << 20), 1), "seconds": round(seconds, 2), "mb_per_s": round(total / (1 << 20) / seconds, 1)}
    except Exception as e:  # a failing inbound path is a finding
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def unix_opener(socket_path):
    def open_socket():
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(10)
        sock.connect(socket_path)
        return sock
    return open_socket


def inbound_tests(scratch):
    out = {"relay_unix_socket": measure_path(unix_opener(os.path.join(scratch, "relay.sock")))}
    try:
        socket.create_connection(("127.0.0.1", SANDBOX_APP_PORT), 2).close()
        out["host_loopback_to_app_port"] = "CONNECTED (child port visible in host namespace)"
    except OSError as e:
        out["host_loopback_to_app_port"] = f"{type(e).__name__}: {e} (expected: the port lives in the child's namespace)"
    return out


def run_scenario(scenario):
    out = {"spec": scenario}
    if scenario["network"] == "veth" and "slirp4netns" not in BIN_PATHS:
        out["skipped"] = "slirp4netns is not usable (see 'binaries' check)"
        return out
    scratch = tempfile.mkdtemp(prefix="probe-ns-", dir=SCRATCH_ROOT)
    decoys = Decoys()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    procs = []
    pump = None
    try:
        listener.bind(os.path.join(scratch, "fd.sock"))
        listener.listen(1)
        with open(os.path.join(scratch, "config.json"), "w") as f:
            json.dump(base_config(scratch, decoys.dir, decoys.tcp_port, decoys.unix_path, decoys.sleeper.pid, scenario), f)
        launch_started = time.time()
        child, variant, attempts = launch_sandbox(scratch)
        out["launch"] = {"variant": variant, "attempts": attempts, "seconds": round(time.time() - launch_started, 2)}
        if child is None:
            out["error"] = "sandbox could not be created"
            return out
        procs.append(child)
        if scenario["network"] == "veth":
            try:
                packet_fd = receive_packet_fd(listener)
            except (OSError, RuntimeError) as e:
                out["error"] = f"no AF_PACKET socket from the sandbox: {type(e).__name__}: {e}"
                out["child_log"] = tail(os.path.join(scratch, "child.log"))
                return out
            nat, pump, out["attach"] = start_nat_and_pump(scenario, scratch, packet_fd)
            procs.append(nat)
            if not out["attach"]["ready"]:
                out["error"] = "slirp4netns did not report ready"
                return out
        with open(os.path.join(scratch, "attached"), "w") as f:
            f.write("1")
        if not wait_for(os.path.join(scratch, "server.ready"), SERVER_READY_TIMEOUT_SECONDS, child):
            out["error"] = "sandbox never reported server.ready"
            out["child_log"] = tail(os.path.join(scratch, "child.log"))
            return out
        out["inbound"] = inbound_tests(scratch)
        with open(os.path.join(scratch, "inbound_done"), "w") as f:
            f.write("1")
        if wait_for(os.path.join(scratch, "result.json"), CHILD_RESULT_TIMEOUT_SECONDS, child):
            with open(os.path.join(scratch, "result.json")) as f:
                out["sandbox"] = json.load(f)
        else:
            out["error"] = "sandbox produced no result.json in time"
        if scenario["network"] == "veth":
            out["slirp_process_cost"] = process_cost(procs[-1].pid)
            out["pump_process_cost"] = process_cost(pump.pid)
        out["child_log"] = tail(os.path.join(scratch, "child.log"), 800)
        with open(os.path.join(scratch, "stop"), "w") as f:
            f.write("1")
        return out
    finally:
        if pump is not None:
            pump.terminate()  # SIGTERM makes the pump print its frame counters
            try:
                pump.wait(timeout=5)
            except subprocess.TimeoutExpired:
                kill_group(pump)
            out["pump_stats"] = tail(os.path.join(scratch, "pump.log"), 800)
        for proc in reversed(procs):
            kill_group(proc)
        listener.close()
        decoys.close()
        shutil.rmtree(scratch, ignore_errors=True)  # best-effort cleanup of a temp dir


def run_control():
    scratch = tempfile.mkdtemp(prefix="probe-control-", dir=SCRATCH_ROOT)
    decoys = Decoys()
    try:
        scenario = {"name": "control", "network": "host", "full": True}
        with open(os.path.join(scratch, "config.json"), "w") as f:
            json.dump(base_config(scratch, decoys.dir, decoys.tcp_port, decoys.unix_path, decoys.sleeper.pid, scenario), f)
        proc = subprocess.run([PYTHON, CHILD_SCRIPT, scratch, "--control"], capture_output=True, text=True, timeout=CONTROL_TIMEOUT_SECONDS, env=child_env(scratch))
        result = {}
        if os.path.exists(os.path.join(scratch, "result.json")):
            with open(os.path.join(scratch, "result.json")) as f:
                result = json.load(f)
        result["rc"] = proc.returncode
        result["stderr_tail"] = proc.stderr.strip()[-600:]
        return result
    finally:
        decoys.close()
        shutil.rmtree(scratch, ignore_errors=True)  # best-effort cleanup of a temp dir


CHECKS = (
    ("platform", check_platform),
    ("binaries", check_binaries),
    ("persistence", check_persistence),
    ("control", run_control),
) + tuple((f"scenario_{s['name']}", (lambda s=s: run_scenario(s))) for s in SCENARIOS)


def run_all_checks():
    # appmux itself must be non-dumpable so a same-uid child cannot read its env/memory.
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    for name, check in CHECKS:
        try:
            result = check()
        except Exception as e:  # a failed check is itself a finding; keep going
            result = {"error": f"{type(e).__name__}: {e}"}
        result = deep_scrub(result)
        with REPORT_LOCK:
            REPORT["checks"][name] = result
        print(f"PROBE2 {name} {json.dumps(result)}", flush=True)
    with REPORT_LOCK:
        REPORT["finished"] = True
    print("PROBE2-DONE", flush=True)


def redacted_headers(headers):
    return {k: (f"<redacted len={len(v)}>" if SENSITIVE_HEADER.search(k) else v) for k, v in headers.items()}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, content_type, body):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        route = self.path.split("?")[0]
        if route == "/healthz":
            self._send(200, "text/plain", "ok")
            return
        with REPORT_LOCK:
            snapshot = copy.deepcopy(REPORT)
        snapshot["request"] = {"peer_address": self.client_address[0], "headers": redacted_headers(self.headers)}
        text = json.dumps(deep_scrub(snapshot), indent=2, sort_keys=True)
        if route == "/report.json":
            self._send(200, "application/json", text)
            return
        status = "finished" if snapshot["finished"] else "still running - refresh in ~1 min"
        self._send(200, "text/html; charset=utf-8", f"<h3>appmux probe v2: {status}</h3><pre>{html.escape(text)}</pre>")

    def log_message(self, *args):
        pass


def main():
    if os.environ.get("PROBE_ONCE"):
        run_all_checks()
        return
    threading.Thread(target=run_all_checks, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
