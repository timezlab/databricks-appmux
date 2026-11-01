"""Spike 2, phase B: build a filesystem allow-list with pivot_root and test it with canaries.

Run on the harness app as the app user (uid 1000). The launcher creates canary
files at the paths the design worries about, starts `unshare --user
--map-root-user --mount --pid --fork`, builds a new root from the mount table in
TABLE below, drops every capability, then runs CHECK_SOURCE with the app
environment's interpreter. Prints one JSON document. Stdlib only; keep it
Python 3.10-safe because the harness runs jobs with the system python3.
"""
import ctypes
import errno
import json
import os
import shutil
import struct
import subprocess
import sys

BASE = "/tmp/spike2"
PIVOT = BASE + "/newroot"
ROOT = PIVOT + "/rootfs"  # the child's root; pivot_root targets PIVOT, then chroot("/rootfs")
RELEASE_A, RELEASE_B = BASE + "/release-A", BASE + "/release-B"
RUN_A, RUN_B = BASE + "/run-A", BASE + "/run-B"
VENV = "/app/python/source_code/.venv"
APP_DIR = "/app/python/source_code"
CANARY = {  # name -> path, planted on the host before the sandbox starts
    "app-dir": APP_DIR + "/spike2-canary-app",
    "home": "/home/app/spike2-canary-home",
    "dev-shm": "/dev/shm/spike2-canary-shm",
    "tmp": "/tmp/spike2-canary-tmp",
    "var-tmp": "/var/tmp/spike2-canary-vartmp",
    "sibling-release": RELEASE_B + "/app.py",
    "sibling-run-file": RUN_B + "/state.json",
}
SIBLING_SOCKET = RUN_B + "/app.sock"
PLATFORM_FILES = [  # opened without reading, so no secret is touched
    "/databricks/runtime-secrets/runtime.cfg",
    "/databricks/dynamic-secrets/shared/configs/deployment.json",
    "/databricks/dynamic-secrets/shared/deployment-unit-info.json",
    "/databricks/customer-logs/apps-runtime/databricksapps.log",
    "/dev/termination-log",
    "/sys/class/dmi/id/sys_vendor",
    "/etc/hostname",
    "/app/static/logz.html",
    "/proc/1/environ",
    "/proc/1/cmdline",
]
# The mount table under test: (host source, path in the new root, mode). "ro" = bind then remount read-only,
# "rw" = bind read-write, "tmpfs" = fresh tmpfs (the source column is its size).
TABLE = [
    ("/usr", "/usr", "ro"),
    (VENV, VENV, "ro"),
    (RELEASE_A, "/srv/app", "ro"),
    (RUN_A, "/run/appmux", "rw"),
    ("64m", "/tmp", "tmpfs"),
    ("16m", "/var/tmp", "tmpfs"),
    ("64m", "/dev/shm", "tmpfs"),
    ("64m", "/home/app", "tmpfs"),
]
ETC_FILES = ["ld.so.cache", "ld.so.conf", "ld.so.conf.d", "ssl", "ca-certificates", "nsswitch.conf", "passwd", "group", "localtime", "os-release"]
LEAKY_PROC_SELF = ["cgroup", "mountinfo", "mounts", "mountstats"]
DEV_NODES = ["null", "zero", "full", "random", "urandom"]
SYMLINKS = {"bin": "usr/bin", "sbin": "usr/sbin", "lib": "usr/lib", "lib64": "usr/lib64"}

MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_REMOUNT, MS_BIND, MS_REC = 1, 2, 4, 8, 32, 4096, 16384
MS_NOATIME, MS_NODIRATIME, MS_PRIVATE, MS_RELATIME = 1024, 2048, 1 << 18, 1 << 21
OPT_FLAGS = {"nosuid": MS_NOSUID, "nodev": MS_NODEV, "noexec": MS_NOEXEC, "noatime": MS_NOATIME, "nodiratime": MS_NODIRATIME, "relatime": MS_RELATIME}

