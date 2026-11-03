"""``GET /workspace/browse``: the folder browser behind the source-folder field (T194).

The route runs behind the real guard and authorizer on the in-memory registry, and the workspace
is an ``httpx.MockTransport`` behind the real ``WorkspaceHttpApi``, so the call it makes (path,
query, bearer token) is the one the platform would receive. What is pinned:

* who may browse: any visitor with a token, a person with no grant included; nobody without an
  identity; a request with no token is ``403 user_token_missing``, the roots included;
* the roots, which call nothing upstream;
* a folder: the call is made with the visitor's own token, and the entries, markers and hints
  come from the listing;
* every upstream failure becomes one of four answers (``404 not_found``, ``403 no_access``,
  ``403 session_expired``, ``503 workspace_unavailable``) and none repeats the platform's text, a
  path or the token;
* a path that is not a workspace folder path is refused before anything is called;
* a browse writes nothing and records nothing.

All emails are placeholders; the token is a fake value.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Final

import httpx
import pytest
from edge_harness import ALICE, BOB, CAROL, DANA, VISITOR_TOKEN, AdminApi, seeded_registry
from stub_registry import StubRegistry

from appmux.admin.workspace_browse import MAX_ENTRIES, WorkspaceBrowseRoutes
from appmux.packages.store import WorkspaceHttpApi

NOBODY = "erin@example.com"  # a visitor with no grant on any app
HOME = f"/Workspace/Users/{ALICE}"
SHARED = "/Workspace/Shared"
SALES = f"{HOME}/sales"
PLATFORM_TEXT = "Provided OAuth token does not have required scopes: workspace.workspace:read"
NO_TOKEN: Final = {"X-Forwarded-Access-Token": ""}


def listed(*objects: tuple[str, str]) -> Callable[[httpx.Request], httpx.Response]:
    """A workspace that answers every list call with ``objects`` as (path, object_type)."""
    body = {"objects": [{"path": path, "object_type": kind} for path, kind in objects]}
    return lambda request: httpx.Response(200, json=body)


def failing(status: int, message: str = PLATFORM_TEXT) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json={"error_code": "X", "message": message})


class Browser:
    """The admin API with the folder browser mounted over a recording mock workspace."""

    def __init__(self, answer: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._answer = answer if answer is not None else listed()
        self.registry: StubRegistry = seeded_registry()
        client = httpx.AsyncClient(
            base_url="https://workspace.example.invalid", transport=httpx.MockTransport(self._send)
        )
        self.api = AdminApi(
            self.registry, lambda authorizer: [WorkspaceBrowseRoutes(WorkspaceHttpApi(client))]
        )

    def _send(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._answer(request)

    def browse(
        self, path: str | None = None, email: str | None = ALICE, *, query: str | None = None
    ) -> httpx.Response:
        if query is None:
            query = "" if path is None else "?" + str(httpx.QueryParams({"path": path}))
        return self.api.call("GET", f"/workspace/browse{query}", email)


def error_of(response: httpx.Response) -> tuple[str, str]:
    error = response.json()["error"]
    return error["code"], error["message"]


def assert_leaks_nothing(response: httpx.Response) -> None:
    for secret in (VISITOR_TOKEN, PLATFORM_TEXT, "workspace.example.invalid", "Traceback"):
        assert secret not in response.text


# --- who may browse -------------------------------------------------------------------------


@pytest.mark.parametrize("email", [ALICE, BOB, CAROL, DANA, NOBODY])
def test_any_visitor_with_a_token_may_browse_even_with_no_grant_on_any_app(email: str) -> None:
    browser = Browser()

    response = browser.browse(SHARED, email)

    assert response.status_code == 200, response.text


def test_a_request_with_no_identity_is_refused_and_nothing_is_called() -> None:
    browser = Browser()

    response = browser.browse(SHARED, None)

    assert response.status_code == 401
    assert browser.requests == []


def test_a_visitor_whose_token_did_not_arrive_is_told_why_and_nothing_is_called() -> None:
    browser = Browser()

    response = browser.api.call("GET", "/workspace/browse?path=" + SHARED, ALICE, headers=NO_TOKEN)

    assert response.status_code == 403
    code, message = error_of(response)
    assert code == "user_token_missing"
    assert "access token" in message
    assert browser.requests == []


def test_the_roots_too_need_the_token() -> None:
    browser = Browser()

    response = browser.api.call("GET", "/workspace/browse", ALICE, headers=NO_TOKEN)

    assert (response.status_code, error_of(response)[0]) == (403, "user_token_missing")


# --- the roots ------------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "?path=", "?path=%20"])
def test_no_path_returns_the_roots_of_the_visitor_and_calls_nothing(query: str) -> None:
    browser = Browser()

    response = browser.browse(query=query, email=BOB)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "path": None,
        "parent": None,
        "roots": [
            {"label": "My folder", "path": f"/Workspace/Users/{BOB}"},
            {"label": "Shared", "path": SHARED},
        ],
        "entries": [],
        "truncated": False,
        "markers": [],
        "looks_like_app": False,
        "runtime_hint": None,
        "suggested_memory_mb": None,
    }
    assert browser.requests == []


# --- a folder -------------------------------------------------------------------------------


def test_a_folder_is_listed_with_the_visitors_own_token() -> None:
    browser = Browser(
        listed(
            (f"{SALES}/server", "DIRECTORY"),
            (f"{SALES}/package.json", "FILE"),
            (f"{SALES}/app.yaml", "FILE"),
            (f"{SALES}/notes", "NOTEBOOK"),
        )
    )

    response = browser.browse(SALES)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "path": SALES,
        "parent": HOME,
        "roots": [],
        "entries": [
            {"name": "server", "path": f"{SALES}/server", "kind": "folder"},
            {"name": "app.yaml", "path": f"{SALES}/app.yaml", "kind": "file"},
            {"name": "notes", "path": f"{SALES}/notes", "kind": "file"},
            {"name": "package.json", "path": f"{SALES}/package.json", "kind": "file"},
        ],
        "truncated": False,
        "markers": ["app.yaml", "package.json"],
        "looks_like_app": True,
        "runtime_hint": "node",
        "suggested_memory_mb": 768,
    }
    [call] = browser.requests
    assert call.method == "GET"
    assert call.url.path == "/api/2.0/workspace/list"
    assert dict(call.url.params) == {"path": SALES}
    assert call.headers["Authorization"] == f"Bearer {VISITOR_TOKEN}"


def test_a_python_app_folder_is_hinted_as_python_with_no_memory_suggestion() -> None:
    browser = Browser(listed((f"{SALES}/requirements.txt", "FILE"), (f"{SALES}/app.py", "FILE")))

    body = browser.browse(SALES).json()

    assert (body["runtime_hint"], body["suggested_memory_mb"], body["looks_like_app"]) == (
        "python",
        None,
        True,
    )


def test_the_listing_is_capped_and_says_so() -> None:
    names = [f"{SALES}/f{n:04}.txt" for n in range(MAX_ENTRIES + 20)]
    browser = Browser(listed(*((path, "FILE") for path in names)))

    body = browser.browse(SALES).json()

    assert len(body["entries"]) == MAX_ENTRIES
    assert body["truncated"] is True


def test_a_trailing_slash_is_dropped_before_the_call() -> None:
    browser = Browser()

    browser.browse(SALES + "/")

    assert dict(browser.requests[0].url.params) == {"path": SALES}


def test_a_name_with_two_dots_inside_it_is_a_folder_like_any_other() -> None:
    browser = Browser()

    response = browser.browse(f"{HOME}/v1..2")

    assert response.status_code == 200, response.text


def test_a_browse_writes_nothing_and_the_answer_is_not_cached() -> None:
    browser = Browser()

    response = browser.browse(SALES)

    assert browser.registry.audit_rows == ()
    assert response.headers["Cache-Control"] == "no-store"


# --- what the platform answers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "http_status", "code"),
    [
        (404, 404, "not_found"),
        (403, 403, "no_access"),
        (401, 403, "session_expired"),
        (400, 503, "workspace_unavailable"),
        (429, 503, "workspace_unavailable"),
        (500, 503, "workspace_unavailable"),
        (502, 503, "workspace_unavailable"),
        (503, 503, "workspace_unavailable"),
    ],
)
def test_each_upstream_failure_becomes_one_answer_that_repeats_nothing(
    status: int, http_status: int, code: str
) -> None:
    browser = Browser(failing(status))

    response = browser.browse(SALES)

    assert response.status_code == http_status
    answered, message = error_of(response)
    assert answered == code
    assert message
    assert_leaks_nothing(response)
    assert SALES not in response.text


def test_a_workspace_that_does_not_answer_is_workspace_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    response = Browser(refuse).browse(SALES)

    assert (response.status_code, error_of(response)[0]) == (503, "workspace_unavailable")
    assert_leaks_nothing(response)


def test_a_listing_in_an_unexpected_shape_is_workspace_unavailable() -> None:
    response = Browser(lambda request: httpx.Response(200, json={"objects": "not a list"})).browse(
        SALES
    )

    assert (response.status_code, error_of(response)[0]) == (503, "workspace_unavailable")


def test_a_listing_with_no_objects_key_is_an_empty_folder() -> None:
    # The platform answers {} for an empty folder (measured).
    response = Browser(lambda request: httpx.Response(200, json={})).browse(SALES)

    assert response.status_code == 200
    assert response.json()["entries"] == []


# --- a path that is not a workspace folder --------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        f"{HOME}/../bob@example.com",
        "/Workspace/..",
        f"{HOME}/sa\nles",
        f"{HOME}/sa\x00les",
        "/Workspace/" + "a" * 512,
        "/Users/alice@example.com",
        "Workspace/Shared",
        "/Workspace",
        "/Volumes/main/default/files",
        "https://example.com/Workspace/Shared",
    ],
)
def test_a_path_that_is_not_a_workspace_folder_is_refused_before_anything_is_called(
    path: str,
) -> None:
    browser = Browser()

    response = browser.browse(path)

    assert response.status_code == 400
    code, message = error_of(response)
    assert code == "invalid_path"
    # One fixed sentence for every refusal, so it cannot repeat what was submitted.
    assert message == error_of(browser.browse("/nope"))[1]
    assert browser.requests == []


def test_a_path_sent_twice_is_refused() -> None:
    browser = Browser()

    response = browser.browse(query=f"?path={SHARED}&path={SALES}")

    assert (response.status_code, error_of(response)[0]) == (400, "bad_request")
    assert browser.requests == []


def test_the_response_body_is_the_contracts_schema() -> None:
    from appmux.admin.schemas import WorkspaceListing

    browser = Browser(listed((f"{SALES}/app.yaml", "FILE")))

    WorkspaceListing.model_validate_json(json.dumps(browser.browse(SALES).json()))
