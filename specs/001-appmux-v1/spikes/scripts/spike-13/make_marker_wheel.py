"""Spike 13: build a one-module pure-Python wheel by hand (standard library only).

The distribution name is not registered on PyPI (checked on the day of the run), so a
successful install of it proves the install read the local wheel folder and not an index.
usage: make_marker_wheel.py <output-dir> [suffix]    (suffix defaults to q7k2x; the control run used c4v8n)
"""
import base64
import hashlib
import sys
import zipfile
from pathlib import Path

SUFFIX = sys.argv[2] if len(sys.argv) > 2 else "q7k2x"
DIST = f"appmux-spike13-marker-{SUFFIX}"
MODULE = f"appmux_spike13_marker_{SUFFIX}"
VERSION = "0.0.1"
# a second, real package the marker depends on; it must come from the same folder
DEPENDENCY = "humanize==4.11.0"


def record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(data)}"


def build(out_dir: Path) -> Path:
    dist_info = f"{MODULE}-{VERSION}.dist-info"
    files = {
        f"{MODULE}/__init__.py": f'VALUE = "{MODULE} {VERSION}"\n'.encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {VERSION}\n"
            f"Requires-Dist: {DEPENDENCY}\n"
        ).encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: spike13\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = [record_line(p, d) for p, d in files.items()] + [f"{dist_info}/RECORD,,"]
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()
    wheel = out_dir / f"{MODULE}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in files.items():
            z.writestr(path, data)
    return wheel


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    print(build(out))
