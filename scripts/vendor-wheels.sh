#!/usr/bin/env bash
# Vendor the wheels appmux needs but the Databricks App environment lacks, and
# write the hashed requirements file the platform installs from. Both outputs
# live in the git-ignored build folder and are never committed.
#
#   build/wheels/             one wheel per missing package, Python 3.11,
#                             manylinux2014_x86_64, wheels only (no source builds)
#   build/requirements.txt    --no-index, --find-links ./wheels, then each
#                             requirement with its --hash
#
# Why: the platform installs a deployed App's requirements from the workspace's
# package configuration, which may be blocked; with --no-index and a wheel
# folder inside the deployment folder it needs no index (design 13.1 item 5,
# spike 13). Which packages are missing is decided against the environment
# list in specs/001-appmux-v1/spikes/evidence/spike-18-environment-packages.txt.
#
# Every wheel comes from uv.lock (the pinned version, the wheel's URL, its
# SHA-256 and its upload time), so nothing is resolved against an index: a
# download whose hash differs from the lock is deleted and fails the run. A
# wheel the lock says was published in the last 24 hours is refused unless
# --allow-recent is given. It reaches the network only to fetch those wheels
# from files.pythonhosted.org.
#
# Usage: scripts/vendor-wheels.sh [--allow-recent]
#
# Environment (all optional):
#   APPMUX_REPO_ROOT  repository root. Default: the folder above scripts/.
#   APPMUX_BUILD_DIR  the generated, git-ignored folder. Default: $APPMUX_REPO_ROOT/build.
#   APPMUX_PYTHON     Python 3.11 or newer that can import `packaging` (appmux's own
#                     runtime dependency, so .venv/bin/python always can). Default: python3.
#   APPMUX_VENDOR_ALLOW_LOOPBACK_HTTP_FOR_TESTS
#                     set to 1 only by the unit test: it also accepts http://127.0.0.1
#                     wheel URLs, so the download checks run against a local file server
#                     with no network. Every other host and scheme stays refused. Never
#                     set it for a release build.

set -euo pipefail

script_name=$(basename -- "$0")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

print_usage() {
  printf 'usage: %s [--allow-recent]\n' "$script_name"
  printf '  --allow-recent  accept a wheel published in the last 24 hours\n'
}

die() {
  printf '%s: %s\n' "$script_name" "$*" >&2
  exit 1
}

allow_recent=false
for argument in "$@"; do
  case "$argument" in
    --allow-recent) allow_recent=true ;;
    -h | --help)
      print_usage
      exit 0
      ;;
    *)
      printf "%s: unknown option '%s'\n" "$script_name" "$argument" >&2
      print_usage >&2
      exit 2
      ;;
  esac
done

repo_root=${APPMUX_REPO_ROOT:-$(dirname -- "$script_dir")}
[[ -f "$repo_root/uv.lock" ]] || die "uv.lock is missing from '$repo_root'"
repo_root=$(cd -- "$repo_root" && pwd -P)
build_dir=${APPMUX_BUILD_DIR:-$repo_root/build}
mkdir -p -- "$build_dir"
build_dir=$(cd -- "$build_dir" && pwd -P)

python_bin=${APPMUX_PYTHON:-python3}
command -v "$python_bin" > /dev/null || die "'$python_bin' not found; set APPMUX_PYTHON to a Python 3.11+"

allow_loopback_http=false
if [[ "${APPMUX_VENDOR_ALLOW_LOOPBACK_HTTP_FOR_TESTS:-}" == 1 ]]; then
  allow_loopback_http=true
fi

"$python_bin" -I - "$repo_root" "$build_dir" "$allow_recent" "$allow_loopback_http" << 'PYTHON'
import hashlib
import http.client
import os
import shutil
import sys
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    from packaging.markers import Marker
    from packaging.tags import Tag, compatible_tags, cpython_tags
    from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
except ImportError as exc:
    sys.exit(
        f"vendor-wheels.sh: {exc}; set APPMUX_PYTHON to an interpreter that has `packaging` "
        "(for example .venv/bin/python)"
    )

PROGRAM = "vendor-wheels.sh"
ENVIRONMENT_LIST = Path("specs/001-appmux-v1/spikes/evidence/spike-18-environment-packages.txt")
ALLOWED_WHEEL_HOSTS = frozenset({"files.pythonhosted.org"})
# Reached only through APPMUX_VENDOR_ALLOW_LOOPBACK_HTTP_FOR_TESTS (see the header).
LOOPBACK_TEST_HOST = "127.0.0.1"
RECENT_WHEEL_AGE = timedelta(hours=24)
DOWNLOAD_TIMEOUT_S = 60
DOWNLOAD_CHUNK_BYTES = 1 << 20

