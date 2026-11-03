"""The rules of the resource catalogue route and the suggestion list that need no route (T195).

``GET /resources/types`` and ``GET /resources/suggestions`` are tested end to end in
``tests/contract/test_resource_suggestions_api.py``; this file pins what they are built from:

* the catalogue the UI reads is derived from ``registry.resources.RESOURCE_TYPES`` plus a table of
  words (a description and a target example per type), and the two cannot drift apart unnoticed;
* the filter, the order and the cut of a suggestion list;
* the one function that sends a request with a person's token (``send_with_token``), which the
  attach check and the suggestions share.

All values are placeholders; the tokens are fake.
"""

from __future__ import annotations

import httpx
import pytest

from appmux.admin.resource_suggestions import (
    SUGGESTABLE_TYPES,
    TYPE_PRESENTATION,
    resource_type_list,
    select_suggestions,
)
from appmux.admin.resources import send_with_token
from appmux.admin.schemas import ResourceSuggestion
from appmux.registry.resources import (
    ACCESS_AS_VALUES,
    RESOURCE_TYPES,
    SECRET_TYPE,
    is_valid_target,
)

TOKEN = "person-token-value-for-tests"  # noqa: S105 - a fake value for a test


# --- the catalogue as data ------------------------------------------------------------------


def test_every_catalogue_type_has_its_words_and_no_other_type_has() -> None:
    # A type added to RESOURCE_TYPES without a description and a target example fails here, and
    # so does a row of words for a type that left the catalogue.
    assert set(TYPE_PRESENTATION) == set(RESOURCE_TYPES)


def test_the_list_holds_one_entry_per_catalogue_type_in_the_catalogues_order() -> None:
    entries = resource_type_list().items

    assert [entry.type for entry in entries] == list(RESOURCE_TYPES)


@pytest.mark.parametrize("name", list(RESOURCE_TYPES))
def test_an_entry_repeats_the_label_and_the_levels_of_the_catalogue(name: str) -> None:
    catalogue = RESOURCE_TYPES[name]
    [entry] = [entry for entry in resource_type_list().items if entry.type == name]

    assert entry.label == catalogue.label
    assert entry.permissions == list(catalogue.levels)
    assert entry.description.strip()
    assert entry.target_hint.strip()


@pytest.mark.parametrize("name", list(RESOURCE_TYPES))
def test_the_access_modes_are_those_the_attach_rules_allow(name: str) -> None:
    catalogue = RESOURCE_TYPES[name]
    [entry] = [entry for entry in resource_type_list().items if entry.type == name]

    if name == SECRET_TYPE:
        assert entry.access_as == []  # a secret has no access_as
    elif catalogue.is_verifiable:
        assert entry.access_as == list(ACCESS_AS_VALUES)
    else:
        # shared_sp needs a verified check, and these types have none.
        assert entry.access_as == [mode for mode in ACCESS_AS_VALUES if mode != "shared_sp"]


@pytest.mark.parametrize("name", [name for name, t in RESOURCE_TYPES.items() if t.is_verifiable])
def test_the_target_example_of_a_checked_type_is_a_target_the_attach_form_accepts(
    name: str,
) -> None:
    assert is_valid_target(name, TYPE_PRESENTATION[name].target_hint)


def test_can_suggest_is_true_exactly_for_the_types_that_have_a_list_call() -> None:
    entries = resource_type_list().items

    suggestable = {entry.type for entry in entries if entry.can_suggest}
    assert suggestable == SUGGESTABLE_TYPES
    assert suggestable == {
        "sql_warehouse",
        "serving_endpoint",
        "secret",
        "mlflow_experiment",
        "job",
    }


# --- the filter, the order and the cut ------------------------------------------------------


def suggestion(target: str, label: str) -> ResourceSuggestion:
    return ResourceSuggestion(target=target, label=label, detail=None, permissions=["CAN_USE"])


