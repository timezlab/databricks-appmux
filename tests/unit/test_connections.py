"""``OpenConnections`` closes the streams of a person who lost access to an app (FR-027, T034).

The authorizer and the handles are fakes: the registry decides nothing about access, it only
asks and closes. Every test runs on asyncio (``tests/conftest.py`` pins the anyio backend).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from pydantic import SecretStr

from appmux.edge.connections import (
    ACCESS_REVOKED_CLOSE_CODE,
    ACCESS_REVOKED_CLOSE_REASON,
    RELEASE_REPLACED_CLOSE_CODE,
    RELEASE_REPLACED_CLOSE_REASON,
    ConnectionId,
    OpenConnections,
)
from appmux.identity.actor import Actor

pytestmark = pytest.mark.anyio

ALICE = Actor(email="alice@example.com")
BOB = Actor(email="bob@example.com")
CLOSE_FAILURE_TEXT = "close failure detail that must stay out of logs"
AUTHORIZER_FAILURE_TEXT = "authorizer failure detail that must stay out of logs"


class RecordingHandle:
    """A stream handle that remembers how it was closed; ``after_close`` runs inside ``close``."""

    def __init__(self, after_close: Callable[[], Awaitable[None]] | None = None) -> None:
        self.closes: list[tuple[int, str]] = []
        self.close_started = asyncio.Event()
        self._after_close = after_close

    @property
    def is_closed(self) -> bool:
        return bool(self.closes)

    async def close(self, code: int, reason: str) -> None:
        self.closes.append((code, reason))
        self.close_started.set()
        if self._after_close is not None:
            await self._after_close()


class FailingHandle(RecordingHandle):
    async def close(self, code: int, reason: str) -> None:
        await super().close(code, reason)
        raise RuntimeError(CLOSE_FAILURE_TEXT)


class HangingHandle(RecordingHandle):
    async def close(self, code: int, reason: str) -> None:
        await super().close(code, reason)
        await asyncio.sleep(3600)


class StubAuthorizer:
    """Denies the (email, slug) pairs in ``denied``; records every question it was asked."""

    def __init__(self, denied: set[tuple[str, str]] | None = None) -> None:
        self.denied = denied or set()
        self.asked: list[tuple[Actor, str]] = []

    async def __call__(self, actor: Actor, slug: str) -> bool:
        self.asked.append((actor, slug))
        return (actor.email, slug) not in self.denied


async def wait_until_closed(handle: RecordingHandle, *, within_s: float = 1.0) -> None:
    """Wait until ``handle.close`` has been called; ``TimeoutError`` if it is not within a bound."""
    async with asyncio.timeout(within_s):
        await handle.close_started.wait()


def result_authorizer(result: object) -> Callable[[Actor, str], Awaitable[bool]]:
    """An authorizer that answers ``result`` whatever its type, as a buggy one might."""

    async def authorizer(actor: Actor, slug: str) -> bool:
        return cast("bool", result)  # the point is that the answer is not a bool

    return authorizer


async def test_unauthorized_handle_is_closed_and_the_others_stay_open() -> None:
    connections = OpenConnections()
    alice_sales = RecordingHandle()
    alice_hr = RecordingHandle()
    bob_sales = RecordingHandle()
    connections.register(ALICE, "sales", alice_sales)
    connections.register(ALICE, "hr", alice_hr)
    connections.register(BOB, "sales", bob_sales)

    closed = await connections.close_unauthorized(StubAuthorizer({("alice@example.com", "sales")}))

    assert closed == 1
    assert alice_sales.is_closed
    assert not alice_hr.is_closed
    assert not bob_sales.is_closed


async def test_every_handle_of_the_revoked_pair_is_closed_after_one_question() -> None:
    connections = OpenConnections()
    first, second, other_app = RecordingHandle(), RecordingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", first)
    connections.register(ALICE, "sales", second)
    connections.register(ALICE, "hr", other_app)
    authorizer = StubAuthorizer({("alice@example.com", "sales")})

    await connections.close_unauthorized(authorizer)

    assert first.is_closed
    assert second.is_closed
    assert not other_app.is_closed
    assert [slug for _, slug in authorizer.asked].count("sales") == 1


async def test_nothing_is_closed_when_everyone_is_still_authorized() -> None:
    connections = OpenConnections()
    handle = RecordingHandle()
    connections.register(ALICE, "sales", handle)

    assert await connections.close_unauthorized(StubAuthorizer()) == 0
    assert not handle.is_closed
    assert len(connections) == 1


async def test_a_closed_handle_leaves_the_registry_and_is_closed_once() -> None:
    connections = OpenConnections()
    handle = RecordingHandle()
    connections.register(ALICE, "sales", handle)
    authorizer = StubAuthorizer({("alice@example.com", "sales")})

    await connections.close_unauthorized(authorizer)
    await connections.close_unauthorized(authorizer)

    assert len(handle.closes) == 1
    assert len(connections) == 0


async def test_close_reason_is_a_fixed_string_that_names_nobody() -> None:
    connections = OpenConnections()
    alice_handle, bob_handle = RecordingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", alice_handle)
    connections.register(BOB, "hr", bob_handle)

    await connections.close_unauthorized(
        StubAuthorizer({("alice@example.com", "sales"), ("bob@example.com", "hr")})
    )

    expected = (ACCESS_REVOKED_CLOSE_CODE, ACCESS_REVOKED_CLOSE_REASON)
    assert alice_handle.closes == [expected]
    assert bob_handle.closes == [expected]
    code, reason = expected
    assert 1000 <= code <= 4999  # a code a WebSocket close frame may carry (RFC 6455)
    for private_text in ("alice", "bob", "example.com", "sales", "hr"):
        assert private_text not in reason


async def test_unregister_is_idempotent_and_an_unregistered_handle_is_never_closed() -> None:
    connections = OpenConnections()
    handle = RecordingHandle()
    connection_id = connections.register(ALICE, "sales", handle)

    connections.unregister(connection_id)
    connections.unregister(connection_id)
    connections.unregister(ConnectionId(10_000))

    await connections.close_unauthorized(StubAuthorizer({("alice@example.com", "sales")}))
    assert not handle.is_closed
    assert len(connections) == 0


async def test_each_registration_gets_its_own_id() -> None:
    connections = OpenConnections()
    first = connections.register(ALICE, "sales", RecordingHandle())
    second = connections.register(ALICE, "sales", RecordingHandle())
    third = connections.register(ALICE, "sales", RecordingHandle())
    assert len({first, second, third}) == 3

    connections.unregister(second)
    assert len(connections) == 2


async def test_authorizer_that_raises_closes_that_handle_and_the_walk_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connections = OpenConnections()
    alice_handle, bob_handle = RecordingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", alice_handle)
    connections.register(BOB, "sales", bob_handle)

    async def authorizer(actor: Actor, slug: str) -> bool:
        if actor.email == ALICE.email:
            raise RuntimeError(AUTHORIZER_FAILURE_TEXT)
        return True

    with caplog.at_level(logging.DEBUG):
        closed = await connections.close_unauthorized(authorizer)

    assert closed == 1
    assert alice_handle.is_closed
    assert not bob_handle.is_closed
    assert AUTHORIZER_FAILURE_TEXT not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("answer", [None, "yes", 1, 0, "", [], object()])
async def test_authorizer_answer_that_is_not_true_closes_the_handle(answer: object) -> None:
    connections = OpenConnections()
    handle = RecordingHandle()
    connections.register(ALICE, "sales", handle)

    await connections.close_unauthorized(result_authorizer(answer))

    assert handle.is_closed


async def test_authorizer_answer_true_keeps_the_handle_open() -> None:
    connections = OpenConnections()
    handle = RecordingHandle()
    connections.register(ALICE, "sales", handle)

    await connections.close_unauthorized(result_authorizer(True))

    assert not handle.is_closed


async def test_close_that_raises_is_logged_without_its_text_and_does_not_stop_the_walk(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connections = OpenConnections()
    broken, healthy = FailingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", broken)
    connections.register(BOB, "sales", healthy)

    with caplog.at_level(logging.DEBUG):
        closed = await connections.close_unauthorized(
            StubAuthorizer({("alice@example.com", "sales"), ("bob@example.com", "sales")})
        )

    assert closed == 1
    assert broken.is_closed
    assert healthy.is_closed
    assert CLOSE_FAILURE_TEXT not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_handle_whose_close_failed_stays_registered_and_is_closed_again_next_time() -> None:
    connections = OpenConnections()
    broken = FailingHandle()
    connections.register(ALICE, "sales", broken)
    authorizer = StubAuthorizer({("alice@example.com", "sales")})

    await connections.close_unauthorized(authorizer)
    assert len(connections) == 1

    await connections.close_unauthorized(authorizer)
    assert len(broken.closes) == 2


async def test_close_that_never_returns_times_out_and_does_not_stop_the_walk() -> None:
    connections = OpenConnections(close_timeout_s=0.05)
    stuck, healthy = HangingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", stuck)
    connections.register(BOB, "sales", healthy)

    closed = await asyncio.wait_for(
        connections.close_unauthorized(
            StubAuthorizer({("alice@example.com", "sales"), ("bob@example.com", "sales")})
        ),
        timeout=5,
    )

    assert closed == 1
    assert healthy.is_closed
    assert len(connections) == 1


async def test_handle_registered_during_a_walk_waits_for_the_next_walk() -> None:
    connections = OpenConnections()
    existing, late = RecordingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", existing)
    denied = {("alice@example.com", "sales"), ("bob@example.com", "sales")}
    late_registered = False

    async def authorizer(actor: Actor, slug: str) -> bool:
        nonlocal late_registered
        if not late_registered:
            late_registered = True
            connections.register(BOB, "sales", late)
        return (actor.email, slug) not in denied

    await connections.close_unauthorized(authorizer)
    assert existing.is_closed
    assert not late.is_closed
    assert len(connections) == 1

    await connections.close_unauthorized(authorizer)
    assert late.is_closed
    assert len(connections) == 0


async def test_handle_registered_while_another_closes_is_kept_for_the_next_walk() -> None:
    connections = OpenConnections()
    late = RecordingHandle()

    async def register_late() -> None:
        connections.register(BOB, "sales", late)

    closing = RecordingHandle(after_close=register_late)
    connections.register(ALICE, "sales", closing)
    denied = {("alice@example.com", "sales"), ("bob@example.com", "sales")}

    await connections.close_unauthorized(StubAuthorizer(denied))

    assert closing.is_closed
    assert not late.is_closed
    assert len(connections) == 1


async def test_handle_unregistered_during_a_walk_is_not_closed() -> None:
    connections = OpenConnections()
    first, second = RecordingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", first)
    second_id = connections.register(BOB, "sales", second)
    denied = {("alice@example.com", "sales"), ("bob@example.com", "sales")}

    async def authorizer(actor: Actor, slug: str) -> bool:
        connections.unregister(second_id)
        return (actor.email, slug) not in denied

    await connections.close_unauthorized(authorizer)

    assert first.is_closed
    assert not second.is_closed


async def test_two_walks_at_once_close_a_handle_only_once() -> None:
    connections = OpenConnections()
    release_close = asyncio.Event()

    async def wait_for_release() -> None:
        await release_close.wait()

    slow = RecordingHandle(after_close=wait_for_release)
    connections.register(ALICE, "sales", slow)
    authorizer = StubAuthorizer({("alice@example.com", "sales")})

    first_walk = asyncio.create_task(connections.close_unauthorized(authorizer))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    second_walk = asyncio.create_task(connections.close_unauthorized(authorizer))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release_close.set()
    results = await asyncio.gather(first_walk, second_walk)

    assert len(slow.closes) == 1
    assert sorted(results) == [0, 1]


async def test_a_decision_is_not_kept_between_walks() -> None:
    connections = OpenConnections()
    handle = RecordingHandle()
    connections.register(ALICE, "sales", handle)
    allowing = StubAuthorizer()
    denying = StubAuthorizer({("alice@example.com", "sales")})

    assert await connections.close_unauthorized(allowing) == 0
    assert not handle.is_closed

    assert await connections.close_unauthorized(denying) == 1
    assert handle.is_closed
    assert len(connections) == 0
    assert len(denying.asked) == 1  # asked afresh; the first walk's "allowed" was not reused


@pytest.mark.parametrize("same_pair", [True, False], ids=["same person and app", "two people"])
async def test_closes_run_together_so_a_stuck_peer_does_not_delay_the_others(
    same_pair: bool,
) -> None:
    connections = OpenConnections()  # the default close timeout is far longer than this test waits
    release_stuck = asyncio.Event()

    async def wait_for_release() -> None:
        await release_stuck.wait()

    stuck, healthy = RecordingHandle(after_close=wait_for_release), RecordingHandle()
    connections.register(ALICE, "sales", stuck)  # first, so a one-by-one walk would stop here
    connections.register(ALICE if same_pair else BOB, "sales", healthy)
    denied = {("alice@example.com", "sales"), ("bob@example.com", "sales")}
    walk = asyncio.create_task(connections.close_unauthorized(StubAuthorizer(denied)))

    try:
        await wait_until_closed(healthy)  # closed while the stuck one is still waiting
        assert not walk.done()
    finally:
        release_stuck.set()

    assert await asyncio.wait_for(walk, timeout=5) == 2


async def test_authorizer_call_that_never_returns_times_out_and_counts_as_a_denial(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connections = OpenConnections(authorizer_timeout_s=0.05)
    hung_person, other_person = RecordingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", hung_person)
    connections.register(BOB, "sales", other_person)

    async def authorizer(actor: Actor, slug: str) -> bool:
        if actor.email == ALICE.email:
            await asyncio.sleep(3600)
        return True

    with caplog.at_level(logging.DEBUG):
        closed = await asyncio.wait_for(connections.close_unauthorized(authorizer), timeout=5)

    assert closed == 1
    assert hung_person.is_closed
    assert not other_person.is_closed
    assert "TimeoutError" in caplog.text


async def test_a_hung_authorizer_call_does_not_delay_revoking_other_people() -> None:
    connections = OpenConnections()  # the default authorizer timeout is longer than this test waits
    release_authorizer = asyncio.Event()
    hung_person, revoked_person = RecordingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", hung_person)  # first, so a one-by-one walk would stop here
    connections.register(BOB, "sales", revoked_person)

    async def authorizer(actor: Actor, slug: str) -> bool:
        if actor.email == ALICE.email:
            await release_authorizer.wait()
            return True
        return False

    walk = asyncio.create_task(connections.close_unauthorized(authorizer))
    try:
        await wait_until_closed(revoked_person)
    finally:
        release_authorizer.set()

    assert await asyncio.wait_for(walk, timeout=5) == 1
    assert not hung_person.is_closed


async def test_an_empty_registry_is_still_truthy() -> None:
    connections = OpenConnections()

    assert len(connections) == 0
    # `if connections:` must mean "the registry exists", never "it holds a stream".
    assert bool(connections) is True


async def test_authorizer_sees_the_person_and_app_but_never_their_token() -> None:
    connections = OpenConnections()
    token_text = "token-value-that-must-not-be-kept"  # noqa: S105 - a fake, to look for in reprs
    carrying_token = Actor(
        email="alice@example.com",
        user="1234",
        preferred_username="alice",
        client_address="203.0.113.7",
        visitor_token=SecretStr(token_text),
    )
    connections.register(carrying_token, "sales", RecordingHandle())
    authorizer = StubAuthorizer()

    await connections.close_unauthorized(authorizer)

    [(seen, slug)] = authorizer.asked
    assert slug == "sales"
    assert seen.email == "alice@example.com"
    assert seen.user == "1234"
    assert seen.preferred_username == "alice"
    assert seen.client_address == "203.0.113.7"
    assert seen.visitor_token is None
    assert token_text not in repr(vars(connections))
    assert carrying_token.visitor_token is not None  # the caller's own value is untouched


# --- a replaced release (Swap.retire, design 5.5) -------------------------------------------------


async def test_the_streams_of_a_replaced_release_are_closed_going_away_and_the_rest_stay() -> None:
    connections = OpenConnections()
    old_a, old_b = RecordingHandle(), RecordingHandle()
    new_release, other_app = RecordingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", old_a, release_no=1)
    connections.register(BOB, "sales", old_b, release_no=1)
    connections.register(ALICE, "sales", new_release, release_no=2)
    connections.register(ALICE, "hr", other_app, release_no=1)

    await connections.close_connections("sales", 1)

    assert (
        old_a.closes
        == old_b.closes
        == [(RELEASE_REPLACED_CLOSE_CODE, RELEASE_REPLACED_CLOSE_REASON)]
    )
    assert not new_release.is_closed
    assert not other_app.is_closed
    assert len(connections) == 2, "a closed stream is forgotten, the others stay registered"


async def test_a_replaced_release_is_not_reported_to_the_browser_as_revoked_access() -> None:
    # 1008 "access revoked" would tell a person their access was withdrawn when the app only moved
    # to a new release; the browser's script then stops reconnecting.
    assert RELEASE_REPLACED_CLOSE_CODE == 1001
    assert (RELEASE_REPLACED_CLOSE_CODE, RELEASE_REPLACED_CLOSE_REASON) != (
        ACCESS_REVOKED_CLOSE_CODE,
        ACCESS_REVOKED_CLOSE_REASON,
    )


async def test_a_stream_registered_without_a_release_is_left_alone_by_a_release_walk() -> None:
    connections = OpenConnections()
    unknown_release = RecordingHandle()
    connections.register(ALICE, "sales", unknown_release)

    await connections.close_connections("sales", 1)

    assert not unknown_release.is_closed


async def test_a_stream_that_will_not_close_does_not_keep_the_others_of_its_release_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connections = OpenConnections()
    stuck, fine = FailingHandle(), RecordingHandle()
    connections.register(ALICE, "sales", stuck, release_no=1)
    connections.register(BOB, "sales", fine, release_no=1)

    with caplog.at_level(logging.DEBUG):
        await connections.close_connections("sales", 1)

    assert fine.is_closed
    assert stuck.is_closed, "the close was tried"
    assert CLOSE_FAILURE_TEXT not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_closing_a_release_with_no_open_streams_does_nothing() -> None:
    await OpenConnections().close_connections("sales", 1)
