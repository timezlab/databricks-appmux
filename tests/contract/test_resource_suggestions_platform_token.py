"""``GET /resources/suggestions`` with appmux's own access as the second try (T211).

An App's ``user_api_scopes`` cannot hold ``secrets``, ``mlflow``, ``jobs``, ``workspace``,
``unity-catalog`` or ``catalog.volumes``, so the person's own token is refused for secrets, MLflow
experiments, jobs, volumes and functions however the admin configured the App. The picker then asks
again with the platform's service-principal token (the one ``GET /principals`` falls back to), and
says so with ``source: "platform_token"``: the list shows what appmux can read, not what the
person can.

The person's token is always tried first. Pinned here:

* the table of answers that are retried (``403``, and ``400``/``403``/``404`` naming a missing
  scope) and the ones that are not (``401``, a transport failure, any other status, an unreadable
  body, a secret scope that does not exist);
* the retry starts the list again from its first page and carries only the platform token;
* when the second try fails too, or the platform has no token to give, the answer is the person's
  own refusal, and nothing the platform said is repeated;
* a person whose token did not arrive is listed with the platform token when it can be had;
* neither token, and no text from the platform, reaches the answer.

All names are placeholders; both tokens are fake values.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import httpx
import pytest
from edge_harness import VISITOR_TOKEN
from suggestion_harness import (
    DENIED_TEXT,
    PLATFORM_TOKEN,
    ROOT_CALLS,
    SECOND_ROOT_CALLS,
    PlatformTokenProvider,
    Suggester,
    Workspace,
    targets_of,
)

from appmux.admin.resource_suggestions import SUGGESTABLE_TYPES

NO_TOKEN: Final = {"X-Forwarded-Access-Token": ""}
SCOPE_TEXT: Final = "Invalid scope, required scopes: secrets"
CATALOGS_PATH: Final = "/api/2.1/unity-catalog/catalogs"
VOLUMES_PATH: Final = "/api/2.1/unity-catalog/volumes"
WAREHOUSES: Final = {
    "warehouses": [
        {"id": "1234567890abcdef", "name": "Shared Serverless", "state": "RUNNING"},
        {"id": "fedcba0987654321", "name": "Reporting"},
    ]
}

Respond = Callable[[httpx.Request], httpx.Response]


def refusal(status: int, message: str) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(status, json={"error_code": "X", "message": message})


def for_platform_only(listing: object, refused: Callable[[], httpx.Response]) -> Respond:
    """Answers ``listing`` to the platform token and ``refused()`` to any other."""

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == f"Bearer {PLATFORM_TOKEN}":
            return httpx.Response(200, json=listing)
        return refused()

    return answer


def rooted(type_name: str, answer: Respond) -> Workspace:
    """A workspace that gives ``answer`` for every first call of ``type_name``."""
    method, path, _ = ROOT_CALLS[type_name]
    answers = {(method, path): answer}
    answers.update(
        {(method, path): answer for method, path, _ in SECOND_ROOT_CALLS.get(type_name, [])}
    )
    return Workspace(answers)


def assert_leaks_nothing(response: httpx.Response) -> None:
    for text in (VISITOR_TOKEN, PLATFORM_TOKEN, DENIED_TEXT, "internal-secret-name", "Traceback"):
        assert text not in response.text


RETRIED: Final = [
    pytest.param(refusal(403, SCOPE_TEXT), id="403 naming a scope"),
    pytest.param(refusal(403, DENIED_TEXT), id="403 for any other reason"),
    pytest.param(refusal(400, SCOPE_TEXT), id="400 naming a scope"),
    pytest.param(refusal(404, SCOPE_TEXT), id="404 naming a scope"),
]


# --- what is retried ------------------------------------------------------------------------


@pytest.mark.parametrize("refused", RETRIED)
def test_a_refusal_of_the_persons_token_is_retried_with_the_platform_token(
    refused: Callable[[], httpx.Response],
) -> None:
    provider = PlatformTokenProvider()
    workspace = rooted("sql_warehouse", for_platform_only(WAREHOUSES, refused))
    suggester = Suggester(workspace, platform_token=provider)

    response = suggester.suggest("sql_warehouse")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["source"], body["truncated"], body["note"]) == ("platform_token", False, None)
    assert targets_of(response) == ["fedcba0987654321", "1234567890abcdef"]
    assert workspace.bearers == [VISITOR_TOKEN, PLATFORM_TOKEN]  # the person first, then appmux
    assert provider.asked == 1
    assert_leaks_nothing(response)


def refuse_everything_with(
    suggester: Suggester, answer: Callable[[httpx.Request], httpx.Response]
) -> Suggester:
    suggester.workspace.answer_everything_with(answer)
    return suggester


def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(lambda request: httpx.Response(401, json={}), id="401"),
        pytest.param(lambda request: httpx.Response(404, json={"message": DENIED_TEXT}), id="404"),
        pytest.param(lambda request: httpx.Response(400, json={"message": DENIED_TEXT}), id="400"),
        pytest.param(lambda request: httpx.Response(429, json={}), id="429"),
        pytest.param(lambda request: httpx.Response(500, json={}), id="500"),
        pytest.param(lambda request: httpx.Response(503, json={}), id="503"),
        pytest.param(
            lambda request: httpx.Response(302, headers={"Location": "https://elsewhere.invalid"}),
            id="302",
        ),
        pytest.param(unreachable, id="no answer"),
        pytest.param(lambda request: httpx.Response(200, json=["a", "list"]), id="odd shape"),
        pytest.param(lambda request: httpx.Response(200, text="<html>"), id="not json"),
    ],
)
@pytest.mark.parametrize("type_name", ["sql_warehouse", "secret", "uc_table", "lakebase_database"])
def test_any_other_answer_is_not_retried_and_the_platform_token_is_not_even_asked_for(
    type_name: str, answer: Respond
) -> None:
    provider = PlatformTokenProvider()
    suggester = refuse_everything_with(
        Suggester(platform_token=provider),
        answer,
    )

    response = suggester.suggest(type_name)

    body = response.json()
    assert (response.status_code, body["source"], body["items"]) == (200, "unavailable", [])
    assert provider.asked == 0
    assert set(suggester.workspace.bearers) == {VISITOR_TOKEN}  # never the platform token
    assert_leaks_nothing(response)


def test_a_secret_scope_that_is_not_there_is_not_retried() -> None:
    provider = PlatformTokenProvider()
    suggester = Suggester(platform_token=provider)  # every path answers 404

    body = suggester.suggest("secret", "nobody-has-this/").json()

    assert (body["source"], body["items"]) == ("caller_token", [])
    assert "scope" in body["note"]
    assert provider.asked == 0
    assert len(suggester.workspace.requests) == 1


def test_a_list_the_persons_token_can_read_never_asks_for_the_platform_token() -> None:
    provider = PlatformTokenProvider()
    workspace = rooted("sql_warehouse", lambda request: httpx.Response(200, json=WAREHOUSES))
    suggester = Suggester(workspace, platform_token=provider)

    body = suggester.suggest("sql_warehouse").json()

    assert body["source"] == "caller_token"
    assert provider.asked == 0
    assert workspace.bearers == [VISITOR_TOKEN]


# --- every type ----------------------------------------------------------------------------


@pytest.mark.parametrize("type_name", sorted(SUGGESTABLE_TYPES))
def test_every_type_is_listed_with_the_platform_token_after_the_persons_is_refused(
    type_name: str,
) -> None:
    provider = PlatformTokenProvider()
    workspace = rooted(type_name, for_platform_only({}, refusal(403, SCOPE_TEXT)))
    suggester = Suggester(workspace, platform_token=provider)

    response = suggester.suggest(type_name)

    assert response.json() == {
        "items": [],
        "truncated": False,
        "source": "platform_token",
        "note": None,
    }
    assert workspace.bearers[0] == VISITOR_TOKEN
    assert workspace.bearers[-1] == PLATFORM_TOKEN
    assert provider.asked == 1


def test_a_volume_step_the_app_cannot_declare_the_scope_for_is_listed_with_the_platform_token() -> (
    None
):
    # `catalog.volumes` is one of the scopes `user_api_scopes` refuses, so the person's token
    # never lists volumes however the App is set up.
    listing = {"volumes": [{"name": "files", "volume_type": "MANAGED"}]}
    workspace = Workspace(
        {
            ("GET", VOLUMES_PATH): for_platform_only(
                listing, refusal(403, "Invalid scope, required scopes: catalog.volumes")
            )
        }
    )
    suggester = Suggester(workspace, platform_token=PlatformTokenProvider())

    response = suggester.suggest("uc_volume", "/Volumes/main/sales/")

    body = response.json()
    assert (body["source"], targets_of(response)) == (
        "platform_token",
        ["/Volumes/main/sales/files"],
    )
    persons, platforms = workspace.requests
    assert dict(persons.url.params) == dict(platforms.url.params)
    assert (persons.url.path, platforms.url.path) == (VOLUMES_PATH, VOLUMES_PATH)


def test_the_pages_are_read_again_from_the_first_with_the_platform_token_only() -> None:
    pages = {None: {"catalogs": [{"name": "alpha"}], "next_page_token": "p1"}}
    pages["p1"] = {"catalogs": [{"name": "beta"}]}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] != f"Bearer {PLATFORM_TOKEN}":
            return refusal(403, SCOPE_TEXT)()
        return httpx.Response(200, json=pages[request.url.params.get("page_token")])

    workspace = Workspace({("GET", CATALOGS_PATH): answer})
    suggester = Suggester(workspace, platform_token=PlatformTokenProvider())

    response = suggester.suggest("uc_table")

    assert response.json()["source"] == "platform_token"
    assert targets_of(response) == ["alpha.", "beta."]
    tokens = [request.url.params.get("page_token") for request in workspace.requests]
    assert tokens == [None, None, "p1"]  # the person's refused first page, then both pages again
    assert workspace.bearers == [VISITOR_TOKEN, PLATFORM_TOKEN, PLATFORM_TOKEN]


def test_a_refusal_on_a_later_page_starts_the_whole_list_again_with_the_platform_token() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == f"Bearer {PLATFORM_TOKEN}":
            return httpx.Response(200, json={"catalogs": [{"name": "everything"}]})
        if request.url.params.get("page_token") is None:
            return httpx.Response(
                200, json={"catalogs": [{"name": "mine"}], "next_page_token": "p1"}
            )
        return refusal(403, DENIED_TEXT)()

    suggester = Suggester(
        Workspace({("GET", CATALOGS_PATH): answer}), platform_token=PlatformTokenProvider()
    )

    response = suggester.suggest("uc_table")

    assert targets_of(response) == ["everything."]  # nothing of the person's half-read list is kept
    assert response.json()["source"] == "platform_token"


def test_the_answer_with_the_platform_token_is_filtered_cut_and_marked_like_any_other() -> None:
    many = {"warehouses": [{"id": f"w{n:02}", "name": f"name-{n:02}"} for n in range(30)]}
    workspace = rooted("sql_warehouse", for_platform_only(many, refusal(403, SCOPE_TEXT)))
    suggester = Suggester(workspace, platform_token=PlatformTokenProvider())

    body = suggester.suggest("sql_warehouse", "name-", limit="10").json()

    assert [item["label"] for item in body["items"]] == [f"name-{n:02}" for n in range(10)]
    assert (body["truncated"], body["source"]) == (True, "platform_token")


# --- when the second try fails too ----------------------------------------------------------


@pytest.mark.parametrize(
    "platform_answer",
    [
        pytest.param(refusal(403, DENIED_TEXT), id="refused"),
        pytest.param(refusal(500, DENIED_TEXT), id="failed"),
        pytest.param(refusal(404, DENIED_TEXT), id="not found"),
    ],
)
def test_when_the_platform_is_refused_too_the_answer_is_the_persons_own_refusal(
    platform_answer: Callable[[], httpx.Response],
) -> None:
    provider = PlatformTokenProvider()

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == f"Bearer {PLATFORM_TOKEN}":
            return platform_answer()
        return refusal(403, SCOPE_TEXT)()

    workspace = rooted("sql_warehouse", answer)
    suggester = Suggester(workspace, platform_token=provider)

    response = suggester.suggest("sql_warehouse")

    body = response.json()
    assert (body["source"], body["items"]) == ("unavailable", [])
    assert '"secrets"' in body["note"]  # what the person's own call said
    assert workspace.bearers == [VISITOR_TOKEN, PLATFORM_TOKEN]
    assert_leaks_nothing(response)


def test_when_the_platform_does_not_answer_the_answer_is_the_persons_own_refusal() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == f"Bearer {PLATFORM_TOKEN}":
            raise httpx.ConnectError("connection refused", request=request)
        return refusal(403, SCOPE_TEXT)()

    suggester = Suggester(rooted("sql_warehouse", answer), platform_token=PlatformTokenProvider())

    body = suggester.suggest("sql_warehouse").json()

    assert (body["source"], body["items"]) == ("unavailable", [])
    assert '"secrets"' in body["note"]


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(PlatformTokenProvider(error=RuntimeError("internal-secret-name")), id="fails"),
        pytest.param(PlatformTokenProvider(""), id="blank"),
        pytest.param(PlatformTokenProvider("   "), id="blank with spaces"),
    ],
)
def test_a_platform_that_has_no_token_to_give_leaves_the_persons_own_refusal(
    provider: PlatformTokenProvider,
) -> None:
    workspace = rooted("sql_warehouse", lambda request: refusal(403, SCOPE_TEXT)())
    suggester = Suggester(workspace, platform_token=provider)

    response = suggester.suggest("sql_warehouse")

    body = response.json()
    assert (response.status_code, body["source"], body["items"]) == (200, "unavailable", [])
    assert '"secrets"' in body["note"]
    assert provider.asked == 1
    assert workspace.bearers == [VISITOR_TOKEN]  # nothing was sent for appmux
    assert_leaks_nothing(response)


def test_without_a_platform_token_provider_a_refusal_is_the_answer_it_was_before() -> None:
    suggester = Suggester(rooted("sql_warehouse", lambda request: refusal(403, SCOPE_TEXT)()))

    body = suggester.suggest("sql_warehouse").json()

    assert (body["source"], body["items"]) == ("unavailable", [])
    assert len(suggester.workspace.requests) == 1


# --- a person whose token did not arrive ----------------------------------------------------


def test_a_person_whose_token_did_not_arrive_is_listed_with_the_platform_token() -> None:
    provider = PlatformTokenProvider()
    workspace = rooted("sql_warehouse", lambda request: httpx.Response(200, json=WAREHOUSES))
    suggester = Suggester(workspace, platform_token=provider)

    response = suggester.suggest("sql_warehouse", headers=NO_TOKEN)

    assert response.json()["source"] == "platform_token"
    assert workspace.bearers == [PLATFORM_TOKEN]
    assert len(targets_of(response)) == 2


def test_a_person_whose_token_did_not_arrive_and_a_platform_with_none_gets_the_old_note() -> None:
    provider = PlatformTokenProvider(error=RuntimeError("internal-secret-name"))
    suggester = Suggester(platform_token=provider)

    response = suggester.suggest("sql_warehouse", headers=NO_TOKEN)

    body = response.json()
    assert (body["source"], body["items"]) == ("unavailable", [])
    assert "access token" in body["note"]
    assert suggester.workspace.requests == []
    assert_leaks_nothing(response)


# --- Lakebase asks twice --------------------------------------------------------------------


def test_one_refused_lakebase_list_keeps_the_other_and_is_not_retried() -> None:
    provider = PlatformTokenProvider()
    workspace = Workspace(
        {
            ("GET", ROOT_CALLS["lakebase_database"][1]): (
                200,
                {"projects": [{"name": "projects/my-project"}]},
            ),
            SECOND_ROOT_CALLS["lakebase_database"][0][:2]: (403, {"message": DENIED_TEXT}),
        }
    )
    suggester = Suggester(workspace, platform_token=provider)

    response = suggester.suggest("lakebase_database")

    body = response.json()
    assert (body["source"], targets_of(response)) == ("caller_token", ["projects/my-project/"])
    assert "Provisioned" in body["note"]
    assert provider.asked == 0
