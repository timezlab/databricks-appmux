"""The snapshot step of an Update: read a workspace folder, make one archive, store it (FR-003).

``source_path`` is read with the *presser's* on-behalf-of token (scope
``workspace.workspace:read``), so appmux can snapshot a folder only for a person who can read it.
The result is one deterministic archive named by its sha256 (``sha256:<hex>``), stored under that
name in the content store with appmux's own service-principal token. The archive's name is its
content: the same folder gives the same digest, so "nothing changed since the live release"
is a string comparison.

What makes the digest depend only on the files:

* entries are sorted by path relative to the source folder, so the workspace's listing order, the
  folder's name and its owner do not matter;
* every entry carries mtime 0, uid and gid 0, no user or group name and mode 0644, so nothing about
  time, ownership or permissions leaks in;
* the archive is not compressed, so no library version can change the bytes.

What the workspace does that this module has to work around (spikes 4 and 15):

* A caller with no access gets an *empty listing* or a 404, never a 403. An empty snapshot is
  therefore refused as ``empty, or you cannot read it``: the person cannot be told which.
* A file over 500 MB is refused by the service with no status, so the listing's declared size is
  checked first and the read is bounded.
* Parallel writes get 429s, so files are read one at a time.
* A user's token can end during a long snapshot; that is reported as something to try again.

Notebooks and any object that is not a file or a folder are refused by name: their export is a
different format and they are not part of a Databricks App's source.
"""

from __future__ import annotations

import asyncio
import shutil
import tarfile
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Protocol

from appmux.packages.store import (
    MAX_FILE_BYTES,
    ContentStore,
    ContentStoreError,
    FileTooLarge,
    WorkspaceError,
    WorkspaceFileTooLarge,
    WorkspaceNotFound,
    WorkspaceObject,
    WorkspaceSessionExpired,
    describe_bytes,
    file_digest,
)

EMPTY_FOLDER_REASON: Final = "empty, or you cannot read it"
SESSION_EXPIRED_REASON: Final = "session expired during the snapshot; press Update again"
# Reading is one file at a time (parallel calls get 429s), measured at roughly ten files a second,
# so a folder of thousands of files would hold an Update for minutes. A source folder of an app is
# its code and assets, not its dependencies; this bounds the mistake of pointing at a larger tree.
MAX_FILE_COUNT: Final = 5000

ARCHIVE_NAME: Final = "snapshot.tar"
_DOWNLOAD_NAME: Final = "download.part"
_FILE: Final = "FILE"
_DIRECTORY: Final = "DIRECTORY"
_ARCHIVE_FILE_MODE: Final = 0o644
_WORKSPACE_PREFIX: Final = "/Workspace"
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_FORBIDDEN: Final = 403
_HTTP_NOT_FOUND: Final = 404


class SnapshotRefused(Exception):
    """The folder cannot be snapshotted; the message is what the person pressing Update sees."""


class FolderReader(Protocol):
    """The two workspace calls a snapshot makes, as the presser."""

    async def list_folder(self, token: str, folder: str) -> list[WorkspaceObject]: ...

    async def read_file(
        self, token: str, path: str, destination: Path, *, max_bytes: int
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class BuiltArchive:
    path: Path
    digest: str
    file_count: int
    size_bytes: int  # the sum of the files' sizes, not the archive's


@dataclass(frozen=True, slots=True)
class Snapshot:
    digest: str
    file_count: int
    size_bytes: int
    already_up_to_date: bool


@dataclass(frozen=True, slots=True)
class _SourceFile:
    relative_path: str
    workspace_path: str


async def build_archive(
    *,
    reader: FolderReader,
    token: str,
    source_path: str,
    work_dir: Path,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_file_count: int = MAX_FILE_COUNT,
) -> BuiltArchive:
    """Write the archive of ``source_path`` to ``work_dir``; raise ``SnapshotRefused`` if unable."""
    files = await _list_files(
        reader, token, source_path.rstrip("/"), max_file_bytes, max_file_count
    )
    if not files:
        raise SnapshotRefused(EMPTY_FOLDER_REASON)
    files.sort(key=lambda file: file.relative_path)

    await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
    archive_path = work_dir / ARCHIVE_NAME
    download_path = work_dir / _DOWNLOAD_NAME
    try:
        size_bytes = await _pack(reader, token, files, archive_path, download_path, max_file_bytes)
        digest = await asyncio.to_thread(file_digest, archive_path)
    except BaseException:
        await asyncio.to_thread(archive_path.unlink, missing_ok=True)
        await asyncio.to_thread(download_path.unlink, missing_ok=True)
        raise
    return BuiltArchive(archive_path, digest, len(files), size_bytes)


class SnapshotSource:
    def __init__(self, reader: FolderReader, store: ContentStore) -> None:
        self._reader = reader
        self._store = store

    async def take(
        self, *, source_path: str, token: str, live_digests: Collection[str]
    ) -> Snapshot:
        """Snapshot ``source_path`` as the holder of ``token`` and make sure the store holds it.

        ``already_up_to_date`` says the digest is one of ``live_digests``, so the caller can answer
        "nothing changed". The store is still asked for the file: a store that lost it would fail
        the next start of that release.
        """
        scratch = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="appmux-snapshot-"))
        try:
            archive = await build_archive(
                reader=self._reader, token=token, source_path=source_path, work_dir=scratch
            )
            await self._ensure_stored(archive)
        finally:
            await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)
        return Snapshot(
            digest=archive.digest,
            file_count=archive.file_count,
            size_bytes=archive.size_bytes,
            already_up_to_date=archive.digest in live_digests,
        )

    async def _ensure_stored(self, archive: BuiltArchive) -> None:
        try:
            if not await self._store.exists(archive.digest):
                await self._store.put(archive.digest, archive.path)
        except FileTooLarge as exc:
            raise SnapshotRefused(str(exc)) from exc
        except ContentStoreError as exc:
            raise SnapshotRefused(str(exc)) from exc


