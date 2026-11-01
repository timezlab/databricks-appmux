"""Build the wheel of the newer_wheel fixture offline, with the standard library only.

usage: python make_wheels.py <output-dir>

Run it into a scratch folder (or the ``wheels/`` folder of a *copy* of this fixture). Never run it
into the repository: wheel files are not committed.

The wheel is a stand-in for a newer ``appdirs``. The App environment holds ``appdirs`` 1.4.4; the
wheel carries version 99.0.0 of a one-module package of the same name, whose only content is a
marker. It is *not* the real package and must never be installed anywhere but in a throwaway
overlay. ``requirements.txt`` pins it with its sha256 and the environment cannot satisfy the pin,
so the wheel has to win over the environment's copy for that release (design 5.3, spec US7
scenario 2).

The archive is stored (not compressed) with a fixed timestamp and fixed modes, so it is
byte-for-byte reproducible and the pin stays valid on every machine. After building, the script
checks the digest against ``requirements.txt`` and exits with status 1 when it is missing from it.
"""

from __future__ import annotations

import base64
import hashlib
import stat
import sys
import zipfile
from pathlib import Path

REQUIREMENTS_PATH = Path(__file__).with_name("requirements.txt")
FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
REGULAR_FILE_MODE = (stat.S_IFREG | 0o644) << 16
UNIX_CREATE_SYSTEM = 3

DISTRIBUTION = "appdirs"
VERSION = "99.0.0"
MODULE_SOURCE = (
    '"""Stand-in for a newer appdirs, built by the appmux newer_wheel fixture. '
    'Not the real package."""\n\n'
    f'__version__ = "{VERSION}"\n'
    'MARKER = "appmux-fixture-newer-wheel"\n'
)


def record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(data)}"


def build_wheel(output_dir: Path) -> Path:
    """Write ``appdirs-99.0.0-py3-none-any.whl`` into ``output_dir`` and return its path."""
    dist_info = f"{DISTRIBUTION}-{VERSION}.dist-info"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {DISTRIBUTION}",
        f"Version: {VERSION}",
        "Summary: appmux test fixture standing in for a newer appdirs; not the real package",
        "Requires-Python: >=3.11",
    ]
    files = {
        f"{DISTRIBUTION}/__init__.py": MODULE_SOURCE.encode(),
        f"{dist_info}/METADATA": ("\n".join(metadata) + "\n").encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: appmux-fixture\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record = [record_line(path, data) for path, data in files.items()]
    files[f"{dist_info}/RECORD"] = ("\n".join([*record, f"{dist_info}/RECORD,,"]) + "\n").encode()

    wheel_path = output_dir / f"{DISTRIBUTION}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_STORED) as archive:
        for path, data in files.items():
            entry = zipfile.ZipInfo(path, FIXED_ZIP_TIMESTAMP)
            entry.create_system = UNIX_CREATE_SYSTEM
            entry.external_attr = REGULAR_FILE_MODE
            archive.writestr(entry, data, compress_type=zipfile.ZIP_STORED)
    return wheel_path


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        sys.stderr.write("usage: python make_wheels.py <output-dir>\n")
        return 2
    output_dir = Path(arguments[1])
    output_dir.mkdir(parents=True, exist_ok=True)
    pinned_text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    wheel = build_wheel(output_dir)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    is_pinned = f"--hash=sha256:{digest}" in pinned_text
    verdict = "pinned in requirements.txt" if is_pinned else "NOT PINNED in requirements.txt"
    sys.stdout.write(f"{wheel} sha256:{digest} {verdict}\n")
    return 0 if is_pinned else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
