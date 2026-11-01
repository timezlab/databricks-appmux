"""Spike 18: build a wheel overlay inside a sandbox, then run an app from it in a second sandbox.

Run on the harness app as the app user (uid 1000), beside sandbox_fs.py from spike 2, whose stage 1
(allow-list root, pivot_root, chroot, capability drop) it reuses with a different mount table.
Layout under /tmp/spike18 (uploaded beforehand): wheels/ (five wheels and lock.txt), app/app.py.

  build: sandbox A mounts the environment and /usr read-only, the wheels read-only and an empty
         overlay directory read-write, then runs `pip install --no-index --no-deps --require-hashes
         --target` and three negative controls.
  run:   sandbox B mounts the environment and the finished overlay read-only, starts uvicorn from the
         overlay's bin with PYTHONPATH, and tries to write to the environment and the overlay.

Prints one JSON document. Stdlib only, Python 3.10-safe (the harness runs jobs with the system python3).
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox_fs as sb

BASE = "/tmp/spike18"
WHEELS, OVERLAY, APP, RUNDIR = BASE + "/wheels", BASE + "/overlay", BASE + "/app", BASE + "/run"
VENV = sb.VENV
TMPFS = [("64m", "/tmp", "tmpfs"), ("16m", "/var/tmp", "tmpfs"), ("64m", "/dev/shm", "tmpfs"), ("64m", "/home/app", "tmpfs")]
TABLES = {
    "build": [("/usr", "/usr", "ro"), (VENV, VENV, "ro"), (WHEELS, "/srv/wheels", "ro"), (OVERLAY, "/srv/build", "rw")] + TMPFS,
    "run": [("/usr", "/usr", "ro"), (VENV, VENV, "ro"), (APP, "/srv/app", "ro"), (OVERLAY, "/srv/overlay", "ro"), (RUNDIR, "/run/appmux", "rw")] + TMPFS,
}

BUILD_SOURCE = r'''
import json, os, subprocess, sys, time
VENV = sys.argv[1]
PY = VENV + "/bin/python"
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/app", "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
LOCK = open("/srv/wheels/lock.txt").read().splitlines()

def pip_install(target, lines, name):
    lockfile = "/tmp/" + name + ".txt"
    open(lockfile, "w").write("\n".join(lines) + "\n")
    t0 = time.time()
    r = subprocess.run([PY, "-m", "pip", "install", "--no-index", "--no-deps", "--require-hashes", "--find-links", "/srv/wheels",
                        "--target", target, "-r", lockfile], capture_output=True, text=True, env=ENV, timeout=120)
    tail = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()][-4:]
    return {"rc": r.returncode, "seconds": round(time.time() - t0, 2), "tail": tail}

out = {"positive": pip_install("/srv/build", LOCK, "lock")}
first = LOCK[0]
bad = first[:-6] + ("000000" if not first.endswith("000000") else "111111")
out["wrong_hash"] = pip_install("/tmp/neg1", [bad] + LOCK[1:], "bad-hash")
out["missing_hash"] = pip_install("/tmp/neg2", [first.split(" --hash")[0]] + LOCK[1:], "no-hash")
out["unpinned"] = pip_install("/tmp/neg3", [first.replace("==", ">=")] + LOCK[1:], "unpinned")
out["negative_targets_created"] = [d for d in ("/tmp/neg1", "/tmp/neg2", "/tmp/neg3") if os.path.isdir(d) and os.listdir(d)]
top = sorted(os.listdir("/srv/build"))
out["overlay_top_level"] = top
bindir = "/srv/build/bin"
out["bin_scripts"] = {n: open(os.path.join(bindir, n)).readline().strip() for n in sorted(os.listdir(bindir))} if os.path.isdir(bindir) else {}
out["bin_modes"] = {n: oct(os.stat(os.path.join(bindir, n)).st_mode & 0o777) for n in sorted(os.listdir(bindir))} if os.path.isdir(bindir) else {}
n_files = size = pyc = 0
for root, dirs, files in os.walk("/srv/build"):
    for f in files:
        n_files += 1
        size += os.path.getsize(os.path.join(root, f))
        pyc += f.endswith(".pyc")
out["overlay_files"], out["overlay_bytes"], out["overlay_pyc_files"] = n_files, size, pyc
print(json.dumps(out))
'''

RUN_SOURCE = r'''
import errno, json, os, subprocess, sys, time, urllib.request
VENV = sys.argv[1]
BASE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/app"}
ARGS = ["--app-dir", "/srv/app", "app:app", "--host", "127.0.0.1", "--port"]
out = {}

def serve(label, exe, port, pythonpath):
    env = dict(BASE_ENV)
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    t0 = time.time()
    p = subprocess.Popen([exe] + ARGS + [str(port)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    res = {"executable": exe, "PYTHONPATH": pythonpath or None}
    try:
        for _ in range(100):
            try:
                body = urllib.request.urlopen("http://127.0.0.1:%d/versions" % port, timeout=2).read()
                res["seconds_to_first_response"] = round(time.time() - t0, 2)
                res["versions"] = json.loads(body)
                break
            except OSError:
                if p.poll() is not None:
                    break
                time.sleep(0.3)
        else:
            res["error"] = "no response in 30 s"
    finally:
        p.terminate()
        try:
            log = p.communicate(timeout=10)[0].decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            p.kill(); log = p.communicate()[0].decode("utf-8", "replace")
        res["exit_code"] = p.returncode
        res["log_tail"] = [l for l in log.splitlines() if l.strip()][-4:]
    out[label] = res

serve("overlay_with_pythonpath", "/srv/overlay/bin/uvicorn", 18101, "/srv/overlay")
serve("overlay_script_without_pythonpath", "/srv/overlay/bin/uvicorn", 18102, None)
serve("environment_control", VENV + "/bin/uvicorn", 18103, None)

r = subprocess.run(["/srv/overlay/bin/uvicorn", "--version"], capture_output=True, text=True, env=dict(BASE_ENV, PYTHONPATH="/srv/overlay"))
out["uvicorn_version_from_overlay_bin"] = {"rc": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()[-200:]}
r = subprocess.run([VENV + "/bin/python", "-m", "pip", "check"], capture_output=True, text=True, env=dict(BASE_ENV, PYTHONPATH="/srv/overlay", PIP_DISABLE_PIP_VERSION_CHECK="1"))
out["pip_check_with_overlay"] = {"rc": r.returncode, "output": [l for l in (r.stdout + r.stderr).splitlines() if l.strip()][:10]}
r = subprocess.run([VENV + "/bin/python", "-m", "pip", "check"], capture_output=True, text=True, env=dict(BASE_ENV, PIP_DISABLE_PIP_VERSION_CHECK="1"))
out["pip_check_environment_alone"] = {"rc": r.returncode, "output": [l for l in (r.stdout + r.stderr).splitlines() if l.strip()][:10]}

def try_write(path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd); os.unlink(path)
        return "created"
    except OSError as e:
        return errno.errorcode.get(e.errno, str(e.errno))

site = VENV + "/lib/python3.11/site-packages"
out["write_checks"] = {
    "new_file_in_environment_site_packages": try_write(site + "/spike18-new-file"),
    "new_file_in_environment_bin": try_write(VENV + "/bin/spike18-new-file"),
    "new_file_in_overlay_root": try_write("/srv/overlay/spike18-new-file"),
    "new_file_in_overlay_package_dir": try_write("/srv/overlay/uvicorn/spike18-new-file"),
    "new_file_in_overlay_bin": try_write("/srv/overlay/bin/spike18-new-file"),
}
try:
    open("/srv/overlay/uvicorn/__init__.py", "a").close()
    out["write_checks"]["append_to_overlay_module"] = "opened"
except OSError as e:
    out["write_checks"]["append_to_overlay_module"] = errno.errorcode.get(e.errno, str(e.errno))
print(json.dumps(out))
'''


def stage1_and_exec(phase):
    sb.BASE = BASE
    sb.PIVOT = BASE + "/newroot"
    sb.ROOT = sb.PIVOT + "/rootfs"
    sb.TABLE = TABLES[phase]
    res = {"steps": sb.stage1()}
    print(json.dumps(res), flush=True)
    source = BUILD_SOURCE if phase == "build" else RUN_SOURCE
    os.execve(VENV + "/bin/python", [VENV + "/bin/python", "-I", "-c", source, VENV],
              {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/app"})


def launch(phase):
    r = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "--pid", "--ipc", "--fork", "--kill-child", "--",
                        "/usr/bin/python3", os.path.abspath(__file__), "stage1", phase], capture_output=True, text=True, timeout=300)
    lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
    stage1 = json.loads(lines[0]) if lines else None
    failed = {k: v for k, v in (stage1 or {}).get("steps", {}).items() if v != "ok"}
    return {"launcher_rc": r.returncode, "stage1_failed_steps": failed, "result": json.loads(lines[1]) if len(lines) > 1 else None,
            "stderr": r.stderr[-1500:] if len(lines) < 2 else "", "raw_tail": r.stdout[-600:] if len(lines) < 2 else ""}


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "stage1":
        stage1_and_exec(sys.argv[2])
    shutil.rmtree(OVERLAY, ignore_errors=True)
    os.makedirs(OVERLAY)
    os.makedirs(RUNDIR, exist_ok=True)
    build = launch("build")
    run = launch("run") if build["result"] and build["result"]["positive"]["rc"] == 0 else None
    print(json.dumps({"build": build, "run": run}))
