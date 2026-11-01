"""The content store: files named by their digest, kept in the service principal's workspace folder.

Snapshots (and, later, wheels) live outside the container so a restart loses nothing (design 5.2).
A file is stored once under the bare hex of its sha256, in ``/Workspace/Users/<sp-client-id>/...``,
and read back by that name; ``get`` hashes what it received and refuses bytes that do not match
their name, so a store that was edited by someone with access cannot feed a child other code.

Two classes, split at the only seam that touches the network:

* ``ContentStore`` is the logic: digest validation, the size limit, the service principal's token
  read fresh on every call (a token expires; the caller supplies a ``token_source``), and the
  mapping of workspace failures to ``ContentStoreError``.
* ``WorkspaceHttpApi`` is the adapter for the Databricks workspace REST calls. The snapshot source
  uses the same adapter with the *presser's* token to read the source folder, so there is one place
  that knows these URLs, their statuses and their quirks.

What spikes 4 and 15 measured, and this module relies on:

* Import: ``POST /api/2.0/workspace-files/import-file{path}?overwrite=true`` with a Content-Length;
  200 and 204 are success. A file over 500 MB makes the service close the connection with no
  status, which surfaces here as a ``WorkspaceError`` with ``status=None`` (and the size is checked
  before sending, so it should not happen).
* Export: ``GET /api/2.0/workspace/export?path=&direct_download=true`` streams the raw bytes.
* A caller with no access to a folder gets an empty listing or a 404, never a 403.
* Parallel writes get 429s, so callers write one file at a time.

The daily clean-up (``cleanup.py``) uses two more calls: ``list_files`` (the folder listing, with
each file's ``modified_at`` in milliseconds since the epoch) and ``delete_file``
(``POST /api/2.0/workspace/delete`` with ``recursive`` false). Neither delete nor the listing's
``modified_at`` was measured by a spike; they follow the documented REST API and are proven only
against ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

BYTES_PER_MB: Final = 1024 * 1024
# The largest file the workspace import accepts (spike 4: 500 MB works, 501 MB is refused).
MAX_FILE_BYTES: Final = 500 * BYTES_PER_MB

_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
_DIGEST_PREFIX: Final = "sha256:"
_FILE_NAME: Final = re.compile(r"[0-9a-f]{64}")
_FILE_OBJECT_TYPE: Final = "FILE"
_MS_PER_SECOND: Final = 1000
_WORKSPACE_TREE: Final = "/Workspace/"
_TRANSFER_CHUNK_BYTES: Final = 1024 * 1024
_WORKSPACE_TIMEOUT: Final = httpx.Timeout(60.0, connect=10.0)
_ERROR_TEXT_LIMIT: Final = 200
_HTTP_OK: Final = 200
_HTTP_NO_CONTENT: Final = 204
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_NOT_FOUND: Final = 404

_IMPORT_FILE: Final = "/api/2.0/workspace-files/import-file"
_EXPORT: Final = "/api/2.0/workspace/export"
_LIST: Final = "/api/2.0/workspace/list"
_GET_STATUS: Final = "/api/2.0/workspace/get-status"
_MKDIRS: Final = "/api/2.0/workspace/mkdirs"
_DELETE: Final = "/api/2.0/workspace/delete"


# --- errors -------------------------------------------------------------------------------------


class WorkspaceError(Exception):
    """A workspace call failed; ``status`` is the HTTP status, or ``None`` if there was none."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WorkspaceNotFound(WorkspaceError):
    """404: the path does not exist, or the caller may not see it."""


class WorkspaceSessionExpired(WorkspaceError):
    """401: the token was not accepted (for a user's token: the session ended)."""


class WorkspaceFileTooLarge(WorkspaceError):
    """The file being read is larger than the limit the caller set."""


class ContentStoreError(Exception):
    """The content store could not do what was asked; the cause is chained."""


class FileTooLarge(ContentStoreError):
    """The file is over the workspace's per-file limit, so it is not sent."""


class MissingFromStore(ContentStoreError):
    """No file is stored under this digest."""


