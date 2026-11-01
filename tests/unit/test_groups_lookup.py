"""A person's Databricks groups: SCIM ``GET /Me`` with their own token, cached, stale up to 1 h.

Design 9.1 and spike 12. The lookup answers ``frozenset[str]`` of lower-cased group names, nested
groups included, or ``None`` for "unknown"; every group-based grant must fail closed on ``None``.
Time comes from an injected clock, so nothing here sleeps; Databricks is an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from appmux.identity.actor import Actor
from appmux.identity.groups import (
    GROUPS_CACHE_TTL_S,
    GROUPS_STALE_WINDOW_S,
    SCIM_REQUEST_TIMEOUT_S,
    GroupLookup,
)

pytestmark = pytest.mark.anyio

BASE_URL = "https://workspace.example.invalid"
# Obviously fake tokens; the tests check that they never show up in a log record or a repr.
VISITOR_VALUE_MARKER = "placeholder-visitor-token-0001"
OTHER_VISITOR_VALUE_MARKER = "placeholder-visitor-token-0002"
GROUPS_LOGGER = "appmux.identity.groups"
BOB_EMAIL = "bob@example.com"
CAROL_EMAIL = "carol@example.com"

Reply = httpx.Response | Exception | Callable[[httpx.Request], Awaitable[httpx.Response]]


class FakeClock:
    """The injected monotonic clock: it moves only when a test advances it."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        reply = self._replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return await reply(request)


@dataclass(frozen=True)
class Harness:
    stub: ScimStub
    clock: FakeClock
    lookup: GroupLookup


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    stub = ScimStub()
    clock = FakeClock()
    # follow_redirects=True on purpose: the lookup must override it for its own request.
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(stub), base_url=BASE_URL, follow_redirects=True
    ) as client:
        yield Harness(stub, clock, GroupLookup(client, clock=clock))


def person(email: str = BOB_EMAIL, token: str | None = VISITOR_VALUE_MARKER) -> Actor:
    return Actor(email=email, visitor_token=None if token is None else SecretStr(token))


def me_response(*groups: tuple[str, str]) -> httpx.Response:
    """The SCIM user a ``Me`` call answers with; ``groups`` are ``(display, type)`` pairs."""
    return httpx.Response(
        200,
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "id": "1111111111111111",
            "userName": BOB_EMAIL,
            "groups": [
                {"display": display, "type": kind, "value": str(index), "$ref": f"Groups/{index}"}
                for index, (display, kind) in enumerate(groups)
            ],
        },
    )


async def settle() -> None:
    """Let every task that can run reach the point where it waits on something else."""
    for _ in range(20):
        await asyncio.sleep(0)


# Each entry is a factory because an httpx.Response is consumed once it is read.
FAILED_ANSWERS: dict[str, Callable[[], Reply]] = {
    "server error": lambda: httpx.Response(503),
    "token rejected": lambda: httpx.Response(401, json={"detail": "invalid token"}),
    "rate limited": lambda: httpx.Response(429),
    "redirect": lambda: httpx.Response(302, headers={"location": "https://other.example.invalid/"}),
    "connection error": lambda: httpx.ConnectError("connection refused"),
    "timeout": lambda: httpx.ReadTimeout("no answer"),
    "not json": lambda: httpx.Response(200, content=b"<html>sign in</html>"),
    "empty body": lambda: httpx.Response(200, content=b""),
    "json list": lambda: httpx.Response(200, json=["sales-team"]),
    "groups not a list": lambda: httpx.Response(200, json={"groups": "sales-team"}),
    "group without display": lambda: httpx.Response(200, json={"groups": [{"type": "direct"}]}),
    "display not text": lambda: httpx.Response(200, json={"groups": [{"display": 7}]}),
    "group not an object": lambda: httpx.Response(200, json={"groups": ["sales-team"]}),
}


# --- what is asked and what comes back ---------------------------------------------------------


