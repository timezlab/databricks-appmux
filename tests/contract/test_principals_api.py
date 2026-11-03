"""``GET /principals``: suggestions for the Sharing form (T187, contracts/admin-api.md addendum).

The route is served by the real admin app and the real ``DirectoryLookup``; only Databricks is a
stand-in, an ``httpx.MockTransport`` that answers the three SCIM list endpoints. That way a test
that a refused ``q`` "never reaches the HTTP client" checks the transport itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from stub_registry import StubRegistry

from appmux.admin.api import create_admin_app
from appmux.admin.authorizer import make_authorizer
from appmux.admin.principals import PrincipalRoutes
from appmux.admin.schemas import PrincipalList
from appmux.identity.actor import Actor
from appmux.identity.directory import DirectoryLookup

MOUNT: Final = "/_appmux/api/v1"
BASE_URL: Final = "https://workspace.example.invalid"
SCIM: Final = "/api/2.0/preview/scim/v2/"
NOW: Final = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
ALICE: Final = "alice@example.com"  # owns `sales`
BOB: Final = "bob@example.com"  # CAN_USE on `sales` only
CAROL: Final = "carol@example.com"  # CAN_MANAGE on `sales`
DANA: Final = "dana@example.com"  # a platform admin
VISITOR_TOKEN: Final = "placeholder-visitor-token-0001"  # noqa: S105 - a fake value for a test
PLATFORM_TOKEN: Final = "placeholder-platform-token-0002"  # noqa: S105 - a fake value for a test
SALES_BOT: Final = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab"
OTHER_BOT: Final = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
NOT_READ: Final = {"users": "not_read", "groups": "not_read", "service_principals": "not_read"}
EVERYONE_ROW: Final = {"type": "everyone", "name": "*", "display_name": "Everyone", "detail": None}


class FakeGroups:
    async def groups_for(self, actor: Actor) -> frozenset[str] | None:
        return frozenset()


class PlatformToken:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> SecretStr:
        self.calls += 1
        return SecretStr(PLATFORM_TOKEN)


class Workspace:
    """The three SCIM list endpoints. Databricks filters on ``q``; this stand-in does not.

    ``resources`` is what each endpoint lists, cut to the ``count`` asked for. ``refuses`` names
    the tokens an endpoint answers ``403`` to, and ``broken`` the endpoints that answer ``500``.
    """

    def __init__(self) -> None:
        self.resources: dict[str, list[dict[str, object]]] = {
            "Users": [],
            "Groups": [],
            "ServicePrincipals": [],
        }
        self.refuses: dict[str, set[str]] = {}
        self.broken: set[str] = set()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        endpoint = request.url.path.removeprefix(SCIM)
        token = request.headers["authorization"].removeprefix("Bearer ")
        if endpoint in self.broken:
            return httpx.Response(500)
        if token in self.refuses.get(endpoint, set()):
            return httpx.Response(403, json={"detail": "not allowed"})
        listed = self.resources[endpoint][: int(request.url.params["count"])]
        return httpx.Response(200, json={"totalResults": len(listed), "Resources": listed})

    def asked(self) -> dict[str, list[str]]:
        """Endpoint -> the tokens it was asked with, in order."""
        asked: dict[str, list[str]] = {}
        for request in self.requests:
            endpoint = request.url.path.removeprefix(SCIM)
            token = request.headers["authorization"].removeprefix("Bearer ")
            asked.setdefault(endpoint, []).append(token)
        return asked


def seeded_registry() -> StubRegistry:
    registry = StubRegistry(clock=lambda: NOW)
    for slug, owner in (("sales", ALICE), ("docs", "frank@example.com")):
        registry.seed(
            "apps",
            {
                "slug": slug,
                "display_name": slug.title(),
                "owner": owner,
                "source_path": f"/Workspace/Users/{owner}/{slug}",
                "memory_budget_mb": 256,
                "obo_delegated": False,
                "created_by": DANA,
                "created_at": NOW,
                "updated_by": DANA,
                "updated_at": NOW,
            },
        )
    for principal, permission in ((BOB, "CAN_USE"), (CAROL, "CAN_MANAGE")):
        registry.seed(
            "app_grants",
            {
                "app_slug": "sales",
                "principal_type": "user",
                "principal": principal,
                "permission": permission,
                "granted_by": ALICE,
                "granted_at": NOW,
            },
        )
    registry.seed(
        "platform_settings",
        {
            "key": "platform_admins",
            "value": [{"type": "user", "name": DANA}],
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )
    return registry


class Harness:
    def __init__(self) -> None:
        self.workspace = Workspace()
        self.platform = PlatformToken()
        client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(self.workspace))
        registry = seeded_registry()
        admin = create_admin_app(
            registry=registry,
            authorizer=make_authorizer(registry, FakeGroups()),
            route_providers=[
                PrincipalRoutes(DirectoryLookup(client, platform_token=self.platform))
            ],
        )
        self.client = TestClient(Starlette(routes=[Mount(MOUNT, app=admin)]))

    def search(
        self,
        params: Mapping[str, str] | list[tuple[str, str]] | None = None,
        *,
        email: str | None = CAROL,
        token: str | None = VISITOR_TOKEN,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if email is not None:
            headers["X-Forwarded-Email"] = email
        if token is not None:
            headers["X-Forwarded-Access-Token"] = token
        return self.client.get(f"{MOUNT}/principals", params=params, headers=headers)

    def found(
        self,
        params: Mapping[str, str],
        *,
        email: str | None = CAROL,
        token: str | None = VISITOR_TOKEN,
    ) -> PrincipalList:
        response = self.search(params, email=email, token=token)
        assert response.status_code == 200, response.text
        return PrincipalList.model_validate_json(response.content)


@pytest.fixture
def harness() -> Harness:
    return Harness()


def rows(answer: PrincipalList) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in answer.items]


def message_of(response: httpx.Response) -> str:
    body = response.json()
    assert body["error"]["code"] == "bad_request", body
    message = body["error"]["message"]
    assert isinstance(message, str)
    return message


# --- what q may hold ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ['al"ice', "al\\ice", "al)ice", "al(ice", "a*", "a\tb", "naïve", "x" * 65],
    ids=["quote", "backslash", "paren-close", "paren-open", "star", "tab", "non-ascii", "65-chars"],
)
def test_a_query_outside_the_allow_list_is_a_400_that_never_reaches_databricks(
    harness: Harness, query: str
) -> None:
    response = harness.search({"q": query})

    assert response.status_code == 400
    message = message_of(response)
    assert "q " in message
    assert query not in message
    assert harness.workspace.requests == []
    assert harness.platform.calls == 0


def test_the_longest_query_of_allowed_characters_is_sent_as_it_came(harness: Harness) -> None:
    query = "Aa0 ._@+-" * 7 + "a"  # 64 characters

    harness.found({"q": query, "types": "group"})

    (request,) = harness.workspace.requests
    assert request.url.params["filter"] == f'displayName co "{query}"'


@pytest.mark.parametrize(
    "params",
    [
        {"types": "robot"},
        {"types": ""},
        {"types": "user,"},
        {"types": "User"},
        {"limit": "0"},
        {"limit": "51"},
        {"limit": "ten"},
        {"limit": "-1"},
        {"limit": "1.5"},
        {"limit": ""},
        {"limit": "9" * 40},
    ],
    ids=lambda params: "-".join(f"{key}={value[:6]}" for key, value in params.items()),
)
def test_a_types_or_limit_outside_the_contract_is_a_400(
    harness: Harness, params: dict[str, str]
) -> None:
    response = harness.search({"q": "ali", **params})

    assert response.status_code == 400
    (name,) = params
    assert message_of(response).startswith(name)
    assert harness.workspace.requests == []


@pytest.mark.parametrize("name", ["q", "types", "limit"])
def test_a_parameter_sent_twice_is_a_400(harness: Harness, name: str) -> None:
    value = {"q": "ali", "types": "user", "limit": "5"}[name]

    response = harness.search([(name, value), (name, value)])

    assert response.status_code == 400
    assert message_of(response) == f"{name} may be sent once"
    assert harness.workspace.requests == []


# --- who may ask --------------------------------------------------------------------------------


@pytest.mark.parametrize("email", [ALICE, CAROL, DANA])
def test_an_owner_a_manager_or_a_platform_admin_may_search(harness: Harness, email: str) -> None:
    assert harness.search({"q": "a"}, email=email).status_code == 200


def test_someone_who_manages_no_app_is_refused_before_anything_is_read(harness: Harness) -> None:
    response = harness.search({"q": "a"}, email=BOB)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert harness.workspace.requests == []
    assert harness.platform.calls == 0


def test_a_request_without_an_identity_is_a_401(harness: Harness) -> None:
    assert harness.search({"q": "a"}, email=None).status_code == 401


# --- what is read and how it is shown -----------------------------------------------------------


def test_an_empty_query_reads_nothing_and_offers_only_everyone(harness: Harness) -> None:
    for params in ({}, {"q": ""}, {"q": "   "}):
        answer = harness.found(params)

        assert rows(answer) == [EVERYONE_ROW]
        assert answer.truncated is False
        assert answer.sources.model_dump() == NOT_READ
    assert harness.workspace.requests == []


def test_an_empty_query_without_everyone_in_the_types_offers_nothing(harness: Harness) -> None:
    answer = harness.found({"types": "user,group"})

    assert answer.items == []
    assert answer.sources.model_dump() == NOT_READ


def test_each_directory_is_asked_once_with_the_person_s_token_and_one_more_than_the_limit(
    harness: Harness,
) -> None:
    answer = harness.found({"q": "sal", "limit": "4"})

    assert answer.sources.model_dump() == {
        "users": "caller_token",
        "groups": "caller_token",
        "service_principals": "caller_token",
    }
    assert harness.workspace.asked() == {
        "Users": [VISITOR_TOKEN],
        "Groups": [VISITOR_TOKEN],
        "ServicePrincipals": [VISITOR_TOKEN],
    }
    filters = {
        r.url.path.removeprefix(SCIM): dict(r.url.params) for r in harness.workspace.requests
    }
    assert filters["Users"]["filter"] == 'userName co "sal" or displayName co "sal"'
    assert filters["Groups"]["filter"] == 'displayName co "sal"'
    assert filters["ServicePrincipals"]["filter"] == 'displayName co "sal"'
    assert {params["count"] for params in filters.values()} == {"5"}
    assert harness.platform.calls == 0


def test_users_groups_and_service_principals_become_suggestions(harness: Harness) -> None:
    harness.workspace.resources.update(
        Users=[
            {"userName": "Erin.Ng@Example.com", "displayName": "Erin Ng"},
            {"userName": "sam@example.com"},
            {"userName": "tom@example.com", "displayName": "   "},
        ],
        Groups=[{"displayName": "sales-team", "id": "7"}],
        ServicePrincipals=[
            {"applicationId": SALES_BOT.upper(), "displayName": "sales-agent"},
            {"applicationId": OTHER_BOT},
        ],
    )

    answer = harness.found({"q": "s", "limit": "50"})

    assert rows(answer) == [
        {"type": "group", "name": "sales-team", "display_name": None, "detail": None},
        {
            "type": "user",
            "name": "erin.ng@example.com",
            "display_name": "Erin Ng",
            "detail": "erin.ng@example.com",
        },
        {"type": "user", "name": "sam@example.com", "display_name": None, "detail": None},
        {"type": "user", "name": "tom@example.com", "display_name": None, "detail": None},
        {"type": "service_principal", "name": OTHER_BOT, "display_name": None, "detail": OTHER_BOT},
        {
            "type": "service_principal",
            "name": SALES_BOT,
            "display_name": "sales-agent",
            "detail": SALES_BOT,
        },
    ]
    assert answer.truncated is False


def test_a_directory_entry_that_no_grant_could_name_is_left_out(harness: Harness) -> None:
    harness.workspace.resources.update(
        Users=[
            {"userName": "not-an-email", "displayName": "Robot"},
            {"displayName": "No user name"},
            {"userName": "ok@example.com"},
        ],
        Groups=[{"displayName": "*"}, {"displayName": "  "}, {"id": "3"}, {"displayName": "ops"}],
        ServicePrincipals=[
            {"applicationId": "sales-bot", "displayName": "not a UUID"},
            {"displayName": "no application id"},
            {"applicationId": "{" + SALES_BOT + "}"},
        ],
    )

    answer = harness.found({"q": "o", "limit": "50"})

    assert [(item.type, item.name) for item in answer.items] == [
        ("group", "ops"),
        ("user", "ok@example.com"),
    ]


def test_everyone_comes_first_then_groups_users_and_service_principals_each_by_shown_name(
    harness: Harness,
) -> None:
    harness.workspace.resources.update(
        Users=[
            {"userName": "zed@example.com", "displayName": "Eve Zed"},
            {"userName": "adam@example.com", "displayName": "every Adam"},
            {"userName": "eva@example.com"},
        ],
        Groups=[{"displayName": "everyone-eu"}, {"displayName": "Eng"}],
        ServicePrincipals=[
            {"applicationId": SALES_BOT, "displayName": "eval-bot"},
            {"applicationId": OTHER_BOT, "displayName": "Etl-bot"},
        ],
    )

    answer = harness.found({"q": "E", "limit": "50"})

    assert [item.display_name or item.name for item in answer.items] == [
        "Everyone",
        "Eng",
        "everyone-eu",
        "eva@example.com",
        "Eve Zed",
        "every Adam",
        "Etl-bot",
        "eval-bot",
    ]


@pytest.mark.parametrize(
    ("query", "is_listed"),
    [("e", True), ("EVERY", True), ("everyone", True), ("very", False), ("everyone2", False)],
)
def test_everyone_is_offered_for_a_query_that_starts_its_name(
    harness: Harness, query: str, is_listed: bool
) -> None:
    answer = harness.found({"q": query})

    is_everyone_first = bool(answer.items) and answer.items[0].type == "everyone"
    assert is_everyone_first is is_listed


def test_only_the_types_asked_for_are_read_and_offered(harness: Harness) -> None:
    harness.workspace.resources.update(
        Users=[{"userName": "eve@example.com"}], Groups=[{"displayName": "eng"}]
    )

    answer = harness.found({"q": "e", "types": "user,service_principal"})

    assert [(item.type, item.name) for item in answer.items] == [("user", "eve@example.com")]
    assert answer.sources.model_dump() == {
        "users": "caller_token",
        "groups": "not_read",
        "service_principals": "caller_token",
    }
    assert set(harness.workspace.asked()) == {"Users", "ServicePrincipals"}


def test_the_limit_cuts_the_list_after_ordering_and_says_it_was_cut(harness: Harness) -> None:
    harness.workspace.resources.update(
        Users=[{"userName": "ann@example.com"}],
        Groups=[{"displayName": "b-team"}, {"displayName": "a-team"}],
    )

    cut = harness.found({"q": "a", "limit": "2"})
    whole = harness.found({"q": "a", "limit": "3"})

    assert [item.name for item in cut.items] == ["a-team", "b-team"]
    assert cut.truncated is True
    assert [item.name for item in whole.items] == ["a-team", "b-team", "ann@example.com"]
    assert whole.truncated is False


def test_one_directory_with_more_than_the_limit_is_truncated(harness: Harness) -> None:
    harness.workspace.resources["Groups"] = [{"displayName": f"team-{n}"} for n in range(5)]

    answer = harness.found({"q": "team", "types": "group", "limit": "3"})

    assert [item.name for item in answer.items] == ["team-0", "team-1", "team-2"]
    assert answer.truncated is True


def test_the_default_limit_is_ten(harness: Harness) -> None:
    harness.workspace.resources["Groups"] = [{"displayName": f"team-{n:02}"} for n in range(20)]

    answer = harness.found({"q": "team"})

    assert len(answer.items) == 10
    assert answer.truncated is True


# --- which token, and what happens when none works ----------------------------------------------


def test_a_directory_that_refuses_the_person_s_token_is_read_with_the_platform_token(
    harness: Harness,
) -> None:
    harness.workspace.resources["Users"] = [{"userName": "ann@example.com"}]
    harness.workspace.refuses["Users"] = {VISITOR_TOKEN}

    answer = harness.found({"q": "a"})

    assert [item.name for item in answer.items] == ["ann@example.com"]
    assert answer.sources.model_dump() == {
        "users": "platform_token",
        "groups": "caller_token",
        "service_principals": "caller_token",
    }
    assert harness.workspace.asked()["Users"] == [VISITOR_TOKEN, PLATFORM_TOKEN]


def test_without_the_person_s_token_every_directory_is_read_with_the_platform_token(
    harness: Harness,
) -> None:
    harness.workspace.resources["Groups"] = [{"displayName": "ann-team"}]

    answer = harness.found({"q": "a"}, token=None)

    assert [item.name for item in answer.items] == ["ann-team"]
    assert set(answer.sources.model_dump().values()) == {"platform_token"}
    assert all(tokens == [PLATFORM_TOKEN] for tokens in harness.workspace.asked().values())


def test_a_directory_neither_token_can_read_is_unavailable_and_the_rest_still_answers(
    harness: Harness,
) -> None:
    harness.workspace.resources.update(
        Users=[{"userName": "ann@example.com"}], Groups=[{"displayName": "a-team"}]
    )
    harness.workspace.refuses["Users"] = {VISITOR_TOKEN, PLATFORM_TOKEN}
    harness.workspace.broken.add("ServicePrincipals")

    response = harness.search({"q": "a"})

    assert response.status_code == 200
    answer = PrincipalList.model_validate_json(response.content)
    assert [item.name for item in answer.items] == ["a-team"]
    assert answer.sources.model_dump() == {
        "users": "unavailable",
        "groups": "caller_token",
        "service_principals": "unavailable",
    }
    assert VISITOR_TOKEN not in response.text and PLATFORM_TOKEN not in response.text


def test_when_no_directory_can_be_read_everyone_is_still_offered(harness: Harness) -> None:
    harness.workspace.broken.update({"Users", "Groups", "ServicePrincipals"})

    answer = harness.found({"q": "ev"})

    assert rows(answer) == [EVERYONE_ROW]
    assert set(answer.sources.model_dump().values()) == {"unavailable"}
