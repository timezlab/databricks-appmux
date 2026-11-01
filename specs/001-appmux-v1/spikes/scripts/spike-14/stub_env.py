"""Spike 14: write a metadata-only copy of the App environment into a virtual environment.

For every `name==version` in the list, fetch that release's metadata from PyPI and write a
`.dist-info` (METADATA, WHEEL, RECORD, INSTALLER) into the venv's site-packages. No package code is
downloaded or installed; the resolver sees the same installed names, versions and dependencies
the real environment has. usage: stub_env.py <environment-packages.txt> <venv-dir>
"""
import concurrent.futures
import glob
import json
import re
import sys
import urllib.request
from pathlib import Path


def canon_file(name):
    return re.sub(r"[-_.]+", "_", name)


def fetch(pin):
    name, version = pin
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as r:
        info = json.load(r)["info"]
    return name, version, info


def write_stub(site, name, version, info):
    d = site / f"{canon_file(name)}-{version}.dist-info"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    if info.get("requires_python"):
        lines.append(f"Requires-Python: {info['requires_python']}")
    lines += [f"Requires-Dist: {r}" for r in (info.get("requires_dist") or [])]
    lines += [f"Provides-Extra: {e}" for e in (info.get("provides_extra") or [])]
    (d / "METADATA").write_text("\n".join(lines) + "\n")
    (d / "INSTALLER").write_text("pip\n")
    (d / "WHEEL").write_text("Wheel-Version: 1.0\nGenerator: spike14-stub\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
    files = ("METADATA", "INSTALLER", "WHEEL", "RECORD")
    (d / "RECORD").write_text("".join(f"{d.name}/{f},,\n" for f in files))


def main(list_path, venv):
    site = Path(glob.glob(f"{venv}/lib/python3.*/site-packages")[0])
    pins = [tuple(l.strip().split("==")) for l in open(list_path) if "==" in l]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        for name, version, info in pool.map(fetch, pins):
            write_stub(site, name, version, info)
    print(f"wrote {len(pins)} stubs into {site}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
