"""The read-only source viewer of a release (T154, FR-029, rule 17).

``GET /apps/{app}/releases/{id}/source`` lists the files of a release's snapshot and
``GET /apps/{app}/releases/{id}/source/{path}`` returns one text file (``release.view_source``,
``CAN_MANAGE`` on the app, which a platform admin holds implicitly). It is how a platform admin
reads the source of any app, so it has three rules of its own.

**Text only.** A file is text when it is at most ``MAX_TEXT_BYTES``, holds no NUL and is valid
UTF-8. Every other file is listed with kind ``binary`` and refused on request (``400 not_text``):
the viewer never returns bytes it cannot show as text, and an over-size text file counts as binary
because the contract's ``binary`` means "listed, not returned".

**Every look is recorded, and first.** Once the snapshot has been read, the ``release.view_source``
row (release, and the path for one file) is written before any answer that depends on its content,
including "no such file" and "that is binary", which also tell the caller something about the
snapshot. A registry that cannot record the look makes the answer ``503`` with no content. A request
refused before the snapshot is read (no right, no such release, a path that is not inside the
snapshot) reveals nothing about it and writes no row.

**Path containment.** The snapshot is a tar archive that this module only reads: nothing is
extracted, so no member name is ever joined to a directory. A requested path is looked up as an
exact key among the archive's regular files, after it is checked to be a relative path with no
empty, ``.`` or ``..`` part and no control character, at most 512 characters (the most an audit
entry holds). The archive's own member names get the same check, and links, folders and special
files are neither listed nor served, so an archive that was not built by the snapshot source
(``reconciler/snapshot_source.py`` writes relative names and regular files only) cannot show a file
outside itself, and a name that bends out of it cannot be asked for.

The archive comes from the content store, which checks its digest on arrival. It is copied to a
scratch folder, read there and removed on every outcome. The store keeps one file per snapshot and
has no ranged read, so each look reads the whole archive; a cache is for when that is measured to
matter.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, TypeVar

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext
from appmux.admin.errors import AdminError, BadRequest, NotFound, Unavailable, validation_error
from appmux.admin.schemas import ReleaseSourceFile, ReleaseSourceListing, SourceFileEntry
from appmux.authz.actions import Action
from appmux.packages.store import ContentStoreError, MissingFromStore
from appmux.registry.audit import AuditDetail

logger = logging.getLogger(__name__)

ResultT = TypeVar("ResultT")

# The largest file the viewer returns: a page of source, not a data dump. It also bounds the memory
# one request holds, since a file is read whole.
MAX_TEXT_BYTES: Final = 1024 * 1024

_ARCHIVE_NAME: Final = "snapshot.tar"
# What an audit entry's ``path`` holds, so a longer name could not be recorded.
_MAX_PATH_LENGTH: Final = 512
_FORBIDDEN_NAME_PARTS: Final = frozenset({"", ".", ".."})
# The members the snapshot source writes. Links, folders and special files are never shown.
_REGULAR_FILE_TYPES: Final = (tarfile.REGTYPE, tarfile.AREGTYPE)
# Release numbers are small positive integers; anything else is no release, whatever it is.
_RELEASE_NO_PATTERN: Final = re.compile(r"[1-9][0-9]{0,9}")


class SnapshotStore(Protocol):
    """The part of the content store the viewer reads (``packages.store.ContentStore``)."""

    async def get(self, digest: str, destination: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class _FileView:
    """One file as the viewer found it: ``text`` is ``None`` when it cannot be shown."""

    size_bytes: int
    text: str | None


class SourceViewerRoutes:
    """The route provider of the two source routes; ``store`` serves a snapshot by its digest."""

    def __init__(self, store: SnapshotStore) -> None:
        self._store = store

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def list_files(request: Request) -> Response:
            slug, release_no, digest = await self._release_of(context, request)
            entry = context.audit_entry(
                request,
                "release.view_source",
                app_slug=slug,
                detail=_audit_detail(release_no),
            )
            files = await self._in_archive(slug, release_no, digest, _list_files)
            await context.write(request, [], [entry])
            listing = ReleaseSourceListing(release_no=release_no, items=files)
            return JSONResponse(listing.model_dump(mode="json"))

        async def get_file(request: Request) -> Response:
            slug, release_no, digest = await self._release_of(context, request)
            path = request.path_params["path"]
            if not _is_inside_snapshot(path):
                raise BadRequest(
                    "that is not a path inside the release: use a relative path from the "
                    "snapshot's root, with no '.', '..' or empty part"
                )
            entry = context.audit_entry(
                request,
                "release.view_source",
                app_slug=slug,
                detail=_audit_detail(release_no, path),
            )
            view = await self._in_archive(
                slug, release_no, digest, lambda archive: _read_file(archive, path)
            )
            await context.write(request, [], [entry])
            if view is None:
                raise NotFound("no such file in this release")
            if view.text is None:
                raise BadRequest(
                    f"this file is binary or larger than {MAX_TEXT_BYTES // 1024} KiB, so the "
                    "viewer lists it but does not show it",
                    code="not_text",
                )
            shown = ReleaseSourceFile(
                release_no=release_no, path=path, size_bytes=view.size_bytes, content=view.text
            )
            return JSONResponse(shown.model_dump(mode="json"))

        return [
            Route("/apps/{app}/releases/{release_id}/source", list_files, methods=["GET"]),
            Route(
                "/apps/{app}/releases/{release_id}/source/{path:path}",
                get_file,
                methods=["GET"],
            ),
        ]

    @staticmethod
    async def _release_of(context: AdminContext, request: Request) -> tuple[str, int, str]:
        """The app, the release number and the snapshot digest, once the actor may look.

        A deleted release has no snapshot, so it reads like a missing one.
        """
        slug = request.path_params["app"]
        await context.require(request, Action.RELEASE_VIEW_SOURCE, slug)
        raw_release_no = request.path_params["release_id"]
        if _RELEASE_NO_PATTERN.fullmatch(raw_release_no) is None:
            raise NotFound("no such release")
        release = context.registry.snapshot.release(slug, int(raw_release_no))
        if release is None or release.deleted_at is not None or release.snapshot_digest is None:
            raise NotFound("no such release")
        return slug, release.release_no, release.snapshot_digest

    async def _in_archive(
        self,
        slug: str,
        release_no: int,
        digest: str,
        read: Callable[[tarfile.TarFile], ResultT],
    ) -> ResultT:
        """Run ``read`` on the scratch copy of the snapshot, in a thread, and remove the copy."""
        folder = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="appmux-source-"))
        try:
            archive_path = folder / _ARCHIVE_NAME
            try:
                await self._store.get(digest, archive_path)
            except MissingFromStore as exc:
                logger.error("app %s release %d: %s", slug, release_no, exc)
                raise Unavailable(
                    "the stored files of this release are missing from the content store; ask "
                    "a platform admin to check it",
                    code="snapshot_missing",
                ) from exc
            except ContentStoreError as exc:
                logger.error("app %s release %d: %s", slug, release_no, exc)
                raise Unavailable(
                    "the content store could not be read; try again in a minute",
                    code="content_store_unavailable",
                ) from exc
            try:
                return await asyncio.to_thread(_with_archive, archive_path, read)
            except tarfile.TarError as exc:
                logger.error(
                    "app %s release %d: the snapshot is not a readable archive", slug, release_no
                )
                raise AdminError(
                    "the stored files of this release cannot be read as an archive; ask a "
                    "platform admin to check the server log",
                    code="snapshot_unreadable",
                ) from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, ignore_errors=True)


def _audit_detail(release_no: int, path: str | None = None) -> AuditDetail:
    try:
        return AuditDetail(release=release_no, path=path)
    except ValidationError as exc:
        # A path shaped like a credential is refused by the audit model, so it cannot be viewed.
        raise validation_error(exc) from exc


def _is_inside_snapshot(name: str) -> bool:
    """Whether ``name`` is a relative path that stays inside the snapshot, lexically.

    Used for the requested path and for every member name of the archive. Nothing is resolved on
    disk: the name is only ever compared with the archive's own names.
    """
    if not name or len(name) > _MAX_PATH_LENGTH:
        return False
    if any(ord(character) < 0x20 or character == "\x7f" for character in name):
        return False
    return all(part not in _FORBIDDEN_NAME_PARTS for part in name.split("/"))


def _with_archive(archive_path: Path, read: Callable[[tarfile.TarFile], ResultT]) -> ResultT:
    # "r:" reads an uncompressed archive only, which is what the snapshot source writes; a
    # compressed one is refused instead of being expanded without a bound.
    with tarfile.open(archive_path, "r:") as archive:
        return read(archive)


def _regular_files(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    """The archive's regular files by name; later members replace earlier ones, as in tar."""
    found: dict[str, tarfile.TarInfo] = {}
    unsafe = 0
    for member in archive:
        if member.type not in _REGULAR_FILE_TYPES:
            continue
        if not _is_inside_snapshot(member.name):
            unsafe += 1
            continue
        found[member.name] = member
    if unsafe:
        logger.warning("a snapshot holds %d files with names the viewer does not show", unsafe)
    return found


def _list_files(archive: tarfile.TarFile) -> list[SourceFileEntry]:
    files = _regular_files(archive)
    return [
        SourceFileEntry(
            path=name,
            size_bytes=files[name].size,
            kind="binary" if _text_of(archive, files[name]) is None else "text",
        )
        for name in sorted(files)
    ]


def _read_file(archive: tarfile.TarFile, name: str) -> _FileView | None:
    member = _regular_files(archive).get(name)
    if member is None:
        return None
    return _FileView(size_bytes=member.size, text=_text_of(archive, member))


def _text_of(archive: tarfile.TarFile, member: tarfile.TarInfo) -> str | None:
    """The file as text, or ``None`` when it is too large, holds a NUL or is not UTF-8."""
    if member.size > MAX_TEXT_BYTES:
        return None
    handle = archive.extractfile(member)
    if handle is None:
        return None
    with handle:
        data = handle.read()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
