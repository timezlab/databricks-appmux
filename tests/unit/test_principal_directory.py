"""Directory reads for the Sharing form: SCIM users, groups and service principals (T187).

``DirectoryLookup.search`` asks one SCIM list endpoint of the workspace with a ``co`` filter,
first with the person's own token and then, when Databricks refuses that token (``401`` or
``403``) or there is none, with the platform's service-principal token. A directory that neither
token can read is ``unavailable``, never an error. Databricks is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import logging
from collections import deque

import httpx
import pytest
from pydantic import SecretStr

from appmux.identity.directory import (
    DIRECTORY_REQUEST_TIMEOUT_S,
    DirectoryAnswer,
    DirectoryEntry,
    DirectoryLookup,
)

pytestmark = pytest.mark.anyio

BASE_URL = "https://workspace.example.invalid"
# Obviously fake tokens; the tests check that they never show up in a log record.
CALLER_MARKER = "placeholder-caller-token-0001"
PLATFORM_MARKER = "placeholder-platform-token-0002"
DIRECTORY_LOGGER = "appmux.identity.directory"
SALES_BOT = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab"

Reply = httpx.Response | Exception


class ScimStub:
    """Plays scripted Databricks answers, in order, and records every request it receives."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._replies: deque[Reply] = deque()

    def reply(self, *replies: Reply) -> None:
        self._replies.extend(replies)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError(f"unexpected request {request.method} {request.url.path}")
        reply = self._replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply


class PlatformToken:
    """The platform token provider: counts its calls and can be told to fail."""

    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    async def __call__(self) -> SecretStr:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SecretStr(PLATFORM_MARKER)


def scim_list(*resources: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
            "totalResults": len(resources),
            "startIndex": 1,
            "itemsPerPage": len(resources),
            "Resources": list(resources),
        },
    )


@pytest.fixture
def stub() -> ScimStub:
    return ScimStub()


@pytest.fixture
def platform() -> PlatformToken:
    return PlatformToken()


@pytest.fixture
def lookup(stub: ScimStub, platform: PlatformToken) -> DirectoryLookup:
    client = httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(stub))
    return DirectoryLookup(client, platform_token=platform)


def caller() -> SecretStr:
    return SecretStr(CALLER_MARKER)


def bearer_of(request: httpx.Request) -> str:
    return request.headers["authorization"]


# --- what is asked ------------------------------------------------------------------------------


async def test_users_are_matched_on_the_user_name_or_the_display_name(
    lookup: DirectoryLookup, stub: ScimStub
) -> None:
    stub.reply(scim_list({"userName": "alice@example.com", "displayName": "Alice Ng"}))

    answer = await lookup.search("users", "ali", count=11, caller_token=caller())

    assert answer == DirectoryAnswer(
        source="caller_token",
        entries=(DirectoryEntry(user_name="alice@example.com", display_name="Alice Ng"),),
    )
    (request,) = stub.requests
    assert request.method == "GET"
    assert request.url.path == "/api/2.0/preview/scim/v2/Users"
    assert dict(request.url.params) == {
        "filter": 'userName co "ali" or displayName co "ali"',
        "count": "11",
        "attributes": "userName,displayName",
    }
    assert bearer_of(request) == f"Bearer {CALLER_MARKER}"


async def test_groups_are_matched_on_the_display_name(
    lookup: DirectoryLookup, stub: ScimStub
) -> None:
    stub.reply(scim_list({"displayName": "sales-team", "id": "123"}))

    answer = await lookup.search("groups", "Sales Team", count=3, caller_token=caller())

    assert answer.entries == (DirectoryEntry(display_name="sales-team"),)
    (request,) = stub.requests
    assert request.url.path == "/api/2.0/preview/scim/v2/Groups"
    assert dict(request.url.params) == {
        "filter": 'displayName co "Sales Team"',
        "count": "3",
        "attributes": "displayName",
    }


