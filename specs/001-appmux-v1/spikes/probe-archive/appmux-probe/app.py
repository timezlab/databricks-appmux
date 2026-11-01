"""Throwaway probe: measures what a Databricks App container allows.

Not part of appmux. It answers the isolation questions the docs leave open
(OS user, namespaces, cgroups, /proc visibility, egress, pip/venv) so the
design can pick one mechanism instead of guessing.

Safety: env vars are reported by NAME only (values only for a small
allowlist), credential-bearing headers are redacted, and every string in
the report goes through deep_scrub() before it leaves the process.
Stdlib only, so the probe itself needs no package install.
"""
import concurrent.futures
import copy
import ctypes
import html
import json
import os
import re
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
BOOT_ID = str(uuid.uuid4())
COMMAND_TIMEOUT_SECONDS = 15
PIP_TIMEOUT_SECONDS = 120
HTTP_TIMEOUT_SECONDS = 6
FORK_PROBE_LIMIT = 64
PR_SET_DUMPABLE = 4
CGROUP_ROOT = "/sys/fs/cgroup"

# Values safe to print; every other env var is reported by name only.
PLAIN_ENV_VALUES = (
    "DATABRICKS_APP_PORT", "DATABRICKS_APP_NAME", "DATABRICKS_WORKSPACE_ID",
    "DATABRICKS_HOST", "USER", "HOME", "LANG", "PWD", "PATH", "VIRTUAL_ENV",
)
MARKER_PATHS = (
    os.path.join(tempfile.gettempdir(), "appmux-probe-marker.json"),
    os.path.join(os.getcwd(), "appmux-probe-marker.json"),
)
OUTBOUND_TARGETS = (
    "https://pypi.org/simple/pip/",
    "https://files.pythonhosted.org/",
    "https://github.com/",
    "https://api.github.com/",
    "https://registry.npmjs.org/",
)
RLIMIT_NAMES = (
    "RLIMIT_NOFILE", "RLIMIT_NPROC", "RLIMIT_AS", "RLIMIT_CPU",
    "RLIMIT_DATA", "RLIMIT_FSIZE", "RLIMIT_CORE",
)
SENSITIVE_HEADER = re.compile(r"(?i)token|authorization|cookie|secret")
SCRUB_PATTERNS = (
    (re.compile(r"(?i)((?:secret|token|password|passwd|api[_-]?key|authorization)[\"'=: ]+)[^\s\"',}]+"), r"\1<redacted>"),
    (re.compile(r"dapi[0-9a-f]{16,}"), "<redacted-pat>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"), "<redacted-jwt>"),
    (re.compile(r"(://)[^/@\s]+:[^/@\s]+@"), r"\1<redacted>@"),
)

# Run in a child process: can it read the target pid's /proc entries?
# Prints env var NAMES only, never values.
PROC_READER = r"""
import json, sys
pid = int(sys.argv[1])
out = {}
for name in ("environ", "mem", "cmdline", "maps"):
    path = f"/proc/{pid}/{name}"
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 16) if name != "mem" else b""
        out[name] = "readable"
        if name == "environ":
            out["environ_names"] = sorted({e.split(b"=", 1)[0].decode("utf8", "replace") for e in data.split(b"\0") if e})
    except Exception as e:
        out[name] = f"{type(e).__name__}: {e}"
print(json.dumps(out))
"""

# Run in a child process: does RLIMIT_AS actually stop an allocation?
RLIMIT_AS_TEST = r"""
import resource
resource.setrlimit(resource.RLIMIT_AS, (256 << 20, 256 << 20))
try:
    bytearray(400 << 20)
    print("NOT-ENFORCED")
except MemoryError:
    print("ENFORCED")
"""

LOCAL_CONNECT_TEST = r"""
import socket, sys
kind, target = sys.argv[1], sys.argv[2]
try:
    if kind == "tcp":
        socket.create_connection(("127.0.0.1", int(target)), 3).close()
    else:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(3)
        s.connect(target)
        s.close()
    print("connected")
except Exception as e:
    print(f"{type(e).__name__}: {e}")
"""

REPORT = {"boot_id": BOOT_ID, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "finished": False, "checks": {}}
REPORT_LOCK = threading.Lock()


