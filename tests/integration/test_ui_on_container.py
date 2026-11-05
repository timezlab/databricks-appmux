"""The admin UI on a container: policy, origin and polling (T177, FR-021, FR-051, spec US11 AC 4).

Deploy appmux and run this against it. For every page of the built export (``web/out``) the test
checks, from the outside, what the spec asks of the UI:

* **no inline script outside the hash list**: the page's ``Content-Security-Policy`` allows
  ``'self'`` and the SHA-256 of each inline script the bytes sent actually contain, and nothing else
  (no ``'unsafe-inline'``, no ``'unsafe-eval'``, no wildcard, no scheme or host source);
* **no CSP violation**: a real Chromium loads each page and reports every
  ``securitypolicyviolation`` event, page error and console error (``ui_browser_check.mjs``, which
  drives the Playwright that ``web/`` already installs; nothing new is added to the Python side);
* **no cross-origin request**: nothing in the page's HTML names another origin to fetch, and the
  browser sent no request to one;
* **polling**: the launcher, the platform page and the dashboard ask their API again every few
  seconds (design 10.1: the page polls; TD-009), not once and not in a burst;
* the isolation headers of the policy: ``Cross-Origin-Opener-Policy`` on every page, ``nosniff`` and
  no CORS header on the pages and on an API answer; an unknown path under the UI prefix is the 404
  page with a policy.

It runs in one of the two modes of ``ops_session.py``. The one that works is **remote**: from this
machine, through the real Databricks proxy, as the owner of the Databricks CLI's token
(``scripts/run-remote-tests.sh tests/integration/test_ui_on_container.py -s``; that script sets the
variables below). The browser is on this machine, so the Chromium and the built export are too. The
other mode plays the proxy with ``X-Forwarded-Email`` on the container's own port; a container has
no Chromium, no ``node`` and no ``web/out``, so only the checks that need no browser can run there.
Every test here is marked ``integration`` and skipped unless ``APPMUX_TEST_CONTAINER=1``. What can
be checked without a deployed App (the judging functions below) is checked by
``tests/unit/test_ui_script.py``.

Result: 7 passed in 175 s on 2026-11-05 (Free Edition dev App, Chromium on the dev machine; see the
spike log). Chromium cancels the requests of a page in flight with ``net::ERR_NETWORK_CHANGED``
whenever the machine's network interfaces change. A page cut that way is tried again up to five
times; if every try is cut, the test **fails** and says the page was never seen whole, because it is
neither a pass nor a fault of the App. On a machine whose interfaces change all day (a Docker host
with a restarting container) the run needs the network to be steady first; the passing run was made
with a local shim that stops Chromium watching the interfaces, which is not part of the repository.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``, ``APPMUX_TEST_USER_TOKEN``, ``APPMUX_TEST_FIXTURES_FOLDER`` and, in a
  remote run, ``APPMUX_TEST_REMOTE=1``, as ``ops_session.load_target`` describes them; the token's
  owner must be a platform admin. An app is registered from ``light_fastapi`` in the fixtures
  folder so that the app pages have an app to show; it is never started and is deleted afterwards;
* ``web/out`` built (``npm run build`` in ``web/``; set ``APPMUX_TEST_UI_EXPORT`` for another
  folder) and ``npm ci`` run in ``web/``, with Playwright's Chromium installed
  (``npx playwright install chromium``), and ``node`` on the path. The export is only the list of
  pages to open: what is judged is what the deployed App sends for each of them.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

import httpx
import pytest

from appmux.edge.csp import scan_page

# ops_session.py sits next to this file; pytest puts this folder on sys.path when it collects the
# tests here, but the unit test of the judging functions loads this file by path and does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ops_session

UI_PREFIX: Final = "/_appmux"
API_PREFIX: Final = f"{UI_PREFIX}/api/v1"
REQUEST_TIMEOUT_S: Final = 30.0
BROWSER_RUN_TIMEOUT_S: Final = 900.0
SETTLE_SECONDS: Final = 3
WATCH_SECONDS: Final = 21
SLUG_SUFFIX_BYTES: Final = 3
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
WEB_DIR: Final = REPO_ROOT / "web"
BROWSER_SCRIPT: Final = Path(__file__).resolve().with_name("ui_browser_check.mjs")

# "Every few seconds" (design 10.1): the page's own default is 5 s and its floor is 1 s. A gap
# below the floor is a burst; a gap over the ceiling is a page that stopped polling.
POLL_GAP_FLOOR_MS: Final = 1000
POLL_GAP_CEILING_MS: Final = 15_000
POLLS_NEEDED: Final = 3
# One refresh can be several calls started in the same tick (the dashboard reads its window and the
# one before it with Promise.all, a few ms apart). Calls this close to the first call of a refresh
# are that refresh, not extra polls; the window is measured from the first call so that a loop
# asking every few tens of ms is still a burst.
SAME_REFRESH_WINDOW_MS: Final = 100

# The pages that poll, by the directory of the export and the API path they ask for.
POLLING_PAGES: Final = {
    "": f"{API_PREFIX}/apps",
    "platform": f"{API_PREFIX}/platform",
    "dashboard": f"{API_PREFIX}/metrics",
}
# Directories of the export that are not pages to open (the 404 page is checked on its own).
NOT_PAGES: Final = frozenset({"404", "_not-found", "_next"})
# These pages read the app from the query string (a static export cannot have a segment for it).
APP_PAGE_DIRECTORY: Final = "app"

# A request for one of these never leaves the browser.
LOCAL_SCHEMES: Final = frozenset({"data", "blob", "about"})
_SHA256_SOURCE: Final = re.compile(r"^'sha256-[A-Za-z0-9+/]{43}='$")
_FORBIDDEN_SOURCE_WORDS: Final = ("'unsafe-inline'", "'unsafe-eval'", "'unsafe-hashes'")
_OTHER_SOURCE_DIRECTIVES: Final = frozenset({"base-uri", "form-action", "frame-ancestors"})


# --- judging: pure functions, checked without a container by tests/unit/test_ui_script.py -------


def policy_problems(csp: str, inline_script_sources: Sequence[str]) -> list[str]:
    """What is wrong with a page's policy, given the sources of the inline scripts it holds.

    Fine means: ``default-src 'none'``, ``frame-ancestors 'none'``, a ``script-src`` of ``'self'``
    plus SHA-256 sources only, every inline script's source in that list, and no directive with an
    unsafe keyword, a wildcard, a scheme or a host.
    """
    directives: dict[str, list[str]] = {}
    for part in csp.split(";"):
        words = part.split()
        if words:
            directives[words[0]] = words[1:]

    problems: list[str] = []
    if directives.get("default-src") != ["'none'"]:
        problems.append("default-src is not 'none'")
    if directives.get("frame-ancestors") != ["'none'"]:
        problems.append("frame-ancestors is not 'none'")
    scripts = directives.get("script-src")
    if scripts is None:
        problems.append("there is no script-src")
        scripts = []
    if "'self'" not in scripts:
        problems.append("script-src does not allow 'self'")
    problems.extend(
        f"script-src allows {word}, which is not 'self' or a SHA-256 hash"
        for word in scripts
        if word != "'self'" and not _SHA256_SOURCE.fullmatch(word)
    )
    for name, sources in directives.items():
        if name.endswith("-src") or name in _OTHER_SOURCE_DIRECTIVES:
            # A source that is not a quoted keyword or hash is a scheme, a host or a wildcard.
            problems.extend(
                f"{name} allows {word}"
                for word in sources
                if word in _FORBIDDEN_SOURCE_WORDS or not word.startswith("'")
            )
    problems.extend(
        f"an inline script of the page ({source[:20]}...) is not in script-src"
        for source in inline_script_sources
        if source not in scripts
    )
    return problems


# Attributes that make a browser fetch something, by tag. An <a href> is a link to click, not a
# request the page makes, so it is not here.
_FETCHING_ATTRIBUTES: Final = {
    "script": "src",
    "img": "src",
    "source": "src",
    "iframe": "src",
    "frame": "src",
    "embed": "src",
    "audio": "src",
    "video": "src",
    "track": "src",
    "input": "src",
    "object": "data",
    "form": "action",
}
_FETCHING_LINK_RELATIONS: Final = frozenset(
    {"stylesheet", "preload", "modulepreload", "prefetch", "preconnect", "dns-prefetch"}
    | {"icon", "shortcut", "apple-touch-icon", "manifest"}
)


class _FetchedUrls(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value for name, value in attrs if value is not None}
        if tag == "link":
            relations = set(values.get("rel", "").lower().split())
            if relations & _FETCHING_LINK_RELATIONS and "href" in values:
                self.urls.append(values["href"])
            return
        attribute = _FETCHING_ATTRIBUTES.get(tag)
        if attribute is not None and attribute in values:
            self.urls.append(values[attribute])


def fetched_urls(html: str, page_url: str) -> list[str]:
    """The absolute URLs a browser fetches for ``html`` when it is the page at ``page_url``."""
    parser = _FetchedUrls()
    parser.feed(html)
    parser.close()
    return [urljoin(page_url, url) for url in parser.urls]


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def other_origins(urls: Sequence[str], own_origin: str) -> list[str]:
    """The URLs that are not on ``own_origin`` (local ``data:`` and ``blob:`` URLs never are)."""
    return [
        url
        for url in urls
        if urlsplit(url).scheme not in LOCAL_SCHEMES and origin_of(url) != own_origin
    ]


def poll_gaps_ms(page: Mapping[str, Any]) -> list[int]:
    """Gaps, in ms, between the refreshes of the page's API path, in the order they were made.

    Only ``GET`` calls to the page's own ``apiPrefix`` count. A page that asks for the same path
    with another query (the dashboard's window moves) is still one poll, and the calls one refresh
    starts together (``SAME_REFRESH_WINDOW_MS`` from its first call) are one refresh.
    """
    prefix = page["apiPrefix"]
    times = sorted(
        request["atMs"]
        for request in page["requests"]
        if request["method"] == "GET" and urlsplit(request["url"]).path == prefix
    )
    refresh_starts: list[int] = []
    for time_ms in times:
        if not refresh_starts or time_ms - refresh_starts[-1] > SAME_REFRESH_WINDOW_MS:
            refresh_starts.append(time_ms)
    return [later - earlier for earlier, later in pairwise(refresh_starts)]


def poll_problems(page: Mapping[str, Any]) -> list[str]:
    """Why a page does not poll "every few seconds" (empty: it does)."""
    gaps = poll_gaps_ms(page)
    if len(gaps) + 1 < POLLS_NEEDED:
        return [
            f"{page['name']}: {len(gaps) + 1} call(s) to {page['apiPrefix']} in "
            f"{page['watchedSeconds']} s, expected at least {POLLS_NEEDED}"
        ]
    return [
        f"{page['name']}: calls to {page['apiPrefix']} {gap} ms apart, expected between "
        f"{POLL_GAP_FLOOR_MS} and {POLL_GAP_CEILING_MS} ms"
        for gap in gaps
        if not POLL_GAP_FLOOR_MS <= gap <= POLL_GAP_CEILING_MS
    ]


# --- the run ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    session: ops_session.Target
    export_dir: Path

    @property
    def base_url(self) -> str:
        return self.session.base_url


@dataclass(frozen=True, slots=True)
class PageUnderTest:
    name: str
    path: str  # under the UI prefix, with the query
    api_prefix: str | None


@pytest.fixture(scope="module")
def target() -> Target:
    export_dir = Path(os.environ.get("APPMUX_TEST_UI_EXPORT") or REPO_ROOT / "web" / "out")
    if not (export_dir / "index.html").is_file():
        pytest.fail(f"no UI export at {export_dir}; run `npm run build` in web/ first")
    return Target(session=ops_session.load_target(), export_dir=export_dir)


def _identify(session: ops_session.Target) -> Callable[[httpx.Request], None]:
    """A hook that says who is calling on each request, so a renewed token is picked up."""

    def add_identity(request: httpx.Request) -> None:
        mutating = request.method not in SAFE_METHODS
        request.headers.update(
            ops_session.identity_headers(session, with_token=True, mutating=mutating)
        )

    return add_identity


@pytest.fixture(scope="module")
def client(target: Target) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=target.base_url,
        timeout=REQUEST_TIMEOUT_S,
        event_hooks={"request": [_identify(target.session)]},
    ) as session:
        yield session


@pytest.fixture(scope="module")
def app_slug(client: httpx.Client, target: Target) -> Iterator[str]:
    """An app for the app pages to show; registered, never started, deleted afterwards."""
    slug = f"ui-check-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
    response = client.post(
        f"{API_PREFIX}/apps",
        json=ops_session.register_body(
            slug=slug,
            name="UI check",
            folder=f"{target.session.fixtures_folder}/light_fastapi",
            owner=target.session.admin_email,
        ),
    )
    assert response.status_code in {200, 201}, f"registering the app: {response.status_code}"
    try:
        yield slug
    finally:
        deleted = client.delete(f"{API_PREFIX}/apps/{slug}", timeout=ops_session.DELETE_TIMEOUT_S)
        if deleted.status_code not in {204, 404}:
            warnings.warn(
                f"could not delete the UI check app {slug} (status {deleted.status_code}); "
                "remove it by hand",
                stacklevel=2,
            )


def pages_of_the_export(export_dir: Path, slug: str) -> list[PageUnderTest]:
    """One entry per page directory of the export; the app pages get ``?slug=``."""
    pages: list[PageUnderTest] = []
    for index in sorted(export_dir.rglob("index.html")):
        directory = index.parent.relative_to(export_dir).as_posix()
        directory = "" if directory == "." else directory
        if directory.split("/")[0] in NOT_PAGES:
            continue
        query = f"?slug={slug}" if directory.split("/")[0] == APP_PAGE_DIRECTORY else ""
        path = f"{UI_PREFIX}/{directory}/" if directory else f"{UI_PREFIX}/"
        pages.append(
            PageUnderTest(
                name=directory or "launcher",
                path=f"{path}{query}",
                api_prefix=POLLING_PAGES.get(directory),
            )
        )
    return pages


@pytest.fixture(scope="module")
def pages(target: Target, app_slug: str) -> list[PageUnderTest]:
    found = pages_of_the_export(target.export_dir, app_slug)
    missing = set(POLLING_PAGES) - {page.path.removeprefix(UI_PREFIX).strip("/") for page in found}
    assert not missing, f"the export has no page for {sorted(missing)}"
    return found


@pytest.fixture(scope="module")
def documents(client: httpx.Client, pages: list[PageUnderTest]) -> dict[str, httpx.Response]:
    """Every page, fetched once through the edge."""
    answers = {page.name: client.get(page.path) for page in pages}
    for name, answer in answers.items():
        assert answer.status_code == 200, f"GET {name}: {answer.status_code}"
    return answers


def run_browser(config: Mapping[str, Any]) -> dict[str, Any]:
    """Run ``ui_browser_check.mjs`` and return its report.

    The configuration holds the identity headers (in a remote run, the owner's bearer token), so it
    goes to node on stdin: an argument would show in the process list of this machine.
    """
    finished = subprocess.run(
        ["node", str(BROWSER_SCRIPT)],
        input=json.dumps(config),
        capture_output=True,
        text=True,
        timeout=BROWSER_RUN_TIMEOUT_S,
        check=False,
    )
    if finished.returncode != 0:
        # stderr is the browser's own message (a missing Chromium says how to install it).
        pytest.fail(
            f"the browser run failed (exit {finished.returncode}): {finished.stderr[-800:]}"
        )
    report: dict[str, Any] = json.loads(finished.stdout)
    return report


@pytest.fixture(scope="module")
def browser_report(target: Target, pages: list[PageUnderTest]) -> dict[str, Any]:
    """What a real Chromium saw on every page (one run for all the browser checks)."""
    if shutil.which("node") is None:
        pytest.fail("node is needed for the browser checks; install Node 22.18 or newer")
    if not (WEB_DIR / "node_modules" / "@playwright" / "test").is_dir():
        pytest.fail("run `npm ci` in web/ first: the browser checks use its Playwright")
    return run_browser(
        {
            "webDir": str(WEB_DIR),
            "baseUrl": target.base_url,
            # A remote run sends the bearer, which the real proxy turns into the identity headers;
            # inside the container only X-Forwarded-Email, which is all a page load needs.
            "headers": ops_session.identity_headers(target.session),
            "pages": [
                {"name": page.name, "path": page.path, "apiPrefix": page.api_prefix}
                for page in pages
            ],
            "settleSeconds": SETTLE_SECONDS,
            "watchSeconds": WATCH_SECONDS,
        }
    )


def _text(answer: httpx.Response) -> str:
    return answer.content.decode("utf-8")


@pytest.mark.integration
def test_every_page_is_sent_with_a_policy_that_allows_exactly_its_inline_scripts(
    documents: dict[str, httpx.Response],
) -> None:
    problems: list[str] = []
    for name, answer in documents.items():
        policy = answer.headers.get("content-security-policy")
        if policy is None:
            problems.append(f"{name}: no Content-Security-Policy header")
            continue
        sources = scan_page(answer.content).script_sources
        problems.extend(f"{name}: {problem}" for problem in policy_problems(policy, sources))

    assert not problems, "\n".join(problems)


@pytest.mark.integration
def test_every_page_carries_the_isolation_headers_and_no_answer_has_a_cors_header(
    client: httpx.Client, documents: dict[str, httpx.Response]
) -> None:
    api_answer = client.get(f"{API_PREFIX}/me")
    problems: list[str] = []
    for name, answer in {**documents, "api /me": api_answer}.items():
        headers = answer.headers
        # The opener policy is about documents a window can open (FR-021 names the UI's pages); an
        # API answer has no browsing context of its own, so only the pages are held to it.
        if name != "api /me" and headers.get("cross-origin-opener-policy") != "same-origin":
            problems.append(f"{name}: Cross-Origin-Opener-Policy is not same-origin")
        if headers.get("x-content-type-options") != "nosniff":
            problems.append(f"{name}: X-Content-Type-Options is not nosniff")
        problems.extend(
            f"{name}: has the header {header}"
            for header in headers
            if header.lower().startswith("access-control-")
        )

    assert api_answer.status_code == 200
    assert not problems, "\n".join(problems)


@pytest.mark.integration
def test_no_page_names_another_origin_to_fetch_and_every_asset_it_names_is_served(
    client: httpx.Client, target: Target, documents: dict[str, httpx.Response]
) -> None:
    own_origin = origin_of(target.base_url)
    foreign: list[str] = []
    assets: set[str] = set()
    for name, answer in documents.items():
        urls = fetched_urls(_text(answer), f"{target.base_url}{answer.request.url.path}")
        foreign.extend(f"{name}: {url}" for url in other_origins(urls, own_origin))
        assets.update(url for url in urls if origin_of(url) == own_origin)

    broken = [
        f"{url}: {served.status_code}"
        for url in sorted(assets)
        if (served := client.get(url)).status_code != 200
    ]

    assert not foreign, "\n".join(foreign)
    assert assets, "no page names a script or a style sheet: the scan found nothing to check"
    assert not broken, "\n".join(broken)


@pytest.mark.integration
def test_an_unknown_path_under_the_ui_prefix_is_the_404_page_with_a_policy(
    client: httpx.Client,
) -> None:
    answer = client.get(f"{UI_PREFIX}/no-such-page-{secrets.token_hex(SLUG_SUFFIX_BYTES)}/")

    assert answer.status_code == 404
    policy = answer.headers.get("content-security-policy", "")
    assert not policy_problems(policy, scan_page(answer.content).script_sources)


def load_problems(page: Mapping[str, Any]) -> list[str]:
    """What is wrong with one page's load, from the browser's report on it."""
    name = page["name"]
    if page["networkChanged"]:
        # The browser's own machine reported a network change on every attempt, so the page was
        # never seen whole. That says nothing about the App, and it is not a pass either.
        return [
            f"{name}: the machine running the browser reported a network change on each of "
            f"{page['attempts']} attempts, so the page was never loaded whole; run it again on a "
            "machine whose interfaces are steady"
        ]
    problems: list[str] = []
    if page["status"] != 200:
        problems.append(f"{name}: the document answered {page['status']}")
    problems.extend(
        f"{name}: CSP violation {v['effectiveDirective']} blocked {v['blockedURI']!r} "
        f"at {v['sourceFile']}:{v['lineNumber']} on {v['target']}"
        for v in page["violations"]
    )
    problems.extend(f"{name}: page error {error}" for error in page["pageErrors"])
    problems.extend(
        f"{name}: console error {error['text']} ({error['url']})" for error in page["consoleErrors"]
    )
    return problems


@pytest.mark.integration
def test_the_browser_loaded_every_page_without_a_csp_violation_or_an_error(
    browser_report: dict[str, Any],
) -> None:
    problems = [problem for page in browser_report["pages"] for problem in load_problems(page)]

    assert len(browser_report["pages"]) >= len(POLLING_PAGES)
    assert not problems, "\n".join(problems)


@pytest.mark.integration
def test_the_browser_sent_no_request_to_another_origin(
    browser_report: dict[str, Any], target: Target
) -> None:
    own_origin = origin_of(target.base_url)
    foreign: list[str] = []
    for page in browser_report["pages"]:
        urls = [request["url"] for request in page["requests"]]
        foreign.extend(f"{page['name']}: {url}" for url in other_origins(urls, own_origin))

    assert not foreign, "\n".join(foreign)


@pytest.mark.integration
def test_the_launcher_the_platform_page_and_the_dashboard_poll_their_api_every_few_seconds(
    browser_report: dict[str, Any],
) -> None:
    polling = [page for page in browser_report["pages"] if page["apiPrefix"] is not None]
    problems = [
        problem
        for page in polling
        for problem in (load_problems(page) if page["networkChanged"] else poll_problems(page))
    ]

    assert {page["apiPrefix"] for page in polling} == set(POLLING_PAGES.values())
    assert not problems, "\n".join(problems)