TARGET_PYTHON = (3, 11)
TARGET_INTERPRETER = "cp311"
# manylinux2014 and the older tags pip accepts for it, newest first (the order is the preference).
TARGET_PLATFORMS = [
    "manylinux2014_x86_64",
    "manylinux_2_17_x86_64",
    "manylinux2010_x86_64",
    "manylinux_2_12_x86_64",
    "manylinux1_x86_64",
    "manylinux_2_5_x86_64",
]
# The marker variables of the target; the rest come from the interpreter running this script.
TARGET_MARKERS = {
    "implementation_name": "cpython",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_system": "Linux",
    "python_full_version": "3.11.0",
    "python_version": "3.11",
    "sys_platform": "linux",
}


# One table of uv.lock, which is external TOML: it stays loosely typed inside this script.
LockTable = dict[str, Any]


class VendorError(Exception):
    """A refusal that stops the run; the message says what to do next."""


@dataclass(frozen=True)
class Wheel:
    name: str
    version: str
    filename: str
    url: str
    sha256: str
    uploaded_at: datetime | None


def read_environment_packages(path: Path) -> dict[str, str]:
    """Canonical package name -> version of the Databricks App environment."""
    packages: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            name, _, version = line.partition("==")
            packages[canonicalize_name(name)] = version
    return packages


def runtime_closure(lock: LockTable) -> dict[str, LockTable]:
    """The locked packages appmux needs at run time on the target (dev groups excluded)."""
    packages: dict[str, LockTable] = {
        canonicalize_name(entry["name"]): entry for entry in lock["package"]
    }
    roots = [
        name for name, entry in packages.items() if entry.get("source", {}).get("editable") == "."
    ]
    if len(roots) != 1:
        raise VendorError("uv.lock does not name exactly one editable project; run `uv lock`")
    visited: set[tuple[str, str]] = set()
    pending = [(roots[0], "")]
    while pending:
        name, extra = pending.pop()
        if (name, extra) in visited:
            continue
        visited.add((name, extra))
        entry = packages[name]
        edges = (
            entry.get("optional-dependencies", {}).get(extra, [])
            if extra
            else entry.get("dependencies", [])
        )
        for edge in edges:
            if "marker" in edge and not Marker(edge["marker"]).evaluate(TARGET_MARKERS):
                continue
            edge_name = canonicalize_name(edge["name"])
            pending.append((edge_name, ""))
            pending.extend((edge_name, edge_extra) for edge_extra in edge.get("extra", []))
    return {name: packages[name] for name, _ in sorted(visited) if name != roots[0]}


def target_tag_ranks() -> dict[Tag, int]:
    tags = list(cpython_tags(TARGET_PYTHON, [TARGET_INTERPRETER], TARGET_PLATFORMS))
    tags += compatible_tags(TARGET_PYTHON, TARGET_INTERPRETER, TARGET_PLATFORMS)
    ranks: dict[Tag, int] = {}
    for rank, tag in enumerate(tags):
        ranks.setdefault(tag, rank)
    return ranks


def parse_upload_time(text: str | None) -> datetime | None:
    if text is None:
        return None
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def select_wheel(entry: LockTable, ranks: dict[Tag, int]) -> Wheel:
    """The locked wheel that fits Python 3.11 on manylinux2014 x86_64 best; wheels only."""
    best_rank: int | None = None
    best: Wheel | None = None
    for candidate in entry.get("wheels", []):
        filename = unquote(Path(urlparse(candidate["url"]).path).name)
        try:
            *_, tags = parse_wheel_filename(filename)
        except InvalidWheelFilename:
            continue
        matching = [ranks[tag] for tag in tags if tag in ranks]
        if matching and (best_rank is None or min(matching) < best_rank):
            best_rank = min(matching)
            best = Wheel(
                name=entry["name"],
                version=entry["version"],
                filename=filename,
                url=candidate["url"],
                sha256=candidate["hash"].removeprefix("sha256:"),
                uploaded_at=parse_upload_time(candidate.get("upload-time")),
            )
    if best is None:
        raise VendorError(
            f"no wheel for '{entry['name']}' {entry['version']} (Python 3.11, manylinux2014 x86_64)"
        )
    return best