async def test_asks_scim_me_with_the_persons_own_token(harness: Harness) -> None:
    harness.stub.reply(me_response(("Sales-Team", "direct"), ("engineers", "direct")))

    groups = await harness.lookup.groups_for(person())

    assert groups == frozenset({"sales-team", "engineers"})
    [request] = harness.stub.requests
    assert request.method == "GET"
    assert request.url == httpx.URL(f"{BASE_URL}/api/2.0/preview/scim/v2/Me")
    assert request.headers["authorization"] == f"Bearer {VISITOR_VALUE_MARKER}"


async def test_groups_reached_through_nesting_count_and_names_are_lower_cased(
    harness: Harness,
) -> None:
    # The shape spike 12 measured: one direct group and three levels above it typed "indirect".
    harness.stub.reply(
        me_response(
            ("appmux-spike-12-child", "direct"),
            ("appmux-spike-12-parent", "indirect"),
            ("appmux-spike-12-grand", "indirect"),
            ("Appmux-Spike-12-GREAT", "indirect"),
            ("users", "direct"),
        )
    )

    groups = await harness.lookup.groups_for(person())

    assert groups == frozenset(
        {
            "appmux-spike-12-child",
            "appmux-spike-12-parent",
            "appmux-spike-12-grand",
            "appmux-spike-12-great",
            "users",
        }
    )


async def test_a_person_in_no_group_is_known_to_have_none(harness: Harness) -> None:
    harness.stub.reply(httpx.Response(200, json={"userName": BOB_EMAIL}))

    groups = await harness.lookup.groups_for(person())

    assert groups == frozenset()
    assert groups is not None


async def test_fields_the_lookup_does_not_read_are_ignored(harness: Harness) -> None:
    harness.stub.reply(
        httpx.Response(
            200,
            json={
                "emails": [{"value": BOB_EMAIL}],
                "entitlements": [{"value": "workspace-access"}],
                "groups": [{"display": "sales-team", "type": "direct", "added_later": True}],
            },
        )
    )

    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})


# --- no token ----------------------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, ""], ids=["absent", "empty"])
async def test_no_token_means_unknown_without_a_request(
    harness: Harness, token: str | None
) -> None:
    assert await harness.lookup.groups_for(person(token=token)) is None
    assert harness.stub.requests == []


async def test_no_token_means_unknown_even_when_the_person_is_cached(harness: Harness) -> None:
    harness.stub.reply(me_response(("sales-team", "direct")))
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})

    assert await harness.lookup.groups_for(person(token=None)) is None
    assert len(harness.stub.requests) == 1


# --- the 300 s cache ---------------------------------------------------------------------------


async def test_a_second_lookup_inside_300_seconds_is_served_from_the_cache(
    harness: Harness,
) -> None:
    harness.stub.reply(me_response(("sales-team", "direct")))
    first = await harness.lookup.groups_for(person())

    harness.clock.advance(299)
    second = await harness.lookup.groups_for(person())

    assert first == second == frozenset({"sales-team"})
    assert len(harness.stub.requests) == 1


async def test_a_lookup_after_300_seconds_asks_again_and_restarts_the_cache(
    harness: Harness,
) -> None:
    harness.stub.reply(
        me_response(("sales-team", "direct")),
        me_response(("sales-team", "direct"), ("finance", "indirect")),
    )
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})

    harness.clock.advance(301)
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team", "finance"})
    assert len(harness.stub.requests) == 2

    harness.clock.advance(250)  # 250 s after the refresh, 551 s after the first answer
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team", "finance"})
    assert len(harness.stub.requests) == 2


async def test_the_cache_window_ends_exactly_at_300_seconds(harness: Harness) -> None:
    assert GROUPS_CACHE_TTL_S == 300.0
    harness.stub.reply(me_response(("sales-team", "direct")), me_response(("finance", "direct")))
    await harness.lookup.groups_for(person())

    harness.clock.advance(299.5)
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})
    assert len(harness.stub.requests) == 1

    harness.clock.advance(0.5)  # now exactly 300 s: no longer younger than 300 s
    assert await harness.lookup.groups_for(person()) == frozenset({"finance"})
    assert len(harness.stub.requests) == 2


