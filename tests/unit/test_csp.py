"""The admin UI's Content-Security-Policy (T165; FR-021, design 7.3, edge-http.md, spike 19).

The policy is ``script-src 'self'`` plus the SHA-256 of each inline script of the page being sent,
never ``'unsafe-inline'`` or ``'unsafe-eval'``. Two layers are pinned:

* ``edge/csp.py``: which scripts a page has and what their hashes are (taken from the exact text, as
  the browser takes them), what in a page the policy cannot allow, and the header text;
* what the server sends for the pages of a fake export (``fake_ui_export``): each page carries the
  hashes of its own scripts, and every response carries ``frame-ancestors 'none'`` and
  ``Cross-Origin-Opener-Policy: same-origin`` and no CORS header.

The expected hashes are computed here with ``hashlib`` from the script texts the fixture wrote, so
the tests do not use the code they check as their own oracle.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Final

import pytest
from fake_ui_export import (
    APP_FLIGHT,
    DASHBOARD_FLIGHT,
    LAUNCHER_FLIGHT,
    NOT_FOUND_FLIGHT,
    FakeExport,
    Reply,
    asgi_call,
    build_export,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette.types import ASGIApp

from appmux.edge.csp import (
    UiSecurityHeadersMiddleware,
    build_csp,
    scan_page,
    script_source,
)
from appmux.edge.ui_static import create_ui_app, load_ui_export

pytestmark = pytest.mark.anyio

FIXED_DIRECTIVES: Final = (
    "default-src 'none'; style-src 'self'; connect-src 'self'; img-src 'self'; "
    "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def expected_source(text: str) -> str:
    """The CSP source for one script text: ``'sha256-<base64 of the digest of its UTF-8>'``."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def expected_policy(script_texts: Iterable[str]) -> str:
    """The whole header value for a page whose inline scripts are ``script_texts``."""
    sources = list(dict.fromkeys(expected_source(text) for text in script_texts))
    return f"{FIXED_DIRECTIVES}; script-src 'self'" + "".join(f" {s}" for s in sources)


def page_with(*scripts: str) -> bytes:
    inline = "".join(f"<script>{text}</script>" for text in scripts)
    return (
        f"<!DOCTYPE html><html><head><title>t</title></head><body>{inline}</body></html>".encode()
    )


# --- which scripts a page has, and their hashes --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "run();",
        "\n  run();\n",  # the newlines and indentation between the tags are part of the text
        "run(); \t",  # trailing white space
        'self.__next_f.push([1,"1:\\"a\\"\\n"])',  # a backslash-n that is two characters
        "const word = 'café ☃';",  # non-ASCII text is hashed as UTF-8
        "if (a<b && c>d) { x = '</b>'; }",  # markup-like text is script text
        "",  # an empty inline script still executes nothing, and is listed all the same
    ],
)
def test_the_hash_is_taken_from_the_exact_text_between_the_tags(text: str) -> None:
    scan = scan_page(page_with(text))
    assert scan.script_sources == (expected_source(text),)
    assert scan.problems == ()


def test_white_space_changes_the_hash() -> None:
    plain, padded = scan_page(page_with("run();")), scan_page(page_with("run(); "))
    assert plain.script_sources != padded.script_sources


def test_script_source_has_the_form_of_a_csp_hash_source() -> None:
    assert script_source("run();") == expected_source("run();")


def test_every_inline_script_is_hashed_whatever_its_type_or_id() -> None:
    html = (
        b'<html><body><script id="_R_">boot();</script>'
        b'<script type="application/json">{"a": 1}</script>'
        b'<script type="module">import("x");</script></body></html>'
    )
    assert scan_page(html).script_sources == tuple(
        expected_source(text) for text in ("boot();", '{"a": 1}', 'import("x");')
    )


def test_a_script_with_a_src_is_not_hashed() -> None:
    html = b'<html><head><script src="/_appmux/a.js" async=""></script></head><body></body></html>'
    assert scan_page(html).script_sources == ()


def test_a_greater_than_sign_inside_an_attribute_value_does_not_end_the_tag_early() -> None:
    html = b"<body><script data-note=\"a>b\" data-other='c>d'>run();</script></body>"
    assert scan_page(html).script_sources == (expected_source("run();"),)


def test_script_end_tag_is_matched_without_regard_to_case() -> None:
    html = b"<body><SCRIPT>run();</SCRIPT></body>"
    assert scan_page(html).script_sources == (expected_source("run();"),)


