"""``GET /resources/types`` and ``GET /resources/suggestions``: the attach form's pickers (T195).

The routes run behind the real guard and authorizer on the in-memory registry, and the workspace is
an ``httpx.MockTransport`` that records every call, so what is pinned is the call the platform would
receive (method, path, query, bearer token) and what the route makes of each answer. Pinned here:

* who may ask: a platform admin or a person with ``CAN_MANAGE`` on at least one app; nobody else,
  and nobody without an identity; a refused request calls nothing upstream;
* the catalogue route calls nothing upstream and needs no token;
* the parameters are checked before anything is called (``unknown_type``, ``invalid_query``,
  ``invalid_limit``);
* the call and the mapping of each listed type, with the person's own token; the key names of a
  secret scope are listed and a secret value is never asked for;
* a type with no list call is ``not_supported`` and calls nothing;
* anything Databricks refuses or does not answer is a ``200`` with ``source: "unavailable"`` and a
  note in words, never an error page, and the note repeats neither the platform's text nor the
  token;
* the list is filtered by ``q`` here, ordered by label, cut at ``limit`` and says when it was cut;
* a suggestion writes nothing and records nothing.

All emails and names are placeholders; the token is a fake value.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Final

import httpx
import pytest
from edge_harness import ALICE, BOB, CAROL, DANA, VISITOR_TOKEN, AdminApi, seeded_registry
from stub_registry import StubRegistry

from appmux.admin.resource_suggestions import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_QUERY_LENGTH,
    SUGGESTABLE_TYPES,
    ResourceSuggestionRoutes,
)
from appmux.admin.schemas import ResourceSuggestionList, ResourceTypeList
from appmux.registry.resources import RESOURCE_TYPES

NOBODY = "erin@example.com"  # a visitor with no grant on any app
PLATFORM_TEXT = "Provided OAuth token does not have required scopes: workspace.workspace:read"
DENIED_TEXT = "User does not have permission to list the thing named internal-secret-name"
NO_TOKEN: Final = {"X-Forwarded-Access-Token": ""}

WAREHOUSES_PATH = "/api/2.0/sql/warehouses"
ENDPOINTS_PATH = "/api/2.0/serving-endpoints"
SCOPES_PATH = "/api/2.0/secrets/scopes/list"
KEYS_PATH = "/api/2.0/secrets/list"
EXPERIMENTS_PATH = "/api/2.0/mlflow/experiments/search"
JOBS_PATH = "/api/2.2/jobs/list"

# (type, method, path): the one list call of each listed type
LIST_CALLS: Final = {
    "sql_warehouse": ("GET", WAREHOUSES_PATH),
    "serving_endpoint": ("GET", ENDPOINTS_PATH),
    "secret": ("GET", SCOPES_PATH),
    "mlflow_experiment": ("POST", EXPERIMENTS_PATH),
    "job": ("GET", JOBS_PATH),
}

WAREHOUSES: Final = {
    "warehouses": [
        {
            "id": "1234567890abcdef",
            "name": "Shared Serverless",
            "state": "RUNNING",
            "cluster_size": "Small",
        },
        {"id": "fedcba0987654321", "name": "Reporting", "state": "STOPPED"},
    ]
}


class Workspace:
    """A recording workspace: ``answers`` maps (method, path) to a status and a JSON body."""

    def __init__(self, answers: Mapping[tuple[str, str], tuple[int, object]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._answers = dict(answers or {})
        self._everything: Callable[[httpx.Request], httpx.Response] | None = None

    def answer_everything_with(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self._everything = answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._everything is not None:
            return self._everything(request)
        found = self._answers.get((request.method, request.url.path))
        if found is None:
            return httpx.Response(404, json={"error_code": "NOT_FOUND", "message": DENIED_TEXT})
        status, body = found
        return httpx.Response(status, json=body)

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]


class Suggester:
    """The admin API with the resource routes mounted over a recording workspace."""

    def __init__(self, workspace: Workspace | None = None) -> None:
        self.workspace = workspace if workspace is not None else Workspace()
        self.registry: StubRegistry = seeded_registry()
        client = httpx.AsyncClient(
            base_url="https://workspace.example.invalid",
            transport=httpx.MockTransport(self.workspace.handle),
        )
        self.api = AdminApi(self.registry, lambda authorizer: [ResourceSuggestionRoutes(client)])

    def types(
        self, email: str | None = ALICE, headers: Mapping[str, str] | None = None
    ) -> httpx.Response:
        return self.api.call("GET", "/resources/types", email, headers=headers)

    def suggest(
        self,
        type_name: str | None = None,
        q: str | None = None,
        limit: str | None = None,
        email: str | None = ALICE,
        *,
        query: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        if query is None:
            pairs = {"type": type_name, "q": q, "limit": limit}
            query = "?" + str(httpx.QueryParams({k: v for k, v in pairs.items() if v is not None}))
        return self.api.call("GET", f"/resources/suggestions{query}", email, headers=headers)


def listed(workspace_body: object, type_name: str = "sql_warehouse") -> Suggester:
    method, path = LIST_CALLS[type_name]
    return Suggester(Workspace({(method, path): (200, workspace_body)}))


def error_of(response: httpx.Response) -> tuple[str, str]:
    error = response.json()["error"]
    return error["code"], error["message"]


def targets_of(response: httpx.Response) -> list[str]:
    return [item["target"] for item in response.json()["items"]]


def assert_leaks_nothing(response: httpx.Response) -> None:
    for secret in (VISITOR_TOKEN, PLATFORM_TEXT, DENIED_TEXT, "internal-secret-name", "Traceback"):
        assert secret not in response.text


# --- who may ask ----------------------------------------------------------------------------


@pytest.mark.parametrize("email", [DANA, ALICE, CAROL])
def test_a_platform_admin_or_a_manager_of_some_app_may_read_the_catalogue(email: str) -> None:
    suggester = Suggester()

    response = suggester.types(email)

    assert response.status_code == 200, response.text
    assert suggester.workspace.requests == []


@pytest.mark.parametrize("email", [BOB, NOBODY])
def test_a_person_who_manages_no_app_is_refused_the_catalogue_and_the_suggestions(
    email: str,
) -> None:
    suggester = listed(WAREHOUSES)

    types = suggester.types(email)
    suggestions = suggester.suggest("sql_warehouse", email=email)

    assert (types.status_code, suggestions.status_code) == (403, 403)
    assert suggester.workspace.requests == []
    assert_leaks_nothing(suggestions)


@pytest.mark.parametrize("email", [DANA, ALICE, CAROL])
def test_a_platform_admin_or_a_manager_of_some_app_may_ask_for_suggestions(email: str) -> None:
    suggester = listed(WAREHOUSES)

    response = suggester.suggest("sql_warehouse", email=email)

    assert response.status_code == 200, response.text


def test_a_request_with_no_identity_is_refused_and_nothing_is_called() -> None:
    suggester = listed(WAREHOUSES)

    types = suggester.types(None)
    suggestions = suggester.suggest("sql_warehouse", email=None)

    assert (types.status_code, suggestions.status_code) == (401, 401)
    assert suggester.workspace.requests == []


# --- the catalogue --------------------------------------------------------------------------


def test_the_catalogue_route_answers_the_whole_catalogue_without_a_token() -> None:
    suggester = Suggester()

    response = suggester.types(headers=NO_TOKEN)

    assert response.status_code == 200, response.text
    body = ResourceTypeList.model_validate_json(response.text)
    assert [entry.type for entry in body.items] == list(RESOURCE_TYPES)
    assert {entry.type for entry in body.items if entry.can_suggest} == SUGGESTABLE_TYPES
    assert suggester.workspace.requests == []
    assert response.headers["Cache-Control"] == "no-store"


# --- the parameters -------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "?type=", "?type=bogus", "?type=SECRET", "?q=x"])
def test_a_missing_or_unknown_type_is_refused_before_anything_is_called(query: str) -> None:
    suggester = listed(WAREHOUSES)

    response = suggester.suggest(query=query)

    assert response.status_code == 400
    code, message = error_of(response)
    assert code == "unknown_type"
    assert "secret" in message  # it says what to use
    assert suggester.workspace.requests == []


@pytest.mark.parametrize("name", ["type", "q", "limit"])
def test_a_parameter_sent_twice_is_refused(name: str) -> None:
    suggester = listed(WAREHOUSES)
    pairs = {"type": "sql_warehouse", "q": "a", "limit": "5"}
    query = "?" + str(httpx.QueryParams({**pairs, name: [pairs[name], pairs[name]]}))

    response = suggester.suggest(query=query)

    assert (response.status_code, error_of(response)[0]) == (400, "bad_request")
    assert suggester.workspace.requests == []


@pytest.mark.parametrize("q", ["x" * (MAX_QUERY_LENGTH + 1), "a\nb", "a\x00b", "a\x7fb"])
def test_a_query_that_is_too_long_or_holds_a_control_character_is_refused(q: str) -> None:
    suggester = listed(WAREHOUSES)

    response = suggester.suggest("sql_warehouse", q)

    assert (response.status_code, error_of(response)[0]) == (400, "invalid_query")
    assert suggester.workspace.requests == []


def test_a_query_of_exactly_the_longest_length_is_accepted() -> None:
    suggester = listed(WAREHOUSES)

    response = suggester.suggest("sql_warehouse", "x" * MAX_QUERY_LENGTH)

    assert response.status_code == 200


@pytest.mark.parametrize("limit", ["0", str(MAX_LIMIT + 1), "-1", "abc", "", "1.5", " 5", "٣"])
def test_a_limit_outside_one_to_fifty_or_not_a_number_is_refused(limit: str) -> None:
    suggester = listed(WAREHOUSES)

    response = suggester.suggest("sql_warehouse", limit=limit)

    assert (response.status_code, error_of(response)[0]) == (400, "invalid_limit")
    assert suggester.workspace.requests == []


@pytest.mark.parametrize("limit", ["1", str(MAX_LIMIT)])
def test_the_ends_of_the_limit_range_are_accepted(limit: str) -> None:
    assert listed(WAREHOUSES).suggest("sql_warehouse", limit=limit).status_code == 200


def test_the_default_limit_is_twenty() -> None:
    assert DEFAULT_LIMIT == 20
    many = {"warehouses": [{"id": f"w{n:03}", "name": f"name-{n:03}"} for n in range(25)]}
    suggester = listed(many)

    body = suggester.suggest("sql_warehouse").json()

    assert len(body["items"]) == DEFAULT_LIMIT
    assert body["truncated"] is True


# --- what each type is read from ------------------------------------------------------------


def test_warehouses_are_read_with_the_persons_token_and_mapped_to_id_and_name() -> None:
    suggester = listed(WAREHOUSES)

    response = suggester.suggest("sql_warehouse")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [
            {
                "target": "fedcba0987654321",
                "label": "Reporting",
                "detail": "STOPPED",
                "permissions": ["CAN_USE", "CAN_MANAGE"],
            },
            {
                "target": "1234567890abcdef",
                "label": "Shared Serverless",
                "detail": "RUNNING, size Small",
                "permissions": ["CAN_USE", "CAN_MANAGE"],
            },
        ],
        "truncated": False,
        "source": "caller_token",
        "note": None,
    }
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", WAREHOUSES_PATH)
    assert call.headers["Authorization"] == f"Bearer {VISITOR_TOKEN}"


def test_a_warehouse_with_no_usable_id_or_name_is_left_out() -> None:
    body = {
        "warehouses": [
            {"id": "ok-1", "name": "Fine"},
            {"name": "No id"},
            {"id": "no-name"},
            {"id": 12, "name": "Numeric id"},
            {"id": "has space", "name": "A target the attach form refuses"},
            "not an object",
        ]
    }

    response = listed(body).suggest("sql_warehouse")

    assert targets_of(response) == ["ok-1"]


def test_serving_endpoints_are_read_and_mapped_to_name_with_their_state() -> None:
    body = {
        "endpoints": [
            {"name": "chat-endpoint", "state": {"ready": "READY"}, "task": "llm/v1/chat"},
            {"name": "embedder", "state": {"ready": "NOT_READY"}},
            {"name": "bare"},
        ]
    }
    suggester = listed(body, "serving_endpoint")

    response = suggester.suggest("serving_endpoint")

    assert response.json()["items"] == [
        {
            "target": "bare",
            "label": "bare",
            "detail": None,
            "permissions": ["CAN_VIEW", "CAN_QUERY", "CAN_MANAGE"],
        },
        {
            "target": "chat-endpoint",
            "label": "chat-endpoint",
            "detail": "READY, llm/v1/chat",
            "permissions": ["CAN_VIEW", "CAN_QUERY", "CAN_MANAGE"],
        },
        {
            "target": "embedder",
            "label": "embedder",
            "detail": "NOT_READY",
            "permissions": ["CAN_VIEW", "CAN_QUERY", "CAN_MANAGE"],
        },
    ]
    assert suggester.workspace.calls == [("GET", ENDPOINTS_PATH)]


def test_secret_scopes_are_listed_as_prefixes_when_the_query_has_no_slash() -> None:
    body = {
        "scopes": [
            {"name": "prod-scope", "backend_type": "DATABRICKS"},
            {"name": "dev scope", "backend_type": "DATABRICKS"},  # not a name a target can hold
            {"name": "analytics", "backend_type": "DATABRICKS"},
        ]
    }
    suggester = listed(body, "secret")

    response = suggester.suggest("secret")

    assert response.json()["items"] == [
        {"target": "analytics/", "label": "analytics", "detail": None, "permissions": ["READ"]},
        {"target": "prod-scope/", "label": "prod-scope", "detail": None, "permissions": ["READ"]},
    ]
    assert suggester.workspace.calls == [("GET", SCOPES_PATH)]


def test_the_query_without_a_slash_filters_the_scope_names() -> None:
    body = {"scopes": [{"name": "prod-scope"}, {"name": "analytics"}]}

    response = listed(body, "secret").suggest("secret", "PROD")

    assert targets_of(response) == ["prod-scope/"]


def test_a_query_of_the_form_scope_slash_lists_the_key_names_of_that_scope() -> None:
    answers = {
        ("GET", KEYS_PATH): (
            200,
            {
                "secrets": [
                    {"key": "db-password", "last_updated_timestamp": 1700000000000},
                    {"key": "api-token", "last_updated_timestamp": 1700000000001},
                    {"key": "bad key", "last_updated_timestamp": 1700000000002},
                ]
            },
        )
    }
    suggester = Suggester(Workspace(answers))

    response = suggester.suggest("secret", "prod-scope/")

    assert response.json()["items"] == [
        {
            "target": "prod-scope/api-token",
            "label": "api-token",
            "detail": None,
            "permissions": ["READ"],
        },
        {
            "target": "prod-scope/db-password",
            "label": "db-password",
            "detail": None,
            "permissions": ["READ"],
        },
    ]
    assert response.json()["source"] == "caller_token"
    [call] = suggester.workspace.requests
    assert dict(call.url.params) == {"scope": "prod-scope"}
    assert call.headers["Authorization"] == f"Bearer {VISITOR_TOKEN}"


def test_the_keys_are_filtered_by_what_follows_the_slash() -> None:
    answers = {("GET", KEYS_PATH): (200, {"secrets": [{"key": "db-password"}, {"key": "api"}]})}

    response = Suggester(Workspace(answers)).suggest("secret", "prod/PASS")

    assert targets_of(response) == ["prod/db-password"]


def test_the_scope_part_of_the_query_does_not_match_every_key() -> None:
    # Every target starts with "prod/", which holds the "p" typed after the slash.
    keys = [{"key": "db-password"}, {"key": "token"}, {"key": "host"}]
    answers = {("GET", KEYS_PATH): (200, {"secrets": keys})}

    response = Suggester(Workspace(answers)).suggest("secret", "prod/p")

    assert targets_of(response) == ["prod/db-password"]


def test_listing_the_keys_of_a_scope_never_asks_for_a_secret_value() -> None:
    answers = {
        ("GET", KEYS_PATH): (200, {"secrets": [{"key": "k"}]}),
        ("GET", "/api/2.0/secrets/get"): (200, {"key": "k", "value": "c2VjcmV0LXZhbHVl"}),
        ("POST", "/api/2.0/secrets/get"): (200, {"value": "c2VjcmV0LXZhbHVl"}),
    }
    suggester = Suggester(Workspace(answers))

    for q in ("prod/", "prod/k", "prod/", ""):
        response = suggester.suggest("secret", q)
        assert "c2VjcmV0LXZhbHVl" not in response.text

    assert all(not path.endswith("/secrets/get") for _, path in suggester.workspace.calls)
    assert all(path.startswith("/api/2.0/secrets/") for _, path in suggester.workspace.calls)


def test_a_scope_that_does_not_exist_or_is_not_visible_is_an_empty_answer_with_a_note() -> None:
    suggester = Suggester(Workspace())  # every path answers 404

    response = suggester.suggest("secret", "nobody-has-this/")

    assert response.status_code == 200
    body = response.json()
    assert (body["items"], body["source"], body["truncated"]) == ([], "caller_token", False)
    assert "scope" in body["note"]
    assert_leaks_nothing(response)


@pytest.mark.parametrize("q", ["/key", "bad scope/key", "a/b/c"])
def test_a_query_whose_scope_part_cannot_be_a_scope_name_calls_nothing(q: str) -> None:
    suggester = Suggester()

    response = suggester.suggest("secret", q)

    assert response.status_code == 200
    body = response.json()
    assert (body["items"], body["source"]) == ([], "caller_token")
    assert body["note"]
    assert suggester.workspace.requests == []


def test_mlflow_experiments_are_searched_with_a_post_and_mapped_to_id_and_name() -> None:
    body = {
        "experiments": [
            {"experiment_id": "42", "name": "/Users/alice@example.com/churn"},
            {"experiment_id": "7", "name": "/Shared/forecast"},
            {"experiment_id": "9", "name": "/Shared/old", "lifecycle_stage": "deleted"},
            {"name": "/Shared/no-id"},
        ]
    }
    suggester = listed(body, "mlflow_experiment")

    response = suggester.suggest("mlflow_experiment")

    assert response.json()["items"] == [
        {
            "target": "7",
            "label": "/Shared/forecast",
            "detail": None,
            "permissions": ["CAN_READ", "CAN_EDIT", "CAN_MANAGE"],
        },
        {
            "target": "42",
            "label": "/Users/alice@example.com/churn",
            "detail": None,
            "permissions": ["CAN_READ", "CAN_EDIT", "CAN_MANAGE"],
        },
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("POST", EXPERIMENTS_PATH)
    assert json.loads(call.read()) == {"max_results": 1000, "view_type": "ACTIVE_ONLY"}
    assert call.headers["Authorization"] == f"Bearer {VISITOR_TOKEN}"


def test_jobs_are_listed_and_filtered_here_because_the_name_parameter_is_an_exact_match() -> None:
    body = {
        "jobs": [
            {"job_id": 11, "settings": {"name": "Nightly ETL"}},
            {"job_id": 12, "settings": {"name": "Weekly report"}},
            {"job_id": 13, "settings": {}},
            {"job_id": "14", "settings": {"name": "String id"}},
            {"settings": {"name": "No id"}},
        ]
    }
    suggester = listed(body, "job")

    response = suggester.suggest("job", "etl")

    assert response.json()["items"] == [
        {
            "target": "11",
            "label": "Nightly ETL",
            "detail": None,
            "permissions": ["CAN_VIEW", "CAN_MANAGE_RUN", "CAN_MANAGE"],
        }
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", JOBS_PATH)
    # The search is done here, so the call carries no name and asks for the biggest page.
    assert dict(call.url.params) == {"limit": "100"}
    assert call.headers["Authorization"] == f"Bearer {VISITOR_TOKEN}"


# --- a list that is partial or empty --------------------------------------------------------


@pytest.mark.parametrize(
    ("type_name", "body"),
    [
        ("sql_warehouse", {"warehouses": [{"id": "w1", "name": "One"}], "next_page_token": "t"}),
        ("mlflow_experiment", {"experiments": [], "next_page_token": "t"}),
        ("job", {"jobs": [], "has_more": True}),
        ("job", {"jobs": [], "next_page_token": "t"}),
    ],
)
def test_a_partial_page_from_databricks_makes_the_answer_truncated_and_says_so(
    type_name: str, body: object
) -> None:
    response = listed(body, type_name).suggest(type_name)

    answer = response.json()
    assert (answer["truncated"], answer["source"]) == (True, "caller_token")
    assert answer["note"]


@pytest.mark.parametrize("type_name", sorted(SUGGESTABLE_TYPES))
def test_an_empty_answer_from_the_platform_is_an_empty_list_with_nothing_to_say(
    type_name: str,
) -> None:
    response = listed({}, type_name).suggest(type_name)  # the platform answers {} for none

    assert response.json() == {
        "items": [],
        "truncated": False,
        "source": "caller_token",
        "note": None,
    }


def test_more_matches_than_the_limit_are_cut_in_label_order_and_marked() -> None:
    many = {"warehouses": [{"id": f"w{n:02}", "name": f"name-{n:02}"} for n in range(30)]}

    response = listed(many).suggest("sql_warehouse", limit="10")

    body = response.json()
    assert [item["label"] for item in body["items"]] == [f"name-{n:02}" for n in range(10)]
    assert (body["truncated"], body["source"], body["note"]) == (True, "caller_token", None)


def test_the_query_filters_what_the_platform_returned() -> None:
    response = listed(WAREHOUSES).suggest("sql_warehouse", "REPORT")

    assert [item["label"] for item in response.json()["items"]] == ["Reporting"]


# --- a type with no list call ---------------------------------------------------------------


@pytest.mark.parametrize("type_name", sorted(set(RESOURCE_TYPES) - SUGGESTABLE_TYPES))
def test_a_type_with_no_safe_list_call_is_not_supported_and_calls_nothing(type_name: str) -> None:
    suggester = Suggester()

    response = suggester.suggest(type_name, "anything", headers=NO_TOKEN)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["items"], body["truncated"], body["source"]) == ([], False, "not_supported")
    assert body["note"]
    assert suggester.workspace.requests == []


# --- what Databricks answers instead of a list ----------------------------------------------


def test_a_person_whose_token_did_not_arrive_gets_an_empty_unavailable_answer() -> None:
    suggester = listed(WAREHOUSES)

    response = suggester.suggest("sql_warehouse", headers=NO_TOKEN)

    assert response.status_code == 200
    body = response.json()
    assert (body["items"], body["source"]) == ([], "unavailable")
    assert "access token" in body["note"]
    assert suggester.workspace.requests == []


@pytest.mark.parametrize("type_name", sorted(SUGGESTABLE_TYPES))
def test_a_refused_scope_is_unavailable_and_says_the_app_lacks_it(type_name: str) -> None:
    suggester = Suggester()
    suggester.workspace.answer_everything_with(
        lambda request: httpx.Response(403, json={"error_code": "X", "message": PLATFORM_TEXT})
    )

    response = suggester.suggest(type_name)

    assert response.status_code == 200
    body = response.json()
    assert (body["items"], body["truncated"], body["source"]) == ([], False, "unavailable")
    assert "scope" in body["note"]
    assert_leaks_nothing(response)


@pytest.mark.parametrize(
    ("status", "words"),
    [
        (403, "cannot list"),
        (401, "reload"),
        (400, "answered 400"),
        (429, "answered 429"),
        (500, "answered 500"),
        (503, "answered 503"),
        (302, "answered 302"),
    ],
)
@pytest.mark.parametrize("type_name", sorted(SUGGESTABLE_TYPES))
def test_any_other_failure_is_unavailable_with_a_note_that_repeats_nothing(
    type_name: str, status: int, words: str
) -> None:
    suggester = Suggester()
    suggester.workspace.answer_everything_with(
        lambda request: httpx.Response(
            status,
            json={"error_code": "X", "message": DENIED_TEXT},
            headers={"Location": "https://elsewhere.example.invalid/x"} if status == 302 else {},
        )
    )

    response = suggester.suggest(type_name)

    assert response.status_code == 200
    body = response.json()
    assert (body["items"], body["source"]) == ([], "unavailable")
    assert words in body["note"]
    assert_leaks_nothing(response)
    assert len(suggester.workspace.requests) == 1  # a redirect is not followed


@pytest.mark.parametrize("type_name", sorted(SUGGESTABLE_TYPES))
def test_a_workspace_that_does_not_answer_is_unavailable(type_name: str) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    suggester = Suggester()
    suggester.workspace.answer_everything_with(refuse)

    response = suggester.suggest(type_name)

    assert response.status_code == 200
    body = response.json()
    assert (body["items"], body["source"]) == ([], "unavailable")
    assert "did not answer" in body["note"]
    assert_leaks_nothing(response)


@pytest.mark.parametrize("type_name", sorted(SUGGESTABLE_TYPES))
@pytest.mark.parametrize("body", [["a", "list"], "text", {"warehouses": "not a list"}, 7])
def test_an_answer_in_an_unexpected_shape_is_unavailable_and_never_an_error_page(
    type_name: str, body: object
) -> None:
    key = {
        "sql_warehouse": "warehouses",
        "serving_endpoint": "endpoints",
        "secret": "scopes",
        "mlflow_experiment": "experiments",
        "job": "jobs",
    }[type_name]
    shaped = body if not isinstance(body, dict) else {key: "not a list"}
    suggester = listed(shaped, type_name)

    response = suggester.suggest(type_name)

    assert response.status_code == 200
    answer = response.json()
    assert (answer["items"], answer["source"]) == ([], "unavailable")
    assert "shape" in answer["note"]


def test_a_body_that_is_not_json_is_unavailable() -> None:
    suggester = Suggester()
    suggester.workspace.answer_everything_with(lambda request: httpx.Response(200, text="<html>"))

    response = suggester.suggest("sql_warehouse")

    body = response.json()
    assert (response.status_code, body["source"]) == (200, "unavailable")


# --- what a suggestion is -------------------------------------------------------------------


def test_a_suggestion_writes_nothing_and_the_answer_is_not_cached() -> None:
    suggester = listed(WAREHOUSES)

    response = suggester.suggest("sql_warehouse")

    assert suggester.registry.audit_rows == ()
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("type_name", sorted(SUGGESTABLE_TYPES))
def test_every_answer_is_the_contracts_schema(type_name: str) -> None:
    method, path = LIST_CALLS[type_name]
    suggester = Suggester(Workspace({(method, path): (200, {})}))

    ResourceSuggestionList.model_validate_json(suggester.suggest(type_name).text)