async def test_the_cache_is_keyed_by_email_and_never_by_token(harness: Harness) -> None:
    harness.stub.reply(me_response(("sales-team", "direct")))
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})

    # The same person with a renewed token and the email in another case: still the same entry.
    renewed = person(email="Bob@Example.com", token=OTHER_VISITOR_VALUE_MARKER)
    assert await harness.lookup.groups_for(renewed) == frozenset({"sales-team"})
    assert len(harness.stub.requests) == 1


async def test_each_person_is_asked_with_their_own_token_and_cached_apart(
    harness: Harness,
) -> None:
    async def by_token(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == f"Bearer {VISITOR_VALUE_MARKER}":
            return me_response(("sales-team", "direct"))
        return me_response(("finance", "direct"))

    harness.stub.reply(by_token, by_token)

    bob = await harness.lookup.groups_for(person())
    carol = await harness.lookup.groups_for(person(CAROL_EMAIL, OTHER_VISITOR_VALUE_MARKER))

    assert bob == frozenset({"sales-team"})
    assert carol == frozenset({"finance"})
    assert [r.headers["authorization"] for r in harness.stub.requests] == [
        f"Bearer {VISITOR_VALUE_MARKER}",
        f"Bearer {OTHER_VISITOR_VALUE_MARKER}",
    ]
    # And both are now cached.
    assert await harness.lookup.groups_for(person()) == bob
    assert len(harness.stub.requests) == 2


# --- failure: the stale window and fail closed -------------------------------------------------


@pytest.mark.parametrize("failure", list(FAILED_ANSWERS))
async def test_a_failed_lookup_with_nothing_cached_is_unknown(
    harness: Harness, failure: str
) -> None:
    harness.stub.reply(FAILED_ANSWERS[failure]())

    assert await harness.lookup.groups_for(person()) is None
    assert len(harness.stub.requests) == 1


@pytest.mark.parametrize("failure", list(FAILED_ANSWERS))
async def test_a_failed_refresh_falls_back_to_the_cached_groups(
    harness: Harness, failure: str
) -> None:
    harness.stub.reply(me_response(("sales-team", "direct")), FAILED_ANSWERS[failure]())
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})

    harness.clock.advance(301)

    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})
    assert len(harness.stub.requests) == 2


async def test_cached_groups_are_used_after_a_failure_for_at_most_one_hour(
    harness: Harness,
) -> None:
    harness.stub.reply(
        me_response(("sales-team", "direct")),
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(503),
    )
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})

    harness.clock.advance(301)  # the cache expired and the refresh fails
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})

    harness.clock.advance(3_298)  # 3599 s after the answer was fetched
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})

    # A failure does not renew the answer: it is 3601 s old now and must not be used.
    harness.clock.advance(2)
    assert await harness.lookup.groups_for(person()) is None


async def test_cached_groups_are_still_usable_at_exactly_one_hour_and_not_after(
    harness: Harness,
) -> None:
    assert GROUPS_STALE_WINDOW_S == 3_600.0
    harness.stub.reply(
        me_response(("sales-team", "direct")), httpx.Response(503), httpx.Response(503)
    )
    await harness.lookup.groups_for(person())

    harness.clock.advance(3_600)  # "at most one hour": exactly one hour is still allowed
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})

    harness.clock.advance(0.5)
    assert await harness.lookup.groups_for(person()) is None


async def test_using_stale_groups_is_logged_with_their_age(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=GROUPS_LOGGER)
    harness.stub.reply(me_response(("sales-team", "direct")), httpx.Response(503))
    await harness.lookup.groups_for(person())
    harness.clock.advance(301)

    await harness.lookup.groups_for(person())

    [record] = [r for r in caplog.records if r.name == GROUPS_LOGGER]
    assert record.levelno == logging.WARNING
    assert "status 503" in record.getMessage()
    assert "using the groups cached 301 s ago" in record.getMessage()