def sh(cmd, timeout=COMMAND_TIMEOUT_SECONDS):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return {"rc": None, "out": "binary not found"}
    except subprocess.TimeoutExpired:
        return {"rc": None, "out": f"timeout after {timeout}s"}
    return {"rc": proc.returncode, "out": (proc.stdout + proc.stderr).strip()[-1500:]}


def read(path, limit=2000):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(limit).strip()
    except OSError as e:
        return f"unreadable: {type(e).__name__}: {e}"


def grep_lines(path, prefixes):
    return [line for line in read(path, 100000).splitlines() if line.startswith(prefixes)]


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


def check_identity():
    return {
        "uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(), "groups": os.getgroups(),
        "id": sh(["id"]),
        "sudo_n_true": sh(["sudo", "-n", "true"]),
        "proc_status": grep_lines("/proc/self/status", ("Uid", "Gid", "Cap", "NoNewPrivs", "Seccomp")),
        "pid": os.getpid(), "ppid": os.getppid(), "pid1_comm": read("/proc/1/comm"),
    }


def check_kernel():
    return {
        "uname": sh(["uname", "-a"]),
        "proc_version": read("/proc/version"),
        "osrelease": read("/proc/sys/kernel/osrelease"),
        "dockerenv": os.path.exists("/.dockerenv"),
        "containerenv": os.path.exists("/run/.containerenv"),
        "cpu_count": os.cpu_count(),
        "cpu_affinity": len(os.sched_getaffinity(0)),
        "meminfo": grep_lines("/proc/meminfo", ("MemTotal", "MemAvailable")),
    }


def check_env():
    names = sorted(os.environ)
    return {
        "names": names,
        "databricks_names": [n for n in names if n.startswith("DATABRICKS_")],
        "plain_values": {n: os.environ[n] for n in PLAIN_ENV_VALUES if n in os.environ},
    }


def check_runtime():
    tools = ("pip", "uv", "node", "npm", "git", "curl", "wget", "gcc", "make")
    return {
        "python": sys.version,
        "executable": sys.executable,
        "in_venv": sys.prefix != sys.base_prefix,
        "tools": {t: shutil.which(t) for t in tools},
        "node_version": sh(["node", "--version"]),
        "pip_version": sh([sys.executable, "-m", "pip", "--version"]),
    }


def check_namespaces():
    tools = ("unshare", "bwrap", "setpriv", "nsenter", "chroot", "sudo", "su", "firejail", "nsjail", "runc")
    attempts = {
        "user_ns": ["unshare", "-Ur", "id"],
        "user_mount_ns": ["unshare", "-Urm", "true"],
        "user_net_ns": ["unshare", "-Urn", "sh", "-c", "cat /proc/net/dev | wc -l"],
        "user_pid_ns": ["unshare", "-Ur", "--pid", "--fork", "--mount-proc", "sh", "-c", "ls /proc | grep -c '^[0-9]'"],
    }
    found = {t: shutil.which(t) for t in tools}
    result = {"tools": found, "attempts": {name: sh(cmd) for name, cmd in attempts.items()}}
    if found["bwrap"]:
        result["attempts"]["bwrap"] = sh(["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--die-with-parent", "id"])
    result["sysctl"] = {
        p: read(p, 50) for p in (
            "/proc/sys/user/max_user_namespaces",
            "/proc/sys/kernel/unprivileged_userns_clone",
            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns",
        )
    }
    result["uid_map"] = read("/proc/self/uid_map")
    result["ns_links"] = {n: os.readlink(f"/proc/self/ns/{n}") for n in sorted(os.listdir("/proc/self/ns"))}
    return result


