"""Probe v2, sandbox side. Not part of appmux.

Runs inside the namespaces that app.py builds (user + mount + net, and pid when
the platform allows it), or with --control in the plain container as the
no-isolation baseline. Both modes run the same battery, so the two result sets
are directly comparable.

Sandbox flow: optionally build a veth pair and hand the AF_PACKET end to the
parent (the no-/dev/net/tun network path) -> wait for the parent to attach the
user-mode NAT -> read the network state -> lock the filesystem down (tmpfs over
/proc and /tmp) -> drop every capability -> serve a tiny app behind a
unix-socket relay -> run the battery as the "hostile child". Everything is
recorded, nothing is fatal: a failed step is a finding. Stdlib only; keep f-strings 3.11-safe (no nested same-type quotes).
"""
import concurrent.futures
import ctypes
import errno
import fcntl
import http.client
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MS_NOSUID, MS_NODEV, MS_NOEXEC = 2, 4, 8
MS_BIND, MS_REC, MS_PRIVATE = 4096, 16384, 1 << 18
MNT_DETACH = 2
PR_CAPBSET_DROP, PR_SET_NO_NEW_PRIVS, PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL = 24, 38, 47, 4
SYS_CAPSET_X86_64 = 126
LINUX_CAPABILITY_VERSION_3 = 0x20080522
SIOCGIFFLAGS, SIOCSIFFLAGS, SIOCGIFADDR = 0x8913, 0x8914, 0x8915
IFF_UP = 1
IFREQ_SIZE = 40
SIOCETHTOOL = 0x8946
# ETHTOOL_S* command ids, checked against linux v6.1 include/uapi/linux/ethtool.h
ETHTOOL_OFFLOADS_OFF = ((0x17, "tx_checksum"), (0x19, "scatter_gather"), (0x1F, "tso"), (0x24, "gso"), (0x2C, "gro"))
NLMSG_ERROR = 2
NLM_F_REQUEST, NLM_F_ACK, NLM_F_EXCL, NLM_F_CREATE = 1, 4, 0x200, 0x400
RTM_NEWLINK, RTM_NEWADDR, RTM_NEWROUTE = 16, 20, 24
IFLA_IFNAME, IFLA_MTU, IFLA_LINKINFO = 3, 4, 18
IFLA_INFO_KIND, IFLA_INFO_DATA, VETH_INFO_PEER = 1, 2, 1
IFA_ADDRESS, IFA_LOCAL = 1, 2
RTA_OIF, RTA_GATEWAY = 4, 5
RT_TABLE_MAIN, RTPROT_BOOT, RTN_UNICAST = 254, 3, 1
ETH_P_ALL = 3
CHILD_IFACE, PUMP_IFACE = "eth0", "vpump0"
CHILD_IP, GATEWAY_IP, SUBNET_PREFIX = "10.0.2.100", "10.0.2.2", 24
SCAN_TIMEOUT_SECONDS = 1.0
SCAN_THREADS = 64
HTTP_TIMEOUT_SECONDS = 8
PIP_TIMEOUT_SECONDS = 150
NETWORK_READY_TIMEOUT_SECONDS = 20
ATTACH_TIMEOUT_SECONDS = 60
INBOUND_TIMEOUT_SECONDS = 240
BULK_CHUNK = b"\0" * 65536
HTTPS_TARGETS = ("pypi.org", "files.pythonhosted.org", "github.com", "registry.npmjs.org")
RUNTIME_CFG_PATH = "/databricks/runtime-secrets/runtime.cfg"

SCRATCH = sys.argv[1] if len(sys.argv) > 1 else ""
CONTROL = "--control" in sys.argv
RESULT = {"timings_s": {}}
libc = ctypes.CDLL(None, use_errno=True)


def path(name):
    return os.path.join(SCRATCH, name)


def touch(name, content=""):
    with open(path(name), "w") as f:
        f.write(content)


def wait_file(name, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path(name)):
            return True
        time.sleep(0.1)
    return False