CHECK_SOURCE = r'''
import ctypes, errno, json, os, socket, subprocess, sys
CANARY = json.loads(sys.argv[1]); SOCK = sys.argv[2]; PLATFORM = json.loads(sys.argv[3]); VENV = sys.argv[4]
out = {}
def tryop(fn):
    try:
        r = fn()
        return "ok" if r is None else r
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))
    except Exception as e:
        return type(e).__name__
libc = ctypes.CDLL(None, use_errno=True)
def errname(ok): return "ok" if ok else errno.errorcode.get(ctypes.get_errno(), "?")
def forked(fn):
    r, w = os.pipe(); pid = os.fork()
    if pid == 0:
        try: res = fn()
        except Exception as e: res = {"error": type(e).__name__ + ": " + str(e)}
        os.write(w, json.dumps(res).encode()); os._exit(0)
    os.waitpid(pid, 0); os.close(w)
    return json.loads(os.read(r, 100000).decode() or "{}")
def nested():
    res = {}
    res["unshare_newuser"] = errname(libc.unshare(0x10000000) == 0)
    if res["unshare_newuser"] != "ok": return res
    res["unshare_newns"] = errname(libc.unshare(0x00020000) == 0)
    def m(src, dst, fs, fl): return errname(libc.mount(src, dst, fs, fl, None) == 0)
    res["remount_usr_rw"] = m(None, b"/usr", None, 32 | 4096)
    res["remount_venv_rw"] = m(None, VENV.encode(), None, 32 | 4096)
    res["remount_release_rw"] = m(None, b"/srv/app", None, 32 | 4096)
    res["remount_root_rw"] = m(None, b"/", None, 32)
    res["umount_usr"] = errname(libc.umount2(b"/usr", 2) == 0)
    res["mount_tmpfs_on_tmp"] = m(b"tmpfs", b"/tmp", b"tmpfs", 0)
    res["root_listing_after"] = sorted(os.listdir("/"))
    return res
out["nested_userns"] = forked(nested)
out["unshare_user_alone_after_cap_drop"] = forked(lambda: {"r": errname(libc.unshare(0x10000000) == 0)})["r"]
ipc = {}
ipc["sysv_shm_visible"] = "VISIBLE" if libc.shmget(0x53504b32, 0, 0) >= 0 else errno.errorcode.get(ctypes.get_errno(), "?")
ipc["sysv_msgqueue_visible"] = "VISIBLE" if libc.msgget(0x53504b32, 0) >= 0 else errno.errorcode.get(ctypes.get_errno(), "?")
out["ipc"] = ipc
# 1. canaries at their host paths
c = {}
for name, p in CANARY.items():
    c[name] = {"stat": tryop(lambda: os.stat(p) and None), "read": tryop(lambda: open(p, "rb").read(8) and "READ")}
s = socket.socket(socket.AF_UNIX); c["sibling-socket"] = {"connect": tryop(lambda: s.connect(SOCK))}
for p in PLATFORM:
    c[p] = {"open_r": tryop(lambda: os.close(os.open(p, os.O_RDONLY))), "open_w": tryop(lambda: os.close(os.open(p, os.O_WRONLY | os.O_APPEND)))}
out["canaries"] = c
# 2. what is visible: top level and every readable file by top-level directory
tops = {}
for root, dirs, files in os.walk("/", onerror=lambda e: None):
    top = "/" + root.strip("/").split("/")[0] if root != "/" else "/"
    t = tops.setdefault(top, {"dirs": 0, "files": 0, "readable": 0})
    t["dirs"] += 1
    for f in files:
        p = os.path.join(root, f); t["files"] += 1
        if os.access(p, os.R_OK): t["readable"] += 1
out["visible"] = {"top_level_entries": sorted(os.listdir("/")), "by_top": tops}
# 3. where can it write?
w = {}
for root, dirs, files in os.walk("/", onerror=lambda e: None):
    if root.startswith("/proc"): dirs[:] = []; continue
    p = os.path.join(root, ".w")
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.close(fd); os.unlink(p)
        w[root] = True
    except OSError as e:
        pass
top_w = {}
for d in w:
    k = "/" + d.strip("/").split("/")[0] if d != "/" else "/"
    top_w[k] = top_w.get(k, 0) + 1
out["writable"] = {"count": len(w), "by_top": top_w, "roots": sorted(d for d in w if d.count("/") <= 2)[:30]}
ro = {}
for name, p in (("usr", "/usr/spike2"), ("venv", VENV + "/spike2"), ("release", "/srv/app/spike2"), ("etc", "/etc/spike2"), ("root", "/spike2")):
    ro[name] = tryop(lambda: os.close(os.open(p, os.O_CREAT | os.O_WRONLY)))
ro["modify_existing_usr_file"] = tryop(lambda: os.close(os.open("/usr/bin/python3.11", os.O_WRONLY)))
ro["chmod_usr"] = tryop(lambda: os.chmod("/usr/bin/python3.11", 0o777))
out["readonly_checks"] = ro
# 4. identity, capabilities and what /proc shows
st = dict(l.split(":\t", 1) for l in open("/proc/self/status") if ":\t" in l)
out["identity"] = {"uid": os.getuid(), "gid": os.getgid(), "CapEff": st.get("CapEff", "").strip(), "CapBnd": st.get("CapBnd", "").strip(), "NoNewPrivs": st.get("NoNewPrivs", "").strip(),
                   "proc_entries": sorted(os.listdir("/proc")), "open_fds": sorted(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else "no /proc/self/fd"}
mi = [l.split() for l in open("/proc/self/mountinfo")] if os.path.exists("/proc/self/mountinfo") else []
out["mountinfo"] = [[m[4], m[8] if m[6] == "-" else m[m.index("-") + 1], m[5]] for m in mi]
# 5. escape attempts
libc = ctypes.CDLL(None, use_errno=True)
def sysc(name, *a):
    r = libc.syscall(*a); return "ok" if r == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
esc = {}
esc["chroot_dot"] = tryop(lambda: os.chroot("."))
esc["mount_tmpfs"] = "ok" if libc.mount(b"tmpfs", b"/tmp", b"tmpfs", 0, None) == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
esc["pivot_root"] = "ok" if libc.syscall(155, b"/tmp", b"/tmp") == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
esc["open_proc_self_root_dotdot"] = tryop(lambda: os.listdir("/proc/self/root/.."))
esc["proc_self_cwd"] = tryop(lambda: os.readlink("/proc/self/cwd"))
esc["open_by_handle_at"] = "ok" if libc.syscall(304, -100, 0, 0) == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
esc["listdir_dotdot_of_root"] = tryop(lambda: sorted(os.listdir("/..")) == sorted(os.listdir("/")) and "same as /")
out["escapes"] = esc
fds = {}
for n in os.listdir("/proc/self/fd"):
    fds[n] = tryop(lambda n=n: os.readlink("/proc/self/fd/" + n))
out["fd_targets"] = fds
open_fds = []
for n in range(256):
    try: m = os.fstat(n).st_mode; open_fds.append(n)
    except OSError: pass
out["open_fds_probe"] = open_fds
def leak_shapes():
    r = {}
    mi = open("/proc/self/mountinfo").read().splitlines()
    r["mountinfo_overlay_has_host_paths"] = any("lowerdir=" in l or "upperdir=" in l for l in mi)
    r["mountinfo_root_field_samples"] = sorted({l.split()[3] for l in mi})[:8]
    r["cgroup_first_line_prefix"] = open("/proc/self/cgroup").readline()[:40]
    r["proc_self_readable_files"] = sum(1 for n in os.listdir("/proc/self") if os.path.isfile("/proc/self/" + n) and os.access("/proc/self/" + n, os.R_OK))
    r["environ_names"] = sorted(k for k in open("/proc/self/environ").read().split("\0") if k) and sorted(x.split("=")[0] for x in open("/proc/self/environ").read().split("\0") if x)
    return r
out["proc_self_leaks"] = tryop(leak_shapes)
import re
pat = re.compile(r"(token|secret|credential|passwd|password|\.pem$|\.key$|\.netrc|\.databrickscfg|\.cfg$|id_rsa|\.env$|kubeconfig)", re.I)
skip = re.compile(r"\.(py|pyc|pyi|css|js|h|txt|md|rst|json|html|c|so|a|ts|map|pm|pl|gz|1|3|5|8|x|md5sums|master)$|^/app/|^/usr/share/(doc|doc-base|lintian|pam|base-passwd|gnupg)|^/usr/include|^/etc/ssl/certs|^/usr/lib/git-core|^/usr/lib/tmpfiles|/(passwd|chpasswd|gpasswd|chgpasswd|update-passwd|common-password)$")
hits = []
for root, dirs, files in os.walk("/usr"):
    for f in files:
        p = os.path.join(root, f)
        if pat.search(f) and not skip.search(p): hits.append(p)
for root, dirs, files in os.walk("/etc"):
    for f in files:
        p = os.path.join(root, f)
        if pat.search(f) and not skip.search(p): hits.append(p)
out["secret_named_files_visible_outside_venv"] = {"count": len(hits), "sample": hits[:25]}
node = subprocess.run(["/usr/bin/env", "node", "--version"], capture_output=True, text=True, env={"PATH": "/usr/local/bin:/usr/bin:/bin"})
out["node"] = node.stdout.strip() or node.stderr.strip()[:100]
# 6. does the allow-list suffice for the app environment?
imports = {}
for mod in ("fastapi", "uvicorn", "streamlit", "dash", "gradio", "requests", "pandas"):
    r = subprocess.run([VENV + "/bin/python", "-I", "-c", "import " + mod], capture_output=True, text=True, timeout=60)
    imports[mod] = "ok" if r.returncode == 0 else (r.stderr.strip().splitlines() or ["failed"])[-1][:100]
out["app_environment_imports"] = imports
print(json.dumps(out))
'''