# --- reading the folder -------------------------------------------------------------------------


async def _list_files(
    reader: FolderReader, token: str, root: str, max_file_bytes: int, max_file_count: int
) -> list[_SourceFile]:
    files: list[_SourceFile] = []
    pending = [root]
    while pending:
        folder = pending.pop()
        try:
            objects = await reader.list_folder(token, folder)
        except WorkspaceError as exc:
            raise _refusal_for(exc, unreadable=EMPTY_FOLDER_REASON) from exc
        for item in objects:
            relative = _relative_path(root, item.path)
            if item.object_type == _DIRECTORY:
                pending.append(item.path)
            elif item.object_type != _FILE:
                raise SnapshotRefused(
                    f"'{relative}' is a {item.object_type}, not a file; only files and folders "
                    "can be part of an app"
                )
            elif item.size_bytes is not None and item.size_bytes > max_file_bytes:
                raise SnapshotRefused(_too_large_text(relative, max_file_bytes))
            else:
                files.append(_SourceFile(relative, item.path))
                if len(files) > max_file_count:
                    raise SnapshotRefused(f"the folder holds more than {max_file_count} files")
    return files


def _relative_path(root: str, listed_path: str) -> str:
    """The path of a listed object below the source folder, in a form safe to use as an entry name.

    The listing may or may not repeat the ``/Workspace`` prefix the request used, so both spellings
    of the root are tried.
    """
    for prefix in (root, root.removeprefix(_WORKSPACE_PREFIX)):
        if listed_path.startswith(prefix + "/"):
            relative = listed_path[len(prefix) + 1 :]
            if any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts) or (
                "//" in relative
            ):
                break
            return relative
    raise SnapshotRefused("the workspace listed a path outside the source folder")


# --- writing the archive ------------------------------------------------------------------------


async def _pack(
    reader: FolderReader,
    token: str,
    files: list[_SourceFile],
    archive_path: Path,
    download_path: Path,
    max_file_bytes: int,
) -> int:
    total_bytes = 0
    # PAX is the default on current Pythons; naming it keeps the bytes from changing with a version.
    archive = await asyncio.to_thread(tarfile.open, archive_path, "w", format=tarfile.PAX_FORMAT)
    try:
        for file in files:
            await _fetch(reader, token, file, download_path, max_file_bytes)
            total_bytes += await asyncio.to_thread(
                _append, archive, file.relative_path, download_path
            )
            await asyncio.to_thread(download_path.unlink)
    finally:
        await asyncio.to_thread(archive.close)
    return total_bytes


async def _fetch(
    reader: FolderReader, token: str, file: _SourceFile, destination: Path, max_file_bytes: int
) -> None:
    try:
        await reader.read_file(token, file.workspace_path, destination, max_bytes=max_file_bytes)
    except WorkspaceFileTooLarge as exc:
        raise SnapshotRefused(_too_large_text(file.relative_path, max_file_bytes)) from exc
    except WorkspaceError as exc:
        unreadable = (
            f"'{file.relative_path}' could not be read; it may have been removed while the "
            "snapshot was taken"
        )
        raise _refusal_for(exc, unreadable=unreadable) from exc


def _append(archive: tarfile.TarFile, name: str, source: Path) -> int:
    """Add ``source`` as ``name`` with every field that could vary between runs fixed."""
    entry = tarfile.TarInfo(name)
    entry.size = source.stat().st_size
    entry.mode = _ARCHIVE_FILE_MODE
    entry.mtime = 0
    entry.uid = entry.gid = 0
    entry.uname = entry.gname = ""
    with source.open("rb") as handle:
        archive.addfile(entry, handle)
    return entry.size


# --- the person's view of a failure -------------------------------------------------------------


def _too_large_text(relative: str, max_file_bytes: int) -> str:
    return f"'{relative}' is over the {describe_bytes(max_file_bytes)} limit of one file"


def _refusal_for(exc: WorkspaceError, *, unreadable: str) -> SnapshotRefused:
    """What to tell the person for a failed workspace call; never the token or the server's text."""
    if isinstance(exc, WorkspaceSessionExpired) or exc.status == _HTTP_UNAUTHORIZED:
        return SnapshotRefused(SESSION_EXPIRED_REASON)
    if isinstance(exc, WorkspaceNotFound) or exc.status in {_HTTP_FORBIDDEN, _HTTP_NOT_FOUND}:
        return SnapshotRefused(unreadable)
    answer = (
        f"the workspace answered {exc.status}"
        if exc.status is not None
        else "the workspace did not answer"
    )
    return SnapshotRefused(f"{answer} while reading the source folder; try again")