async def test_service_principals_are_matched_on_the_display_name(
    lookup: DirectoryLookup, stub: ScimStub
) -> None:
    stub.reply(scim_list({"applicationId": SALES_BOT, "displayName": "sales-agent"}))

    answer = await lookup.search("service_principals", "agent", count=5, caller_token=caller())

    assert answer.entries == (DirectoryEntry(application_id=SALES_BOT, display_name="sales-agent"),)
    (request,) = stub.requests
    assert request.url.path == "/api/2.0/preview/scim/v2/ServicePrincipals"
    assert dict(request.url.params) == {
        "filter": 'displayName co "agent"',
        "count": "5",
        "attributes": "applicationId,displayName",
    }


async def test_the_request_carries_only_the_token_and_has_a_short_timeout(
    lookup: DirectoryLookup, stub: ScimStub
) -> None:
    stub.reply(scim_list())

    await lookup.search("groups", "x", count=1, caller_token=caller())

    (request,) = stub.requests
    assert set(request.headers) <= {"host", "authorization"}
    assert request.extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), DIRECTORY_REQUEST_TIMEOUT_S
    )


async def test_an_entry_keeps_what_it_has_and_an_empty_answer_has_no_entries(
    lookup: DirectoryLookup, stub: ScimStub
) -> None:
    stub.reply(
        scim_list({"userName": "bob@example.com"}, {"displayName": "Nameless"}, {"id": "9"}),
        httpx.Response(200, json={"totalResults": 0}),
    )

    first = await lookup.search("users", "b", count=10, caller_token=caller())
    second = await lookup.search("users", "zz", count=10, caller_token=caller())

    assert first.entries == (
        DirectoryEntry(user_name="bob@example.com"),
        DirectoryEntry(display_name="Nameless"),
        DirectoryEntry(),
    )
    assert second == DirectoryAnswer(source="caller_token", entries=())


# --- which token --------------------------------------------------------------------------------


@pytest.mark.parametrize("refusal", [401, 403])
async def test_a_refused_caller_token_falls_back_to_the_platform_token(
    lookup: DirectoryLookup, stub: ScimStub, platform: PlatformToken, refusal: int
) -> None:
    stub.reply(httpx.Response(refusal, json={"detail": "no"}), scim_list({"displayName": "ops"}))

    answer = await lookup.search("groups", "op", count=2, caller_token=caller())

    assert answer == DirectoryAnswer(
        source="platform_token", entries=(DirectoryEntry(display_name="ops"),)
    )
    assert [bearer_of(r) for r in stub.requests] == [
        f"Bearer {CALLER_MARKER}",
        f"Bearer {PLATFORM_MARKER}",
    ]
    assert stub.requests[0].url == stub.requests[1].url
    assert platform.calls == 1


@pytest.mark.parametrize(
    "token",
    [None, SecretStr(""), SecretStr("two words"), SecretStr("line\nbreak")],
    ids=["none", "empty", "space", "newline"],
)
async def test_no_usable_caller_token_goes_straight_to_the_platform_token(
    lookup: DirectoryLookup, stub: ScimStub, token: SecretStr | None
) -> None:
    stub.reply(scim_list())

    answer = await lookup.search("users", "a", count=1, caller_token=token)

    assert answer.source == "platform_token"
    assert [bearer_of(r) for r in stub.requests] == [f"Bearer {PLATFORM_MARKER}"]


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(500),
        httpx.Response(429),
        httpx.Response(400, json={"detail": "bad filter"}),
        httpx.Response(302, headers={"Location": "https://elsewhere.example.invalid/"}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"Resources": [{"displayName": 5}]}),
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
    ],
    ids=["500", "429", "400", "302", "not-json", "bad-shape", "connect", "timeout"],
)
async def test_any_other_failure_of_the_caller_token_is_unavailable_without_a_second_try(
    lookup: DirectoryLookup, stub: ScimStub, platform: PlatformToken, reply: Reply
) -> None:
    stub.reply(reply)

    answer = await lookup.search("groups", "x", count=1, caller_token=caller())

    assert answer == DirectoryAnswer(source="unavailable", entries=())
    assert len(stub.requests) == 1
    assert platform.calls == 0