libc = ctypes.CDLL(None, use_errno=True)
PLANTED = {}


def mount(src, dst, fstype, flags, data=None):
    rc = libc.mount(src.encode() if src else None, dst.encode(), fstype.encode() if fstype else None, flags, data.encode() if data else None)
    if rc != 0:
        e = ctypes.get_errno()
        raise OSError(e, "mount %s -> %s: %s" % (fstype or "bind", dst, os.strerror(e)))


def mount_flags_for(path):
    best, opts = "", ""
    for line in open("/proc/self/mountinfo"):
        f = line.split()
        mp = f[4]
        if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
            best, opts = mp, f[5]
    flags = 0
    for o in opts.split(","):
        flags |= OPT_FLAGS.get(o, 0)
    return flags


def bind(src, rel, mode):
    dst = ROOT + rel
    if os.path.isdir(src):
        os.makedirs(dst, exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        open(dst, "a").close()
    mount(src, dst, None, MS_BIND | (MS_REC if os.path.isdir(src) else 0))
    if mode == "ro":
        mount(None, dst, None, MS_BIND | MS_REMOUNT | MS_RDONLY | mount_flags_for(src))


def drop_capabilities():
    for cap in range(64):
        if libc.prctl(24, cap, 0, 0, 0) != 0 and ctypes.get_errno() == errno.EINVAL:
            break
    libc.prctl(47, 4, 0, 0, 0)
    hdr = ctypes.create_string_buffer(struct.pack("Ii", 0x20080522, 0))
    data = ctypes.create_string_buffer(struct.pack("6I", 0, 0, 0, 0, 0, 0))
    if libc.syscall(126, hdr, data) != 0:
        raise OSError(ctypes.get_errno(), "capset")
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "no_new_privs")