def check_cgroups():
    own = read("/proc/self/cgroup")
    relative = own.split(":", 2)[-1].strip() if own.startswith("0::") else ""
    dirs = [CGROUP_ROOT]
    if relative and relative != "/":
        dirs.append(os.path.join(CGROUP_ROOT, relative.lstrip("/")))
    files = ("cgroup.controllers", "cgroup.subtree_control", "memory.max", "memory.current", "cpu.max", "pids.max", "pids.current")
    per_dir = {}
    for d in dirs:
        entry = {f: read(os.path.join(d, f), 200) for f in files}
        entry["writable"] = os.access(d, os.W_OK)
        child = os.path.join(d, "appmux-probe")
        try:
            os.mkdir(child)
            os.rmdir(child)
            entry["mkdir_child_cgroup"] = "ok"
        except OSError as e:
            entry["mkdir_child_cgroup"] = f"{type(e).__name__}: {e}"
        per_dir[d] = entry
    return {
        "self_cgroup": own,
        "mounts": grep_lines("/proc/mounts", ("cgroup",)),
        "dirs": per_dir,
        "v1_memory_limit": read("/sys/fs/cgroup/memory/memory.limit_in_bytes", 50),
    }


def check_limits():
    limits = {n: resource.getrlimit(getattr(resource, n)) for n in RLIMIT_NAMES}
    spawned = []
    try:
        for _ in range(FORK_PROBE_LIMIT):
            spawned.append(subprocess.Popen(["sleep", "10"]))
        fork_result = f"spawned {len(spawned)}/{FORK_PROBE_LIMIT}"
    except OSError as e:
        fork_result = f"stopped at {len(spawned)}: {type(e).__name__}: {e}"
    finally:
        for p in spawned:
            p.kill()
            p.wait()
    return {
        "rlimits": limits,
        "rlimit_as_enforcement": sh([sys.executable, "-c", RLIMIT_AS_TEST]),
        "fork_probe": fork_result,
    }


def check_filesystem():
    candidates = (os.getcwd(), tempfile.gettempdir(), os.path.expanduser("~"), "/home", "/opt", "/var/tmp", "/dev/shm", "/usr/local", "/")
    dirs = {}
    for d in dict.fromkeys(candidates):
        entry = {"exists": os.path.isdir(d)}
        if entry["exists"]:
            try:
                with tempfile.NamedTemporaryFile(dir=d):
                    entry["can_create_file"] = True
            except OSError as e:
                entry["can_create_file"] = f"{type(e).__name__}: {e}"
            usage = shutil.disk_usage(d)
            entry["free_mb"] = usage.free >> 20
            entry["total_mb"] = usage.total >> 20
        dirs[d] = entry
    # A marker that survives a restart reveals whether the filesystem persists.
    markers = {}
    for path in MARKER_PATHS:
        markers[path] = {"previous": read(path, 200) if os.path.exists(path) else None}
        try:
            with open(path, "w") as f:
                json.dump({"boot_id": BOOT_ID, "written_at": time.time()}, f)
        except OSError as e:
            markers[path]["write_error"] = f"{type(e).__name__}: {e}"
    return {"dirs": dirs, "markers": markers, "mount_count": len(read("/proc/mounts", 200000).splitlines())}


def check_processes():
    procs = []
    for entry in sorted((e for e in os.listdir("/proc") if e.isdigit()), key=int):
        uid_line = next(iter(grep_lines(f"/proc/{entry}/status", ("Uid",))), "")
        cmd = read(f"/proc/{entry}/cmdline", 200).replace("\x00", " ")
        procs.append({"pid": int(entry), "uid": uid_line.split()[1:2], "cmd": cmd[:120]})
    return {
        "count": len(procs),
        "proc_mount": grep_lines("/proc/mounts", ("proc ",)),
        "processes": procs[:60],
    }


def tcp_listeners():
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in read(path, 200000).splitlines()[1:]:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "0A":
                ports.add(int(parts[1].rsplit(":", 1)[1], 16))
    return sorted(ports)


