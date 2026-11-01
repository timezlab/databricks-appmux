"""Per-app caps on the requests the edge forwards (design 7.1 step 6, FR-020).

An app may not take the platform's connections, memory or time without limit, so each forwarded
request is held to three platform settings: how many may be in flight at once for the app
(``max_concurrent_requests``), how many body bytes one may carry (``max_request_body_mb``) and how
long the child may stay silent (``request_timeout_s``, applied by ``UpstreamClient`` as the wait
before the answer and between two chunks).

This module holds the pieces and none of the policy about what the edge answers: ``InFlightLimiter``
counts, ``CappedBody`` counts bytes as they stream, and ``AppCaps`` is the settings of one app as
plain numbers. ``edge.pipeline`` reads the settings out of the registry snapshot and turns a refusal
into ``429`` or ``413``. It imports nothing from ``registry``, so the upstream package stays free of
the control plane.

Nothing here buffers a body: ``CappedBody`` passes each chunk on as it arrives, and a body with no
declared ``Content-Length`` is held to the cap while it streams.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Final

BYTES_PER_MEGABYTE: Final = 1024 * 1024
_MINIMUM_CAP: Final = 1


class RequestBodyTooLarge(Exception):
    """The request body passed the app's cap (the edge answers ``413``)."""

    def __init__(self, limit_bytes: int) -> None:
        super().__init__("the request body is larger than the app accepts")
        self.limit_bytes = limit_bytes


@dataclass(frozen=True, slots=True)
class AppCaps:
    """The three caps of one app, as the platform settings give them."""

    max_concurrent_requests: int
    max_request_body_mb: int
    request_timeout_s: int

    def __post_init__(self) -> None:
        # The settings model already refuses these; a cap of 0 here would silently turn an app off
        # (every request refused), so a caller that skips the model gets a loud error instead.
        for name in ("max_concurrent_requests", "max_request_body_mb", "request_timeout_s"):
            if getattr(self, name) < _MINIMUM_CAP:
                raise ValueError(f"{name} must be at least {_MINIMUM_CAP}")

    @property
    def max_request_body_bytes(self) -> int:
        return self.max_request_body_mb * BYTES_PER_MEGABYTE


class InFlightPermit:
    """One place in an app's in-flight count; ``release`` gives it back, once."""

    __slots__ = ("_give_back", "_released", "_remaining")

    def __init__(self, give_back: Callable[[], int]) -> None:
        self._give_back = give_back
        self._released = False
        self._remaining = 0

    def release(self) -> int:
        """Free the place; return how many requests of the app remained in flight after it.

        A second call changes nothing and returns the same number, so a ``finally`` and an error
        path can both release without freeing a place that belongs to another request.
        """
        if not self._released:
            self._released = True
            self._remaining = self._give_back()
        return self._remaining


class InFlightLimiter:
    """Counts the requests in flight per app and refuses one over the app's cap.

    Single-threaded asyncio: the count changes only between awaits, so no lock is needed. The cap
    is passed on each ``try_acquire`` because an admin can change it at any time; lowering it ends
    no request already running, it only stops new ones until the count falls below it.
    """

    def __init__(self) -> None:
        self._in_flight: dict[str, int] = {}

    def try_acquire(self, slug: str, limit: int) -> InFlightPermit | None:
        """A permit when the app has a free place under ``limit``, else ``None``."""
        current = self._in_flight.get(slug, 0)
        if current >= limit:
            return None
        self._in_flight[slug] = current + 1
        return InFlightPermit(lambda: self._release(slug))

    def in_flight(self, slug: str) -> int:
        return self._in_flight.get(slug, 0)

    def tracked_apps(self) -> frozenset[str]:
        """The apps with at least one request in flight (an idle app is not kept)."""
        return frozenset(self._in_flight)

    def _release(self, slug: str) -> int:
        remaining = self._in_flight.get(slug, 0) - 1
        if remaining > 0:
            self._in_flight[slug] = remaining
            return remaining
        self._in_flight.pop(slug, None)
        return 0


class CappedBody:
    """A request body that fails the moment it passes ``max_bytes``.

    It wraps the chunks of the incoming request and hands each one on untouched, so the body is
    never held whole. The chunk that crosses the cap is not returned: the child never sees bytes
    beyond what the app accepts.
    """

    def __init__(self, source: AsyncIterator[bytes], *, max_bytes: int) -> None:
        self._source = source
        self._max_bytes = max_bytes
        self.received_bytes = 0

    def __aiter__(self) -> CappedBody:
        return self

    async def __anext__(self) -> bytes:
        chunk = await self._source.__anext__()
        self.received_bytes += len(chunk)
        if self.received_bytes > self._max_bytes:
            raise RequestBodyTooLarge(self._max_bytes)
        return chunk
