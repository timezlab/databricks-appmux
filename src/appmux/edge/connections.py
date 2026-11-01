"""Open streaming connections per person and app, closed when access is withdrawn (FR-027).

A WebSocket or a long response outlives the request check that admitted it. The edge registers
each one here with the ``Actor`` that opened it; after every group-cache or snapshot refresh it
calls ``close_unauthorized`` and every connection whose person no longer authorizes for its app
is closed, while the rest stay open (design 9.1, "Revocation").

A stream also remembers the release that serves it, so that a swap can close the ones left on the
replaced release once its drain is over (``close_connections``, the reconciler's
``ConnectionCloser``; design 5.5).

This module decides nothing about access: the caller passes the authorizer. Every method runs on
the event-loop thread. A plain dict is then safe without a lock, because no ``await`` falls between
a check and the change that follows it, and so no lock exists for a slow close to hold.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, replace
from typing import Final, NewType, Protocol

from appmux.identity.actor import Actor

logger = logging.getLogger(__name__)

# RFC 6455 "policy violation": the closest standard code to "you may no longer see this".
ACCESS_REVOKED_CLOSE_CODE: Final = 1008
# Fixed text: it reaches the person's browser or client, so it names no email, app or group.
ACCESS_REVOKED_CLOSE_REASON: Final = "access revoked"

# A peer that has stopped reading can block a close; without a bound one such connection would
# keep its own entry (and the walk that returns its count) waiting for as long as the peer likes.
DEFAULT_CLOSE_TIMEOUT_S: Final = 10.0
# The authorizer answers from memory, so a call that takes this long is stuck, not slow. Same
# reasoning as the close bound; the stream whose question timed out is closed (fail closed).
DEFAULT_AUTHORIZER_TIMEOUT_S: Final = 5.0

# RFC 6455 "going away": the app moved to a new release, nobody's access changed. A client's script
# reconnects on this code, which is what the person wants; on 1008 it would give up.
RELEASE_REPLACED_CLOSE_CODE: Final = 1001
RELEASE_REPLACED_CLOSE_REASON: Final = "app updated"

ConnectionId = NewType("ConnectionId", int)


class ConnectionHandle(Protocol):
    """What the registry needs from an open stream: a way to close it."""

    async def close(self, code: int, reason: str) -> None:
        """Close the stream with a WebSocket close ``code`` and a short ``reason``."""
        ...


class ConnectionAuthorizer(Protocol):
    """Whether a person may still use an app, answered from the edge's in-memory data."""

    async def __call__(self, actor: Actor, slug: str) -> bool:
        """``True`` when ``actor`` may still open the app ``slug``; anything else closes."""
        ...


@dataclass(frozen=True, slots=True)
class _OpenConnection:
    actor: Actor
    slug: str
    handle: ConnectionHandle
    release_no: int | None