async def test_an_answer_found_too_old_is_dropped_from_the_cache(harness: Harness) -> None:
    harness.stub.reply(
        me_response(("sales-team", "direct")), httpx.Response(503), httpx.Response(503)
    )
    await harness.lookup.groups_for(person())
    harness.clock.advance(4_000)
    assert await harness.lookup.groups_for(person()) is None

    # Wind the clock back (a real monotonic clock never does) so the old answer would be young
    # enough to use again: it is not, because finding it too old removed it.
    harness.clock.now -= 3_700
    assert await harness.lookup.groups_for(person()) is None


async def test_groups_older_than_an_hour_do_not_come_back_after_they_expired(
    harness: Harness,
) -> None:
    harness.stub.reply(
        me_response(("sales-team", "direct")), httpx.Response(503), httpx.Response(503)
    )
    await harness.lookup.groups_for(person())
    harness.clock.advance(4_000)
    assert await harness.lookup.groups_for(person()) is None

    # The expired answer was dropped, so even a failure a moment later finds nothing.
    harness.clock.advance(1)
    assert await harness.lookup.groups_for(person()) is None


async def test_a_lookup_that_works_again_replaces_the_stale_groups(harness: Harness) -> None:
    harness.stub.reply(
        me_response(("sales-team", "direct")),
        httpx.Response(503),
        me_response(("finance", "direct")),
    )
    await harness.lookup.groups_for(person())

    harness.clock.advance(301)
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})  # stale, 503

    # The stale answer did not start a new 300 s window: the very next lookup asks again.
    harness.clock.advance(1)
    assert await harness.lookup.groups_for(person()) == frozenset({"finance"})
    assert len(harness.stub.requests) == 3


async def test_a_refresh_that_answers_no_groups_replaces_the_cached_groups(
    harness: Harness,
) -> None:
    # Removal from every group is an answer, not a failure: it must not fall back to the old set.
    harness.stub.reply(me_response(("sales-team", "direct")), me_response())
    await harness.lookup.groups_for(person())

    harness.clock.advance(301)

    assert await harness.lookup.groups_for(person()) == frozenset()


async def test_a_redirect_is_never_followed(harness: Harness) -> None:
    harness.stub.reply(
        httpx.Response(302, headers={"location": "https://other.example.invalid/api/2.0/Me"}),
        me_response(("sales-team", "direct")),
    )

    assert await harness.lookup.groups_for(person()) is None
    [request] = harness.stub.requests
    assert request.url.host == "workspace.example.invalid"


# --- concurrency -------------------------------------------------------------------------------


async def test_concurrent_lookups_for_one_person_share_one_request(harness: Harness) -> None:
    release = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return me_response(("sales-team", "direct"))

    harness.stub.reply(held)

    callers = [asyncio.create_task(harness.lookup.groups_for(person())) for _ in range(5)]
    await settle()
    assert len(harness.stub.requests) == 1
    assert not any(caller.done() for caller in callers)

    release.set()
    results = await asyncio.gather(*callers)

    assert results == [frozenset({"sales-team"})] * 5
    assert len(harness.stub.requests) == 1


async def test_concurrent_lookups_share_a_failure_too(harness: Harness) -> None:
    release = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(503)

    harness.stub.reply(held)

    callers = [asyncio.create_task(harness.lookup.groups_for(person())) for _ in range(3)]
    await settle()
    release.set()

    assert await asyncio.gather(*callers) == [None, None, None]
    assert len(harness.stub.requests) == 1


async def test_concurrent_lookups_for_two_people_make_two_requests(harness: Harness) -> None:
    release = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        await release.wait()
        if request.headers["authorization"] == f"Bearer {VISITOR_VALUE_MARKER}":
            return me_response(("sales-team", "direct"))
        return me_response(("finance", "direct"))

    harness.stub.reply(held, held)

    bob = asyncio.create_task(harness.lookup.groups_for(person()))
    carol = asyncio.create_task(
        harness.lookup.groups_for(person(CAROL_EMAIL, OTHER_VISITOR_VALUE_MARKER))
    )
    await settle()
    assert len(harness.stub.requests) == 2
    release.set()

    assert await bob == frozenset({"sales-team"})
    assert await carol == frozenset({"finance"})


