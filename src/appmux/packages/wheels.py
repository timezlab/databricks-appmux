"""Wheels: file names, the store by sha256, the places wheels come from, and the fetch (T134).

A release that needs a package no environment provides installs it from a *hashed wheel* (design
5.3). The lock names the wheel by sha256 (``--hash=sha256:...``); this module finds a file with
exactly those bytes and puts it in the content store, where every later release reuses it without
fetching anything.

* ``parse_wheel_file`` turns a file name into a distribution, a version and tags, and refuses any
  name that could leave the directory it is joined to (the Zip-Slip class: ``..``, separators, NUL).
  ``packaging`` accepts a ``/`` in the tag part of a name, so a strict character list runs first.
* ``WheelStore`` is the content store seen by bare hex digest, and checks the bytes of every file it
  copies out: a store someone edited cannot feed a build other code.
* ``WheelSource`` is a place wheels can be listed and read from. ``WorkspaceWheelSource`` is a
  workspace folder read with the *presser's* token (the identity that already may read it); the
  app's own ``wheels/`` folder and the admin-set ``wheel_sources`` are both such folders.
* ``fetch_wheels`` resolves a list of requests to wheels, strictly in order and one file at a time
  (workspace calls are rate-limited, spike 4). A file is accepted only if its sha256 is one the lock
  names, so a source that changed a file, or a name that matches different bytes, is not a hit.
* ``stage_wheels`` copies a release's wheels out of the store into the directory a build installs
  from, under their real file names (pip reads the tags from the name).

Nothing here starts a process or opens a socket of its own: the workspace calls go through the same
seam the content store uses.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from packaging.tags import Tag, sys_tags
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import Version

from appmux.packages.store import (
    MAX_FILE_BYTES,
    DigestMismatch,
    WorkspaceFileTooLarge,
    WorkspaceNotFound,
    WorkspaceObject,
    describe_bytes,
    file_digest,
)

# `WheelEntry.source` of a wheel that was already in the content store (nothing was fetched).
STORE_SOURCE: Final = "content store"

_DIGEST_PREFIX: Final = "sha256:"
_BARE_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
# Letters, digits and the few marks a wheel name uses (`_ . + ! -`). No separator of any kind, no
# NUL, no space: the file name is joined to a directory, so it has to be a single plain component.
_WHEEL_FILENAME: Final = re.compile(r"[A-Za-z0-9_.+!-]{1,200}\.whl")
_WORKSPACE_FILE: Final = "FILE"
_DIGEST_SHOWN_CHARS: Final = 4


# --- errors -------------------------------------------------------------------------------------


class UnsafeWheelName(ValueError):
    """A wheel file name that is not a plain wheel name, or that would leave its directory."""


class WheelSourceError(Exception):
    """A wheel source could not be read; the message names it and says what to check."""


# --- file names ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedWheel:
    filename: str
    name: str  # canonical (lower case, '-' for '_' and '.')
    version: Version
    tags: frozenset[Tag]


def parse_wheel_file(filename: str) -> ParsedWheel:
    """Read a wheel file name, or raise ``UnsafeWheelName`` if it is not a plain wheel name."""
    if _WHEEL_FILENAME.fullmatch(filename) is None:
        raise UnsafeWheelName(
            "a wheel file name is one plain name ending in .whl, with no path separators"
        )
    try:
        name, version, _build, tags = parse_wheel_filename(filename)
    except InvalidWheelFilename as exc:
        raise UnsafeWheelName(
            f"{filename} is not a wheel file name (name-version-python-abi-platform.whl)"
        ) from exc
    return ParsedWheel(filename, name, version, tags)


def running_tags() -> tuple[Tag, ...]:
    """The tags this interpreter can install, most specific first (the order ``pip`` prefers)."""
    return tuple(sys_tags())


# --- the store ----------------------------------------------------------------------------------


class _ContentFiles(Protocol):
    """The part of ``ContentStore`` the wheel store uses (``sha256:<hex>`` names)."""

    async def exists(self, digest: str) -> bool: ...

    async def put(self, digest: str, source: Path) -> None: ...

    async def get(self, digest: str, destination: Path) -> None: ...


class WheelStore:
    """The content store by bare hex digest, which is how releases record their wheels."""

    def __init__(self, content: _ContentFiles) -> None:
        self._content = content

    async def has(self, sha256: str) -> bool:
        return await self._content.exists(_stored_name(sha256))

    async def add(self, sha256: str, source: Path) -> None:
        """Store ``source`` as ``sha256``; refuses a file whose bytes are not that digest."""
        name = _stored_name(sha256)
        if await asyncio.to_thread(file_digest, source) != name:
            raise DigestMismatch(f"the file offered as {name} holds other bytes; it was not stored")
        await self._content.put(name, source)

    async def copy_to(self, sha256: str, destination: Path) -> None:
        """Copy the stored wheel to ``destination`` after hashing what arrived.

        ``ContentStore.get`` already does the check; it is repeated here because this is the last
        step before the bytes reach a build, and the store behind the seam may be another one.
        """
        name = _stored_name(sha256)
        await self._content.get(name, destination)
        if await asyncio.to_thread(file_digest, destination) != name:
            await asyncio.to_thread(destination.unlink, missing_ok=True)
            raise DigestMismatch(f"the wheel stored as {name} holds other bytes; it was discarded")


def _stored_name(sha256: str) -> str:
    if _BARE_DIGEST.fullmatch(sha256) is None:
        raise ValueError("a wheel digest is 64 lower-case hex digits, without a 'sha256:' prefix")
    return _DIGEST_PREFIX + sha256


# --- the sources --------------------------------------------------------------------------------


class WheelSource(Protocol):
    """A place wheels can be listed and read from."""

    @property
    def label(self) -> str:
        """How this source is named in a release's wheel list and in messages."""
        ...

    async def list_filenames(self) -> list[str]: ...

    async def fetch(self, filename: str, destination: Path) -> None: ...