def refuse_recent(wheels: list[Wheel], now: datetime) -> None:
    """Fail closed: a wheel with no upload time cannot be shown to be old enough."""
    refusals = []
    for wheel in wheels:
        if wheel.uploaded_at is None:
            refusals.append(f"{wheel.filename}: uv.lock records no upload time")
        elif now - wheel.uploaded_at < RECENT_WHEEL_AGE:
            hours = (now - wheel.uploaded_at).total_seconds() / 3600
            refusals.append(f"{wheel.filename}: published {hours:.1f} hours ago")
    if refusals:
        raise VendorError(
            "refusing wheels published in the last 24 hours "
            "(pass --allow-recent to accept them):\n  " + "\n  ".join(refusals)
        )


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_allowed_wheel_url(url: str, allow_loopback_http: bool) -> bool:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname in ALLOWED_WHEEL_HOSTS:
        return True
    return allow_loopback_http and parsed.scheme == "http" and parsed.hostname == LOOPBACK_TEST_HOST


def download(wheel: Wheel, destination: Path, allow_loopback_http: bool) -> None:
    if not is_allowed_wheel_url(wheel.url, allow_loopback_http):
        raise VendorError(
            f"{wheel.filename}: uv.lock points at a host that is not allowed: "
            f"{urlparse(wheel.url).hostname}"
        )
    digest = hashlib.sha256()
    request = urllib.request.Request(wheel.url, headers={"User-Agent": "appmux-vendor-wheels"})
    try:
        with (
            urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response,
            destination.open("wb") as target,
        ):
            while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
                digest.update(chunk)
                target.write(chunk)
    except (OSError, http.client.HTTPException) as exc:
        # HTTPException is not an OSError: a server that sends a malformed reply raises it.
        destination.unlink(missing_ok=True)
        raise VendorError(
            f"{wheel.filename}: download failed ({type(exc).__name__}: {exc}); "
            "check the network and run again"
        ) from exc
    if digest.hexdigest() != wheel.sha256:
        destination.unlink()
        raise VendorError(
            f"{wheel.filename}: the download does not match the SHA-256 in uv.lock; it was deleted"
        )


def requirements_text(wheels: list[Wheel]) -> str:
    lines = ["--no-index", "--find-links ./wheels"]
    for wheel in wheels:
        lines.append(f"{wheel.name}=={wheel.version} \\")
        lines.append(f"    --hash=sha256:{wheel.sha256}")
    return "\n".join(lines) + "\n"


def vendor(repo_root: Path, build_dir: Path, allow_recent: bool, allow_loopback_http: bool) -> None:
    lock = tomllib.loads((repo_root / "uv.lock").read_text(encoding="utf-8"))
    environment_list = repo_root / ENVIRONMENT_LIST
    if not environment_list.is_file():
        raise VendorError(
            f"{ENVIRONMENT_LIST} is missing from '{repo_root}'. It names the packages the App "
            "environment already has, so none of them is vendored; restore it from the "
            "repository (git checkout) and run again"
        )
    environment = read_environment_packages(environment_list)
    closure = runtime_closure(lock)

    missing = {name: entry for name, entry in closure.items() if name not in environment}
    in_environment = [name for name in closure if name in environment]
    print(f"in the App environment, not vendored: {', '.join(in_environment) or 'none'}")

    ranks = target_tag_ranks()
    wheels = [select_wheel(entry, ranks) for entry in missing.values()]
    if not allow_recent:
        refuse_recent(wheels, datetime.now(UTC))

    wheel_folder = build_dir / "wheels"
    staging = Path(tempfile.mkdtemp(prefix=".wheels-", dir=build_dir))
    staging.chmod(0o755)  # mkdtemp makes it private; the folder is deployed like any other
    requirements_partial = build_dir / "requirements.txt.partial"
    try:
        for wheel in wheels:
            kept = wheel_folder / wheel.filename
            if kept.is_file() and sha256_of(kept) == wheel.sha256:
                shutil.copyfile(kept, staging / wheel.filename)
            else:
                download(wheel, staging / wheel.filename, allow_loopback_http)
            print(f"vendored {wheel.filename} ({(staging / wheel.filename).stat().st_size} bytes)")
        requirements_partial.write_text(requirements_text(wheels), encoding="utf-8")
        shutil.rmtree(wheel_folder, ignore_errors=True)
        os.replace(staging, wheel_folder)
        os.replace(requirements_partial, build_dir / "requirements.txt")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        requirements_partial.unlink(missing_ok=True)
    print(f"wrote {build_dir / 'requirements.txt'} and {len(wheels)} wheel(s) in {wheel_folder}")


repo_root_argument, build_dir_argument, *flag_arguments = sys.argv[1:5]
allow_recent_argument, allow_loopback_argument = flag_arguments
try:
    vendor(
        Path(repo_root_argument),
        Path(build_dir_argument),
        allow_recent_argument == "true",
        allow_loopback_argument == "true",
    )
except VendorError as exc:
    sys.exit(f"{PROGRAM}: {exc}")
PYTHON
