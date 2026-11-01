"""Build the wheels of the wheels_only fixture offline, with the standard library only.

usage: python make_wheels.py <output-dir>

Run it into the ``wheels/`` folder of a *copy* of this fixture, or into any folder the test lists as
a wheel source. Never run it into the repository: wheel files are not committed.

The two wheels are pure Python and byte-for-byte reproducible: the archive is stored (not
compressed) with a fixed timestamp and fixed modes, so the sha256 pins in ``requirements.txt`` stay
valid on every machine. After building, the script checks every digest against ``requirements.txt``
and exits with status 1 when one is missing from it, so a drift cannot go unnoticed.

The distribution names are this project's own and are not published on PyPI (not checked from the
fixture's authoring environment). The hash pins keep an index from serving anything else under the
same name.

``appmux-fixture-alpha`` is built byte-for-byte the same way by the wheel_reuse fixture, so both
fixtures pin the same sha256. Fixtures are standalone folders and cannot share code.
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

BETA_DISTRIBUTION = "appmux-fixture-beta"
BETA_SOURCE = (
    '"""Wheel of the appmux offline-install fixtures that depends on alpha."""\n\n'
    "from appmux_fixture_alpha import VALUE as ALPHA_VALUE\n\n"
    f'VALUE = "{BETA_DISTRIBUTION} {VERSION} on " + ALPHA_VALUE\n'
)


def record_line(path: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(data)}"


def build_wheel(
    output_dir: Path,
    distribution: str,
    module_source: str,
    requires: tuple[str, ...] = (),
) -> Path:
    """Write ``<module>-<version>-py3-none-any.whl`` into ``output_dir`` and return its path."""
    module = distribution.replace("-", "_")
    dist_info = f"{module}-{VERSION}.dist-info"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {VERSION}",
        "Summary: appmux test fixture; not a published package",
        "Requires-Python: >=3.11",
        *(f"Requires-Dist: {requirement}" for requirement in requires),
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


def build_all(output_dir: Path) -> list[Path]:
    return [
        build_wheel(output_dir, ALPHA_DISTRIBUTION, ALPHA_SOURCE),
        build_wheel(
            output_dir,
            BETA_DISTRIBUTION,
            BETA_SOURCE,
            requires=(f"{ALPHA_DISTRIBUTION}=={VERSION}",),
        ),
    ]


def main(arguments: list[str]) -> int:
    if len(arguments) != 2:
        sys.stderr.write("usage: python make_wheels.py <output-dir>\n")
        return 2
    output_dir = Path(arguments[1])
    output_dir.mkdir(parents=True, exist_ok=True)
    pinned_text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    exit_status = 0
    for wheel in build_all(output_dir):
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        is_pinned = f"--hash=sha256:{digest}" in pinned_text
        verdict = "pinned in requirements.txt" if is_pinned else "NOT PINNED in requirements.txt"
        sys.stdout.write(f"{wheel} sha256:{digest} {verdict}\n")
        if not is_pinned:
            exit_status = 1
    return exit_status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