class DigestMismatch(ContentStoreError):
    """The stored bytes do not hash to the name they are stored under."""


# --- the workspace seam -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkspaceObject:
    """One entry of a workspace folder listing."""

    path: str
    object_type: str
    size_bytes: int | None = None
    modified_at: datetime | None = None  # ``None``: the listing did not say (or said nonsense)


@dataclass(frozen=True, slots=True)
class StoredFile:
    """One file of the content-store folder, as the daily clean-up sees it."""

    name: str
    modified_at: datetime | None  # ``None``: the listing did not say

    def __post_init__(self) -> None:
        if self.modified_at is not None and self.modified_at.tzinfo is None:
            raise ValueError(
                "a file's modification time must carry a time zone; "
                "a naive time cannot be compared with the clock safely"
            )


class StoreApi(Protocol):
    """The workspace calls the content store makes; every call carries the token it runs as."""

    async def write_file(self, token: str, path: str, source: Path) -> None: ...

    async def read_file(
        self, token: str, path: str, destination: Path, *, max_bytes: int
    ) -> None: ...

    async def file_exists(self, token: str, path: str) -> bool: ...

    async def make_folder(self, token: str, path: str) -> None: ...

    async def list_folder(self, token: str, folder: str) -> list[WorkspaceObject]: ...

    async def delete_path(self, token: str, path: str) -> None: ...


def describe_bytes(count: int) -> str:
    """``524288000`` -> ``500 MB`` and ``10`` -> ``10 bytes``, as the limit is shown to a person."""
    if count > 0 and count % BYTES_PER_MB == 0:
        return f"{count // BYTES_PER_MB} MB"
    return f"{count} bytes"


def file_digest(path: Path) -> str:
    """``sha256:<hex>`` of the file at ``path``."""
    with path.open("rb") as handle:
        return _DIGEST_PREFIX + hashlib.file_digest(handle, "sha256").hexdigest()


# --- the store ----------------------------------------------------------------------------------


class _TokenSource(Protocol):
    def __call__(self) -> str: ...


