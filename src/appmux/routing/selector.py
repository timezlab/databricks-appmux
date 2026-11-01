"""Which app a request is for, and what its response must change (design 7.2, FR-017).

A selector is the one place the URL scheme lives: it picks the app from a request and says how that
app's response headers must change so the browser's view stays consistent with the pick. v1 ships
one selector, ``prefix`` (``/<slug>/...``); a later one (host-based) adds an entry to ``SELECTORS``
without touching the edge. The selector is pure: it reads the request and the registry's
``app(slug)`` lookup, and it does no I/O.

The pick is made on the *normalised* path (``normalise.py``), so the app that is authorized is the
app that is reached. Anything the path cannot be judged for raises ``AmbiguousPath`` (``400``);
a path that is clear but names no app is ``NoMatch`` (``404``). The two are never mixed up, so a
caller cannot probe for slugs with malformed paths.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Final, Protocol

from appmux.rewrite.response import rewrite_response_headers
from appmux.routing.normalise import AmbiguousPath, decode_segment, normalise_path

# The edge's own URL space (``/_appmux/...``): no app can be mounted on it, even by a stored row.
RESERVED_SEGMENT: Final = "_appmux"

_SPACE: Final = 0x20  # the first printable ASCII code
_DELETE: Final = 0x7F


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """The parts of a request a selector reads: the raw request-line path and the query string."""

    raw_path: str
    query_string: str


@dataclass(frozen=True, slots=True)
class Selection:
    """The app a request is for and the request-target the child will receive."""

    app_slug: str
    upstream_path: str
    query_string: str = ""

    @property
    def upstream_target(self) -> str:
        return (
            f"{self.upstream_path}?{self.query_string}" if self.query_string else self.upstream_path
        )


@dataclass(frozen=True, slots=True)
class NoMatch:
    """The path is clear, and no app answers to it."""


class AppDirectory(Protocol):
    """The part of a registry snapshot a selector needs: is there an app with this slug?"""

    def app(self, slug: str) -> object | None: ...


class Selector(Protocol):
    name: ClassVar[str]

    def select(self, request: RoutingRequest, apps: AppDirectory) -> Selection | NoMatch: ...

    def rewrite_response(
        self, selection: Selection, headers: Sequence[tuple[str, str]]
    ) -> list[tuple[str, str]]: ...


class PrefixSelector:
    """``/<slug>/rest`` goes to ``slug`` as ``/rest``; ``/<slug>`` as ``/``."""

    name: ClassVar[str] = "prefix"

    def select(self, request: RoutingRequest, apps: AppDirectory) -> Selection | NoMatch:
        _refuse_control_characters_in_query(request.query_string)
        path = normalise_path(request.raw_path)
        first_segment, _, rest = path[1:].partition("/")
        if first_segment == "":
            return NoMatch()
        slug = decode_segment(first_segment)
        if slug == RESERVED_SEGMENT or apps.app(slug) is None:
            return NoMatch()
        return Selection(app_slug=slug, upstream_path="/" + rest, query_string=request.query_string)

    def rewrite_response(
        self, selection: Selection, headers: Sequence[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        return rewrite_response_headers(headers, slug=selection.app_slug)


SELECTORS: Final[Mapping[str, type[Selector]]] = {PrefixSelector.name: PrefixSelector}


def _refuse_control_characters_in_query(query_string: str) -> None:
    # The query is forwarded as the client sent it, so a CR, LF or NUL would reach the child's
    # request line (header injection). Nothing else about a query is judged here.
    if any(ord(character) < _SPACE or ord(character) == _DELETE for character in query_string):
        raise AmbiguousPath("the query string holds a control character")