def stage1():
    steps = {}

    def step(name, fn):
        try:
            fn()
            steps[name] = "ok"
        except Exception as e:  # recorded as the finding for this step
            steps[name] = "%s: %s" % (type(e).__name__, e)

    step("make_mounts_private", lambda: mount(None, "/", None, MS_REC | MS_PRIVATE))
    os.makedirs(PIVOT, exist_ok=True)
    step("tmpfs_pivot", lambda: mount("tmpfs", PIVOT, "tmpfs", MS_NOSUID | MS_NODEV, "size=16m,mode=0755"))
    os.makedirs(ROOT, exist_ok=True)
    self_pid = os.readlink("/proc/self")
    for name, target in SYMLINKS.items():
        os.symlink(target, ROOT + "/" + name)
    for src, rel, mode in TABLE:
        if mode == "tmpfs":
            os.makedirs(ROOT + rel, exist_ok=True)
            step("tmpfs " + rel, lambda r=rel, s=src: mount("tmpfs", ROOT + r, "tmpfs", MS_NOSUID | MS_NODEV, "size=%s,mode=1777" % s))
        else:
            step("%s %s" % (mode, rel), lambda s=src, r=rel, m=mode: bind(s, r, m))
    os.makedirs(ROOT + "/etc", exist_ok=True)
    step("tmpfs /etc", lambda: mount("tmpfs", ROOT + "/etc", "tmpfs", MS_NOSUID | MS_NODEV, "size=1m,mode=0755"))
    for f in ETC_FILES:
        if os.path.exists("/etc/" + f):
            step("ro /etc/" + f, lambda f=f: bind("/etc/" + f, "/etc/" + f, "ro"))
    open(ROOT + "/etc/hosts", "w").write("127.0.0.1 localhost\n")
    open(ROOT + "/etc/resolv.conf", "w").write("nameserver 10.0.2.3\n")
    os.makedirs(ROOT + "/dev", exist_ok=True)
    step("tmpfs /dev", lambda: mount("tmpfs", ROOT + "/dev", "tmpfs", MS_NOSUID, "size=1m,mode=0755"))
    os.makedirs(ROOT + "/dev/shm", exist_ok=True)
    step("tmpfs /dev/shm", lambda: mount("tmpfs", ROOT + "/dev/shm", "tmpfs", MS_NOSUID | MS_NODEV, "size=64m,mode=1777"))
    for n in DEV_NODES:
        step("bind /dev/" + n, lambda n=n: bind("/dev/" + n, "/dev/" + n, "rw"))
    for n, t in (("stdin", "/proc/self/fd/0"), ("stdout", "/proc/self/fd/1"), ("stderr", "/proc/self/fd/2"), ("fd", "/proc/self/fd")):
        os.symlink(t, ROOT + "/dev/" + n)
    os.makedirs(ROOT + "/proc", exist_ok=True)
    step("tmpfs /proc", lambda: mount("tmpfs", ROOT + "/proc", "tmpfs", MS_NOSUID | MS_NODEV | MS_NOEXEC, "size=1m,mode=0755"))
    for n in ("cpuinfo", "meminfo"):
        step("bind /proc/" + n, lambda n=n: bind("/proc/" + n, "/proc/" + n, "rw"))
    os.makedirs(ROOT + "/proc/self", exist_ok=True)
    step("bind /proc/self", lambda: mount("/proc/%s" % self_pid, ROOT + "/proc/self", None, MS_BIND))
    for n in LEAKY_PROC_SELF:  # files that name the host: kubernetes pod id (cgroup) and overlay paths (mountinfo)
        step("overmount /proc/self/" + n, lambda n=n: mount("/dev/null", ROOT + "/proc/self/" + n, None, MS_BIND))
    os.makedirs(PIVOT + "/.old", exist_ok=True)
    for rel in ("/etc", "/dev", "/proc"):  # the skeleton must not be writable; submounts keep their own mode
        step("remount ro " + (rel or "/"), lambda r=rel: mount(None, ROOT + r, None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | (MS_NOEXEC if r == "/proc" else 0)))
    os.chdir(PIVOT)
    rc = libc.syscall(155, b".", b".old")
    steps["pivot_root"] = "ok" if rc == 0 else "%s" % os.strerror(ctypes.get_errno())
    os.chdir("/")
    if rc == 0:
        step("detach_old_root", lambda: libc.umount2(b"/.old", 2) == 0 or (_ for _ in ()).throw(OSError(ctypes.get_errno(), "umount2")))
        try:
            os.rmdir("/.old")
        except OSError as e:
            steps["rmdir_old"] = str(e)
    step("remount ro pivot", lambda: mount(None, "/", None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV))
    step("chroot /rootfs", lambda: (os.chroot("/rootfs"), os.chdir("/")))
    step("drop_capabilities", drop_capabilities)
    return steps


