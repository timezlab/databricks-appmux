"""The live routing table: app slug -> the socket of its active release (constitution II).

The edge looks a route up on every request; the swap (``swap.py``) is the only module that opens or
closes one, and it does so after the new release's health check passed. A source-scan test
(``test_release_states.py``) fails if any other module calls the setters, so "a route opens only
after fetch, build, start and health check" cannot be broken by a stray call.

Replacing a route must not show a reader half a change. The table therefore holds one immutable
mapping and swaps the whole reference on every write; a reader that took ``routes()`` earlier
keeps a complete, unchanged picture. Rebinding an attribute is atomic, and the table is meant to be
used from one event loop, so it takes no lock.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from appmux.registry.models import SLUG_PATTERN

_SLUG: Final = re.compile(SLUG_PATTERN)


@dataclass(frozen=True, slots=True)
class Route:
    """Where an app's requests go: its active release and the socket that release listens on."""

    release_no: int
    socket_path: Path


class RouteTable:
    def __init__(self) -> None:
        self._routes: Mapping[str, Route] = MappingProxyType({})

    def lookup(self, slug: str) -> Route | None:
        return self._routes.get(slug)

    def routes(self) -> Mapping[str, Route]:
        """A read-only picture of the table as it is now; later changes do not alter it."""
        return self._routes

    def open_route(self, slug: str, route: Route) -> None:
        """Route ``slug`` to ``route``, replacing any earlier route in one step."""
        if _SLUG.fullmatch(slug) is None:
            raise ValueError("the app slug is not a valid slug, so it cannot have a route")
        self._routes = MappingProxyType({**self._routes, slug: route})

    def close_route(self, slug: str) -> None:
        """Stop routing ``slug``; closing an app that has no route does nothing."""
        if slug in self._routes:
            self._routes = MappingProxyType(
                {name: route for name, route in self._routes.items() if name != slug}
            )
