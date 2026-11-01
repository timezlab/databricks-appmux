"""Package resolution inside the Update flow: the release's wheel list and what it covered (FR-031).

The flow stores a snapshot, and the build later installs a release's packages from the wheels the
release *recorded* (``releases.wheels``, read by ``reconciler/build.py``). Nothing else fills that
list, so this module does it at Update time (design 5.3, "Wheels: declared per child, stored once"):

* read the snapshot's ``requirements.txt`` (none: the release needs no packages);
* hand it to ``packages.resolve.resolve_requirements`` with the platform's package mode, which
  decides per requirement: the environment, a hashed wheel, or (in an index mode) the index;
* look for the wheels in this order, and store each one by sha256 in the content store:
  1. ``wheels/`` inside the app's own folder, read from the snapshot (it was snapshotted with it);
  2. the folders in the release config's ``wheel_sources``;
  3. the platform's ``default_wheel_sources``.
  Folders in the workspace are read with the *presser's* token, like ``source_path`` (the token is
  used here and nowhere else, and never appears in a message);
* an unresolved requirement **refuses the Update**, with every unresolved requirement in one
  message (design 5.3: "the Update fails and lists every requirement it could not resolve at once",
  ADR 0007: "a package with no wheel blocks the Update"). Nothing is written, so a refused Update
  also deletes no release to make room. A failure that may pass (the session ended, the workspace
  or the content store did not answer) is ``unavailable``: press Update again.

The result is a ``PackagePlan``: the wheels to record, and lines for the release page. The lines go
in ``detected.warnings`` prefixed ``packages:`` (the one free-text list a release shows): the
coverage summary (``41 of 58 from the environment, 17 from wheels``), each wheel that replaces the
environment's version of a package, and each requirements option that was ignored.

In ``index`` mode no wheel is read (recorded wheels are ignored by the build in that mode); the plan
only reports the coverage and refuses a line no build can install. The build re-checks the
environment at every start, so a plan made here is never the last word.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from pydantic import SecretStr

from appmux.packages.environment import Environment, read_environment
from appmux.packages.modes import PackageMode
from appmux.packages.resolve import Resolution, resolve_requirements
from appmux.packages.store import ContentStoreError, WorkspaceError, WorkspaceSessionExpired
from appmux.packages.wheels import (
    WheelSource,
    WheelSourceError,
    WheelStore,
    WorkspaceWheelSource,
    parse_wheel_file,
)
from appmux.reconciler.build import MAX_REQUIREMENTS_BYTES, REQUIREMENTS_FILE, package_policy_from
from appmux.reconciler.ports import UpdateRefused
from appmux.reconciler.snapshot_source import FolderReader
from appmux.registry.models import ReleaseConfig, WheelEntry
from appmux.registry.snapshot import RegistrySnapshot
from appmux.settings import PlatformSettings

logger = logging.getLogger(__name__)

# `WheelEntry.source` of a wheel taken from the app's own folder, and the folder's name in the
# snapshot (a path relative to the source folder, as the snapshot names its entries).
OWN_WHEELS_LABEL: Final = "wheels/"
_OWN_WHEELS_FOLDER: Final = PurePosixPath("wheels")
DEFAULT_WHEEL_SOURCES_KEY: Final = "default_wheel_sources"
SESSION_EXPIRED_WHILE_READING_WHEELS: Final = (
    "session expired while the wheels were read; press Update again"
)
_WORK_FOLDER_NAME: Final = "wheel-work"
_NOTE_PREFIX: Final = "packages: "
_HTTP_UNAUTHORIZED: Final = 401


# --- the app's own wheels/ folder, inside the snapshot ------------------------------------------


class ArchiveWheelSource:
    """The ``.whl`` files directly inside ``wheels/`` of the snapshot archive (design 5.3, first).

    The archive holds the app's folder as the Update read it, so its ``wheels/`` is what the person
    saw when they pressed the button, and reading it costs no second trip to the workspace.
    """

    def __init__(self, archive: Path) -> None:
        self._archive = archive

    @property
    def label(self) -> str:
        return OWN_WHEELS_LABEL

    async def list_filenames(self) -> list[str]:
        return await asyncio.to_thread(self._list)

    async def fetch(self, filename: str, destination: Path) -> None:
        parse_wheel_file(filename)  # the name is joined to a folder: it must be a plain name
        await asyncio.to_thread(self._copy, filename, destination)

    def _list(self) -> list[str]:
        try:
            with tarfile.open(self._archive, mode="r:") as archive:
                paths = [
                    PurePosixPath(member.name) for member in archive.getmembers() if member.isreg()
                ]
        except tarfile.TarError as exc:
            raise WheelSourceError(
                f"the {OWN_WHEELS_LABEL} folder of the snapshot could not be read"
            ) from exc
        return sorted(
            path.name
            for path in paths
            if path.parent == _OWN_WHEELS_FOLDER and path.suffix == ".whl"
        )

    def _copy(self, filename: str, destination: Path) -> None:
        member_name = (_OWN_WHEELS_FOLDER / filename).as_posix()
        try:
            with tarfile.open(self._archive, mode="r:") as archive:
                try:
                    member = archive.getmember(member_name)
                except KeyError as exc:
                    raise WheelSourceError(
                        f"{filename} is not in the {OWN_WHEELS_LABEL} folder of the snapshot"
                    ) from exc
                source = archive.extractfile(member) if member.isreg() else None
                if source is None:
                    raise WheelSourceError(
                        f"{filename} in the {OWN_WHEELS_LABEL} folder of the snapshot is not a file"
                    )
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)
        except tarfile.TarError as exc:
            raise WheelSourceError(
                f"the {OWN_WHEELS_LABEL} folder of the snapshot could not be read"
            ) from exc


# --- the plan ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackagePlan:
    """What an Update records for the packages of one release."""

    wheels: tuple[WheelEntry, ...]  # the release's wheel list: what its overlay installs
    notes: tuple[str, ...]  # lines for ``detected.warnings``, each starting ``packages: ``


class UpdatePackages:
    """Resolves the packages of a snapshot at Update time. One per service, shared by Updates."""

    def __init__(
        self,
        *,
        store: WheelStore,
        workspace: FolderReader,
        environment: Callable[[], Environment] = read_environment,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._environment = environment

    async def open(self, *, archive: Path, token: SecretStr) -> PackageSession | None:
        """The session of one Update, or ``None`` when the snapshot has no ``requirements.txt``.

        ``UpdateRefused`` when the file is too large or is not text. ``token`` is the presser's.
        """
        try:
            requirements = await asyncio.to_thread(_read_requirements, archive)
        except tarfile.TarError as exc:
            raise UpdateRefused(
                "unavailable",
                f"the stored snapshot's {REQUIREMENTS_FILE} could not be read; press Update again",
            ) from exc
        if requirements is None:
            return None
        return PackageSession(
            self, requirements=requirements, archive=archive, token=token,
            work_dir=archive.parent / _WORK_FOLDER_NAME,
        )  # fmt: skip


@dataclass(slots=True)
class PackageSession:
    """The packages of one Update: resolved once for each set of wheel sources, then reused.

    A write that loses a race makes the flow try again; the second try must not fetch the same
    wheels again, so a plan is remembered by the mode and the folders it was made for.
    """

    owner: UpdatePackages = field(repr=False)
    requirements: str = field(repr=False)
    archive: Path = field(repr=False)
    token: SecretStr = field(repr=False)
    work_dir: Path
    _plans: dict[tuple[PackageMode, tuple[str, ...]], PackagePlan] = field(
        default_factory=dict, repr=False
    )
    _environment: Environment | None = field(default=None, repr=False)

    async def plan_for(
        self, snapshot: RegistrySnapshot, app_slug: str, release_config: ReleaseConfig
    ) -> PackagePlan:
        """The plan for a release with ``release_config``; ``UpdateRefused`` if it cannot be met."""
        mode = package_policy_from(snapshot, app_slug).mode
        folders = _wheel_folders(release_config, snapshot) if mode.uses_wheels else ()
        key = (mode, folders)
        if key not in self._plans:
            self._plans[key] = await self._resolve(snapshot, mode, folders)
        return self._plans[key]

    async def _resolve(
        self, snapshot: RegistrySnapshot, mode: PackageMode, folders: tuple[str, ...]
    ) -> PackagePlan:
        if self._environment is None:
            self._environment = await asyncio.to_thread(self.owner._environment)
        sources: list[WheelSource] = []
        if mode.uses_wheels:
            token = self.token.get_secret_value()
            sources.append(ArchiveWheelSource(self.archive))
            sources.extend(
                WorkspaceWheelSource(api=self.owner._workspace, folder=folder, token=token)
                for folder in folders
            )
        try:
            resolution = await resolve_requirements(
                self.requirements,
                mode=mode,
                environment=self._environment,
                store=self.owner._store,
                sources=sources,
                stored_filenames=_recorded_filenames(snapshot),
                work_dir=self.work_dir,
            )
        except WorkspaceError as exc:
            raise _workspace_refusal(exc) from exc
        except ContentStoreError as exc:
            raise UpdateRefused("unavailable", str(exc)) from exc
        except OSError as exc:
            raise UpdateRefused(
                "unavailable",
                f"the wheels could not be saved on this server ({exc.strerror or 'I/O error'}); "
                "try again",
            ) from exc
        if resolution.unresolved:
            raise UpdateRefused("refused", _unresolved_message(resolution))
        return _plan_of(resolution)


# --- reading the inputs ---------------------------------------------------------------------------


def _read_requirements(archive_path: Path) -> str | None:
    """The text of ``requirements.txt`` at the root of the archive, or ``None`` if it has none."""
    with tarfile.open(archive_path, mode="r:") as archive:
        try:
            member = archive.getmember(REQUIREMENTS_FILE)
        except KeyError:
            return None
        source = archive.extractfile(member) if member.isreg() else None
        if source is None:
            return None
        if member.size > MAX_REQUIREMENTS_BYTES:
            raise UpdateRefused(
                "refused",
                f"{REQUIREMENTS_FILE} is larger than {MAX_REQUIREMENTS_BYTES} bytes; "
                "split the app or trim the lock",
            )
        with source:
            data = source.read(MAX_REQUIREMENTS_BYTES)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateRefused(
            "refused", f"{REQUIREMENTS_FILE} is not UTF-8 text; save it as UTF-8 and Update"
        ) from exc


def _wheel_folders(release_config: ReleaseConfig, snapshot: RegistrySnapshot) -> tuple[str, ...]:
    """The workspace folders to read wheels from, in order and without repeats."""
    ordered = [*release_config.wheel_sources, *_default_wheel_sources(snapshot)]
    return tuple(dict.fromkeys(folder for folder in ordered if folder))


def _default_wheel_sources(snapshot: RegistrySnapshot) -> list[str]:
    """The platform's folders, or none if the stored value is not a list of folders."""
    row = snapshot.setting(DEFAULT_WHEEL_SOURCES_KEY)
    if row is None:
        return list(PlatformSettings().default_wheel_sources)
    value = row.value
    if isinstance(value, list | tuple) and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    # A setting is free text to the registry, so the log names the key and not the value.
    logger.error(
        "the stored %s setting is not a list of folders; none are used", DEFAULT_WHEEL_SOURCES_KEY
    )
    return []