def plant():
    for d in (RELEASE_A, RELEASE_B, RUN_A, RUN_B, "/dev/shm", "/var/tmp"):
        os.makedirs(d, exist_ok=True)
    for name, p in CANARY.items():
        with open(p, "w") as fh:
            fh.write("CANARY-" + name)
    with open(RELEASE_A + "/app.py", "w") as fh:
        fh.write("print('release A')\n")
    import socket
    s = socket.socket(socket.AF_UNIX)
    s.bind(SIBLING_SOCKET)
    s.listen(1)
    IPC_CREAT = 0o1000
    PLANTED["shm"] = libc.shmget(0x53504b32, 4096, IPC_CREAT | 0o600)
    PLANTED["msg"] = libc.msgget(0x53504b32, IPC_CREAT | 0o600)
    return s


def unplant(sock):
    sock.close()
    if PLANTED.get("shm", -1) >= 0:
        libc.shmctl(PLANTED["shm"], 0, None)  # IPC_RMID
    if PLANTED.get("msg", -1) >= 0:
        libc.msgctl(PLANTED["msg"], 0, None)
    for p in list(CANARY.values()):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
    shutil.rmtree(BASE, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stage1":
        import time
        t0 = time.time()
        res = {"steps": stage1()}
        res["build_seconds"] = round(time.time() - t0, 3)
        print(json.dumps(res), flush=True)
        os.execve(VENV + "/bin/python", [VENV + "/bin/python", "-I", "-c", CHECK_SOURCE, json.dumps(CANARY), SIBLING_SOCKET, json.dumps(PLATFORM_FILES), VENV],
                  {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/app"})
    sock = plant()
    try:
        r = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "--pid", "--fork", "--kill-child"] + (["--ipc"] if os.environ.get("SPIKE2_IPC") else []) + ["--", "/usr/bin/python3", os.path.abspath(__file__), "stage1"],
                           capture_output=True, text=True, timeout=400)
        lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
        print(json.dumps({"launcher_rc": r.returncode, "stderr": r.stderr[-1500:], "stage1": json.loads(lines[0]) if lines else None,
                          "check": json.loads(lines[1]) if len(lines) > 1 else None, "raw_tail": r.stdout[-800:] if len(lines) < 2 else ""}))
    finally:
        unplant(sock)
