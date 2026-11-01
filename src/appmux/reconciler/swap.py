"""The swap of design 5.5: the only module that opens or closes a route (constitution II).

A route opens after the release's health check passed, so every path to a live route ends here:
a first activation, a replacement, a restore of the active release at start-up. A source-scan test
(``test_release_states.py``) fails if any other module calls ``open_route`` or ``close_route``.

``activate`` is the swap proper, in two steps that cannot be interleaved with anything else:

1. one registry transaction: the new release becomes ``active`` and the old one ``superseded``
   (the old one first: the table allows one ``active`` release per app after every statement);
2. one change of the route table, which replaces the route in a single step.

Registry first: if the write fails nothing has changed, the old release serves and the new copy is
the caller's to stop (FR-004). If the process dies between the two steps, the registry says the new
release is active and the next start brings it up and opens its route after its health check; the
route table is memory and never outlives the process.

``rename_route`` moves a route to another slug without touching the copy (an app was renamed).

``retire`` is what follows for a replaced copy: in-flight requests get ``DRAIN_S`` seconds, the
WebSockets still open then are closed, and only then is the copy stopped. It is a separate call so
the reconciler can record the new copy as the live one before the drain starts.

The edge owns the open connections and the reconciler must not import it (design 3.4), so closing
them goes through the ``ConnectionCloser`` seam, told the slug *and* the release number: a stream
already open on the new release must survive.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final, Protocol

from appmux.reconciler.children import CHILD_STOP_GRACE_S, RunningChild
from appmux.reconciler.clock import Clock
from appmux.reconciler.route_table import Route, RouteTable
from appmux.registry.models import Release
from appmux.registry.protocol import Registry
from appmux.registry.releases import activation_changes

logger = logging.getLogger(__name__)

# Requests in flight on the old release finish within this long (design 5.5, a constant).
DRAIN_S: Final = 30.0


class ConnectionCloser(Protocol):
    async def close_connections(self, slug: str, release_no: int) -> None:
        """Close every open connection (WebSocket) of ``slug`` that is served by ``release_no``."""
        ...


@dataclass(frozen=True, slots=True)
class LiveCopy:
    """A running copy of one release: its registry row as the caller last read it, and its child."""

    release: Release
    child: RunningChild

    @property
    def route(self) -> Route:
        return Route(self.release.release_no, self.child.socket_path)


class Swap:
    def __init__(
        self,
        *,
        registry: Registry,
        routes: RouteTable,
        closer: ConnectionCloser,
        clock: Clock,
        drain_s: float = DRAIN_S,
        stop_grace_s: float = CHILD_STOP_GRACE_S,
    ) -> None:
        self._registry = registry
        self._routes = routes
        self._closer = closer
        self._clock = clock
        self._drain_s = drain_s
        self._stop_grace_s = stop_grace_s

    async def activate(self, new: LiveCopy) -> None:
        """Make ``new`` (healthy) the live copy: the registry transaction, then the route.

        ``new.release`` is the row as the caller last wrote it, not a read of the snapshot: a
        snapshot whose reload failed after a write would still show an earlier status. The release
        it replaces is whichever one the registry calls active, so a copy that crashed and was
        forgotten is still superseded. Raises what the registry raises, or ``IllegalTransition``
        for a release that is not healthy; in both cases the route table and the old release are
        as they were.
        """
        slug = new.release.app_slug
        old_row = next(
            (
                release
                for release in self._registry.snapshot.releases_for(slug)
                if release.status == "active" and release.release_no != new.release.release_no
            ),
            None,
        )
        await self._registry.write(activation_changes(new.release, old_row))
        self._routes.open_route(slug, new.route)

    def open_active(self, copy: LiveCopy) -> None:
        """Route to a copy of the release the registry already calls active (start-up restore)."""
        self._routes.open_route(copy.release.app_slug, copy.route)

    def close(self, slug: str) -> None:
        """Stop routing ``slug`` (the app was stopped or removed); nothing else changes."""
        self._routes.close_route(slug)

    def rename_route(self, old_slug: str, new_slug: str, copy: LiveCopy) -> None:
        """Serve ``copy`` under ``new_slug`` instead of ``old_slug``, in one step (FR-010).

        The registry already names the app ``new_slug``. Both table changes happen with nothing
        awaited between them, so no request sees the app under neither slug. The copy keeps its
        process; only the name it is reached by changes.
        """
        self._routes.close_route(old_slug)
        self._routes.open_route(new_slug, copy.route)

    async def retire(self, old: LiveCopy, *, close_streams: bool = True) -> None:
        """Drain ``old``, close the connections still open on it, and stop it.

        The copy is stopped even if the drain is cancelled or the connections cannot be closed:
        a replaced copy that kept running would hold memory admission control already gave back.

        ``close_streams=False`` is for a copy replaced by another copy of the **same release** (a
        restart, a rename): the edge names a stream by slug and release number, so closing "the
        streams of release N" would also close the ones now open on the new copy. Those on the old
        copy end when it stops.
        """
        slug = old.release.app_slug
        try:
            await self._clock.sleep(self._drain_s)
            if close_streams:
                await self._close_connections(slug, old.release.release_no)
        finally:
            await asyncio.shield(old.child.stop(grace_s=self._stop_grace_s))

    async def _close_connections(self, slug: str, release_no: int) -> None:
        try:
            await self._closer.close_connections(slug, release_no)
        except Exception:
            # Best effort: the copy is stopped next, which ends the connections anyway.
            logger.exception("could not close the open connections of %s v%d", slug, release_no)