class _WorkspaceReader(Protocol):
    async def list_folder(self, token: str, folder: str) -> list[WorkspaceObject]: ...

    async def read_file(
        self, token: str, path: str, destination: Path, *, max_bytes: int
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkspaceWheelSource:
    """The ``.whl`` files directly inside one workspace folder, read with the presser's token."""

    api: _WorkspaceReader = field(repr=False)
    folder: str
    token: str = field(repr=False)
    max_file_bytes: int = MAX_FILE_BYTES

    @property
    def label(self) -> str:
        return self.folder

    async def list_filenames(self) -> list[str]:
        try:
            objects = await self.api.list_folder(self.token, self.folder)
        except WorkspaceNotFound as exc:
            raise self._unreadable(self.folder) from exc
        names = (PurePosixPath(item.path).name for item in objects if _is_wheel_file(item))
        return sorted(name for name in names if name.endswith(".whl"))

    async def fetch(self, filename: str, destination: Path) -> None:
        parse_wheel_file(filename)  # the name is joined to the folder: it must be a plain name
        path = f"{self.folder.rstrip('/')}/{filename}"
        try:
            await self.api.read_file(self.token, path, destination, max_bytes=self.max_file_bytes)
        except WorkspaceNotFound as exc:
            raise self._unreadable(f"{self.folder}/{filename}") from exc
        except WorkspaceFileTooLarge as exc:
            limit = describe_bytes(self.max_file_bytes)
            raise WheelSourceError(f"{filename} in '{self.folder}' is larger than {limit}") from exc

    @staticmethod
    def _unreadable(path: str) -> WheelSourceError:
        return WheelSourceError(
            f"'{path}' could not be read (it is missing, or you may not read it)"
        )


def _is_wheel_file(item: WorkspaceObject) -> bool:
    return item.object_type == _WORKSPACE_FILE


# --- fetching -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WheelRequest:
    """One pinned, hashed requirement of a lock: ``name==version`` and the digests it accepts."""

    requirement: str  # as the person wrote it, for messages: ``foo==2.0``
    name: str
    version: Version
    sha256s: tuple[str, ...]
    line_number: int


@dataclass(frozen=True, slots=True)
class ResolvedWheel:
    """A wheel in the content store, as a release records it (``releases.wheels``)."""

    sha256: str
    filename: str
    source: str


@dataclass(frozen=True, slots=True)
class WheelMatch:
    request: WheelRequest
    wheel: ResolvedWheel


@dataclass(frozen=True, slots=True)
class MissingWheel:
    request: WheelRequest
    reason: str


@dataclass(frozen=True, slots=True)
class FetchResult:
    matches: tuple[WheelMatch, ...]
    missing: tuple[MissingWheel, ...]

    @property
    def wheels(self) -> tuple[ResolvedWheel, ...]:
        """The distinct wheels, in the order of the requests that first needed them."""
        distinct: dict[str, ResolvedWheel] = {}
        for match in self.matches:
            distinct.setdefault(match.wheel.sha256, match.wheel)
        return tuple(distinct.values())


async def fetch_wheels(
    requests: Sequence[WheelRequest],
    *,
    store: WheelStore,
    sources: Sequence[WheelSource],
    stored_filenames: Mapping[str, str],
    work_dir: Path,
    supported_tags: Sequence[Tag] | None = None,
) -> FetchResult:
    """Find, for each request, a wheel with one of its digests; the store first, then the sources.

    ``stored_filenames`` maps a digest to the file name recorded for it by a release that used it:
    the store holds bytes under their digest only, and pip needs the real name. A recorded name
    counts only if it names the pinned distribution and version, so a digest reused for another
    package is not a hit.

    Requests are handled strictly in order and one file is downloaded at a time. A source that
    cannot be listed is named in the message of each request that needed it and the search goes on;
    a lost session or any other workspace failure stops the whole fetch (it would fail the same way
    for every request).
    """
    tags = tuple(supported_tags) if supported_tags is not None else running_tags()
    search = _Search(store, sources, stored_filenames, work_dir, tags)
    matches: list[WheelMatch] = []
    missing: list[MissingWheel] = []
    for request in requests:
        outcome = await search.find(request)
        if isinstance(outcome, ResolvedWheel):
            matches.append(WheelMatch(request, outcome))
        else:
            missing.append(MissingWheel(request, outcome))
    return FetchResult(tuple(matches), tuple(missing))


class _Search:
    """The state of one ``fetch_wheels`` call: what each source lists, and what is found so far."""

    def __init__(
        self,
        store: WheelStore,
        sources: Sequence[WheelSource],
        stored_filenames: Mapping[str, str],
        work_dir: Path,
        tags: Sequence[Tag],
    ) -> None:
        self._store = store
        self._sources = sources
        self._stored_filenames = stored_filenames
        self._work_dir = work_dir
        self._tag_rank = {tag: rank for rank, tag in enumerate(tags)}
        self._listings: dict[int, list[str] | WheelSourceError] = {}
        self._found: dict[str, ResolvedWheel] = {}
        self._downloads = 0

    async def find(self, request: WheelRequest) -> ResolvedWheel | str:
        """The wheel for ``request``, or the sentence that says why there is none."""
        for sha256 in request.sha256s:
            if sha256 in self._found:
                return self._found[sha256]
        stored = await self._in_store(request)
        if stored is not None:
            return stored
        notes: list[str] = []
        for index, source in enumerate(self._sources):
            wheel = await self._from_source(index, source, request, notes)
            if wheel is not None:
                self._found[wheel.sha256] = wheel
                return wheel
        return _missing_reason(request, notes)

    async def _in_store(self, request: WheelRequest) -> ResolvedWheel | None:
        for sha256 in request.sha256s:
            filename = self._stored_filenames.get(sha256)
            if filename is None or not _names_request(filename, request):
                continue
            if await self._store.has(sha256):
                wheel = ResolvedWheel(sha256, filename, STORE_SOURCE)
                self._found[sha256] = wheel
                return wheel
        return None

    async def _from_source(
        self, index: int, source: WheelSource, request: WheelRequest, notes: list[str]
    ) -> ResolvedWheel | None:
        listing = await self._listing(index, source)
        if isinstance(listing, WheelSourceError):
            notes.append(str(listing))
            return None
        candidates, other_platform = self._candidates(listing, request)
        if other_platform and not candidates:
            notes.append(
                f"'{source.label}' has {request.name} {request.version} only for another Python "
                f"or platform ({', '.join(other_platform)})"
            )
        for filename in candidates:
            try:
                wheel = await self._download(source, filename, request)
            except WheelSourceError as exc:
                notes.append(str(exc))
                continue
            if wheel is not None:
                return wheel
            notes.append(f"{filename} in '{source.label}' holds other bytes than the lock names")
        return None

    async def _listing(self, index: int, source: WheelSource) -> list[str] | WheelSourceError:
        if index not in self._listings:
            try:
                self._listings[index] = await source.list_filenames()
            except WheelSourceError as exc:
                self._listings[index] = exc
        return self._listings[index]

    def _candidates(
        self, filenames: Sequence[str], request: WheelRequest
    ) -> tuple[list[str], list[str]]:
        """Names that are the pinned version and fit this Python (most specific tag first)."""
        ranked: list[tuple[int, str]] = []
        other_platform: list[str] = []
        for filename in filenames:
            try:
                parsed = parse_wheel_file(filename)
            except UnsafeWheelName:
                continue  # a stray file in a shared folder is not an error, just not a candidate
            if not _is_pinned(parsed, request):
                continue
            ranks = [self._tag_rank[tag] for tag in parsed.tags if tag in self._tag_rank]
            if ranks:
                ranked.append((min(ranks), filename))
            else:
                other_platform.append(filename)
        return [filename for _, filename in sorted(ranked)], sorted(other_platform)

    async def _download(
        self, source: WheelSource, filename: str, request: WheelRequest
    ) -> ResolvedWheel | None:
        """Fetch one file and keep it only if its digest is one the lock names."""
        await asyncio.to_thread(self._work_dir.mkdir, parents=True, exist_ok=True)
        self._downloads += 1
        partial = self._work_dir / f"download-{self._downloads}.part"
        try:
            await source.fetch(filename, partial)
            digest = await asyncio.to_thread(file_digest, partial)
            sha256 = digest.removeprefix(_DIGEST_PREFIX)
            if sha256 not in request.sha256s:
                return None
            await self._store.add(sha256, partial)
            return ResolvedWheel(sha256, filename, source.label)
        finally:
            await asyncio.to_thread(partial.unlink, missing_ok=True)


def _is_pinned(parsed: ParsedWheel, request: WheelRequest) -> bool:
    return parsed.name == canonicalize_name(request.name) and parsed.version == request.version


def _names_request(filename: str, request: WheelRequest) -> bool:
    try:
        return _is_pinned(parse_wheel_file(filename), request)
    except UnsafeWheelName:
        return False


def _missing_reason(request: WheelRequest, notes: Sequence[str]) -> str:
    shown = " or ".join(f"{sha256[:_DIGEST_SHOWN_CHARS]}…" for sha256 in request.sha256s)
    reason = f"no wheel for '{request.requirement}' (sha256 {shown}) in the wheel sources"
    return f"{reason}: {'; '.join(notes)}" if notes else reason


# --- staging ------------------------------------------------------------------------------------


async def stage_wheels(store: WheelStore, wheels: Sequence[ResolvedWheel], directory: Path) -> None:
    """Copy each wheel from the store into ``directory`` under its file name, one at a time.

    All names are checked before the first copy, so a bad list writes nothing. The directory is
    the build's own (a fresh one per build), never a shared one.
    """
    base = await asyncio.to_thread(directory.resolve)
    seen: set[str] = set()
    for wheel in wheels:
        parse_wheel_file(wheel.filename)
        if wheel.filename in seen:
            raise UnsafeWheelName(
                f"two wheels share the file name {wheel.filename}; a build needs one per file name"
            )
        seen.add(wheel.filename)
        if (base / wheel.filename).resolve().parent != base:
            raise UnsafeWheelName(f"{wheel.filename} would be written outside the wheel directory")
    for wheel in wheels:
        await store.copy_to(wheel.sha256, base / wheel.filename)
