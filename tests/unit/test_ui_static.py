"""Serving the admin UI's static export (T170; design 7.3 and 10.1, contracts/edge-http.md).

The server is a port of the reference edge the browser test uses (``web/e2e/server.ts``): the
export is mounted under ``/_appmux/`` (and ``/`` is the launcher), a directory without its trailing
slash gets a ``308``, an unknown path gets ``404.html`` with status ``404``, the ``__next.*.txt``
prefetch files are served as text, and only ``GET`` and ``HEAD`` are answered. The policy headers
are pinned in ``test_csp.py``; here are the paths, the file system rules and the load-time checks.

Every test builds its own small export in ``tmp_path`` (``fake_ui_export``); none needs ``web/``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import pytest
from fake_ui_export import (
    BUILD_ID,
    LAUNCHER_FLIGHT,
    PAGE_MARKERS,
    FakeExport,
    asgi_call,
    build_export,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message

from appmux.edge import ui_static
from appmux.edge.csp import PageScan, scan_page, script_source
from appmux.edge.ui_static import UiExportError, create_ui_app, load_ui_export, ui_routes

pytestmark = pytest.mark.anyio

SECRET: Final = "TOP-SECRET-OUTSIDE-THE-EXPORT"  # noqa: S105 - a fake marker for a leak test
CHUNK: Final = "/_appmux/_next/static/chunks/main-0a1b2c.js"


@pytest.fixture
def export(tmp_path: Path) -> FakeExport:
    return build_export(tmp_path / "out")


@pytest.fixture
def ui_app(export: FakeExport) -> ASGIApp:
    return create_ui_app(load_ui_export(export.root))


# --- pages and redirects -------------------------------------------------------------------------


async def test_the_launcher_is_served_at_the_root_and_under_the_prefix(ui_app: ASGIApp) -> None:
    at_root = await asgi_call(ui_app, "GET", "/")
    under_prefix = await asgi_call(ui_app, "GET", "/_appmux/")
    assert at_root.status == under_prefix.status == 200
    assert PAGE_MARKERS["index.html"] in at_root.text
    assert at_root.body == under_prefix.body
    assert at_root.headers["content-type"] == "text/html; charset=utf-8"


async def test_a_page_is_the_index_of_its_directory(ui_app: ASGIApp) -> None:
    reply = await asgi_call(ui_app, "GET", "/_appmux/app/")
    assert reply.status == 200
    assert PAGE_MARKERS["app/index.html"] in reply.text


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("/_appmux", "/_appmux/"),
        ("/_appmux/app", "/_appmux/app/"),
        ("/_appmux/_next/static", "/_appmux/_next/static/"),
    ],
)
async def test_a_directory_without_its_slash_gets_a_308_to_the_slash_form(
    ui_app: ASGIApp, path: str, location: str
) -> None:
    reply = await asgi_call(ui_app, "GET", path)
    assert reply.status == 308
    assert reply.headers["location"] == location
    assert reply.body == b""


async def test_the_redirect_keeps_the_query_string(ui_app: ASGIApp) -> None:
    reply = await asgi_call(ui_app, "GET", "/_appmux/app", query=b"slug=sales&tab=releases")
    assert reply.headers["location"] == "/_appmux/app/?slug=sales&tab=releases"


async def test_the_redirect_does_not_echo_a_query_it_cannot_carry_safely(ui_app: ASGIApp) -> None:
    reply = await asgi_call(ui_app, "GET", "/_appmux/app", query=b"a=1\r\nSet-Cookie: x=y")
    assert reply.status == 308
    assert reply.headers["location"] == "/_appmux/app/"
    assert "set-cookie" not in reply.headers


async def test_a_directory_with_a_slash_but_no_index_page_is_not_found(ui_app: ASGIApp) -> None:
    assert (await asgi_call(ui_app, "GET", "/_appmux/bare/")).status == 404
    assert (await asgi_call(ui_app, "GET", "/_appmux/bare/data.json")).status == 200


async def test_a_file_asked_for_with_a_trailing_slash_is_not_found(ui_app: ASGIApp) -> None:
    assert (await asgi_call(ui_app, "GET", "/_appmux/index.txt/")).status == 404


@pytest.mark.parametrize(
    "path", ["/_appmux/missing/", "/_appmux/missing.js", "/_appmux/app/missing", CHUNK + ".map"]
)
async def test_an_unknown_path_gets_the_export_404_page_with_status_404(
    ui_app: ASGIApp, path: str
) -> None:
    reply = await asgi_call(ui_app, "GET", path)
    assert reply.status == 404
    assert PAGE_MARKERS["404.html"] in reply.text
    assert reply.headers["content-type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize("path", ["/_appmuxfoo", "/_APPMUX/", "/other", "/sales/x", "//"])
async def test_a_path_outside_the_prefix_is_not_found(ui_app: ASGIApp, path: str) -> None:
    reply = await asgi_call(ui_app, "GET", path)
    assert reply.status == 404
    assert PAGE_MARKERS["index.html"] not in reply.text


async def test_without_a_404_page_the_answer_is_a_short_text(
    export: FakeExport,
) -> None:
    (export.root / "404.html").unlink()
    reply = await asgi_call(create_ui_app(load_ui_export(export.root)), "GET", "/_appmux/missing")
    assert reply.status == 404
    assert reply.headers["content-type"].startswith("text/plain")
    assert reply.text == "not found"


# --- files ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/_appmux/__next._tree.txt",
        "/_appmux/__next.__PAGE__.txt",
        "/_appmux/app/__next._tree.txt",
        "/_appmux/app/__next.app.__PAGE__.txt",
    ],
)
async def test_the_prefetch_files_are_served_as_text(ui_app: ASGIApp, path: str) -> None:
    reply = await asgi_call(ui_app, "GET", path)
    assert reply.status == 200
    assert reply.headers["content-type"] == "text/plain; charset=utf-8"
    assert reply.body


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        (CHUNK, "text/javascript; charset=utf-8"),
        ("/_appmux/_next/static/chunks/main-0a1b2c.css", "text/css; charset=utf-8"),
        ("/_appmux/bare/data.json", "application/json; charset=utf-8"),
        ("/_appmux/icon.svg", "image/svg+xml"),
    ],
)
async def test_a_file_is_served_with_its_content_type_and_its_bytes(
    ui_app: ASGIApp, export: FakeExport, path: str, content_type: str
) -> None:
    reply = await asgi_call(ui_app, "GET", path)
    assert reply.status == 200
    assert reply.headers["content-type"] == content_type
    assert reply.body == (export.root / path.removeprefix("/_appmux/")).read_bytes()
    assert reply.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [CHUNK, f"/_appmux/_next/static/{BUILD_ID}/_buildManifest.js"],
)
async def test_the_files_under_next_static_are_cached_for_good(ui_app: ASGIApp, path: str) -> None:
    cache_control = (await asgi_call(ui_app, "GET", path)).headers["cache-control"]
    assert "immutable" in cache_control
    assert "max-age=31536000" in cache_control


@pytest.mark.parametrize(
    "path",
    [
        "/_appmux/",
        "/",
        "/_appmux/app/",
        "/_appmux/__next._tree.txt",
        "/_appmux/unhashed.js",  # not under _next/static: its name carries no hash
        "/_appmux/_next/static/chunks/gone.js",  # a 404 must not be cached as if it were a chunk
        "/_appmux/missing",
        "/_appmux/app",
    ],
)
async def test_nothing_else_is_cached(ui_app: ASGIApp, path: str) -> None:
    cache_control = (await asgi_call(ui_app, "GET", path)).headers["cache-control"]
    assert cache_control == "no-store"


# --- methods -------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"])
async def test_only_get_and_head_are_answered(ui_app: ASGIApp, method: str) -> None:
    reply = await asgi_call(ui_app, method, "/_appmux/")
    assert reply.status == 405
    assert reply.headers["allow"] == "GET, HEAD"
    assert PAGE_MARKERS["index.html"] not in reply.text


@pytest.mark.parametrize("path", ["/_appmux/", "/_appmux/app/", CHUNK, "/_appmux/missing"])
async def test_head_answers_the_headers_of_get_with_no_body(ui_app: ASGIApp, path: str) -> None:
    got = await asgi_call(ui_app, "GET", path)
    head = await asgi_call(ui_app, "HEAD", path)
    assert head.status == got.status
    assert head.body == b""
    for name in ("content-type", "content-length", "content-security-policy", "cache-control"):
        assert head.headers[name] == got.headers[name]


# --- paths that try to leave the export ----------------------------------------------------------


@pytest.fixture
def secret_beside_the_export(export: FakeExport) -> Path:
    secret = export.root.parent / "secret.txt"
    secret.write_text(SECRET, encoding="utf-8")
    return secret


AMBIGUOUS: Final = [
    pytest.param("/_appmux/../secret.txt", None, id="dot-dot"),
    pytest.param("/_appmux/app/../../secret.txt", None, id="dot-dot-twice"),
    pytest.param("/_appmux/./index.txt", None, id="dot"),
    pytest.param("/_appmux/../secret.txt", b"/_appmux/%2e%2e/secret.txt", id="encoded-dot-dot"),
    pytest.param("/_appmux/%2e%2e/secret.txt", None, id="double-encoded-dot-dot"),
    pytest.param("/_appmux/../secret.txt", b"/_appmux/..%2fsecret.txt", id="encoded-slash-dot-dot"),
    pytest.param("/_appmux/app/index.txt", b"/_appmux/app%2findex.txt", id="encoded-slash"),
    pytest.param("/_appmux/app/index.txt", b"/_appmux/app%5Cindex.txt", id="encoded-backslash"),
    pytest.param("/_appmux/app\\..\\..\\secret.txt", None, id="backslashes"),
    pytest.param("/_appmux/index.txt\x00.png", None, id="nul"),
    pytest.param("/_appmux/app/index.txt", b"/_appmux/app/index.txt%00", id="encoded-nul"),
    pytest.param("/_appmux//secret.txt", None, id="empty-segment"),
    pytest.param("/_appmux//", None, id="empty-segments"),
]


@pytest.mark.parametrize(("path", "raw_path"), AMBIGUOUS)
async def test_an_ambiguous_path_is_a_400_and_reads_nothing_outside_the_export(
    ui_app: ASGIApp, secret_beside_the_export: Path, path: str, raw_path: bytes | None
) -> None:
    reply = await asgi_call(ui_app, "GET", path, raw_path=raw_path)
    assert reply.status == 400
    assert SECRET not in reply.text
    assert path.strip("/") not in reply.text  # the answer does not echo the path


async def test_a_symlink_that_leaves_the_export_is_not_served(
    tmp_path: Path, export: FakeExport, caplog: pytest.LogCaptureFixture
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    (outside / "index.html").write_text(f"<html><body>{SECRET}</body></html>", encoding="utf-8")
    (export.root / "leak.txt").symlink_to(outside / "secret.txt")
    (export.root / "leak.html").symlink_to(outside / "index.html")
    (export.root / "leakdir").symlink_to(outside, target_is_directory=True)

    with caplog.at_level(logging.WARNING, logger="appmux.edge.ui_static"):
        app = create_ui_app(load_ui_export(export.root))

    for path in (
        "/_appmux/leak.txt",
        "/_appmux/leak.html",
        "/_appmux/leakdir/",
        "/_appmux/leakdir",
        "/_appmux/leakdir/secret.txt",
        "/_appmux/leakdir/index.html",
    ):
        reply = await asgi_call(app, "GET", path)
        assert reply.status == 404, path
        assert SECRET not in reply.text, path
    named = " ".join(record.getMessage() for record in caplog.records)
    for link in ("leak.txt", "leak.html", "leakdir"):
        assert link in named
    assert SECRET not in named


async def test_a_symlink_to_a_file_inside_the_export_is_served(export: FakeExport) -> None:
    (export.root / "alias.txt").symlink_to(export.root / "index.txt")
    reply = await asgi_call(create_ui_app(load_ui_export(export.root)), "GET", "/_appmux/alias.txt")
    assert reply.status == 200
    assert reply.body == (export.root / "index.txt").read_bytes()


# --- hashes are made once, from what is sent -----------------------------------------------------


async def test_hashes_are_computed_at_load_and_not_again_per_request(
    export: FakeExport, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanned: list[int] = []

    def counting_scan(html: bytes) -> PageScan:
        scanned.append(len(html))
        return scan_page(html)

    monkeypatch.setattr(ui_static, "scan_page", counting_scan)

    app = create_ui_app(load_ui_export(export.root))
    assert len(scanned) == len(export.inline_scripts)  # one scan per HTML file, at load
    for _ in range(3):
        for path in ("/", "/_appmux/app/", "/_appmux/missing", CHUNK):
            await asgi_call(app, "GET", path)
    assert len(scanned) == len(export.inline_scripts)


async def test_the_page_sent_is_the_page_that_was_hashed_even_if_the_file_changes_later(
    export: FakeExport,
) -> None:
    app = create_ui_app(load_ui_export(export.root))
    (export.root / "index.html").write_text(
        "<html><body><script>stolen();</script></body></html>", encoding="utf-8"
    )
    reply = await asgi_call(app, "GET", "/_appmux/")
    assert PAGE_MARKERS["index.html"] in reply.text
    assert "stolen" not in reply.text
    assert script_source(LAUNCHER_FLIGHT) in reply.headers["content-security-policy"]


# --- an export the policy cannot serve is refused at load ----------------------------------------


def _write_page(export: FakeExport, name: str, body: str) -> None:
    (export.root / name).write_text(
        f"<!DOCTYPE html><html><body>{body}</body></html>", encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("body", "mentions"),
    [
        ('<p style="color: red">x</p>', "inline style attribute"),
        ('<button onclick="go()">x</button>', "onclick"),
        ("<style>p { color: red }</style>", "<style>"),
    ],
)
def test_a_page_with_an_inline_style_or_handler_is_refused_and_the_file_is_named(
    export: FakeExport, body: str, mentions: str
) -> None:
    _write_page(export, "app/index.html", body)
    with pytest.raises(UiExportError) as raised:
        load_ui_export(export.root)
    message = str(raised.value)
    assert "app/index.html" in message
    assert mentions in message
    assert "color: red" not in message and "go()" not in message  # names the kind, not the content


def test_every_offending_file_is_named_in_the_one_error(export: FakeExport) -> None:
    _write_page(export, "app/index.html", '<p style="a">x</p>')
    _write_page(export, "dashboard/index.html", '<i onclick="b()">x</i>')
    with pytest.raises(UiExportError) as raised:
        load_ui_export(export.root)
    assert "app/index.html" in str(raised.value)
    assert "dashboard/index.html" in str(raised.value)


def test_a_page_that_is_not_utf8_is_refused_and_the_file_is_named(export: FakeExport) -> None:
    (export.root / "app/index.html").write_bytes(b"<html><body>\xff</body></html>")
    with pytest.raises(UiExportError, match=r"app/index\.html"):
        load_ui_export(export.root)


def test_a_missing_directory_is_refused_with_the_path_and_the_way_out(tmp_path: Path) -> None:
    with pytest.raises(UiExportError) as raised:
        load_ui_export(tmp_path / "web" / "out")
    assert str(Path("web") / "out") in str(raised.value)
    assert "APPMUX_UI_DIR" in str(raised.value)


def test_a_directory_with_no_index_page_is_refused(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "other.txt").write_text("x", encoding="utf-8")
    with pytest.raises(UiExportError, match=r"index\.html"):
        load_ui_export(tmp_path / "out")


def test_a_directory_over_the_size_bound_is_refused_not_loaded_into_memory(
    export: FakeExport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_static, "MAX_EXPORT_BYTES", 100)
    with pytest.raises(UiExportError, match="APPMUX_UI_DIR"):
        load_ui_export(export.root)


async def test_a_relative_directory_is_read_from_the_working_directory(
    export: FakeExport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(export.root.parent)
    app = create_ui_app(load_ui_export(Path("out")))
    assert (await asgi_call(app, "GET", "/_appmux/")).status == 200


# --- the ASGI contract ---------------------------------------------------------------------------


async def test_a_websocket_is_refused_before_it_is_accepted(ui_app: ASGIApp) -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "websocket", "path": "/_appmux/", "headers": [], "query_string": b""}
    await ui_app(scope, receive, send)
    assert [message["type"] for message in sent] == ["websocket.close"]


# --- mounting beside the admin API ---------------------------------------------------------------


def _top_level_app(ui: ASGIApp) -> Starlette:
    def whoami(request: Request) -> Response:
        return JSONResponse({"from": "api"})

    api = Starlette(routes=[Route("/me", whoami)])
    application = Starlette(routes=[Mount("/_appmux/api/v1", app=api), *ui_routes(ui)])
    application.router.redirect_slashes = False  # as edge/app.py does
    return application


def test_the_routes_mount_beside_the_admin_api_and_leave_other_paths_to_the_edge(
    export: FakeExport,
) -> None:
    client = TestClient(_top_level_app(create_ui_app(load_ui_export(export.root))))

    for page in ("/", "/_appmux/"):
        reply = client.get(page, follow_redirects=False)
        assert reply.status_code == 200
        assert PAGE_MARKERS["index.html"] in reply.text
    assert client.get("/_appmux/api/v1/me").json() == {"from": "api"}  # the API wins its prefix
    redirect = client.get("/_appmux", follow_redirects=False)
    assert (redirect.status_code, redirect.headers["location"]) == (308, "/_appmux/")
    redirect = client.get("/_appmux/app", follow_redirects=False)
    assert (redirect.status_code, redirect.headers["location"]) == (308, "/_appmux/app/")
    assert client.get("/_appmux/__next._tree.txt").status_code == 200
    assert client.post("/_appmux/", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 405
    other = client.get("/sales/x")  # a child's path: not the UI's business
    assert other.status_code == 404
    assert PAGE_MARKERS["404.html"] not in other.text