def test_a_new_line_in_the_file_is_hashed_the_way_the_browser_reads_it() -> None:
    # The HTML parser turns CR LF and a lone CR into LF before a script's text is hashed (checked
    # in Chromium: a policy with the hash of the CR LF text blocks the script, the LF one runs it).
    crlf = scan_page(b"<body><script>a();\r\nb();\rc();</script></body>")
    assert crlf.script_sources == (expected_source("a();\nb();\nc();"),)


def test_the_same_script_twice_is_listed_once_in_document_order() -> None:
    scan = scan_page(page_with("one();", "two();", "one();"))
    assert scan.script_sources == (expected_source("one();"), expected_source("two();"))


def test_a_page_without_inline_scripts_has_none() -> None:
    assert scan_page(b"<html><body><p>hello</p></body></html>").script_sources == ()


def test_page_that_is_not_utf8_is_refused() -> None:
    # Another encoding would be decoded by the browser by another rule, so no hash could be trusted.
    with pytest.raises(UnicodeDecodeError):
        scan_page(b"<body><script>var s = '\xff';</script></body>")


# --- what the policy cannot allow ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "mentions"),
    [
        (b'<body><p style="color: red">x</p></body>', ("style", "<p>")),
        (b'<body><P STYLE="color: red">x</P></body>', ("style",)),
        (b'<body><svg><rect style="fill:red"/></svg></body>', ("style", "<rect>")),
        (b'<body><button onclick="go()">x</button></body>', ("onclick", "<button>")),
        (b'<body><img src="/a.png" onerror="go()"/></body>', ("onerror",)),
        (b'<body><BODY ONLOAD="go()"></BODY></body>', ("onload",)),
        (b"<head><style>p { color: red }</style></head>", ("<style>",)),
        (b'<body><script src="/a.js"/><p>after</p></body>', ("self-closing", "<script>")),
        (b"<body><script>run();", ("never closed", "<script>")),
    ],
)
def test_what_the_policy_cannot_allow_is_reported(html: bytes, mentions: tuple[str, ...]) -> None:
    problems = scan_page(html).problems
    assert len(problems) == 1
    for word in mentions:
        assert word in problems[0]


def test_a_problem_names_the_line_it_is_on() -> None:
    html = b'<html>\n<body>\n<p class="a"\n   style="color: red">x</p>\n</body></html>'
    assert scan_page(html).problems == ("line 3: <p> has an inline style attribute",)


def test_every_problem_of_a_page_is_reported() -> None:
    html = b'<p style="a"></p><button onclick="b()"></button><style></style>'
    assert len(scan_page(html).problems) == 3


@pytest.mark.parametrize(
    "html",
    [
        # Text that only looks like an attribute: the page is parsed, not searched.
        b'<body><script>const s = \'<p style="x" onclick="y()">\';</script></body>',
        b'<body><!-- <p style="x" onclick="y()"> --><p>fine</p></body>',
        b'<body><p class="style" data-note="onclick=1" data-style="x" title="on">x</p></body>',
        b'<body><a href="/_appmux/app/" aria-current="page" data-on="1">x</a></body>',
    ],
)
def test_text_that_only_looks_like_an_inline_style_or_handler_is_not_reported(
    html: bytes,
) -> None:
    assert scan_page(html).problems == ()


# --- the header text -----------------------------------------------------------------------------


def test_policy_without_inline_scripts_allows_only_the_own_origin() -> None:
    assert build_csp() == (
        "default-src 'none'; style-src 'self'; connect-src 'self'; img-src 'self'; "
        "font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
        "script-src 'self'"
    )


def test_policy_lists_the_sources_after_self() -> None:
    sources = [script_source("one();"), script_source("two();")]
    assert build_csp(sources) == build_csp() + " " + " ".join(sources)


# --- the middleware for responses the UI server does not make ------------------------------------


def _inner_app(extra_headers: dict[str, str] | None = None) -> Starlette:
    def answer(request: Request) -> Response:
        return JSONResponse({"ok": True}, headers=extra_headers)

    return Starlette(routes=[Route("/x", answer)])


def test_middleware_adds_the_security_headers_to_a_response_that_has_none() -> None:
    client = TestClient(UiSecurityHeadersMiddleware(_inner_app()))
    response = client.get("/x")
    assert response.headers["content-security-policy"] == build_csp()
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert not [name for name in response.headers if name.startswith("access-control-")]


