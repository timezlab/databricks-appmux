"""The judging half of the on-container UI check (T177), run without a container.

``tests/integration/test_ui_on_container.py`` drives a deployed appmux and a real browser, so it is
skipped here (``tests/conftest.py`` skips everything under ``tests/integration``). Its decisions are
plain functions of the page, the policy and the browser's report; those are checked here, against
the real policy builder and the real export loader, so that the on-container run cannot pass on a
check that judges nothing. The file is loaded by path because the test directories are not packages.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from appmux.edge.csp import build_csp, scan_page, script_source
from appmux.edge.ui_static import load_ui_export

SCRIPT = Path(__file__).resolve().parents[1] / "integration" / "test_ui_on_container.py"
MODULE_NAME = "ui_on_container_script"
ORIGIN = "http://127.0.0.1:8000"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[MODULE_NAME]
        raise
    return module


ui = _load_script()

INLINE = "self.__next_f.push([1,'x'])"
PAGE = f"""<!DOCTYPE html><html><head>
<link rel="stylesheet" href="/_appmux/_next/static/app.css">
<script src="/_appmux/_next/static/app.js" async></script>
</head><body><h1>Apps</h1><a href="https://docs.example.com/">docs</a>
<script>{INLINE}</script></body></html>"""


# --- the policy ---------------------------------------------------------------------------------


def test_the_policy_the_edge_builds_for_a_page_is_accepted() -> None:
    sources = scan_page(PAGE.encode()).script_sources

    assert ui.policy_problems(build_csp(sources), sources) == []


def test_an_inline_script_that_is_not_in_the_hash_list_is_named() -> None:
    other = script_source("alert(1)")

    problems = ui.policy_problems(build_csp([other]), [script_source(INLINE)])

    assert len(problems) == 1 and "is not in script-src" in problems[0]


@pytest.mark.parametrize(
    "script_src",
    [
        "'self' 'unsafe-inline'",
        "'self' 'unsafe-eval'",
        "'self' https://cdn.example.com",
        "'self' *",
        "'self' https:",
        "'self' data:",
        "'self' example.com",
    ],
)
def test_a_script_source_that_is_not_self_or_a_hash_is_refused(script_src: str) -> None:
    csp = build_csp().replace("script-src 'self'", f"script-src {script_src}")

    problems = ui.policy_problems(csp, [])

    assert problems and any("script-src allows" in problem for problem in problems)


def test_a_missing_script_src_or_default_src_or_frame_ancestors_is_refused() -> None:
    assert "there is no script-src" in ui.policy_problems("default-src 'none'", [])
    assert "default-src is not 'none'" in ui.policy_problems(
        "default-src 'self'; script-src 'self'; frame-ancestors 'none'", []
    )
    assert "frame-ancestors is not 'none'" in ui.policy_problems(
        "default-src 'none'; script-src 'self'", []
    )


def test_a_wildcard_or_unsafe_word_in_another_directive_is_refused() -> None:
    csp = build_csp().replace("connect-src 'self'", "connect-src 'self' *")

    assert ui.policy_problems(csp, []) == ["connect-src allows *"]


def test_a_real_export_page_passes_the_check_the_container_run_makes(tmp_path: Path) -> None:
    """The edge's own loader computes the policy; the check agrees with it page by page."""
    (tmp_path / "index.html").write_text(PAGE, encoding="utf-8")
    export = load_ui_export(tmp_path)

    page = export.documents["index.html"]

    policy = page.headers["Content-Security-Policy"]
    assert ui.policy_problems(policy, scan_page(page.body).script_sources) == []


# --- what a page fetches -----------------------------------------------------------------------


def test_the_urls_a_page_fetches_are_absolute_and_a_plain_link_is_not_one() -> None:
    urls = ui.fetched_urls(PAGE, f"{ORIGIN}/_appmux/")

    assert urls == [
        f"{ORIGIN}/_appmux/_next/static/app.css",
        f"{ORIGIN}/_appmux/_next/static/app.js",
    ]


