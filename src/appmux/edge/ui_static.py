"""Serving the admin UI's Next.js static export (design 7.3 and 10.1, FR-021, edge-http.md).

The Python counterpart of the reference edge the browser test uses (``web/e2e/server.ts``): the
export is served under ``/_appmux/`` (and ``/`` is the launcher), a directory asked for without its
trailing slash gets a ``308``, an unknown path gets the export's ``404.html`` with status ``404``,
and only ``GET`` and ``HEAD`` are answered.

Two differences from the reference are deliberate, and both are about trust:

* The export is read **once, at load**. Every file is held in memory with its headers, and each
  HTML page's ``Content-Security-Policy`` is computed from the very bytes that will be sent
  (``csp.scan_page``), so the hashes can never go stale, are not recomputed per request, and a file
  rewritten after the load cannot make the policy and the page disagree.
* A request path is only ever **looked up** in the index made at load; it is never turned into a
  file system path. Traversal (encoded or not) therefore has nothing to reach, and a symlink that
  leaves the export is skipped at load (and logged) instead of followed. Paths that are ambiguous
  between this server and a browser or proxy (``..``, ``%2f``, a backslash, a NUL, an empty
  segment) are refused with ``400`` rather than guessed at (FR-017).

A page the policy cannot serve (an inline ``style`` attribute, an event handler, a ``<style>``
element, text that is not UTF-8) makes ``load_ui_export`` raise ``UiExportError`` naming the files:
failing at start is better than a UI that is blank or half dead in the browser. What the process
does with that error is the caller's decision.

Mounting (the admin API keeps its own prefix, so it is listed first)::

    ui = create_ui_app(load_ui_export(settings.ui_dir))
    routes = [Mount(ADMIN_API_MOUNT, app=admin_app), *ui_routes(ui)]
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final
from urllib.parse import quote

from starlette.responses import Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from appmux.edge.csp import BASE_CSP, build_csp, scan_page, security_headers

logger = logging.getLogger(__name__)

UI_PREFIX: Final = "/_appmux"
IMMUTABLE_PREFIX: Final = "_next/static/"
ALLOWED_METHODS: Final = ("GET", "HEAD")
# The whole export is held in memory; a directory far over this is not an admin UI export.
MAX_EXPORT_BYTES: Final = 64 * 1024 * 1024

NO_STORE: Final = "no-store"
# Files under _next/static carry a content hash in their name, so they never change.
IMMUTABLE_CACHE: Final = "public, max-age=31536000, immutable"

TEXT_UTF8: Final = "text/plain; charset=utf-8"
UI_UNAVAILABLE_TEXT: Final = "the appmux admin UI is not available; see the appmux log"
HTML_UTF8: Final = "text/html; charset=utf-8"
CONTENT_TYPES: Final = {
    ".html": HTML_UTF8,
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    # The RSC prefetch files (__next._tree.txt and friends) are text.
    ".txt": TEXT_UTF8,
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}
FALLBACK_CONTENT_TYPE: Final = "application/octet-stream"

# A path that a browser, a proxy and this server could each read differently.
_ENCODED_AMBIGUITY: Final = re.compile(rb"%(?:2f|5c|00)", re.IGNORECASE)  # "/", "\" and NUL
_DOUBLE_ENCODED: Final = re.compile(r"%[0-9a-fA-F]{2}")
# What a query string may hold to be copied into a Location header unchanged.
_SAFE_QUERY: Final = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@/?%]*")


class UiExportError(RuntimeError):
    """The export cannot be served as it is. The message names the cause and the files."""


@dataclass(frozen=True, slots=True)
class UiDocument:
    """One file of the export, as it is sent: its bytes and every response header but the length."""

    body: bytes
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class UiExport:
    """The index made at load.

    ``documents``: files by their path below the export root (``app/index.html``).
    ``directories``: every directory that holds a served file, by path (``app``; the root is
    ``""``), so ``/_appmux/app`` can be told from a file and from nothing.
    """

    documents: Mapping[str, UiDocument]
    directories: frozenset[str]


# --- loading -------------------------------------------------------------------------------------


def _document(relative: str, body: bytes, csp: str) -> UiDocument:
    suffix = PurePosixPath(relative).suffix.lower()
    cache_control = IMMUTABLE_CACHE if relative.startswith(IMMUTABLE_PREFIX) else NO_STORE
    return UiDocument(
        body=body,
        headers={
            "content-type": CONTENT_TYPES.get(suffix, FALLBACK_CONTENT_TYPE),
            "cache-control": cache_control,
            **security_headers(csp),
        },
    )


def _read_export_files(root: Path) -> dict[str, bytes]:
    """Every regular file below ``root`` by relative path; links that leave it are skipped."""
    files: dict[str, bytes] = {}
    total_bytes = 0
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        here = Path(directory)
        for name in directory_names:
            if (here / name).is_symlink():
                # os.walk lists a link to a directory but does not enter it; say so, once.
                logger.warning(
                    "ui export: skipped %s: a link to a directory is not followed",
                    (here / name).relative_to(root).as_posix(),
                )
        for name in file_names:
            path = here / name
            relative = path.relative_to(root).as_posix()
            source = path.resolve()
            if not source.is_relative_to(root):
                logger.warning("ui export: skipped %s: the link leaves the export", relative)
                continue
            if not source.is_file():
                continue  # a socket, a FIFO or a dangling link: nothing to serve
            budget = MAX_EXPORT_BYTES - total_bytes
            try:
                with source.open("rb") as handle:
                    body = handle.read(budget + 1)
            except OSError as exc:
                raise UiExportError(
                    f"cannot read {relative} in the UI export at {root}: {type(exc).__name__}"
                ) from exc
            if len(body) > budget:
                raise UiExportError(
                    f"the UI export at {root} is over {MAX_EXPORT_BYTES} bytes; point "
                    "APPMUX_UI_DIR at the directory `npm run build` wrote (web/out)"
                )
            total_bytes += len(body)
            files[relative] = body
    return files


def load_ui_export(directory: Path) -> UiExport:
    """Read the export at ``directory`` and compute every HTML page's policy.

    Raises ``UiExportError`` for a missing directory, one with no ``index.html``, or HTML the policy
    cannot serve (the error names each file and the kind of problem, never the content).
    """
    root = directory.resolve()
    if not root.is_dir():
        raise UiExportError(
            f"the UI export directory {directory} does not exist; build it with `npm run build` "
            "in web/ (it writes web/out) or set APPMUX_UI_DIR to the directory that holds it"
        )
    files = _read_export_files(root)

    documents: dict[str, UiDocument] = {}
    refused: list[str] = []
    for relative, body in files.items():
        if not relative.endswith(".html"):
            documents[relative] = _document(relative, body, BASE_CSP)
            continue
        try:
            scan = scan_page(body)
        except UnicodeDecodeError:
            refused.append(f"{relative}: not UTF-8, so no script hash could be trusted")
            continue
        refused.extend(f"{relative}: {problem}" for problem in scan.problems)
        documents[relative] = _document(relative, body, build_csp(scan.script_sources))
    if refused:
        raise UiExportError(
            f"the UI export at {root} holds markup the Content-Security-Policy cannot allow "
            "(style-src 'self' and a script-src of hashes); remove it from the UI source and "
            "rebuild:\n  " + "\n  ".join(refused)
        )
    if "index.html" not in documents:
        raise UiExportError(
            f"the UI export at {root} has no index.html; point APPMUX_UI_DIR at the directory "
            "`npm run build` wrote (web/out)"
        )

    directories = {""}
    for relative in documents:
        directories.update(str(parent) for parent in PurePosixPath(relative).parents)
    directories.discard(".")
    logger.info("ui export: %d files from %s", len(documents), root)
    return UiExport(documents=documents, directories=frozenset(directories))


# --- serving -------------------------------------------------------------------------------------


def _plain(status: int, text: str, extra: Mapping[str, str] | None = None) -> Response:
    headers = {
        "content-type": TEXT_UTF8,
        "cache-control": NO_STORE,
        **security_headers(BASE_CSP),
        **(extra or {}),
    }
    return Response(text, status_code=status, headers=headers)


def _is_ambiguous(path: str, raw_path: bytes, segments: list[str]) -> bool:
    return (
        "\x00" in path
        or "\\" in path
        or _DOUBLE_ENCODED.search(path) is not None
        or _ENCODED_AMBIGUITY.search(raw_path) is not None
        # The last segment may be empty (a trailing slash); no other may be.
        or "" in segments[:-1]
        or any(segment in (".", "..") for segment in segments)
    )


def _redirect(path_below_prefix: str, query: bytes) -> Response:
    """A 308 to the slash form of ``path_below_prefix`` (``""`` or ``/app``, no trailing slash).

    The Location is built from the validated path, re-quoted, and the query is copied only when it
    holds nothing that could end the header line or start another.
    """
    location = quote(f"{UI_PREFIX}{path_below_prefix}/", safe="/")
    try:
        query_text = query.decode("ascii")
    except UnicodeDecodeError:
        query_text = ""
    if query_text and _SAFE_QUERY.fullmatch(query_text):
        location += "?" + query_text
    headers = {"location": location, "cache-control": NO_STORE, **security_headers(BASE_CSP)}
    return Response(status_code=308, headers=headers)


def _as_response(document: UiDocument, status: int, *, head_only: bool) -> Response:
    # A HEAD answer states the length of the body it leaves out.
    headers = {**document.headers, "content-length": str(len(document.body))}
    return Response(b"" if head_only else document.body, status_code=status, headers=headers)


class _UiApp:
    def __init__(self, export: UiExport) -> None:
        self._export = export

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        await self._answer(scope)(scope, receive, send)

    def _answer(self, scope: Scope) -> Response:
        path: str = scope["path"]
        if path != "/" and path != UI_PREFIX and not path.startswith(UI_PREFIX + "/"):
            return _plain(404, "not found")  # a child's path: not the UI's business

        method: str = scope["method"]
        if method not in ALLOWED_METHODS:
            return _plain(405, "method not allowed", {"allow": ", ".join(ALLOWED_METHODS)})
        if path == UI_PREFIX:
            return _redirect("", scope["query_string"])

        below_prefix = "/" if path == "/" else path.removeprefix(UI_PREFIX)
        raw_path: bytes = scope.get("raw_path") or path.encode()
        segments = below_prefix.split("/")[1:]
        if _is_ambiguous(path, raw_path.partition(b"?")[0], segments):
            return _plain(400, "bad request")

        documents = self._export.documents
        directories = self._export.directories
        relative = below_prefix.removeprefix("/")  # "", "app/", "app" or "main.js"
        name = relative.removesuffix("/")
        if relative == "" or relative.endswith("/"):
            index_path = f"{name}/index.html" if name else "index.html"
            if name in directories and index_path in documents:
                return _as_response(documents[index_path], 200, head_only=method == "HEAD")
        elif name in documents:
            return _as_response(documents[name], 200, head_only=method == "HEAD")
        elif name in directories:
            return _redirect(below_prefix, scope["query_string"])

        not_found = documents.get("404.html")
        if not_found is None:
            return _plain(404, "not found")
        return _as_response(not_found, 404, head_only=method == "HEAD")


def create_ui_app(export: UiExport) -> ASGIApp:
    """The ASGI application that serves ``export``. It answers any path it is given."""
    return _UiApp(export)


class _UnavailableUiApp:
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        await _plain(503, UI_UNAVAILABLE_TEXT)(scope, receive, send)


def unavailable_ui_app() -> ASGIApp:
    """The application that stands in for the UI when its export cannot be served.

    Every request answers ``503`` with a fixed sentence. The cause (``UiExportError`` names files
    and paths) goes to the log, never to a visitor. Only the UI is lost: the admin API and the
    proxy do not depend on it, so a broken export must not stop the process or the apps it hosts.
    """
    return _UnavailableUiApp()


def ui_routes(ui_app: ASGIApp) -> list[BaseRoute]:
    """The routes that hand ``/``, ``/_appmux`` and everything below ``/_appmux/`` to ``ui_app``.

    ``Mount`` does not match ``/_appmux`` without its slash, so that path has its own route. The
    application must keep ``redirect_slashes`` off (as ``edge/app.py`` does): the app answers
    its own redirects. List the admin API's mount before these.
    """
    return [Route("/", ui_app), Route(UI_PREFIX, ui_app), Mount(UI_PREFIX, app=ui_app)]