def test_middleware_keeps_what_the_inner_application_already_set() -> None:
    inner = _inner_app(
        {"Content-Security-Policy": "default-src 'none'", "Cache-Control": "no-store"}
    )
    response = TestClient(UiSecurityHeadersMiddleware(inner)).get("/x")
    assert response.headers["content-security-policy"] == "default-src 'none'"
    assert response.headers["cache-control"] == "no-store"


# --- what the server sends for the pages of an export --------------------------------------------


@pytest.fixture
def export(tmp_path: Path) -> FakeExport:
    return build_export(tmp_path / "out")


@pytest.fixture
def ui_app(export: FakeExport) -> ASGIApp:
    return create_ui_app(load_ui_export(export.root))


PAGES: Final = [
    pytest.param("/", "index.html", id="root"),
    pytest.param("/_appmux/", "index.html", id="launcher"),
    pytest.param("/_appmux/app/", "app/index.html", id="app-page"),
    pytest.param("/_appmux/dashboard/", "dashboard/index.html", id="dashboard"),
]


@pytest.mark.parametrize(("url_path", "file"), PAGES)
async def test_a_page_carries_script_src_self_and_the_hash_of_each_of_its_inline_scripts(
    ui_app: ASGIApp, export: FakeExport, url_path: str, file: str
) -> None:
    reply = await asgi_call(ui_app, "GET", url_path)
    assert reply.status == 200
    assert reply.headers["content-security-policy"] == expected_policy(export.inline_scripts[file])


async def test_an_unknown_path_gets_the_not_found_page_with_the_hashes_of_that_page(
    ui_app: ASGIApp, export: FakeExport
) -> None:
    reply = await asgi_call(ui_app, "GET", "/_appmux/no-such-page/")
    assert reply.status == 404
    assert reply.headers["content-security-policy"] == expected_policy(
        export.inline_scripts["404.html"]
    )


async def test_a_page_lists_only_its_own_flight_script(ui_app: ASGIApp) -> None:
    csp = (await asgi_call(ui_app, "GET", "/_appmux/app/")).headers["content-security-policy"]
    assert expected_source(APP_FLIGHT) in csp
    for other in (LAUNCHER_FLIGHT, DASHBOARD_FLIGHT, NOT_FOUND_FLIGHT):
        assert expected_source(other) not in csp


async def test_a_file_that_is_not_a_page_gets_the_policy_without_hashes(
    ui_app: ASGIApp,
) -> None:
    reply = await asgi_call(ui_app, "GET", "/_appmux/_next/static/chunks/main-0a1b2c.js")
    assert reply.status == 200
    assert reply.headers["content-security-policy"] == build_csp()


REQUESTS: Final = [
    pytest.param("GET", "/_appmux/", id="page"),
    pytest.param("GET", "/", id="root"),
    pytest.param("HEAD", "/_appmux/", id="head"),
    pytest.param("GET", "/_appmux/_next/static/chunks/main-0a1b2c.js", id="script"),
    pytest.param("GET", "/_appmux/__next._tree.txt", id="prefetch"),
    pytest.param("GET", "/_appmux/missing.js", id="not-found"),
    pytest.param("GET", "/_appmux", id="redirect"),
    pytest.param("POST", "/_appmux/", id="method-refused"),
    pytest.param("GET", "/_appmux/../secret", id="bad-request"),
]


@pytest.mark.parametrize(("method", "path"), REQUESTS)
async def test_every_response_denies_framing_isolates_the_window_and_sends_no_cors_header(
    ui_app: ASGIApp, method: str, path: str
) -> None:
    reply = await asgi_call(ui_app, method, path, headers=[("Origin", "https://other.example.com")])
    directives = [part.strip() for part in reply.headers["content-security-policy"].split(";")]
    assert "frame-ancestors 'none'" in directives
    script_src = next(d for d in directives if d.startswith("script-src")).split()[1:]
    assert script_src[0] == "'self'"
    assert all(token.startswith("'sha256-") for token in script_src[1:])
    assert reply.headers["cross-origin-opener-policy"] == "same-origin"
    assert [name for name in reply.headers if name.startswith("access-control-")] == []


async def test_a_cors_preflight_is_refused_and_not_answered(ui_app: ASGIApp) -> None:
    reply: Reply = await asgi_call(
        ui_app,
        "OPTIONS",
        "/_appmux/",
        headers=[
            ("Origin", "https://other.example.com"),
            ("Access-Control-Request-Method", "GET"),
        ],
    )
    assert reply.status == 405
    assert [name for name in reply.headers if name.startswith("access-control-")] == []