class ContentStore:
    def __init__(
        self,
        api: StoreApi,
        *,
        folder: str,
        token_source: _TokenSource,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        if not folder.startswith(_WORKSPACE_TREE):
            raise ValueError(f"the content store folder must be under {_WORKSPACE_TREE}")
        self._api = api
        self._folder = folder.rstrip("/")
        self._token_source = token_source
        self._max_file_bytes = max_file_bytes

    async def ensure_folder(self) -> None:
        """Create the store folder if it is missing (spike 4 created it with ``mkdirs``)."""
        try:
            await self._api.make_folder(self._token_source(), self._folder)
        except WorkspaceError as exc:
            raise ContentStoreError(
                f"the content store folder could not be created{_status_text(exc)}"
            ) from exc

    async def exists(self, digest: str) -> bool:
        path = self._path_of(digest)
        try:
            return await self._api.file_exists(self._token_source(), path)
        except WorkspaceError as exc:
            raise ContentStoreError(
                f"the content store could not look up {digest}{_status_text(exc)}"
            ) from exc

    async def put(self, digest: str, source: Path) -> None:
        """Store the file at ``source`` as ``digest`` (the caller took it with ``file_digest``)."""
        path = self._path_of(digest)
        if await asyncio.to_thread(_size_of, source) > self._max_file_bytes:
            limit = describe_bytes(self._max_file_bytes)
            raise FileTooLarge(f"{digest} is over the {limit} limit of one stored file")
        try:
            await self._api.write_file(self._token_source(), path, source)
        except WorkspaceError as exc:
            raise ContentStoreError(
                f"the content store could not store {digest}{_status_text(exc)}"
            ) from exc

    async def get(self, digest: str, destination: Path) -> None:
        """Copy the file stored as ``digest`` to ``destination``, checking the bytes on arrival."""
        path = self._path_of(digest)
        try:
            await self._api.read_file(
                self._token_source(), path, destination, max_bytes=self._max_file_bytes
            )
        except WorkspaceNotFound as exc:
            raise MissingFromStore(f"{digest} is not in the content store") from exc
        except WorkspaceError as exc:
            raise ContentStoreError(
                f"the content store could not read {digest}{_status_text(exc)}"
            ) from exc
        if await asyncio.to_thread(file_digest, destination) != digest:
            await asyncio.to_thread(destination.unlink, missing_ok=True)
            raise DigestMismatch(f"the file stored as {digest} holds other bytes; it was discarded")

    async def list_files(self) -> list[StoredFile]:
        """The plain files of the store folder with their modification times.

        Folders, notebooks and anything listed from outside the folder are left out: the clean-up
        works on what is listed, so what is not listed is never removed.
        """
        try:
            objects = await self._api.list_folder(self._token_source(), self._folder)
        except WorkspaceError as exc:
            raise ContentStoreError(
                f"the content store could not list its files{_status_text(exc)}"
            ) from exc
        prefix = f"{self._folder}/"
        files: list[StoredFile] = []
        for item in objects:
            name = item.path.removeprefix(prefix)
            if (
                item.object_type == _FILE_OBJECT_TYPE
                and item.path.startswith(prefix)
                and "/" not in name
            ):
                files.append(StoredFile(name, item.modified_at))
        return files

    async def delete_file(self, name: str) -> None:
        """Remove the file called ``name`` (the bare hex of its digest); a missing file is fine."""
        if _FILE_NAME.fullmatch(name) is None:
            raise ValueError("a content-store file name is 64 lower-case hex digits")
        try:
            await self._api.delete_path(self._token_source(), f"{self._folder}/{name}")
        except WorkspaceNotFound:
            return  # already gone (another run, or someone by hand): the wanted end state
        except WorkspaceError as exc:
            raise ContentStoreError(
                f"the content store could not remove {name}{_status_text(exc)}"
            ) from exc

    def _path_of(self, digest: str) -> str:
        if _DIGEST.fullmatch(digest) is None:
            raise ValueError(
                "a content-store name is 'sha256:' followed by 64 lower-case hex digits"
            )
        return f"{self._folder}/{digest.removeprefix(_DIGEST_PREFIX)}"


def _status_text(exc: WorkspaceError) -> str:
    return f" (workspace status {exc.status})" if exc.status is not None else " (no answer)"


# --- the HTTP adapter ---------------------------------------------------------------------------


class _ListedObject(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    object_type: str
    size: int | None = None
    # Typed loosely on purpose: the same listing reads the user's source folder, and a field the
    # clean-up alone cares about must not make that fail. ``_from_epoch_ms`` decides what it means.
    modified_at: object = None


class _Listing(BaseModel):
    model_config = ConfigDict(extra="ignore")

    objects: list[_ListedObject] = []


class WorkspaceHttpApi:
    """The workspace REST calls over an ``httpx.AsyncClient`` whose base URL is the workspace."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def write_file(self, token: str, path: str, source: Path) -> None:
        size = await asyncio.to_thread(_size_of, source)
        headers = _headers(token) | {
            "Content-Length": str(size),
            "Content-Type": "application/octet-stream",
        }
        response = await self._call(
            "POST",
            _IMPORT_FILE + quote(path, safe="/"),
            params={"overwrite": "true"},
            headers=headers,
            content=_file_chunks(source),
        )
        if response.status_code not in {_HTTP_OK, _HTTP_NO_CONTENT}:
            raise _error_for(response)

    async def read_file(self, token: str, path: str, destination: Path, *, max_bytes: int) -> None:
        request = self._client.build_request(
            "GET",
            _EXPORT,
            params={"path": path, "direct_download": "true"},
            headers=_headers(token),
            timeout=_WORKSPACE_TIMEOUT,
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc
        try:
            if response.status_code != _HTTP_OK:
                await response.aread()
                raise _error_for(response)
            await _stream_to_file(response, destination, max_bytes)
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc
        finally:
            await response.aclose()

    async def file_exists(self, token: str, path: str) -> bool:
        response = await self._call(
            "GET", _GET_STATUS, params={"path": path}, headers=_headers(token)
        )
        if response.status_code == _HTTP_OK:
            return True
        if response.status_code == _HTTP_NOT_FOUND:
            return False
        raise _error_for(response)

    async def list_folder(self, token: str, folder: str) -> list[WorkspaceObject]:
        response = await self._call("GET", _LIST, params={"path": folder}, headers=_headers(token))
        if response.status_code != _HTTP_OK:
            raise _error_for(response)
        try:
            listing = _Listing.model_validate_json(response.content)
        except ValidationError as exc:
            raise WorkspaceError(
                "the workspace folder listing was not in the expected shape"
            ) from exc
        return [
            WorkspaceObject(
                item.path, item.object_type, item.size, _from_epoch_ms(item.modified_at)
            )
            for item in listing.objects
        ]

    async def make_folder(self, token: str, path: str) -> None:
        response = await self._call("POST", _MKDIRS, headers=_headers(token), json={"path": path})
        if response.status_code not in {_HTTP_OK, _HTTP_NO_CONTENT}:
            raise _error_for(response)

    async def delete_path(self, token: str, path: str) -> None:
        # ``recursive`` false: a folder bearing a digest's name is refused, never emptied.
        response = await self._call(
            "POST", _DELETE, headers=_headers(token), json={"path": path, "recursive": False}
        )
        if response.status_code not in {_HTTP_OK, _HTTP_NO_CONTENT}:
            raise _error_for(response)

    async def _call(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        content: AsyncIterator[bytes] | None = None,
        json: Mapping[str, str | bool] | None = None,
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                url,
                params=params,
                headers=headers,
                content=content,
                json=json,
                timeout=_WORKSPACE_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise _transport_error(exc) from exc


def _from_epoch_ms(value: object) -> datetime | None:
    # Milliseconds since the epoch as a whole number; anything else is "unknown", which the
    # clean-up reads as "keep".
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / _MS_PER_SECOND, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _size_of(path: Path) -> int:
    return path.stat().st_size


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _file_chunks(source: Path) -> AsyncIterator[bytes]:
    handle: BinaryIO = await asyncio.to_thread(source.open, "rb")
    try:
        while chunk := await asyncio.to_thread(handle.read, _TRANSFER_CHUNK_BYTES):
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


async def _stream_to_file(response: httpx.Response, destination: Path, max_bytes: int) -> None:
    handle: BinaryIO = await asyncio.to_thread(destination.open, "wb")
    written = 0
    try:
        async for chunk in response.aiter_bytes(_TRANSFER_CHUNK_BYTES):
            written += len(chunk)
            if written > max_bytes:
                raise WorkspaceFileTooLarge(
                    f"the workspace file is larger than {describe_bytes(max_bytes)}", status=None
                )
            await asyncio.to_thread(handle.write, chunk)
    except BaseException:
        await asyncio.to_thread(handle.close)
        await asyncio.to_thread(destination.unlink, missing_ok=True)
        raise
    await asyncio.to_thread(handle.close)


def _error_for(response: httpx.Response) -> WorkspaceError:
    status = response.status_code
    detail = _server_message(response)
    text = f"the workspace answered {status}" + (f": {detail}" if detail else "")
    if status == _HTTP_NOT_FOUND:
        return WorkspaceNotFound(text, status=status)
    if status == _HTTP_UNAUTHORIZED:
        return WorkspaceSessionExpired(text, status=status)
    return WorkspaceError(text, status=status)


def _server_message(response: httpx.Response) -> str:
    # Only the JSON `message` is passed on, cut short: a proxy's HTML page or a long trace is noise,
    # and nothing the server echoes is allowed to grow an error text without bound.
    try:
        body = response.json()
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    return message[:_ERROR_TEXT_LIMIT] if isinstance(message, str) else ""


def _transport_error(exc: httpx.HTTPError) -> WorkspaceError:
    # The class name says what happened (timeout, closed connection); `str(exc)` is not used, so a
    # URL or a header can never reach the message.
    return WorkspaceError(f"the workspace gave no answer ({type(exc).__name__})", status=None)