def _recorded_filenames(snapshot: RegistrySnapshot) -> Mapping[str, str]:
    """Digest to file name for every wheel a live release recorded, in any app.

    The content store keeps bytes under their digest only; a release records the real name, which
    pip needs. A hit still needs the lock to name the digest, so no app gets another's wheel by
    accident (design 5.3, "No cross-app effect").
    """
    return {
        wheel.sha256: wheel.filename
        for release in snapshot.releases
        if release.deleted_at is None
        for wheel in release.wheels
    }


# --- the outcome ----------------------------------------------------------------------------------


def _plan_of(resolution: Resolution) -> PackagePlan:
    lines = [resolution.summary()] if resolution.total_count else []
    lines.extend(resolution.replaced)
    lines.extend(resolution.notes)
    return PackagePlan(
        wheels=tuple(
            WheelEntry(sha256=wheel.sha256, filename=wheel.filename, source=wheel.source)
            for wheel in resolution.wheels
        ),
        notes=tuple(f"{_NOTE_PREFIX}{line}" for line in lines),
    )


def _unresolved_message(resolution: Resolution) -> str:
    count = len(resolution.unresolved)
    noun = "requirement" if count == 1 else "requirements"
    return (
        f"{count} {noun} cannot be installed: {'; '.join(resolution.unresolved)}; "
        f"fix {REQUIREMENTS_FILE} or add the wheels, then press Update again"
    )


def _workspace_refusal(exc: WorkspaceError) -> UpdateRefused:
    """What to tell the person for a failed workspace call; never the token or the server's text."""
    if isinstance(exc, WorkspaceSessionExpired) or exc.status == _HTTP_UNAUTHORIZED:
        return UpdateRefused("unavailable", SESSION_EXPIRED_WHILE_READING_WHEELS)
    answer = (
        f"the workspace answered {exc.status}"
        if exc.status is not None
        else "the workspace did not answer"
    )
    return UpdateRefused("unavailable", f"{answer} while reading the wheel sources; try again")
