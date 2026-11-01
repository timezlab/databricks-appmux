"""Build the decoy wheel of the env_covered fixture offline, with the standard library only.

usage: python make_wheels.py <output-dir>

This fixture needs no wheel: every requirement in ``requirements.txt`` is met by the App
environment, so the Update must fetch nothing and build no overlay (design 5.3, spec US7
scenario 1). The wheel built here exists only so a test can offer a *non-empty* wheel source and
then assert that nothing was fetched from it: ``appmux-fixture-unneeded`` is named by no requirement
and by no hash, so it is a wheel that is present and still must not be touched. The app tries to
import it and expects the import to fail.

Run it into a scratch folder (or the ``wheels/`` folder of a *copy* of this fixture). Never run it
into the repository: wheel files are not committed. The archive is stored (not compressed) with a
fixed timestamp and fixed modes, so it is byte-for-byte reproducible.

The distribution name is this project's own and is not published on PyPI (not checked from the
fixture's authoring environment); nothing in this fixture asks an index for it.
"""

from __future__ import annotations

import base64
import hashlib
import stat
import sys
import zipfile
from pathlib import Path

FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
REGULAR_FILE_MODE = (stat.S_IFREG | 0o644) << 16
UNIX_CREATE_SYSTEM = 3

DISTRIBUTION = "appmux-fixture-unneeded"
MODULE = "appmux_fixture_unneeded"
VERSION = "1.0.0"
MODULE_SOURCE = (
    '"""Decoy wheel of the appmux env_covered fixture; no requirement names it."""\n\n'
    f'VALUE = "{DISTRIBUTION} {VERSION}"\n'
)


def record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(data)}"


def build_wheel(output_dir: Path) -> Path:
    """Write ``appmux_fixture_unneeded-1.0.0-py3-none-any.whl`` into ``output_dir``."""
    dist_info = f"{MODULE}-{VERSION}.dist-info"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {DISTRIBUTION}",
        f"Version: {VERSION}",
        "Summary: appmux test fixture; not a published package",
        "Requires-Python: >=3.11",
    ]
    files = {
        f"{MODULE}/__init__.py": MODULE_SOURCE.encode(),
        f"{dist_info}/METADATA": ("\n".join(metadata) + "\n").encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: appmux-fixture\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record = [record_line(path, data) for path, data in files.items()]
    files[f"{dist_info}/RECORD"] = ("\n".join([*record, f"{dist_info}/RECORD,,"]) + "\n").encode()

    wheel_path = output_dir / f"{MODULE}-{VERSION}-py3-none-any.whl"
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
    wheel = build_wheel(output_dir)
    sys.stdout.write(f"{wheel} sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