async def test_a_caller_that_gives_up_does_not_cancel_the_shared_request(
    harness: Harness,
) -> None:
    release = asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return me_response(("sales-team", "direct"))

    harness.stub.reply(held)
    first = asyncio.create_task(harness.lookup.groups_for(person()))
    second = asyncio.create_task(harness.lookup.groups_for(person()))
    await settle()

    first.cancel()  # a visitor who closed the tab
    await settle()
    release.set()

    assert await second == frozenset({"sales-team"})
    with pytest.raises(asyncio.CancelledError):
        await first
    # The shared request finished and filled the cache, so nobody has to ask again.
    assert await harness.lookup.groups_for(person()) == frozenset({"sales-team"})
    assert len(harness.stub.requests) == 1


# --- what is sent: only the visitor's Authorization header ------------------------------------

EXPECTED_TIMEOUT = {
    "connect": SCIM_REQUEST_TIMEOUT_S,
    "read": SCIM_REQUEST_TIMEOUT_S,
    "write": SCIM_REQUEST_TIMEOUT_S,
    "pool": SCIM_REQUEST_TIMEOUT_S,
}


class ServiceAccountAuth(httpx.Auth):
    """What a shared workspace client might carry: another identity's credential."""

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        request.headers["Authorization"] = "Bearer placeholder-service-credential"
        yield request


async def stamp_request(request: httpx.Request) -> None:
    request.headers["Authorization"] = "Bearer placeholder-hook-credential"


def sticky_cookie_reply() -> httpx.Response:
    return httpx.Response(
        200, json={"groups": []}, headers={"set-cookie": "sticky=person-one; Path=/"}
    )


@pytest.fixture
async def seen() -> list[httpx.Request]:
    return []


def recording_client(seen: list[httpx.Request], **options: Any) -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return sticky_cookie_reply()

    return httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url=BASE_URL, **options)


CLIENTS_THAT_ADD_THINGS: dict[str, dict[str, Any]] = {
    "auth": {"auth": ServiceAccountAuth()},
    "basic auth": {"auth": ("service", "placeholder-password")},
    "default Authorization header": {
        "headers": {"Authorization": "Bearer placeholder-default-credential"}
    },
    "default headers": {"headers": {"X-Default": "1", "Cookie": "a=b"}},
    "cookies": {"cookies": {"sticky": "from-the-jar"}},
    "params": {"params": {"extra": "1"}},
}


@pytest.mark.parametrize(
    "options", CLIENTS_THAT_ADD_THINGS.values(), ids=list(CLIENTS_THAT_ADD_THINGS)
)
async def test_only_the_visitors_authorization_header_is_sent(
    seen: list[httpx.Request], options: dict[str, Any]
) -> None:
    async with recording_client(seen, **options) as client:
        lookup = GroupLookup(client)

        await lookup.groups_for(person())
        await lookup.groups_for(person(CAROL_EMAIL, OTHER_VISITOR_VALUE_MARKER))

    assert [r.headers["authorization"] for r in seen] == [
        f"Bearer {VISITOR_VALUE_MARKER}",
        f"Bearer {OTHER_VISITOR_VALUE_MARKER}",
    ]
    for request in seen:
        assert set(request.headers.keys()) == {"host", "authorization"}
        assert request.url == httpx.URL(f"{BASE_URL}/api/2.0/preview/scim/v2/Me")


async def test_one_persons_set_cookie_is_not_replayed_for_the_next_person(
    seen: list[httpx.Request],
) -> None:
    async with recording_client(seen) as client:
        lookup = GroupLookup(client)
        await lookup.groups_for(person())
        assert "sticky" in client.cookies  # the shared jar did learn the cookie

        await lookup.groups_for(person(CAROL_EMAIL, OTHER_VISITOR_VALUE_MARKER))

    assert all("cookie" not in request.headers for request in seen)


