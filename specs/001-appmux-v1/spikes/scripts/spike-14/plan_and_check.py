"""Spike 14: turn each `uv pip install --dry-run` log into a plan, apply it to a copy of the
stub environment (metadata only) and run `uv pip check` on the result.

usage: plan_and_check.py <stub-venv> <lock-dir> <log-dir> <fixture> ...
For <fixture>.pinned.in it reads dry-<fixture>.pinned.in.log; writes <fixture>.plan.txt (the
`+` entries, with the lock's hash lines when the lock has that exact name==version) and prints
one JSON object per fixture: installed, replaced (same name uninstalled), new (name not in the
environment) and the output of `uv pip check` after applying the plan.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from coverage import canon, read_lock
from stub_env import fetch, write_stub


def parse(log):
    plus, minus = [], []
    for line in open(log):
        m = re.match(r"^ ([+-]) ([A-Za-z0-9_.-]+)==(\S+)", line)
        if m:
            (plus if m.group(1) == "+" else minus).append((m.group(2), m.group(3)))
    return plus, minus


def main(stub, lock_dir, log_dir, fixtures):
    out = {}
    for fx in fixtures:
        plus, minus = parse(f"{log_dir}/dry-{fx}.pinned.in.log")
        locked = {(canon(n), v): lines for n, v, lines in read_lock(f"{lock_dir}/{fx}.lock.txt")}
        with open(f"{fx}.plan.txt", "w") as f:
            for n, v in plus:
                f.write("\n".join(locked.get((canon(n), v), [f"{n}=={v}"])) + "\n")
        copy = Path(f"{stub}-{fx}")
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(stub, copy, symlinks=True)
        site = next(copy.glob("lib/python3.*/site-packages"))
        for n, v in minus:
            for d in site.glob("*.dist-info"):
                if d.name.lower().startswith(f"{canon(n).replace('-', '_')}-{v}".lower()):
                    shutil.rmtree(d)
        for n, v in plus:
            write_stub(site, *fetch((n, v)))
        check = subprocess.run(["uv", "pip", "check", "--python", str(copy)], capture_output=True, text=True)
        names_minus = {canon(n) for n, _ in minus}
        out[fx] = {
            "installed": len(plus),
            "replaced_in_environment": sorted(f"{n}=={v}" for n, v in plus if canon(n) in names_minus),
            "new_to_environment": sorted(f"{n}=={v}" for n, v in plus if canon(n) not in names_minus),
            "not_in_lock_at_that_version": sorted(f"{n}=={v}" for n, v in plus if (canon(n), v) not in locked),
            "check_returncode": check.returncode,
            "check_output": [l for l in (check.stdout + check.stderr).splitlines() if l.strip()],
        }
        shutil.rmtree(copy, ignore_errors=True)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
