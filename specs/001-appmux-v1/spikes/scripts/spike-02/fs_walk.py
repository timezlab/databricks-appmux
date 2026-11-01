"""Spike 2, phase A: what can an ordinary container process (uid 1000) read and write?

Run on the harness app. Prints a JSON summary; never reads the content of a
file whose name looks secret (it only checks that open() succeeds).
"""
import json
import os
import re
import stat
import sys
import time

SKIP = ("/proc",)
SECRET_NAME = re.compile(r"(token|secret|credential|passwd|password|\.pem$|\.key$|\.netrc|\.databrickscfg|runtime\.cfg|id_rsa|\.env$|kubeconfig)", re.I)


def mountinfo():
    rows = []
    for line in open("/proc/self/mountinfo"):
        left, _, right = line.partition(" - ")
        f = left.split()
        fstype, source, super_opts = right.split()[:3]
        rows.append({"mount": f[4], "fstype": fstype, "source": source, "opts": f[5], "super": super_opts})
    return rows


def can_read_file(p):
    try:
        with open(p, "rb"):
            return True
    except OSError:
        return False


def can_create_in(d):
    p = os.path.join(d, f".spike2-{os.getpid()}")
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.unlink(p)
        return True
    except OSError:
        return False


def walk():
    t0 = time.time()
    tops = {}
    secret_hits = []
    writable_dirs = []
    setuid = []
    for root, dirs, files in os.walk("/", topdown=True, onerror=lambda e: None, followlinks=False):
        if root in SKIP:
            dirs[:] = []
            continue
        top = "/" + root.strip("/").split("/")[0] if root != "/" else "/"
        t = tops.setdefault(top, {"dirs": 0, "files": 0, "readable_files": 0, "unreadable_dirs": 0})
        t["dirs"] += 1
        if not os.access(root, os.R_OK | os.X_OK):
            t["unreadable_dirs"] += 1
        if not root.startswith("/tmp") and os.access(root, os.W_OK | os.X_OK) and can_create_in(root):
            writable_dirs.append(root)
        for name in files:
            p = os.path.join(root, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            t["files"] += 1
            readable = can_read_file(p)
            if readable:
                t["readable_files"] += 1
            if st.st_mode & stat.S_ISUID:
                setuid.append(p)
            if SECRET_NAME.search(name) and readable:
                secret_hits.append({"path": p, "size": st.st_size, "mode": oct(st.st_mode & 0o7777), "owner_uid": st.st_uid})
    return {"seconds": round(time.time() - t0, 1), "top_level": tops, "secret_named_readable": secret_hits[:80], "secret_named_count": len(secret_hits),
            "writable_dirs_outside_tmp": writable_dirs[:80], "writable_dirs_count": len(writable_dirs), "setuid": setuid[:20]}


def candidates():
    out = {}
    for p in ("/app", "/app/python", "/app/python/source_code", os.path.expanduser("~"), "/dev/shm", "/mnt", "/databricks", "/databricks/runtime-secrets", "/sys",
              "/sys/class/dmi/id", "/var/run/secrets", "/run/secrets", "/run", "/var/run", "/root", "/opt", "/srv", "/media", "/home", "/etc", "/var", "/var/tmp",
              "/var/lib", "/var/log", "/usr/local", "/workspace", "/Workspace", "/Volumes", "/dbfs", "/local_disk0", "/sys/fs/cgroup", "/dev"):
        e = {"exists": os.path.lexists(p)}
        if e["exists"]:
            try:
                names = os.listdir(p)
                e["list"] = len(names)
                e["sample"] = sorted(names)[:12]
            except OSError as ex:
                e["list"] = f"{type(ex).__name__}: {ex.strerror}"
            e["write"] = can_create_in(p) if os.path.isdir(p) else None
        out[p] = e
    return out


def identity():
    return {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(), "cwd": os.getcwd(), "home": os.path.expanduser("~"),
            "env_names": sorted(k for k in os.environ if not k.startswith("LESS")), "python": sys.version.split()[0]}


if __name__ == "__main__":
    which = sys.argv[1:] or ["identity", "mounts", "candidates", "walk"]
    res = {}
    if "identity" in which:
        res["identity"] = identity()
    if "mounts" in which:
        res["mounts"] = mountinfo()
    if "candidates" in which:
        res["candidates"] = candidates()
    if "walk" in which:
        res["walk"] = walk()
    print(json.dumps(res, indent=1, sort_keys=True))