async def test_a_platform_token_that_cannot_be_had_is_unavailable(
    lookup: DirectoryLookup, stub: ScimStub, platform: PlatformToken
) -> None:
    platform.error = RuntimeError("the token endpoint broke")

    answer = await lookup.search("users", "a", count=1, caller_token=None)

    assert answer == DirectoryAnswer(source="unavailable", entries=())
    assert stub.requests == []


@pytest.mark.parametrize("refusal", [401, 403, 500])
async def test_a_refused_platform_token_is_unavailable(
    lookup: DirectoryLookup, stub: ScimStub, refusal: int
) -> None:
    stub.reply(httpx.Response(401), httpx.Response(refusal))

    answer = await lookup.search("service_principals", "a", count=1, caller_token=caller())

    assert answer == DirectoryAnswer(source="unavailable", entries=())
    assert len(stub.requests) == 2


async def test_no_token_reaches_a_log_record(
    lookup: DirectoryLookup,
    stub: ScimStub,
    platform: PlatformToken,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=DIRECTORY_LOGGER)
    stub.reply(
        httpx.Response(403),
        httpx.ConnectError(f"refused with {PLATFORM_MARKER}"),
        httpx.Response(500, text=CALLER_MARKER),
    )
    await lookup.search("users", "a", count=1, caller_token=caller())
    await lookup.search("users", "a", count=1, caller_token=caller())
    platform.error = RuntimeError(f"token {PLATFORM_MARKER}")
    await lookup.search("users", "a", count=1, caller_token=None)

    records = [r for r in caplog.records if r.name == DIRECTORY_LOGGER]
    assert records, "a failed read is logged"
    for record in records:
        text = record.getMessage() + repr(record.args) + str(record.exc_info)
        assert CALLER_MARKER not in text and PLATFORM_MARKER not in text


# --- what is refused before anything is sent ---------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ['a"b', "a\\b", "a)b", "a(b", "", "   ", "x" * 65, "a\nb", "é", "*"],
    ids=[
        "quote",
        "backslash",
        "paren-close",
        "paren-open",
        "empty",
        "blank",
        "65",
        "newline",
        "non-ascii",
        "star",
    ],
)
async def test_a_query_outside_the_allow_list_is_refused_and_nothing_is_sent(
    lookup: DirectoryLookup, stub: ScimStub, platform: PlatformToken, query: str
) -> None:
    with pytest.raises(ValueError, match="query"):
        await lookup.search("users", query, count=1, caller_token=caller())

    assert stub.requests == []
    assert platform.calls == 0


async def test_the_longest_allowed_query_is_sent(lookup: DirectoryLookup, stub: ScimStub) -> None:
    stub.reply(scim_list())
    query = "Aa0 ._@+-" * 7 + "a"  # 64 characters, every allowed kind

    await lookup.search("groups", query, count=1, caller_token=caller())

    assert stub.requests[0].url.params["filter"] == f'displayName co "{query}"'


def test_a_client_with_a_request_hook_is_refused() -> None:
    async def hook(request: httpx.Request) -> None:
        return None

    client = httpx.AsyncClient(base_url=BASE_URL, event_hooks={"request": [hook]})
    with pytest.raises(ValueError, match="event hook"):
        DirectoryLookup(client, platform_token=PlatformToken())


@pytest.mark.parametrize("base_url", ["", "https://user:pass@workspace.example.invalid"])
def test_a_client_without_a_plain_absolute_base_url_is_refused(base_url: str) -> None:
    client = httpx.AsyncClient(base_url=base_url)
    with pytest.raises(ValueError, match="base_url"):
        DirectoryLookup(client, platform_token=PlatformToken())


async def test_a_hook_added_after_construction_stops_the_token_from_leaving(
    lookup: DirectoryLookup, stub: ScimStub, platform: PlatformToken
) -> None:
    seen: list[httpx.Request] = []

    async def hook(request: httpx.Request) -> None:
        seen.append(request)

    lookup._client.event_hooks["request"].append(hook)  # the shared client gained a hook

    answer = await lookup.search("users", "a", count=1, caller_token=caller())

    assert answer.source == "unavailable"
    assert stub.requests == [] and seen == []