async def test_a_client_with_a_request_hook_is_refused() -> None:
    client = httpx.AsyncClient(base_url=BASE_URL, event_hooks={"request": [stamp_request]})
    async with client:
        with pytest.raises(ValueError, match="event hook"):
            GroupLookup(client)


async def test_a_request_hook_added_after_construction_stops_the_token_leaving(
    seen: list[httpx.Request],
) -> None:
    async with recording_client(seen) as client:
        lookup = GroupLookup(client)
        client.event_hooks = {"request": [stamp_request], "response": []}

        assert await lookup.groups_for(person()) is None

    assert seen == []  # no request was sent, so the hook never saw the visitor's token


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "/relative",
        "//other.example.invalid",  # a host but no scheme
        "file:///workspace",  # a scheme but no host
        "https://user:placeholder-password@workspace.example.invalid",
    ],
)
async def test_a_client_without_a_plain_absolute_base_url_is_refused(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url) as client:
        with pytest.raises(ValueError, match="base_url"):
            GroupLookup(client)


async def test_a_base_url_path_prefix_is_kept(seen: list[httpx.Request]) -> None:
    async with recording_client(seen) as client:
        client.base_url = f"{BASE_URL}/prefix"
        await GroupLookup(client).groups_for(person())

    assert seen[0].url.path == "/prefix/api/2.0/preview/scim/v2/Me"


@pytest.mark.parametrize("client_timeout", [None, 99.0, 0.5], ids=["none", "long", "short"])
async def test_the_request_has_its_own_10_second_timeout(
    seen: list[httpx.Request], client_timeout: float | None
) -> None:
    async with recording_client(seen, timeout=client_timeout) as client:
        await GroupLookup(client).groups_for(person())

    assert SCIM_REQUEST_TIMEOUT_S == 10.0
    assert seen[0].extensions["timeout"] == EXPECTED_TIMEOUT


@pytest.mark.parametrize(
    "token",
    ["caf\u00e9-token", "\u2603", "line\r\nInjected: 1", "with space", "nul\x00byte", "tab\there"],
    ids=["latin1", "snowman", "crlf", "space", "nul", "tab"],
)
async def test_a_token_that_cannot_be_a_bearer_credential_is_unknown_without_a_request(
    harness: Harness, token: str
) -> None:
    assert await harness.lookup.groups_for(person(token=token)) is None
    assert harness.stub.requests == []


# --- the token stays secret --------------------------------------------------------------------

SECRETS = (VISITOR_VALUE_MARKER, OTHER_VISITOR_VALUE_MARKER, BOB_EMAIL, CAROL_EMAIL)
ECHO = f"{VISITOR_VALUE_MARKER} {BOB_EMAIL} {OTHER_VISITOR_VALUE_MARKER} {CAROL_EMAIL}"

# Failures whose own text (an httpx message, a pydantic validation message, a body) quotes the
# token and the email: a log line or an error built from that text would leak them.
ECHOING_FAILURES: dict[str, Callable[[], Reply]] = {
    "connect error text": lambda: httpx.ConnectError(f"cannot reach {ECHO}"),
    "timeout text": lambda: httpx.ReadTimeout(f"no answer for {ECHO}"),
    "protocol error text": lambda: httpx.RemoteProtocolError(f"bad header Bearer {ECHO}"),
    "error body": lambda: httpx.Response(500, text=f"failed for {ECHO}"),
    "unauthorized body": lambda: httpx.Response(401, json={"detail": f"token {ECHO} rejected"}),
    "html echo": lambda: httpx.Response(200, content=f"<html>{ECHO}</html>".encode()),
    "json echo of wrong shape": lambda: httpx.Response(200, json=[ECHO]),
    "validation error on display": lambda: httpx.Response(
        200, json={"groups": [{"display": {"echo": ECHO}}]}
    ),
    "validation error on groups": lambda: httpx.Response(200, json={"groups": ECHO}),
}


def assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


@pytest.mark.parametrize("failure", list(ECHOING_FAILURES))
async def test_no_secret_reaches_a_log_record_or_a_returned_value(
    harness: Harness, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    caplog.set_level(logging.DEBUG, logger=GROUPS_LOGGER)
    harness.stub.reply(
        me_response(("sales-team", "direct")),
        ECHOING_FAILURES[failure](),  # the refresh fails with the cached groups still usable
        ECHOING_FAILURES[failure](),  # the same failure once the cached groups are too old
        ECHOING_FAILURES[failure](),  # and for a person with nothing cached at all
    )
    results = [await harness.lookup.groups_for(person())]
    harness.clock.advance(301)
    results.append(await harness.lookup.groups_for(person()))
    harness.clock.advance(4_000)
    results.append(await harness.lookup.groups_for(person()))
    results.append(await harness.lookup.groups_for(person(CAROL_EMAIL, OTHER_VISITOR_VALUE_MARKER)))

    assert results == [frozenset({"sales-team"})] * 2 + [None, None]
    own_records = [r for r in caplog.records if r.name == GROUPS_LOGGER]
    assert len(own_records) == 3, "every failed refresh is logged once, by this module"
    for record in own_records:
        assert record.levelno == logging.WARNING
        assert record.exc_info is None
        assert record.exc_text is None
        assert_no_secret(record.getMessage())
        assert_no_secret(repr(record.args))
        assert_no_secret(repr(record.__dict__))
    assert_no_secret(repr(results))


@pytest.mark.parametrize("failure", list(ECHOING_FAILURES))
async def test_the_error_raised_inside_the_lookup_names_no_secret(
    harness: Harness, failure: str
) -> None:
    harness.stub.reply(ECHOING_FAILURES[failure]())

    with pytest.raises(Exception) as raised:
        await harness.lookup._fetch(SecretStr(VISITOR_VALUE_MARKER))

    assert_no_secret(str(raised.value))
    assert_no_secret(repr(raised.value))
    assert_no_secret(repr(raised.value.args))


@pytest.mark.parametrize("failure", list(FAILED_ANSWERS))
async def test_neither_token_nor_email_reaches_a_log_record(
    harness: Harness, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    caplog.set_level(logging.DEBUG)
    harness.stub.reply(
        me_response(("sales-team", "direct")),
        FAILED_ANSWERS[failure](),
        FAILED_ANSWERS[failure](),
        FAILED_ANSWERS[failure](),
    )
    await harness.lookup.groups_for(person())
    harness.clock.advance(301)
    await harness.lookup.groups_for(person())
    harness.clock.advance(4_000)
    await harness.lookup.groups_for(person())
    await harness.lookup.groups_for(person(CAROL_EMAIL, OTHER_VISITOR_VALUE_MARKER))

    # Every logger, httpx's own included: nothing may carry a secret.
    assert_no_secret(caplog.text)
    assert_no_secret("".join(str(arg) for record in caplog.records for arg in (record.args or ())))


async def test_a_failed_lookup_logs_the_status_code(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=GROUPS_LOGGER)
    harness.stub.reply(httpx.Response(503))

    await harness.lookup.groups_for(person())

    assert "503" in caplog.text


async def test_a_failed_lookup_without_cache_logs_that_grants_will_not_match(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=GROUPS_LOGGER)
    harness.stub.reply(httpx.ConnectError("refused"))

    await harness.lookup.groups_for(person())

    [record] = [r for r in caplog.records if r.name == GROUPS_LOGGER]
    assert "ConnectError" in record.getMessage()
    assert "group grants will not match" in record.getMessage()


async def test_the_lookup_does_not_hold_the_token_after_it_answered(harness: Harness) -> None:
    harness.stub.reply(me_response(("sales-team", "direct")))
    await harness.lookup.groups_for(person())

    held = repr(vars(harness.lookup))
    assert VISITOR_VALUE_MARKER not in held
    assert re.search(r"SecretStr|Bearer", held) is None