def test_another_origin_is_found_in_a_script_a_style_sheet_an_image_and_a_form() -> None:
    html = (
        '<link rel="stylesheet" href="https://cdn.example.com/a.css">'
        '<script src="//evil.example/x.js"></script>'
        '<img src="http://other.example/p.png">'
        '<form action="https://elsewhere.example/post"></form>'
        '<link rel="canonical" href="https://elsewhere.example/page">'
        '<img src="data:image/png;base64,AAAA">'
        '<script src="/_appmux/own.js"></script>'
    )

    urls = ui.fetched_urls(html, f"{ORIGIN}/_appmux/")

    assert ui.other_origins(urls, ORIGIN) == [
        "https://cdn.example.com/a.css",
        "http://evil.example/x.js",
        "http://other.example/p.png",
        "https://elsewhere.example/post",
    ]


def test_a_port_or_a_scheme_makes_an_origin_different() -> None:
    urls = [f"{ORIGIN}/a", "http://127.0.0.1:8001/a", "https://127.0.0.1:8000/a", "blob:x/y"]

    assert ui.other_origins(urls, ORIGIN) == [urls[1], urls[2]]


# --- polling ------------------------------------------------------------------------------------

API = "/_appmux/api/v1/apps"


def _page(*calls: tuple[int, str, str], watched_seconds: int = 21) -> dict[str, Any]:
    return {
        "name": "launcher",
        "apiPrefix": API,
        "watchedSeconds": watched_seconds,
        "requests": [
            {"atMs": at, "method": method, "url": f"{ORIGIN}{path}"} for at, method, path in calls
        ],
    }


def test_calls_five_seconds_apart_are_polling_every_few_seconds() -> None:
    page = _page((100, "GET", "/_appmux/"), *((t, "GET", API) for t in (400, 5400, 10400, 15400)))

    assert ui.poll_gaps_ms(page) == [5000, 5000, 5000]
    assert ui.poll_problems(page) == []


def test_a_page_that_asked_once_is_not_polling() -> None:
    page = _page((400, "GET", API))

    problems = ui.poll_problems(page)

    assert len(problems) == 1 and "1 call(s)" in problems[0]


def test_a_burst_and_a_long_silence_are_both_refused() -> None:
    burst = _page((400, "GET", API), (450, "GET", API), (5450, "GET", API))
    silence = _page((400, "GET", API), (5400, "GET", API), (30_400, "GET", API))

    burst_problems = ui.poll_problems(burst)
    silence_problems = ui.poll_problems(silence)

    assert len(burst_problems) == 1 and " 50 ms apart" in burst_problems[0]
    assert len(silence_problems) == 1 and " 25000 ms apart" in silence_problems[0]


def test_only_get_calls_to_the_pages_own_path_count_and_the_query_does_not_matter() -> None:
    page = _page(
        (400, "GET", API),
        (900, "POST", API),
        (1000, "GET", "/_appmux/api/v1/platform"),
        (5400, "GET", f"{API}?x=1"),
        (10400, "GET", API),
    )

    assert ui.poll_gaps_ms(page) == [5000, 5000]


# --- which pages are checked --------------------------------------------------------------------


def test_the_pages_of_an_export_are_found_and_the_app_pages_get_a_slug(tmp_path: Path) -> None:
    for directory in ("", "app", "app/releases", "platform", "dashboard", "404", "_not-found"):
        folder = tmp_path / directory
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    (tmp_path / "_next" / "static").mkdir(parents=True)
    (tmp_path / "_next" / "static" / "index.html").write_text("x", encoding="utf-8")

    pages = ui.pages_of_the_export(tmp_path, "ui-check-0a1b2c")

    assert {(page.name, page.path, page.api_prefix) for page in pages} == {
        ("launcher", "/_appmux/", f"{ui.API_PREFIX}/apps"),
        ("platform", "/_appmux/platform/", f"{ui.API_PREFIX}/platform"),
        ("dashboard", "/_appmux/dashboard/", f"{ui.API_PREFIX}/metrics"),
        ("app", "/_appmux/app/?slug=ui-check-0a1b2c", None),
        ("app/releases", "/_appmux/app/releases/?slug=ui-check-0a1b2c", None),
    }


def test_the_polling_pages_the_check_expects_exist_in_the_built_export() -> None:
    """The three pages the check watches are real routes of the UI (read from its source)."""
    app_dir = Path(__file__).resolve().parents[2] / "web" / "src" / "app"

    for directory in ui.POLLING_PAGES:
        page = app_dir / directory / "page.tsx" if directory else app_dir / "page.tsx"
        assert page.is_file(), f"web/src/app has no page for {directory or 'the launcher'}"
