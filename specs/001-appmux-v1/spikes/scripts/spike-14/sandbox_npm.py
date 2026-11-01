"""Spike 14: `npm ci --offline` in a sandbox, then run the Express app from node_modules mounted read-only.

Run on the harness app as the app user (uid 1000), beside sandbox_fs.py from spike 2, whose stage 1
(allow-list root, pivot_root, chroot, capability drop) it reuses with a different mount table.
Layout under /tmp/spike14 (uploaded beforehand): npm-cache/ (a populated npm cache), proj/ (package.json,
package-lock.json, server.js).

  build: its own network namespace (loopback down, so no connection of any kind is possible); /usr
         read-only, the npm cache read-only, the project directory read-write; runs `npm ci --offline
         --ignore-scripts`, then two negative controls (empty cache, tampered integrity).
  run:   /usr and the finished project (node_modules included) read-only; starts server.js, makes an
         HTTP and a WebSocket request, then tries to write to the project and to node_modules.

Prints one JSON document. Stdlib only, Python 3.10-safe (the harness runs jobs with the system python3).
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox_fs as sb

BASE = "/tmp/spike14"
CACHE, PROJ = BASE + "/npm-cache", BASE + "/proj"
TMPFS = [("64m", "/tmp", "tmpfs"), ("16m", "/var/tmp", "tmpfs"), ("64m", "/dev/shm", "tmpfs"), ("64m", "/home/app", "tmpfs")]
TABLES = {
    "build": [("/usr", "/usr", "ro"), (CACHE, "/srv/npm-cache", "ro"), (PROJ, "/srv/proj", "rw")] + TMPFS,
    "run": [("/usr", "/usr", "ro"), (PROJ, "/srv/proj", "ro")] + TMPFS,
}

BUILD_SOURCE = r'''
import json, os, shutil, socket, subprocess, sys, time
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/app", "npm_config_update_notifier": "false"}
FLAGS = ["--offline", "--ignore-scripts", "--no-audit", "--no-fund"]

def run(cmd, cwd):
    t0 = time.time()
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=ENV, timeout=240)
    lines = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
    return {"rc": r.returncode, "seconds": round(time.time() - t0, 2), "tail": lines[-6:]}

def npm_ci(cache, cwd):
    return run(["npm", "ci"] + FLAGS + ["--cache", cache], cwd)

def scratch_project(name, mutate=None):
    d = "/tmp/" + name
    os.makedirs(d)
    shutil.copy("/srv/proj/package.json", d)
    lock = json.load(open("/srv/proj/package-lock.json"))
    if mutate:
        mutate(lock)
    json.dump(lock, open(d + "/package-lock.json", "w"))
    return d

def tamper(lock):
    entry = lock["packages"]["node_modules/express"]
    entry["integrity"] = "sha512-AAAAAAAA" + entry["integrity"][len("sha512-AAAAAAAA"):]

out = {"versions": {"node": run(["node", "--version"], "/srv/proj")["tail"], "npm": run(["npm", "--version"], "/srv/proj")["tail"]}}
try:
    socket.create_connection(("registry.npmjs.org", 443), timeout=3).close()
    out["network_probe"] = "CONNECTED"
except OSError as e:
    out["network_probe"] = type(e).__name__ + ": " + str(e)[:80]
out["positive_read_only_cache"] = npm_ci("/srv/npm-cache", "/srv/proj")
cache_for_controls = "/srv/npm-cache"
if out["positive_read_only_cache"]["rc"] != 0:
    shutil.copytree("/srv/npm-cache", "/tmp/npm-cache")
    out["positive_writable_cache_copy"] = npm_ci("/tmp/npm-cache", "/srv/proj")
    cache_for_controls = "/tmp/npm-cache"
os.makedirs("/tmp/empty-cache")
out["empty_cache"] = npm_ci("/tmp/empty-cache", scratch_project("neg1"))
out["tampered_integrity"] = npm_ci(cache_for_controls, scratch_project("neg2", tamper))
nm = "/srv/proj/node_modules"
if os.path.isdir(nm):
    names = [n for n in os.listdir(nm) if not n.startswith(".")]
    scoped = sum(len(os.listdir(os.path.join(nm, n))) for n in names if n.startswith("@"))
    files = size = 0
    for root, dirs, fs in os.walk(nm):
        for f in fs:
            files += 1
            size += os.path.getsize(os.path.join(root, f))
    out["node_modules"] = {"top_level_dirs": len(names) - sum(1 for n in names if n.startswith("@")) + scoped, "files": files, "bytes": size,
                           "express": json.load(open(nm + "/express/package.json"))["version"], "ws": json.load(open(nm + "/ws/package.json"))["version"]}
print(json.dumps(out))
'''

RUN_SOURCE = r'''
import errno, json, os, subprocess, sys, time, urllib.request
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/app", "PORT": "18201"}
out = {}
server = subprocess.Popen(["node", "server.js"], cwd="/srv/proj", env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(50):
        try:
            out["ping"] = urllib.request.urlopen("http://127.0.0.1:18201/api/ping", timeout=2).read().decode()
            break
        except OSError:
            time.sleep(0.2)
    out["index_status"] = urllib.request.urlopen("http://127.0.0.1:18201/", timeout=2).status
    client = ("const W=require('ws');const s=new W('ws://127.0.0.1:18201/ws');s.on('open',()=>s.send('hi'));"
              "s.on('message',m=>{console.log(String(m));process.exit(0)});setTimeout(()=>process.exit(2),5000)")
    r = subprocess.run(["node", "-e", client], cwd="/srv/proj", env=ENV, capture_output=True, text=True, timeout=20)
    out["websocket"] = {"rc": r.returncode, "reply": r.stdout.strip()}
finally:
    server.terminate()
    server.wait(timeout=10)

def write_attempt(path, mode="w"):
    try:
        with open(path, mode) as f:
            f.write("x")
        os.remove(path) if mode == "w" else None
        return "ok"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))

out["write_checks"] = {
    "new file in project": write_attempt("/srv/proj/new-file"),
    "new file in node_modules": write_attempt("/srv/proj/node_modules/new-file"),
    "new file in node_modules/express": write_attempt("/srv/proj/node_modules/express/new-file"),
    "append to server.js": write_attempt("/srv/proj/server.js", "a"),
    "append to express/index.js": write_attempt("/srv/proj/node_modules/express/index.js", "a"),
}
print(json.dumps(out))
'''


def stage1_and_exec(phase):
    sb.BASE = BASE
    sb.PIVOT = BASE + "/newroot"
    sb.ROOT = sb.PIVOT + "/rootfs"
    sb.TABLE = TABLES[phase]
    print(json.dumps({"steps": sb.stage1()}), flush=True)
    source = BUILD_SOURCE if phase == "build" else RUN_SOURCE
    os.execve("/usr/bin/python3", ["/usr/bin/python3", "-I", "-c", source], {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/app"})


def launch(phase):
    namespaces = ["--user", "--map-root-user", "--mount", "--pid", "--ipc"] + (["--net"] if phase == "build" else [])
    r = subprocess.run(["unshare"] + namespaces + ["--fork", "--kill-child", "--", "/usr/bin/python3", os.path.abspath(__file__), "stage1", phase],
                       capture_output=True, text=True, timeout=600)
    lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
    stage1 = json.loads(lines[0]) if lines else None
    failed = {k: v for k, v in (stage1 or {}).get("steps", {}).items() if v != "ok"}
    return {"launcher_rc": r.returncode, "stage1_failed_steps": failed, "result": json.loads(lines[1]) if len(lines) > 1 else None,
            "stderr": r.stderr[-1500:] if len(lines) < 2 else "", "raw_tail": r.stdout[-600:] if len(lines) < 2 else ""}


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "stage1":
        stage1_and_exec(sys.argv[2])
    shutil.rmtree(PROJ + "/node_modules", ignore_errors=True)
    build = launch("build")
    ok = build["result"] and build["result"].get("node_modules")
    print(json.dumps({"build": build, "run": launch("run") if ok else None}))