def test_a_blank_query_matches_everything_and_the_list_is_ordered_by_label_without_case() -> None:
    candidates = [suggestion("3", "beta"), suggestion("1", "Alpha"), suggestion("2", "alpha")]

    chosen, truncated = select_suggestions(candidates, "", limit=10)

    assert [(item.label, item.target) for item in chosen] == [
        ("Alpha", "1"),
        ("alpha", "2"),
        ("beta", "3"),
    ]
    assert truncated is False


def test_the_query_is_a_case_insensitive_substring_of_the_label_or_the_target() -> None:
    candidates = [
        suggestion("wh-001", "Reporting"),
        suggestion("wh-002", "Shared Serverless"),
        suggestion("abc", "Finance"),
    ]

    by_label, _ = select_suggestions(candidates, "SERVER", limit=10)
    by_target, _ = select_suggestions(candidates, "wh-00", limit=10)
    none, _ = select_suggestions(candidates, "nothing like it", limit=10)

    assert [item.label for item in by_label] == ["Shared Serverless"]
    assert [item.label for item in by_target] == ["Reporting", "Shared Serverless"]
    assert none == []


def test_the_query_is_not_a_pattern() -> None:
    candidates = [suggestion("1", "a.c"), suggestion("2", "abc")]

    chosen, _ = select_suggestions(candidates, "a.c", limit=10)

    assert [item.label for item in chosen] == ["a.c"]


def test_exactly_the_limit_is_not_truncated_and_one_more_is() -> None:
    candidates = [suggestion(str(n), f"name-{n:02}") for n in range(5)]

    at_limit, at_limit_truncated = select_suggestions(candidates, "", limit=5)
    over, over_truncated = select_suggestions(candidates, "", limit=4)

    assert (len(at_limit), at_limit_truncated) == (5, False)
    assert [item.label for item in over] == ["name-00", "name-01", "name-02", "name-03"]
    assert over_truncated is True


def test_the_cut_counts_matches_and_not_candidates() -> None:
    candidates = [suggestion(str(n), f"keep-{n}") for n in range(3)] + [
        suggestion(str(n), f"drop-{n}") for n in range(10)
    ]

    chosen, truncated = select_suggestions(candidates, "keep", limit=3)

    assert len(chosen) == 3
    assert truncated is False


# --- the request that carries a person's token ----------------------------------------------


def recording_client(
    answer: httpx.Response | Exception, *, auth: httpx.Auth | None = None
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = httpx.AsyncClient(
        base_url="https://workspace.example.invalid",
        transport=httpx.MockTransport(handler),
        auth=auth,
    )
    return client, requests


@pytest.mark.anyio
async def test_the_request_carries_only_the_persons_token_in_the_authorization_header() -> None:
    client, requests = recording_client(
        httpx.Response(200, json={}), auth=httpx.BasicAuth("another", "credential")
    )

    response = await send_with_token(
        client, TOKEN, "GET", "/api/2.0/things", timeout_s=5.0, params={"q": "x y"}
    )

    assert response is not None and response.status_code == 200
    [request] = requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept"] == "application/json"
    assert request.url.path == "/api/2.0/things"
    assert dict(request.url.params) == {"q": "x y"}


@pytest.mark.anyio
async def test_a_json_body_is_sent_as_given() -> None:
    client, requests = recording_client(httpx.Response(200, json={}))

    await send_with_token(
        client, TOKEN, "POST", "/api/2.0/things", timeout_s=5.0, json={"max_results": 3, "k": "v"}
    )

    [request] = requests
    assert request.method == "POST"
    assert request.read() == b'{"max_results":3,"k":"v"}'


@pytest.mark.anyio
async def test_a_redirect_is_not_followed() -> None:
    client, requests = recording_client(
        httpx.Response(302, headers={"Location": "https://elsewhere.example.invalid/steal"})
    )

    response = await send_with_token(client, TOKEN, "GET", "/api/2.0/things", timeout_s=5.0)

    assert response is not None and response.status_code == 302
    assert len(requests) == 1


@pytest.mark.anyio
async def test_a_transport_failure_is_none_not_an_exception() -> None:
    client, _ = recording_client(httpx.ConnectError("connection refused"))

    response = await send_with_token(client, TOKEN, "GET", "/api/2.0/things", timeout_s=5.0)

    assert response is None