class OpenConnections:
    """The open streams of the edge, by person and app, and the revocation walk over them."""

    def __init__(
        self,
        *,
        close_timeout_s: float = DEFAULT_CLOSE_TIMEOUT_S,
        authorizer_timeout_s: float = DEFAULT_AUTHORIZER_TIMEOUT_S,
    ) -> None:
        self._close_timeout_s = close_timeout_s
        self._authorizer_timeout_s = authorizer_timeout_s
        self._connections: dict[ConnectionId, _OpenConnection] = {}
        # Ids a walk is closing right now, so a second walk does not close the same handle again.
        self._closing: set[ConnectionId] = set()
        self._next_id = itertools.count(1)

    def __len__(self) -> int:
        return len(self._connections)

    def __bool__(self) -> bool:
        # Without this, an empty registry would be falsy and `if connections:` would read "no
        # registry" where the caller means "no open streams".
        return True

    def register(
        self, actor: Actor, slug: str, handle: ConnectionHandle, *, release_no: int | None = None
    ) -> ConnectionId:
        """Remember an open stream of ``actor`` on app ``slug``; the id unregisters it.

        ``release_no`` is the release whose copy serves the stream. A stream registered without
        it is never touched by ``close_connections``.

        The registry keeps the actor without its ``visitor_token``: a stream can outlive the token
        by hours, the authorizer answers from cached groups, and holding a credential for that long
        would serve nothing. A connection admitted by a check that ran before this call can miss a
        walk that ran in between, so register first and then check, or check again after.
        """
        connection_id = ConnectionId(next(self._next_id))
        self._connections[connection_id] = _OpenConnection(
            actor=replace(actor, visitor_token=None),
            slug=slug,
            handle=handle,
            release_no=release_no,
        )
        return connection_id

    def unregister(self, connection_id: ConnectionId) -> None:
        """Forget a stream that ended. Safe to call twice, or for an id the registry closed."""
        self._connections.pop(connection_id, None)

    async def close_unauthorized(self, authorizer: ConnectionAuthorizer) -> int:
        """Close every stream whose person no longer authorizes for its app; return how many.

        The authorizer is asked once per (email, slug) pair, so the streams of one person on one
        app are judged alike. Only streams registered when the walk starts are judged; one that
        registers later waits for the next walk. Fails closed: an authorizer that raises, that
        does not answer within ``authorizer_timeout_s``, or that answers anything but ``True``,
        closes the stream. A close that raises or times out is logged by exception class only and
        leaves the stream registered, so the next walk tries again; it never stops the walk.

        Each pair is judged and closed on its own task, and every close of a pair runs together,
        so one stuck authorizer call or one unresponsive peer delays nobody else.
        """
        by_pair: dict[tuple[str, str], list[tuple[ConnectionId, _OpenConnection]]] = {}
        for connection_id, connection in list(self._connections.items()):
            pair = (connection.actor.email, connection.slug)
            by_pair.setdefault(pair, []).append((connection_id, connection))

        closed_per_pair = await asyncio.gather(
            *(self._close_if_unauthorized(authorizer, members) for members in by_pair.values())
        )
        return sum(closed_per_pair)

    async def _close_if_unauthorized(
        self,
        authorizer: ConnectionAuthorizer,
        members: list[tuple[ConnectionId, _OpenConnection]],
    ) -> int:
        """Ask once about the pair behind ``members``; close all of them unless it says yes."""
        _, first_registered = members[0]
        if await self._is_still_authorized(authorizer, first_registered):
            return 0
        closed = await asyncio.gather(
            *(self._close(connection_id, connection) for connection_id, connection in members)
        )
        return sum(closed)

    async def _is_still_authorized(
        self, authorizer: ConnectionAuthorizer, connection: _OpenConnection
    ) -> bool:
        """The authorizer's answer, every doubt counted as "no": only ``True`` keeps a stream."""
        try:
            async with asyncio.timeout(self._authorizer_timeout_s):
                answer = await authorizer(connection.actor, connection.slug)
        except Exception as exc:
            # Broad on purpose: the authorizer is caller-supplied code and any failure (a bug, a
            # lookup error, a timeout) must count as a denial, which is the fail-closed outcome.
            # Class name only, no logger.exception: a traceback prints the exception text, which
            # can carry identity or group detail.
            logger.warning(
                "authorizer failed for a connection to app %s (%s); closing it",
                connection.slug,
                type(exc).__name__,
            )
            return False
        return answer is True

    async def close_connections(self, slug: str, release_no: int) -> None:
        """Close every stream of ``slug`` that the copy of release ``release_no`` serves.

        This is the reconciler's ``ConnectionCloser``: ``Swap.retire`` calls it once the drain of a
        replaced release is over, and stops the copy next, which would end the streams abruptly.
        Streams on the new release, on other apps, and ones registered without a release stay
        open. A stream that will not close is logged and left; it ends with its copy.
        """
        members = [
            (connection_id, connection)
            for connection_id, connection in list(self._connections.items())
            if connection.slug == slug and connection.release_no == release_no
        ]
        await asyncio.gather(
            *(
                self._close(
                    connection_id,
                    connection,
                    code=RELEASE_REPLACED_CLOSE_CODE,
                    reason=RELEASE_REPLACED_CLOSE_REASON,
                )
                for connection_id, connection in members
            )
        )

    async def _close(
        self,
        connection_id: ConnectionId,
        connection: _OpenConnection,
        *,
        code: int = ACCESS_REVOKED_CLOSE_CODE,
        reason: str = ACCESS_REVOKED_CLOSE_REASON,
    ) -> bool:
        """Close one stream; ``False`` when it is gone, already being closed, or will not close."""
        if connection_id not in self._connections or connection_id in self._closing:
            return False
        self._closing.add(connection_id)
        try:
            async with asyncio.timeout(self._close_timeout_s):
                await connection.handle.close(code, reason)
        except Exception as exc:
            # Broad on purpose: the handle is transport code from the caller, and whatever it
            # raises must neither stop the walk nor drop the entry, so the stream stays
            # registered (a revocation walk tries again on its next run). Class name only, no
            # logger.exception: a traceback prints the exception text, which can carry peer or
            # transport detail.
            logger.warning(
                "could not close a connection to app %s (%s); it stays registered",
                connection.slug,
                type(exc).__name__,
            )
            return False
        finally:
            self._closing.discard(connection_id)
        self._connections.pop(connection_id, None)
        return True
