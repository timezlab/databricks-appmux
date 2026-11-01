"""An in-memory Databricks workspace for the tests of the content store and the snapshot source.

It stands in for the three workspace calls appmux makes (list a folder, read a file, write a file)
and behaves as spikes 4 and 15 measured the real one: a caller with no access gets an empty listing
or a 404 (never a 403), an unknown folder is a 404, and a write replaces what is there. Every call
records the token it was made with, so a test can prove which identity did what.
"""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath

from appmux.packages.store import (
    WorkspaceError,
    WorkspaceFileTooLarge,
    WorkspaceNotFound,
    WorkspaceObject,
    WorkspaceSessionExpired,
)

FILE = "FILE"
DIRECTORY = "DIRECTORY"


class FakeWorkspace:
    def __init__(self, *, required_token: str | None = None) -> None:
        self.files: dict[str, bytes] = {}
        self.claimed_sizes: dict[str, int] = {}
        self._folders: set[str] = set()
        self._other_objects: dict[str, str] = {}
        self.required_token = required_token
        self.reverse_listing = False
        self.expire_after_calls: int | None = None
        self.failing_status: dict[str, int] = {}
        self.calls: list[tuple[str, str, str]] = []  # (operation, path, token)

    # --- building the workspace -----------------------------------------------------------------

    def add_folder(self, path: str) -> None:
        self._folders.add(path)

    def add_file(self, path: str, data: bytes = b"", *, claimed_size: int | None = None) -> None:
        self.files[path] = data
        if claimed_size is not None:
            self.claimed_sizes[path] = claimed_size

    def add_other_object(self, path: str, object_type: str) -> None:
        self._other_objects[path] = object_type

    # --- the calls appmux makes -----------------------------------------------------------------

    def _enter(self, operation: str, path: str, token: str) -> None:
        self.calls.append((operation, path, token))
        if self.expire_after_calls is not None and len(self.calls) > self.expire_after_calls:
            raise WorkspaceSessionExpired("the token has expired", status=401)
        if path in self.failing_status:
            raise WorkspaceError("the workspace failed", status=self.failing_status[path])

    def _has_access(self, token: str) -> bool:
        return self.required_token is None or token == self.required_token

    def _children(self, folder: str) -> list[WorkspaceObject]:
        prefix = folder.rstrip("/") + "/"
        found: dict[str, WorkspaceObject] = {}
        for path, data in self.files.items():
            if path.startswith(prefix):
                head, _, rest = path[len(prefix) :].partition("/")
                if rest:
                    found[prefix + head] = WorkspaceObject(prefix + head, DIRECTORY)
                else:
                    size = self.claimed_sizes.get(path, len(data))
                    found[path] = WorkspaceObject(path, FILE, size)
        for path in self._folders:
            if path.startswith(prefix):
                head = path[len(prefix) :].partition("/")[0]
                found.setdefault(prefix + head, WorkspaceObject(prefix + head, DIRECTORY))
        for path, object_type in self._other_objects.items():
            if PurePosixPath(path).parent.as_posix() == folder.rstrip("/"):
                found[path] = WorkspaceObject(path, object_type)
        listing = sorted(found.values(), key=lambda item: item.path)
        return listing[::-1] if self.reverse_listing else listing

    def _exists_as_folder(self, folder: str) -> bool:
        prefix = folder.rstrip("/") + "/"
        return (
            folder in self._folders
            or any(path.startswith(prefix) for path in self.files)
            or any(path.startswith(prefix) for path in self._folders)
        )

    async def list_folder(self, token: str, folder: str) -> list[WorkspaceObject]:
        self._enter("list", folder, token)
        if not self._has_access(token):
            return []  # spike 15: no access is an empty listing, not an error
        if not self._exists_as_folder(folder):
            raise WorkspaceNotFound("the folder does not exist", status=404)
        return self._children(folder)

    async def read_file(self, token: str, path: str, destination: Path, *, max_bytes: int) -> None:
        self._enter("read", path, token)
        if not self._has_access(token) or path not in self.files:
            raise WorkspaceNotFound("the file does not exist", status=404)
        data = self.files[path]
        if len(data) > max_bytes:
            raise WorkspaceFileTooLarge("the file is over the limit", status=None)
        await asyncio.to_thread(destination.write_bytes, data)

    async def write_file(self, token: str, path: str, source: Path) -> None:
        self._enter("write", path, token)
        if not self._has_access(token):
            raise WorkspaceError("permission denied", status=403)
        self.files[path] = await asyncio.to_thread(source.read_bytes)

    async def file_exists(self, token: str, path: str) -> bool:
        self._enter("exists", path, token)
        if not self._has_access(token):
            raise WorkspaceError("permission denied", status=403)
        return path in self.files

    async def make_folder(self, token: str, path: str) -> None:
        self._enter("mkdirs", path, token)
        if not self._has_access(token):
            raise WorkspaceError("permission denied", status=403)
        self._folders.add(path)