def step(name, fn, *args):
    started = time.perf_counter()
    try:
        RESULT[name] = fn(*args)
    except Exception as e:  # a failed step is itself a finding; keep going
        RESULT[name] = {"error": f"{type(e).__name__}: {e}"}
    RESULT["timings_s"][name] = round(time.perf_counter() - started, 2)


def sub(results, name, fn, *args):
    try:
        results[name] = fn(*args)
    except Exception as e:  # same: record, keep going
        results[name] = {"error": f"{type(e).__name__}: {e}"}


def errno_text(e):
    return f"{errno.errorcode.get(e.errno, e.errno)}: {e.strerror}"


def read_text(p, limit=4000):
    try:
        with open(p, errors="replace") as f:
            return f.read(limit)
    except OSError as e:
        return f"unreadable: {errno_text(e)}"


# --- privileged setup (runs as namespace root, before capabilities are dropped) ---

def mount(source, target, fstype, flags, data=None):
    rc = libc.mount(
        source.encode() if source else None, target.encode(),
        fstype.encode() if fstype else None, flags, data.encode() if data else None,
    )
    if rc != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"mount {fstype or 'bind'} -> {target}: {os.strerror(code)}")


def umount_detach(target):
    if libc.umount2(target.encode(), MNT_DETACH) != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"umount {target}: {os.strerror(code)}")


def ifreq(name, flags=0):
    return struct.pack("16sH22s", name.encode(), flags, b"")


def set_loopback_up():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        current = struct.unpack("16sH22s", fcntl.ioctl(sock.fileno(), SIOCGIFFLAGS, ifreq("lo")))[1]
        fcntl.ioctl(sock.fileno(), SIOCSIFFLAGS, ifreq("lo", current | IFF_UP))
    finally:
        sock.close()
    return "up"


