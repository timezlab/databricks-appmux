"""The folder browser behind the source-folder field (T194, admin-api.md addendum).

``GET /workspace/browse?path=`` lists one folder of the Databricks workspace so a person can pick
the source folder of an app instead of typing its path. It is read with the person's own token
(scope ``workspace.workspace:read``), exactly as the Update flow reads the folder later, so the
browser shows what the Update will be able to read and nothing more. It writes nothing and records
nothing (``workspace.browse``: any visitor with a token).

**Roots.** Without a ``path`` the answer is the two places a person starts from, ``My folder``
(``/Workspace/Users/<their e-mail>``) and ``Shared``, and nothing is called upstream.

**Paths.** Only the ``/Workspace/`` tree can be browsed. A path is refused before any call when it
is longer than 512 characters, holds a control character, or has an empty, ``.`` or ``..`` part
(a name that merely contains two dots is a name). The platform resolves paths itself, so this is not
what keeps a person in their own folders (their token does that); it keeps the route from sending
the platform something that is not a workspace path, and the listing from naming a path the next
call would refuse. The refusal never repeats the path.

**Failures.** Whatever the platform answers becomes one of four answers: ``404 not_found`` (it also
answers 404 for a folder the person may not see), ``403 no_access``, ``403 session_expired`` (it did
not accept the token) and ``503 workspace_unavailable`` (anything else, or no answer). The
platform's own text and the path are never in the body.

**The listing.** At most ``MAX_ENTRIES`` entries, folders first, then by name without regard to
case. The markers (``app.yaml`` and the other files that say what a folder is) are read from the
whole folder, not only the entries shown, so a folder of 300 files still says it is an app.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from typing import Final, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext
from appmux.admin.errors import BadRequest, Forbidden, NotFound, Unavailable
from appmux.admin.schemas import (
    WORKSPACE_PATH_PREFIX,
    Runtime,
    WorkspaceEntry,
    WorkspaceListing,
    WorkspaceMarker,
    WorkspaceRoot,
)
from appmux.authz.actions import Action
from appmux.packages.store import (
    WorkspaceError,
    WorkspaceNotFound,
    WorkspaceObject,
    WorkspaceSessionExpired,
)
from appmux.reconciler.runtime_limits import NODE_MIN_MEMORY_MB

MAX_ENTRIES: Final = 200
MAX_PATH_LENGTH: Final = 512

_HTTP_FORBIDDEN: Final = 403
_SHARED_ROOT: Final = "/Workspace/Shared"
_FORBIDDEN_SEGMENTS: Final = frozenset({"", ".", ".."})
# A directory and a repo can be opened; every other object (a file, a notebook, a library, a kind
# this code has not heard of) is listed as a file.
_FOLDER_OBJECT_TYPES: Final = frozenset({"DIRECTORY", "REPO"})
# In the order the contract lists them.
_MARKER_FILES: Final[tuple[WorkspaceMarker, ...]] = (
    "app.yaml",
    "databricks.yml",
    "requirements.txt",
    "package.json",
    "pyproject.toml",
)
_APP_MARKERS: Final = frozenset({"app.yaml", "requirements.txt", "package.json"})
_CONTROL_CATEGORY: Final = "Cc"

_INVALID_PATH_MESSAGE: Final = (
    "that is not a workspace folder path: use a path that starts with /Workspace/, has at most "
    f"{MAX_PATH_LENGTH} characters, no control character and no empty, '.' or '..' part"
)
_NO_USER_SESSION_MESSAGE: Final = (
    "appmux did not receive your Databricks access token, so it cannot browse the workspace as "
    "you; ask a platform admin to turn on user authorization for the appmux App, then reload"
)


class FolderLister(Protocol):
    """The part of the workspace client the browser uses (``packages.store.WorkspaceHttpApi``)."""

    async def list_folder(self, token: str, folder: str) -> list[WorkspaceObject]: ...


# --- the pure parts -------------------------------------------------------------------------


def clean_browse_path(raw: str) -> str:
    """The folder ``raw`` names, without one trailing slash, or ``BadRequest`` ``invalid_path``."""
    if len(raw) > MAX_PATH_LENGTH or any(
        unicodedata.category(character) == _CONTROL_CATEGORY for character in raw
    ):
        raise _invalid_path()
    path = raw.removesuffix("/")
    if not path.startswith(WORKSPACE_PATH_PREFIX):
        raise _invalid_path()
    if any(part in _FORBIDDEN_SEGMENTS for part in path[len(WORKSPACE_PATH_PREFIX) :].split("/")):
        raise _invalid_path()
    return path


def _invalid_path() -> BadRequest:
    return BadRequest(_INVALID_PATH_MESSAGE, code="invalid_path")


def _roots_of(email: str) -> list[WorkspaceRoot]:
    return [
        WorkspaceRoot(label="My folder", path=f"/Workspace/Users/{email}"),
        WorkspaceRoot(label="Shared", path=_SHARED_ROOT),
    ]


def parent_of(path: str, email: str) -> str | None:
    """The folder above ``path``; ``None`` at a root or a top-level folder (back to the roots)."""
    if any(path == root.path for root in _roots_of(email)):
        return None
    parent = path.rpartition("/")[0]
    return parent if parent.startswith(WORKSPACE_PATH_PREFIX) else None


def roots_listing(email: str) -> WorkspaceListing:
    """The answer for no ``path``: the starting places of ``email`` and nothing else."""
    return WorkspaceListing(
        path=None,
        parent=None,
        roots=_roots_of(email),
        entries=[],
        truncated=False,
        markers=[],
        looks_like_app=False,
        runtime_hint=None,
        suggested_memory_mb=None,
    )


def listing_of(path: str, objects: Iterable[WorkspaceObject], email: str) -> WorkspaceListing:
    """The answer for the folder ``path`` holding ``objects`` (the platform's listing of it)."""
    entries = sorted(
        (entry for item in objects if (entry := _entry_of(item)) is not None),
        key=lambda entry: (entry.kind != "folder", entry.name.casefold(), entry.name),
    )
    file_names = {entry.name for entry in entries if entry.kind == "file"}
    markers = [marker for marker in _MARKER_FILES if marker in file_names]
    runtime_hint = _runtime_hint_of(markers)
    return WorkspaceListing(
        path=path,
        parent=parent_of(path, email),
        roots=[],
        entries=entries[:MAX_ENTRIES],
        truncated=len(entries) > MAX_ENTRIES,
        markers=markers,
        looks_like_app=any(marker in _APP_MARKERS for marker in markers),
        runtime_hint=runtime_hint,
        suggested_memory_mb=NODE_MIN_MEMORY_MB if runtime_hint == "node" else None,
    )


def _entry_of(item: WorkspaceObject) -> WorkspaceEntry | None:
    name = item.path.rpartition("/")[2]
    if not item.path.startswith(WORKSPACE_PATH_PREFIX) or not name:
        return None  # not something the next browse (or an Update) could open
    return WorkspaceEntry(
        name=name,
        path=item.path,
        kind="folder" if item.object_type in _FOLDER_OBJECT_TYPES else "file",
    )


def _runtime_hint_of(markers: Sequence[WorkspaceMarker]) -> Runtime | None:
    """Python first, as the platform decides; a hint for the form, the Update decides for real."""
    if "requirements.txt" in markers:
        return "python"
    if "package.json" in markers:
        return "node"
    if "pyproject.toml" in markers:
        return "python"
    return None


# --- the route ------------------------------------------------------------------------------


class WorkspaceBrowseRoutes:
    """The route provider of ``GET /workspace/browse``; ``folders`` lists a workspace folder."""

    def __init__(self, folders: FolderLister) -> None:
        self._folders = folders

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def browse(request: Request) -> Response:
            await context.require(request, Action.WORKSPACE_BROWSE)
            actor = context.actor(request)
            if actor.visitor_token is None:
                raise Forbidden(_NO_USER_SESSION_MESSAGE, code="user_token_missing")
            raw = _single_path(request)
            if raw is None:
                return _json(roots_listing(actor.email))
            path = clean_browse_path(raw)
            objects = await self._list(actor.visitor_token.get_secret_value(), path)
            return _json(listing_of(path, objects, actor.email))

        return [Route("/workspace/browse", browse, methods=["GET"])]

    async def _list(self, token: str, path: str) -> list[WorkspaceObject]:
        try:
            return await self._folders.list_folder(token, path)
        except WorkspaceNotFound as exc:
            raise NotFound("no such folder, or it is not shared with you") from exc
        except WorkspaceSessionExpired as exc:
            raise Forbidden(
                "your Databricks session has ended; reload this page to sign in again",
                code="session_expired",
            ) from exc
        except WorkspaceError as exc:
            if exc.status == _HTTP_FORBIDDEN:
                raise Forbidden(
                    "your Databricks account cannot list this folder; ask its owner to share it "
                    "with you",
                    code="no_access",
                ) from exc
            raise Unavailable(
                _unavailable_message(exc.status), code="workspace_unavailable"
            ) from exc


def _single_path(request: Request) -> str | None:
    """The ``path`` parameter, or ``None`` when it is absent or blank (the roots)."""
    values = request.query_params.getlist("path")
    if len(values) > 1:
        raise BadRequest("path may be sent once")
    return values[0] if values and values[0].strip() else None


def _unavailable_message(status: int | None) -> str:
    answer = "did not answer" if status is None else f"answered {status}"
    return f"the workspace {answer} the folder listing; try again, or type the folder path"


def _json(listing: WorkspaceListing) -> JSONResponse:
    return JSONResponse(listing.model_dump(mode="json"))
