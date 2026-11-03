"""The health clock of design 5.5: when does a new release count as up?

A child cannot be asked for a health endpoint, so health is one thing: a ``GET`` on ``health_path``
that answers with **any status below 500**. A 200, a 302 to a login page, a 401 and a 404 all mean
"up and answering"; a 500, a refused connection or a timeout do not. ``health_deadline_s`` is how
long a *new release* has, counted from the moment its process starts, to give a first healthy
answer. appmux polls on the second: at t=0, t=1, ... and at t=deadline, which is the last poll.
A poll that takes longer than a second does not push the next one a whole second later; the next
poll is the next whole second.

The failure reason is the sentence the design quotes, ``health check did not pass within 60 s (last
error: connection refused)``. The last error is a fixed phrase for an error of the connection and
``status 503`` for an answer; it never carries a body, a header or a path the child chose.

Once a release is running it is judged by crashes, not by repeated probes (design 5.6), so this
module is only the wait for the first healthy answer.

The clock and the probe are injected: a test gives a clock whose ``sleep`` moves a number, so the
whole schedule is checked without waiting. The default probe talks to the child's relay socket the
way the edge does (``upstream/client.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import httpcore

from appmux.reconciler.clock import Clock
from appmux.upstream.client import (
    UpstreamClient,
    UpstreamError,
    UpstreamRequest,
    UpstreamTimeout,
    UpstreamUnreachable,
)

HEALTH_POLL_INTERVAL_S: Final = 1.0
# Any answer below this status means the child is up (design 5.5).
HEALTHY_BELOW_STATUS: Final = 500
# The bounds of ``app_config.health_deadline_s`` (data model 4.4): a slow starter may have ten
# minutes, and a typo fails fast.
HEALTH_DEADLINE_MIN_S: Final = 5
HEALTH_DEADLINE_MAX_S: Final = 600
# How long one poll waits for an answer. Short, because the next poll is a second away anyway.
PROBE_TIMEOUT_S: Final = 2.0

# How every reason of a health check that ran out of time begins (``start_failure`` reads it).
HEALTH_TIMEOUT_PREFIX: Final = "health check did not pass within "

_REFUSED: Final = "connection refused"
_TIMED_OUT: Final = "timed out"
_CLOSED: Final = "connection closed before an answer"
_UNSENT: Final = "the probe request could not be sent"


class ProbeFailed(Exception):
    """One poll got no answer; the message is the short phrase that goes into the reason."""


class HealthProbe(Protocol):
    async def probe(self, socket_path: Path, health_path: str, *, timeout_s: float) -> int:
        """``GET health_path`` on the child's socket; return the status or raise ``ProbeFailed``."""
        ...


@dataclass(frozen=True, slots=True)
class HealthResult:
    """How the wait ended: healthy (with the answer's status), or failed with a stored reason."""

    is_healthy: bool
    status: int | None
    reason: str | None
    elapsed_s: float


def failure_reason(deadline_s: int, last_error: str) -> str:
    return f"{HEALTH_TIMEOUT_PREFIX}{deadline_s} s (last error: {last_error})"


async def wait_until_healthy(
    probe: HealthProbe,
    *,
    socket_path: Path,
    health_path: str,
    deadline_s: int,
    clock: Clock,
) -> HealthResult:
    """Poll until the first answer below 500, or until ``deadline_s`` seconds have passed.

    Call it when the child's process has just started: the deadline counts from the call. It
    returns a failed result at the deadline and never raises for an unhealthy child; a
    cancellation propagates.
    """
    if not HEALTH_DEADLINE_MIN_S <= deadline_s <= HEALTH_DEADLINE_MAX_S:
        raise ValueError(
            f"health_deadline_s must be between {HEALTH_DEADLINE_MIN_S} and "
            f"{HEALTH_DEADLINE_MAX_S} seconds, not {deadline_s}"
        )
    started_s = clock.monotonic()
    last_error = _REFUSED
    while True:
        try:
            status = await probe.probe(socket_path, health_path, timeout_s=PROBE_TIMEOUT_S)
        except ProbeFailed as exc:
            last_error = str(exc)
        else:
            if status < HEALTHY_BELOW_STATUS:
                return HealthResult(True, status, None, clock.monotonic() - started_s)
            last_error = f"status {status}"
        elapsed_s = clock.monotonic() - started_s
        if elapsed_s >= deadline_s:
            return HealthResult(False, None, failure_reason(deadline_s, last_error), elapsed_s)
        next_poll_s = (math.floor(elapsed_s / HEALTH_POLL_INTERVAL_S) + 1) * HEALTH_POLL_INTERVAL_S
        await clock.sleep(min(next_poll_s, deadline_s) - elapsed_s)


class UnixSocketHealthProbe:
    """The default probe: an HTTP ``GET`` over the child's relay socket, as the edge sends one."""

    def __init__(self, client: UpstreamClient | None = None) -> None:
        self._client = client or UpstreamClient()

    async def probe(self, socket_path: Path, health_path: str, *, timeout_s: float) -> int:
        request = UpstreamRequest("GET", health_path, headers=())
        try:
            response = await self._client.send(socket_path, request, timeout_s=timeout_s)
        except UpstreamTimeout:
            raise ProbeFailed(_TIMED_OUT) from None
        except UpstreamUnreachable as exc:
            raise ProbeFailed(_describe(exc)) from exc
        except UpstreamError as exc:
            raise ProbeFailed(_UNSENT) from exc
        else:
            try:
                return response.status
            finally:
                await response.aclose()
        finally:
            # One socket per release: drop the pool so a finished release leaves nothing behind.
            await self._client.forget(socket_path)

    async def aclose(self) -> None:
        await self._client.aclose()


def _describe(exc: UpstreamUnreachable) -> str:
    # httpcore's ConnectError covers a socket that is missing and one nobody listens on.
    return _REFUSED if isinstance(exc.__cause__, httpcore.ConnectError) else _CLOSED