def fetch_status(url):
    request = urllib.request.Request(url, headers={"User-Agent": "appmux-probe"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return {"url": url, "status": response.status}
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "note": "server reached"}
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"}


def check_local_reachability():
    # Models a hostile sibling child connecting straight to another child's port.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    tcp_port = listener.getsockname()[1]
    sock_dir = tempfile.mkdtemp(prefix="probe-sock-")
    sock_path = os.path.join(sock_dir, "a.sock")
    unix_listener = socket.socket(socket.AF_UNIX)
    unix_listener.bind(sock_path)
    os.chmod(sock_path, 0o600)
    unix_listener.listen(1)
    try:
        return {
            "tcp_localhost_from_other_process": sh([sys.executable, "-c", LOCAL_CONNECT_TEST, "tcp", str(tcp_port)]),
            "unix_socket_0600_from_other_process": sh([sys.executable, "-c", LOCAL_CONNECT_TEST, "unix", sock_path]),
        }
    finally:
        listener.close()
        unix_listener.close()
        shutil.rmtree(sock_dir, ignore_errors=True)  # best-effort cleanup of a temp dir


def check_network():
    targets = list(OUTBOUND_TARGETS)
    host = os.environ.get("DATABRICKS_HOST", "")
    if host:
        targets.append(host if host.startswith("http") else f"https://{host}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        outbound = list(pool.map(fetch_status, targets))
    work_dir = tempfile.mkdtemp(prefix="probe-pip-")
    try:
        pip_download = sh([sys.executable, "-m", "pip", "download", "--no-deps", "--no-cache-dir", "-d", work_dir, "six==1.16.0"], PIP_TIMEOUT_SECONDS)
        venv_create = sh([sys.executable, "-m", "venv", os.path.join(work_dir, "venv")], PIP_TIMEOUT_SECONDS)
        pip_config = sh([sys.executable, "-m", "pip", "config", "list"])
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)  # best-effort cleanup of a temp dir
    return {
        "listening_tcp_ports": tcp_listeners(),
        "outbound": outbound,
        "pip_download_six": pip_download,
        "venv_create": venv_create,
        "pip_config": pip_config,
        "local_reachability": check_local_reachability(),
    }


def run_proc_reader(pid):
    proc = subprocess.run([sys.executable, "-c", PROC_READER, str(pid)], capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stdout + proc.stderr).strip()[-500:]}


def check_proc_access():
    """Runs last: it flips this process to non-dumpable."""
    sleeper = subprocess.Popen(["sleep", "30"], env={**os.environ, "PROBE_SIBLING_MARKER": "x"})
    try:
        before = run_proc_reader(os.getpid())
        sibling = run_proc_reader(sleeper.pid)
        pid1 = run_proc_reader(1)
        set_dumpable_rc = ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
        after = run_proc_reader(os.getpid())
    finally:
        sleeper.kill()
        sleeper.wait()
    return {
        "ptrace_scope": read("/proc/sys/kernel/yama/ptrace_scope", 20),
        "main_process_before_nondumpable": before,
        "sibling_same_uid": sibling,
        "pid1": pid1,
        "set_dumpable_rc": set_dumpable_rc,
        "main_process_after_nondumpable": after,
    }


CHECKS = (
    ("identity", check_identity),
    ("kernel", check_kernel),
    ("env", check_env),
    ("runtime", check_runtime),
    ("namespaces", check_namespaces),
    ("cgroups", check_cgroups),
    ("limits", check_limits),
    ("filesystem", check_filesystem),
    ("processes", check_processes),
    ("network", check_network),
    ("proc_access", check_proc_access),
)


def run_all_checks():
    for name, check in CHECKS:
        try:
            result = check()
        except Exception as e:  # a failed check is itself a finding; keep going
            result = {"error": f"{type(e).__name__}: {e}"}
        with REPORT_LOCK:
            REPORT["checks"][name] = result
    with REPORT_LOCK:
        REPORT["finished"] = True
        snapshot = copy.deepcopy(REPORT)
    print(json.dumps(deep_scrub(snapshot)), flush=True)


def redacted_headers(headers):
    return {
        k: (f"<redacted len={len(v)}>" if SENSITIVE_HEADER.search(k) else v)
        for k, v in headers.items()
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, content_type, body):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            self._send(200, "text/plain", "ok")
            return
        with REPORT_LOCK:
            snapshot = copy.deepcopy(REPORT)
        snapshot["request_headers"] = redacted_headers(self.headers)
        text = json.dumps(deep_scrub(snapshot), indent=2, sort_keys=True)
        if path == "/report.json":
            self._send(200, "application/json", text)
            return
        status = "finished" if snapshot["finished"] else "still running - refresh in ~30s"
        self._send(200, "text/html; charset=utf-8", f"<h3>appmux probe: {status}</h3><pre>{html.escape(text)}</pre>")


def main():
    threading.Thread(target=run_all_checks, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
