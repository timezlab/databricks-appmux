"""The build step of a release: from a stored snapshot to a directory the sandbox can mount.

A release is built by unpacking its snapshot (the archive the Update flow stored under the
snapshot's digest) into ``<releases_dir>/<slug>/<release_no>``. The build is idempotent: it always
starts from the stored archive, so a release interrupted halfway, or one whose directory the
container lost on restart, is built again without any state to repair.

The archive is hostile input. The content store lives in the workspace, where anyone holding the
shared service principal's token can write, so extraction takes only what a snapshot is made of:
regular files whose names stay inside the directory, within a count and a size bound. A refused
archive leaves no directory behind: the files are unpacked next to the target and renamed into place
only when all of them were accepted.

The build also does the packages' share of every start (design 5.3, T136), in the ``building``
state, because the reconciler calls the builder at every start of a release (a restart and a
restore included): ``PackageStep`` reads the release's ``requirements.txt`` and judges it again
against the environment the container has *now*, fails the release before it starts if the
environment no longer provides what it used to, and builds the overlay from the wheels the release
recorded (cached, so only the first start of a wheel list does the work). A release with no
``requirements.txt``, or whose requirements the environment still meets, gets no overlay.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from appmux.packages.environment import Environment, read_environment
from appmux.packages.modes import PackageMode
from appmux.packages.overlay import Overlay, OverlayBuildFailed, OverlayError, launch_overlays
from appmux.packages.resolve import recheck_environment
from appmux.packages.store import BYTES_PER_MB, ContentStoreError
from appmux.packages.wheels import ResolvedWheel
from appmux.reconciler.snapshot_source import ARCHIVE_NAME, MAX_FILE_COUNT
from appmux.registry.models import Release
from appmux.registry.snapshot import RegistrySnapshot
from appmux.sandbox.rules import EgressRule
from appmux.settings import PlatformSettings

logger = logging.getLogger(__name__)

# What a snapshot unpacks to is bounded the way what it was made from is: by file count (the
# snapshot refuses a source folder over MAX_FILE_COUNT files) and, here, by total size.
MAX_EXTRACTED_FILES: Final = MAX_FILE_COUNT
MAX_EXTRACTED_BYTES: Final = 2048 * BYTES_PER_MB

REQUIREMENTS_FILE: Final = "requirements.txt"
# A hashed lock of a few hundred packages is some 100 KB; this is generous and still bounded.
MAX_REQUIREMENTS_BYTES: Final = 1 * BYTES_PER_MB
PACKAGE_MODE_KEY: Final = "package_mode"
BUILD_TIMEOUT_KEY: Final = "build_timeout_s"

_FILE_MODE: Final = 0o644
_EXECUTABLE_FILE_MODE: Final = 0o755
_ANY_EXECUTE_BIT: Final = 0o111
_SCRATCH_PREFIX: Final = ".build-"


class BuildFailed(Exception):
    """The release cannot be built. ``reason`` is stored as the release's ``status_reason``."""

    def __init__(self, reason: str, log_tail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.log_tail = log_tail


@dataclass(frozen=True, slots=True)
class BuiltRelease:
    release_dir: Path
    overlays: Sequence[Path] = field(default_factory=tuple)
    # ``pip check`` lines for the overlay: shown on the release, never a reason to fail it.
    warnings: tuple[str, ...] = ()


class ReleaseBuilder(Protocol):
    async def build(self, release: Release) -> BuiltRelease: ...


class ArchiveStore(Protocol):
    """The one call of the content store a build makes (``ContentStore.get`` satisfies it)."""

    async def get(self, digest: str, destination: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class PackagePolicy:
    """What the platform and the app decide about packages, read again at every build."""

    mode: PackageMode
    build_timeout_s: float
    app_rules: tuple[EgressRule, ...]  # the app's own egress list: the index step runs with it


def package_policy_from(snapshot: RegistrySnapshot, app_slug: str) -> PackagePolicy:
    """The stored package mode and build time limit, or the defaults if a value is not usable.

    A bad stored value must not stop every build, and must not name the value in the log (a
    setting is free text to the registry), so it falls back to the platform default, loudly.
    """
    defaults = PlatformSettings()
    mode_row = snapshot.setting(PACKAGE_MODE_KEY)
    mode = _stored_mode(mode_row.value) if mode_row is not None else None
    timeout_row = snapshot.setting(BUILD_TIMEOUT_KEY)
    timeout_s = _stored_timeout_s(timeout_row.value) if timeout_row is not None else None
    app = snapshot.app(app_slug)
    return PackagePolicy(
        mode=mode or PackageMode(defaults.package_mode),
        build_timeout_s=timeout_s if timeout_s is not None else defaults.build_timeout_s,
        app_rules=tuple(app.egress) if app is not None else (),
    )


def _stored_mode(value: object) -> PackageMode | None:
    if isinstance(value, str):
        try:
            return PackageMode(value)
        except ValueError:
            pass  # reported below, in one place for both ways a value can be wrong
    logger.error(
        "the stored %s setting is not a package mode; the default applies", PACKAGE_MODE_KEY
    )
    return None


def _stored_timeout_s(value: object) -> float | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return float(value)
    logger.error(
        "the stored %s setting is not a positive number; the default applies", BUILD_TIMEOUT_KEY
    )
    return None


class OverlayProvider(Protocol):
    """``packages.overlay.OverlayBuilder.ensure``, the one call the package step makes."""

    async def ensure(
        self,
        *,
        mode: PackageMode,
        wheels: Sequence[ResolvedWheel],
        index_requirements: Sequence[str],
        app_rules: Sequence[EgressRule],
        build_timeout_s: float | None = None,
    ) -> Overlay | None: ...


class PackageStep:
    """The packages' share of a build: re-check the environment, then build the overlay."""

    def __init__(
        self,
        *,
        overlays: OverlayProvider,
        policy: Callable[[str], PackagePolicy],
        environment: Callable[[], Environment] = read_environment,
    ) -> None:
        self._overlays = overlays
        self._policy = policy
        self._environment = environment

    async def prepare(self, release: Release, release_dir: Path) -> Overlay | None:
        """The overlay of ``release`` (``None`` if it needs none); ``BuildFailed`` if it cannot."""
        requirements = await asyncio.to_thread(_read_requirements, release_dir)
        if requirements is None:
            return None
        policy = self._policy(release.app_slug)
        environment = await asyncio.to_thread(self._environment)
        # In ``index`` mode wheels are not used, so a wheel list left from another mode is ignored
        # and everything the environment lacks goes to the index (the mode is a platform setting
        # an admin can change under a release).
        wheels = (
            [ResolvedWheel(entry.sha256, entry.filename, entry.source) for entry in release.wheels]
            if policy.mode.uses_wheels
            else []
        )
        recheck = recheck_environment(
            requirements,
            mode=policy.mode,
            environment=environment,
            wheel_sha256s=[wheel.sha256 for wheel in wheels],
        )
        reason = recheck.failure_reason()
        if reason is not None:
            raise BuildFailed(reason)
        if not wheels and not recheck.index_requirements:
            return None
        try:
            return await self._overlays.ensure(
                mode=policy.mode,
                wheels=wheels,
                index_requirements=recheck.index_requirements,
                app_rules=policy.app_rules,
                build_timeout_s=policy.build_timeout_s,
            )
        except OverlayBuildFailed as exc:
            raise BuildFailed(exc.reason, exc.log_tail) from exc
        except OverlayError as exc:
            raise BuildFailed(str(exc)) from exc
        except ContentStoreError as exc:
            raise BuildFailed(
                f"a wheel of this release could not be read from the content store ({exc}); "
                "add the wheel again and Update"
            ) from exc
        except OSError as exc:
            raise BuildFailed(
                f"the overlay could not be built ({exc.strerror or 'I/O error'}); "
                "check the free space under the overlay cache directory"
            ) from exc


def _read_requirements(release_dir: Path) -> str | None:
    """The text of the release's ``requirements.txt``, or ``None`` if it has none."""
    path = release_dir / REQUIREMENTS_FILE
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_REQUIREMENTS_BYTES:
        raise BuildFailed(
            f"{REQUIREMENTS_FILE} is larger than {MAX_REQUIREMENTS_BYTES} bytes; "
            "split the app or trim the lock"
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BuildFailed(
            f"{REQUIREMENTS_FILE} is not UTF-8 text; save it as UTF-8 and Update"
        ) from exc


class StoreReleaseBuilder:
    def __init__(
        self, store: ArchiveStore, releases_dir: Path, packages: PackageStep | None = None
    ) -> None:
        self._store = store
        self._releases_dir = releases_dir
        self._packages = packages

    async def build(self, release: Release) -> BuiltRelease:
        """Unpack the snapshot of ``release`` and build its overlay; ``BuildFailed`` says why."""
        if release.snapshot_digest is None:
            raise BuildFailed("the release has no snapshot to build from (it was deleted)")
        app_dir = self._releases_dir / release.app_slug
        target = app_dir / str(release.release_no)
        await asyncio.to_thread(app_dir.mkdir, parents=True, exist_ok=True)
        scratch = Path(
            await asyncio.to_thread(tempfile.mkdtemp, prefix=_SCRATCH_PREFIX, dir=app_dir)
        )
        try:
            archive = scratch / ARCHIVE_NAME
            try:
                await self._store.get(release.snapshot_digest, archive)
            except ContentStoreError as exc:
                raise BuildFailed(str(exc)) from exc
            await asyncio.to_thread(_unpack_into, archive, scratch / "tree", target)
        finally:
            # Not on the event loop (a large tree), and shielded so a cancelled build is cleaned up.
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, scratch, True))
        if self._packages is None:
            return BuiltRelease(release_dir=target)
        overlay = await self._packages.prepare(release, target)
        return BuiltRelease(
            release_dir=target,
            overlays=launch_overlays(overlay),
            warnings=overlay.warnings if overlay is not None else (),
        )


def _unpack_into(archive_path: Path, tree: Path, target: Path) -> None:
    """Extract ``archive_path`` to ``tree``, then replace ``target`` with it; all or nothing."""
    try:
        with tarfile.open(archive_path, mode="r:") as archive:
            members = archive.getmembers()
            _check_members(members)
            tree.mkdir()
            for member in members:
                if member.isreg():
                    _write_member(archive, member, tree)
    except tarfile.TarError as exc:
        raise BuildFailed("the snapshot is not a readable archive") from exc
    except OSError as exc:
        raise BuildFailed(
            f"the snapshot could not be unpacked ({exc.strerror or 'I/O error'})"
        ) from exc
    with contextlib.suppress(FileNotFoundError):
        shutil.rmtree(target)
    tree.rename(target)


def _check_members(members: list[tarfile.TarInfo]) -> None:
    files = 0
    total_bytes = 0
    for member in members:
        if not _is_safe_name(member.name):
            raise BuildFailed(
                f"the snapshot holds an entry that is not a safe path: {member.name!r}"
            )
        if member.isdir():
            continue
        if not member.isreg():
            raise BuildFailed(
                f"the snapshot holds {member.name!r}, which is not a file; only files are unpacked"
            )
        files += 1
        total_bytes += member.size
    if files > MAX_EXTRACTED_FILES:
        raise BuildFailed(f"the snapshot holds more than {MAX_EXTRACTED_FILES} files")
    if total_bytes > MAX_EXTRACTED_BYTES:
        raise BuildFailed(
            f"the snapshot unpacks to more than {MAX_EXTRACTED_BYTES // BYTES_PER_MB} MB"
        )


def _is_safe_name(name: str) -> bool:
    """A relative path of plain segments: no leading ``/``, no empty, ``.`` or ``..``, no NUL."""
    if not name or "\x00" in name or name.startswith("/"):
        return False
    segments = name.removesuffix("/").split("/")
    return all(segment not in {"", ".", ".."} for segment in segments)


def _write_member(archive: tarfile.TarFile, member: tarfile.TarInfo, tree: Path) -> None:
    destination = tree / member.name
    if not destination.resolve().is_relative_to(tree.resolve()):
        raise BuildFailed(f"the snapshot holds an entry that is not a safe path: {member.name!r}")
    source = archive.extractfile(member)
    if source is None:
        raise BuildFailed(f"the snapshot holds {member.name!r}, which could not be read")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source, destination.open("wb") as output:
        shutil.copyfileobj(source, output)
    executable = member.mode & _ANY_EXECUTE_BIT
    destination.chmod(_EXECUTABLE_FILE_MODE if executable else _FILE_MODE)