def interface_ipv4(name):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        raw = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, struct.pack("256s", name.encode()[:15]))
        return socket.inet_ntoa(raw[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def default_route():
    for line in read_text("/proc/net/route", 20000).splitlines()[1:]:
        fields = line.split()
        if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 2:
            return fields[0], socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    return None


def wait_for_network():
    started = time.time()
    while time.time() - started < NETWORK_READY_TIMEOUT_SECONDS:
        route = default_route()
        if route:
            return {"interface": route[0], "gateway": route[1], "ready_after_s": round(time.time() - started, 2)}
        time.sleep(0.1)
    return {"error": "no default route within timeout"}


# --- no-TUN network path: veth pair + AF_PACKET, built over rtnetlink (the container has no `ip`) ---

def nl_attr(kind, payload):
    length = 4 + len(payload)
    return struct.pack("HH", length, kind) + payload + b"\0" * (-length % 4)


def nl_request(sock, msg_type, flags, body):
    sock.send(struct.pack("IHHII", 16 + len(body), msg_type, flags | NLM_F_REQUEST | NLM_F_ACK, 1, 0) + body)
    reply = sock.recv(8192)
    reply_type = struct.unpack_from("IHHII", reply)[1]
    if reply_type == NLMSG_ERROR:
        code = struct.unpack_from("i", reply, 16)[0]
        if code != 0:
            raise OSError(-code, f"rtnetlink request {msg_type}: {os.strerror(-code)}")


def ifinfomsg(index=0, flags=0, change=0):
    return struct.pack("BxHiII", 0, 0, index, flags, change)


def disable_offloads(name):
    """veth hands CHECKSUM_PARTIAL frames to AF_PACKET readers; turn offloads off so frames leave complete."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    results = {}
    try:
        for command, label in ETHTOOL_OFFLOADS_OFF:
            value = ctypes.create_string_buffer(struct.pack("II", command, 0))
            request = struct.pack("16sP", name.encode(), ctypes.addressof(value)).ljust(IFREQ_SIZE, b"\0")
            try:
                fcntl.ioctl(sock.fileno(), SIOCETHTOOL, request)
                results[label] = "off"
            except OSError as e:
                results[label] = errno_text(e)
    finally:
        sock.close()
    return results


def setup_veth(cfg):
    nl = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
    nl.bind((0, 0))
    out = {}
    try:
        peer = ifinfomsg() + nl_attr(IFLA_IFNAME, PUMP_IFACE.encode() + b"\0")
        linkinfo = nl_attr(IFLA_INFO_KIND, b"veth\0") + nl_attr(IFLA_INFO_DATA, nl_attr(VETH_INFO_PEER, peer))
        create = ifinfomsg() + nl_attr(IFLA_IFNAME, CHILD_IFACE.encode() + b"\0") + nl_attr(IFLA_LINKINFO, linkinfo)
        nl_request(nl, RTM_NEWLINK, NLM_F_CREATE | NLM_F_EXCL, create)
        out["veth_created"] = f"{CHILD_IFACE} <-> {PUMP_IFACE}"
        child_index = socket.if_nametoindex(CHILD_IFACE)
        pump_index = socket.if_nametoindex(PUMP_IFACE)
        address = socket.inet_aton(CHILD_IP)
        nl_request(nl, RTM_NEWADDR, NLM_F_CREATE | NLM_F_EXCL,
                   struct.pack("BBBBI", socket.AF_INET, SUBNET_PREFIX, 0, 0, child_index)
                   + nl_attr(IFA_LOCAL, address) + nl_attr(IFA_ADDRESS, address))
        out["offloads"] = {name: disable_offloads(name) for name in (CHILD_IFACE, PUMP_IFACE)}
        mtu = cfg.get("mtu", 1500)
        for index in (child_index, pump_index):
            nl_request(nl, RTM_NEWLINK, 0, ifinfomsg(index, IFF_UP, IFF_UP) + nl_attr(IFLA_MTU, struct.pack("I", mtu)))
        out["mtu"] = mtu
        nl_request(nl, RTM_NEWROUTE, NLM_F_CREATE | NLM_F_EXCL,
                   struct.pack("BBBBBBBBI", socket.AF_INET, 0, 0, 0, RT_TABLE_MAIN, RTPROT_BOOT, 0, RTN_UNICAST, 0)
                   + nl_attr(RTA_GATEWAY, socket.inet_aton(GATEWAY_IP)) + nl_attr(RTA_OIF, struct.pack("I", child_index)))
        out["configured"] = f"{CHILD_IP}/{SUBNET_PREFIX} via {GATEWAY_IP}"
    finally:
        nl.close()
    packet = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    try:
        packet.bind((PUMP_IFACE, 0))
        out["af_packet"] = "bound"
        handoff = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            handoff.connect(path("fd.sock"))
            socket.send_fds(handoff, [b"packet-socket"], [packet.fileno()])
        finally:
            handoff.close()
        out["handoff"] = "AF_PACKET fd sent to parent"
    finally:
        packet.close()  # the parent holds the only copy; the hostile child must never see this fd
    return out


def lockdown(cfg):
    steps = {}

    def attempt(name, fn):
        try:
            fn()
            steps[name] = "ok"
        except Exception as e:  # recorded as the finding for this step
            steps[name] = f"{type(e).__name__}: {e}"

    attempt("make_mounts_private", lambda: mount(None, "/", None, MS_REC | MS_PRIVATE))
    if cfg.get("resolv_bind"):
        resolv = path("resolv.conf")
        with open(resolv, "w") as f:
            f.write("nameserver 10.0.2.3\n")
        attempt("bind_resolv_conf", lambda: mount(resolv, "/etc/resolv.conf", None, MS_BIND))

    old_proc = path("oldproc")
    host_view = {}

    def snapshot_old_proc():
        os.makedirs(old_proc, exist_ok=True)
        mount("/proc", old_proc, None, MS_BIND | MS_REC)
        host_view["self_pid"] = os.readlink("/proc/self")

    attempt("snapshot_old_proc", snapshot_old_proc)
    attempt("tmpfs_over_proc", lambda: mount("tmpfs", "/proc", "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, "size=1m,mode=0755"))
    if steps["tmpfs_over_proc"] == "ok":
        def bind_proc_file(name):
            target = f"/proc/{name}"
            open(target, "w").close()
            mount(f"{old_proc}/{name}", target, None, MS_BIND)

        def bind_proc_self():
            os.mkdir("/proc/self")
            mount(f"{old_proc}/{host_view['self_pid']}", "/proc/self", None, MS_BIND)

        for name in ("cpuinfo", "meminfo"):
            attempt(f"bind_proc_{name}", lambda n=name: bind_proc_file(n))
        attempt("bind_proc_self", bind_proc_self)
    if steps["snapshot_old_proc"] == "ok":
        attempt("detach_old_proc_snapshot", lambda: umount_detach(old_proc))
    attempt("tmpfs_over_tmp", lambda: mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "size=256m,mode=1777"))
    attempt("tmpfs_over_var_tmp", lambda: mount("tmpfs", "/var/tmp", "tmpfs", MS_NOSUID | MS_NODEV, "size=16m,mode=1777"))
    if os.path.isdir(os.path.dirname(RUNTIME_CFG_PATH)):
        attempt("tmpfs_over_runtime_secrets", lambda: mount("tmpfs", os.path.dirname(RUNTIME_CFG_PATH), "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, "size=1m,mode=0755"))
    attempt("drop_capabilities", drop_capabilities)
    return steps


def drop_capabilities():
    for cap in range(64):
        if libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0) != 0 and ctypes.get_errno() == errno.EINVAL:
            break
    libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    header = ctypes.create_string_buffer(struct.pack("Ii", LINUX_CAPABILITY_VERSION_3, 0))
    data = ctypes.create_string_buffer(struct.pack("6I", 0, 0, 0, 0, 0, 0))
    if libc.syscall(SYS_CAPSET_X86_64, header, data) != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"capset: {os.strerror(code)}")
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"no_new_privs: {os.strerror(code)}")


# --- observations ---

def namespace_identity():
    return {
        "pid": os.getpid(), "uid": os.getuid(), "gid": os.getgid(),
        "uid_map": read_text("/proc/self/uid_map", 200).strip(),
        "ns": {n: os.readlink(f"/proc/self/ns/{n}") for n in sorted(os.listdir("/proc/self/ns"))},
        "caps": [l for l in read_text("/proc/self/status", 6000).splitlines() if l.startswith(("Cap", "NoNewPrivs", "Seccomp"))],
    }


def environ_probe(pid):
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            data = f.read(1 << 16)
    except OSError as e:
        return errno_text(e)
    names = [e for e in data.split(b"\0") if e]
    return f"READABLE ({len(names)} vars)"


def signal_probe(pid):
    try:
        os.kill(pid, 0)
        return "SIGNALABLE"
    except ProcessLookupError:
        return "ESRCH (not visible)"
    except PermissionError:
        return "EPERM"


def visibility(cfg):
    """What a hostile child learns about processes it should not touch."""
    try:
        listing = [e for e in os.listdir("/proc") if e.isdigit()]
        visible = len(listing)
    except OSError as e:
        visible = errno_text(e)
    return {
        "visible_pids_in_proc": visible,
        "pid1_environ": environ_probe(1),
        "appmux_environ": environ_probe(cfg["appmux_pid"]),
        "sibling_environ": environ_probe(cfg["sibling_pid"]),
        "kill_appmux": signal_probe(cfg["appmux_pid"]),
        "kill_sibling": signal_probe(cfg["sibling_pid"]),
        "runtime_cfg": runtime_cfg_access(),
    }


def runtime_cfg_access():
    # Permission check only: never reads the file's content.
    try:
        st = os.stat(RUNTIME_CFG_PATH)
        with open(RUNTIME_CFG_PATH, "rb"):
            opened = True
        return {"exists": True, "size": st.st_size, "mode": oct(st.st_mode & 0o777), "open_ok": opened}
    except OSError as e:
        return {"exists": os.path.exists(RUNTIME_CFG_PATH), "error": errno_text(e)}


def sibling_file_probe(cfg):
    out = {}
    canary = os.path.join(cfg["sibling_dir"], "canary.txt")
    try:
        with open(canary) as f:
            out["canary_in_old_tmp"] = f"READABLE ({len(f.read())} bytes)"
    except OSError as e:
        out["canary_in_old_tmp"] = errno_text(e)
    probe = socket.socket(socket.AF_UNIX)
    probe.settimeout(2)
    try:
        probe.connect(cfg["decoy_unix"])
        out["decoy_unix_socket"] = "CONNECTED"
    except OSError as e:
        out["decoy_unix_socket"] = errno_text(e)
    finally:
        probe.close()
    return out


def privilege_checks():
    """After the drop, the child must not be able to undo the sandbox."""
    out = {}
    try:
        mount("tmpfs", "/tmp", "tmpfs", MS_NOSUID, "size=1m")
        out["mount_tmpfs"] = "ALLOWED"
    except OSError as e:
        out["mount_tmpfs"] = errno_text(e)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        fcntl.ioctl(sock.fileno(), SIOCSIFFLAGS, ifreq("lo", 0))
        out["loopback_down"] = "ALLOWED"
    except OSError as e:
        out["loopback_down"] = errno_text(e)
    finally:
        sock.close()
    try:
        socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL)).close()
        out["open_af_packet"] = "ALLOWED"
    except OSError as e:
        out["open_af_packet"] = errno_text(e)
    nl = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
    try:
        nl.bind((0, 0))
        peer = ifinfomsg() + nl_attr(IFLA_IFNAME, b"zz1\0")
        linkinfo = nl_attr(IFLA_INFO_KIND, b"veth\0") + nl_attr(IFLA_INFO_DATA, nl_attr(VETH_INFO_PEER, peer))
        nl_request(nl, RTM_NEWLINK, NLM_F_CREATE | NLM_F_EXCL, ifinfomsg() + nl_attr(IFLA_IFNAME, b"zz0\0") + nl_attr(IFLA_LINKINFO, linkinfo))
        out["create_veth"] = "ALLOWED"
    except OSError as e:
        out["create_veth"] = errno_text(e)
    finally:
        nl.close()
    out["status"] = [l for l in read_text("/proc/self/status", 6000).splitlines() if l.startswith(("CapEff", "CapBnd", "NoNewPrivs"))]
    return out


def runtime_compat(cfg):
    """Do common runtimes still work with /proc replaced by a mostly-empty tmpfs?"""
    out = {}
    sub(out, "subprocess_true", lambda: subprocess.run(["true"], timeout=5).returncode)
    sub(out, "proc_self_fd_listing", lambda: len(os.listdir("/proc/self/fd")))
    sub(out, "proc_meminfo", lambda: read_text("/proc/meminfo", 80).splitlines()[:1])
    sub(out, "proc_net_route", lambda: read_text("/proc/net/route", 80)[:60])
    sub(out, "cpu_count", os.cpu_count)

    def node_fetch():
        node = shutil.which("node")
        if not node:
            return "node not installed"
        proc = subprocess.run(
            [node, "-e", "fetch('https://registry.npmjs.org/').then(r=>console.log('node fetch status',r.status)).catch(e=>console.log('node fetch error',String(e.cause||e).slice(0,120)))"],
            capture_output=True, text=True, timeout=30,
        )
        return {"rc": proc.returncode, "out": (proc.stdout + proc.stderr).strip()[-300:]}

    sub(out, "node_fetch", node_fetch)
    return out


# --- the sandboxed "child app" and the relay that exposes it ---

class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        route, _, query = self.path.partition("?")
        if route == "/bulk":
            megabytes = int(query.partition("=")[2] or "1")
            self.send_response(200)
            self.send_header("Content-Length", str(megabytes << 20))
            self.end_headers()
            for _ in range(megabytes * 16):
                self.wfile.write(BULK_CHUNK)
            return
        body = f"pong from {self.client_address[0]}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def pump(source, sink):
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            sink.sendall(chunk)
    except OSError:
        pass  # peer closed mid-transfer; the other direction ends the connection
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass  # already closed


def serve_relay(socket_path, app_port):
    server = socket.socket(socket.AF_UNIX)
    server.bind(socket_path)
    os.chmod(socket_path, 0o600)
    server.listen(128)

    def handle(client):
        upstream = socket.create_connection(("127.0.0.1", app_port), 5)
        threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
        pump(upstream, client)
        client.close()
        upstream.close()

    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


def start_app_and_relay(cfg):
    app = ThreadingHTTPServer(("0.0.0.0", cfg["app_port"]), AppHandler)
    app.daemon_threads = True
    threading.Thread(target=app.serve_forever, daemon=True).start()
    threading.Thread(target=serve_relay, args=(path("relay.sock"), cfg["app_port"]), daemon=True).start()
    time.sleep(0.3)
    return "serving"


# --- outbound battery (identical in sandbox and control mode) ---

def scan(host, ports):
    def one(port):
        try:
            socket.create_connection((host, port), SCAN_TIMEOUT_SECONDS).close()
            return port
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(SCAN_THREADS) as pool:
        connected = [p for p in pool.map(one, ports) if p]
    return {"target": host, "tried": len(ports), "connected": len(connected), "ports": connected[:100]}


def network_view():
    interfaces = {name: interface_ipv4(name) for _, name in socket.if_nameindex()}
    return {
        "interfaces": interfaces,
        "route": default_route(),
        "nameservers": [l.strip() for l in read_text("/etc/resolv.conf", 2000).splitlines() if l.startswith("nameserver")],
    }


def reach(host, port, timeout=2.0):
    """Connect outcome as a word, so 'filtered' (timeout) can be told from 'closed' (refused) and 'open'."""
    started = time.perf_counter()
    try:
        socket.create_connection((host, port), timeout).close()
        return {"result": "connected", "ms": round((time.perf_counter() - started) * 1000)}
    except socket.timeout:
        return {"result": "timeout"}
    except OSError as e:
        return {"result": errno.errorcode.get(e.errno, str(e.errno))}


def internal_reach(cfg):
    """Can the child touch the platform's own side of the network, beyond loopback?"""
    targets = {
        "appmux_http_via_container_ip": (cfg["container_ip"], cfg["appmux_http_port"]),
        "container_gateway_53": (cfg["platform_gateway"], 53),
        "container_gateway_443": (cfg["platform_gateway"], 443),
        "metadata_169.254.169.254_80": ("169.254.169.254", 80),
        "host_nameserver_tcp53": (cfg["host_nameserver"], 53),
    }
    out = {label: reach(*target) for label, target in targets.items() if target[0]}
    ports = [p for p in cfg["host_ports"] if p != cfg["app_port"]]
    out["scan_container_ip"] = scan(cfg["container_ip"], ports) if cfg["container_ip"] else None
    return out


def https_get(host):
    started = time.perf_counter()
    conn = http.client.HTTPSConnection(host, 443, timeout=HTTP_TIMEOUT_SECONDS)
    try:
        conn.request("GET", "/", headers={"User-Agent": "appmux-probe2"})
        response = conn.getresponse()
        response.read(1024)
        return {"status": response.status, "ms": round((time.perf_counter() - started) * 1000)}
    finally:
        conn.close()


def ssh_banner():
    sock = socket.create_connection(("github.com", 22), 6)
    try:
        sock.settimeout(6)
        return sock.recv(40).decode("ascii", "replace").strip()
    finally:
        sock.close()


def udp_dns(server):
    query = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    try:
        sock.sendto(query, (server, 53))
        reply, _ = sock.recvfrom(512)
        return {"reply_bytes": len(reply), "id_matches": reply[:2] == query[:2]}
    finally:
        sock.close()


def pip_download(cfg, requirement, extra=()):
    target = tempfile.mkdtemp(prefix="pipdl-", dir="/tmp")
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [cfg["python"], "-m", "pip", "download", "--no-deps", "--no-cache-dir", "--disable-pip-version-check", "-q", *extra, "-d", target, requirement],
            capture_output=True, text=True, timeout=PIP_TIMEOUT_SECONDS,
            env={"PATH": os.environ.get("PATH", ""), "HOME": SCRATCH or "/tmp", "LANG": "C.UTF-8"},
        )
        seconds = time.perf_counter() - started
        size = sum(os.path.getsize(os.path.join(target, f)) for f in os.listdir(target))
        return {
            "rc": proc.returncode, "seconds": round(seconds, 2), "bytes": size,
            "mb_per_s": round(size / (1 << 20) / seconds, 2) if size else None,
            "err": proc.stderr.strip()[-200:],
        }
    finally:
        shutil.rmtree(target, ignore_errors=True)  # best-effort cleanup of a temp dir


def outbound_battery(cfg):
    out = {}
    ports = [p for p in cfg["host_ports"] if p != cfg["app_port"]]
    sub(out, "network_view", network_view)
    sub(out, "scan_loopback", scan, "127.0.0.1", ports)
    sub(out, "scan_decoy_loopback", scan, "127.0.0.1", [cfg["decoy_tcp_port"]])
    if cfg.get("network") == "none":
        return out
    route = RESULT.get("network_ready", {})
    gateways = [g for g in dict.fromkeys([route.get("gateway"), GATEWAY_IP]) if g]
    if not CONTROL:
        for gateway in gateways:
            sub(out, f"scan_gateway_{gateway}", scan, gateway, ports + [cfg["decoy_tcp_port"]])
    sub(out, "internal_reach", internal_reach, cfg)
    sub(out, "dns_getaddrinfo", lambda: [a[4][0] for a in socket.getaddrinfo("pypi.org", 443, socket.AF_INET, socket.SOCK_STREAM)][:3])
    targets = list(HTTPS_TARGETS)
    host = cfg.get("databricks_host", "").replace("https://", "").strip("/")
    if host:
        targets.append(host)
    out["https"] = {}
    for target in targets:
        sub(out["https"], target, https_get, target)
    sub(out, "tcp_raw_ssh_banner", ssh_banner)
    out["udp_dns_direct"] = {}
    for server in ("1.1.1.1", "8.8.8.8"):
        sub(out["udp_dns_direct"], server, udp_dns, server)
    sub(out, "pip_six", pip_download, cfg, "six==1.16.0")
    if cfg.get("do_numpy"):
        sub(out, "pip_numpy", pip_download, cfg, "numpy", ("--only-binary=:all:",))
    return out


def main():
    with open(path("config.json")) as f:
        cfg = json.load(f)
    RESULT["mode"] = "control" if CONTROL else "sandbox"
    if not CONTROL:
        step("identity", namespace_identity)
        step("loopback_up", set_loopback_up)
        if cfg["network"] == "veth":
            step("veth_setup", setup_veth, cfg)
        touch("stage1.ready", json.dumps(RESULT["identity"]))
        RESULT["attached_signal"] = wait_file("attached", ATTACH_TIMEOUT_SECONDS)
        if cfg["network"] == "veth":
            step("network_ready", wait_for_network)
        else:
            RESULT["network_ready"] = {"skipped": "offline scenario: no interface besides lo"}
        step("visibility_before_lockdown", visibility, cfg)
        step("lockdown", lockdown, cfg)
        step("visibility_after_lockdown", visibility, cfg)
        step("sibling_files_after_lockdown", sibling_file_probe, cfg)
        step("privilege_after_drop", privilege_checks)
        step("runtime_compat", runtime_compat, cfg)
        step("app_and_relay", start_app_and_relay, cfg)
        touch("server.ready")
        RESULT["inbound_done_signal"] = wait_file("inbound_done", INBOUND_TIMEOUT_SECONDS)
    else:
        step("visibility_after_lockdown", visibility, cfg)
        step("sibling_files_after_lockdown", sibling_file_probe, cfg)
    step("battery", outbound_battery, cfg)
    temporary = path("result.json.tmp")
    with open(temporary, "w") as f:
        json.dump(RESULT, f)
    os.replace(temporary, path("result.json"))
    if not CONTROL:
        wait_file("stop", 30)


if __name__ == "__main__":
    main()
