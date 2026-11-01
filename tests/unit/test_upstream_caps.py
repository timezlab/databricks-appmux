"""The per-app caps on forwarded requests (design 7.1 step 6, FR-020, T084).

Three pieces, all pure asyncio: the in-flight limiter (a permit per request, a refusal over the
cap), the counting wrapper of a request body (it fails the moment the bytes pass the cap, also when
no length was declared), and the ``AppCaps`` value the edge builds from the platform settings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from appmux.upstream.caps import (
    BYTES_PER_MEGABYTE,
    AppCaps,
    CappedBody,
    InFlightLimiter,
    RequestBodyTooLarge,
)

pytestmark = pytest.mark.anyio


async def chunks_of(*pieces: bytes) -> AsyncIterator[bytes]:
    for piece in pieces:
        yield piece


async def drained(body: CappedBody) -> list[bytes]:
    return [piece async for piece in body]


# --- AppCaps -----------------------------------------------------------------------------------


def test_the_body_cap_is_given_in_megabytes_and_read_in_bytes() -> None:
    caps = AppCaps(max_concurrent_requests=64, max_request_body_mb=3, request_timeout_s=120)

    assert caps.max_request_body_bytes == 3 * BYTES_PER_MEGABYTE


@pytest.mark.parametrize(
    "bad",
    [
        {"max_concurrent_requests": 0},
        {"max_request_body_mb": 0},
        {"request_timeout_s": 0},
        {"request_timeout_s": -1},
    ],
)
def test_a_cap_below_one_is_refused_instead_of_turning_into_a_wall(bad: dict[str, int]) -> None:
    values = {"max_concurrent_requests": 64, "max_request_body_mb": 100, "request_timeout_s": 120}

    with pytest.raises(ValueError, match="at least 1"):
        AppCaps(**{**values, **bad})


# --- InFlightLimiter ---------------------------------------------------------------------------


def test_requests_are_admitted_up_to_the_cap_and_the_next_one_is_refused() -> None:
    limiter = InFlightLimiter()

    permits = [limiter.try_acquire("sales", 2), limiter.try_acquire("sales", 2)]
    refused = limiter.try_acquire("sales", 2)

    assert all(permit is not None for permit in permits)
    assert refused is None
    assert limiter.in_flight("sales") == 2


def test_a_released_permit_frees_its_place() -> None:
    limiter = InFlightLimiter()
    first = limiter.try_acquire("sales", 1)
    assert first is not None

    assert limiter.try_acquire("sales", 1) is None
    assert first.release() == 0
    assert limiter.try_acquire("sales", 1) is not None


def test_releasing_a_permit_twice_frees_one_place_only() -> None:
    limiter = InFlightLimiter()
    first = limiter.try_acquire("sales", 2)
    second = limiter.try_acquire("sales", 2)
    assert first is not None and second is not None

    first.release()
    first.release()

    assert limiter.in_flight("sales") == 1


def test_apps_are_counted_apart() -> None:
    limiter = InFlightLimiter()
    assert limiter.try_acquire("sales", 1) is not None

    assert limiter.try_acquire("sales", 1) is None
    assert limiter.try_acquire("docs", 1) is not None
    assert limiter.in_flight("sales") == limiter.in_flight("docs") == 1


def test_a_cap_lowered_while_requests_are_in_flight_ends_no_request_and_admits_none() -> None:
    limiter = InFlightLimiter()
    permits = [limiter.try_acquire("sales", 5) for _ in range(3)]
    assert all(permit is not None for permit in permits)

    assert limiter.try_acquire("sales", 2) is None
    assert limiter.in_flight("sales") == 3


def test_an_app_with_nothing_in_flight_is_not_kept_in_the_books() -> None:
    limiter = InFlightLimiter()
    permit = limiter.try_acquire("sales", 1)
    assert permit is not None

    permit.release()

    assert limiter.in_flight("sales") == 0
    assert limiter.tracked_apps() == frozenset()


# --- CappedBody --------------------------------------------------------------------------------


async def test_a_body_within_the_cap_passes_through_unchanged_and_is_counted() -> None:
    body = CappedBody(chunks_of(b"abc", b"", b"defg"), max_bytes=7)

    assert await drained(body) == [b"abc", b"", b"defg"]
    assert body.received_bytes == 7


async def test_a_body_that_passes_the_cap_fails_before_the_chunk_that_crossed_it_is_returned() -> (
    None
):
    body = CappedBody(chunks_of(b"abcdef", b"ghijk", b"never"), max_bytes=10)
    seen: list[bytes] = []

    with pytest.raises(RequestBodyTooLarge) as caught:
        async for piece in body:
            seen.append(piece)

    assert seen == [b"abcdef"]
    assert caught.value.limit_bytes == 10


async def test_one_chunk_bigger_than_the_whole_cap_fails_at_once() -> None:
    body = CappedBody(chunks_of(b"x" * 11), max_bytes=10)

    with pytest.raises(RequestBodyTooLarge):
        await body.__anext__()


async def test_the_count_is_kept_for_the_metrics_when_the_body_ends_early() -> None:
    body = CappedBody(chunks_of(b"abc"), max_bytes=100)

    await drained(body)

    assert body.received_bytes == 3


async def test_an_empty_body_is_an_empty_body() -> None:
    body = CappedBody(chunks_of(), max_bytes=1)

    assert await drained(body) == []
    assert body.received_bytes == 0
