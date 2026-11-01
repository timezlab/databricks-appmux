"""Build the wheel of the wheel_reuse fixture offline, with the standard library only.

usage: python make_wheels.py <output-dir>

Run it into a scratch folder (or the ``wheels/`` folder of a *copy* of this fixture). Never run it
into the repository: wheel files are not committed.

The one wheel, ``appmux-fixture-alpha`` 1.0.0, is the very same file the wheels_only fixture builds:
both scripts write the archive byte for byte alike (stored, fixed timestamp and modes), so both
``requirements.txt`` files pin the same sha256. A test registers wheels_only first, which puts the
wheel in the content store, and then updates this fixture with a wheel source that does *not* hold
the file (or with a source that does, counting the fetches): the store hit by sha256 must be enough.
Fixtures are standalone folders and cannot share code, so the builder is repeated here.

After building, the script checks the digest against ``requirements.txt`` and exits with status 1
when it is missing from it.

The distribution name is this project's own and is not published on PyPI (not checked from the
fixture's authoring environment). The hash pin keeps an index from serving anything else under it.
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
VERSION = "1.0.0"

ALPHA_DISTRIBUTION = "appmux-fixture-alpha"
ALPHA_SOURCE = (
    '"""One-module wheel of the appmux offline-install fixtures."""\n\n'
    f'VALUE = "{ALPHA_DISTRIBUTION} {VERSION}"\n'
)


def record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(data)}"


def build_wheel(output_dir: Path, distribution: str, module_source: str) -> Path:
    """Write ``<module>-<version>-py3-none-any.whl`` into ``output_dir`` and return its path."""
    module = distribution.replace("-", "_")
    dist_info = f"{module}-{VERSION}.dist-info"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {VERSION}",
        "Summary: appmux test fixture; not a published package",
        "Requires-Python: >=3.11",
    ]
    files = {
        f"{module}/__init__.py": module_source.encode(),
        f"{dist_info}/METADATA": ("\n".join(metadata) + "\n").encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: appmux-fixture\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record = [record_line(path, data) for path, data in files.items()]
    files[f"{dist_info}/RECORD"] = ("\n".join([*record, f"{dist_info}/RECORD,,"]) + "\n").encode()

    wheel_path = output_dir / f"{module}-{VERSION}-py3-none-any.whl"
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
    wheel = build_wheel(output_dir, ALPHA_DISTRIBUTION, ALPHA_SOURCE)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    is_pinned = f"--hash=sha256:{digest}" in pinned_text
    verdict = "pinned in requirements.txt" if is_pinned else "NOT PINNED in requirements.txt"
    sys.stdout.write(f"{wheel} sha256:{digest} {verdict}\n")
    return 0 if is_pinned else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
