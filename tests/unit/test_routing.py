"""Path routing: normalisation, the reserved segment and prefix matching (FR-017, T072).

``routing`` is pure, so every case here is a table row: a raw request path in, a ``Selection``, a
``NoMatch`` or an ``AmbiguousPath`` out. The registry snapshot is stood in for by a set of slugs.
"""

from __future__ import annotations

import pytest

from appmux.routing.normalise import AmbiguousPath, normalise_path
from appmux.routing.selector import (
    SELECTORS,
    NoMatch,
    PrefixSelector,
    RoutingRequest,
    Selection,
)


class Apps:
    """The part of a registry snapshot a selector reads: which slugs are registered."""

    def __init__(self, *slugs: str) -> None:
        self._slugs = frozenset(slugs)

    def app(self, slug: str) -> object | None:
        return object() if slug in self._slugs else None


APPS = Apps("sales", "sales-old", "hr", "a")
SELECTOR = PrefixSelector()


def select(path: str, query: str = "") -> Selection | NoMatch:
    return SELECTOR.select(RoutingRequest(raw_path=path, query_string=query), APPS)


# --- normalisation ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("/", "/"),
        ("/sales/x", "/sales/x"),
        ("/sales/x/", "/sales/x/"),
        # repeated slashes collapse
        ("//hr", "/hr"),
        ("/sales//api///orders", "/sales/api/orders"),
        # dot segments resolve, a trailing dot segment leaves a trailing slash (RFC 3986 5.2.4)
        ("/sales/./x", "/sales/x"),
        ("/sales/a/../b", "/sales/b"),
        ("/sales/a/..", "/sales/"),
        ("/sales/a/.", "/sales/a/"),
        # an encoded dot segment is a dot segment, whatever the case of the hex digits
        ("/sales/%2e%2E/hr/x", "/hr/x"),
        ("/sales/.%2e/hr", "/hr"),
        ("/sales/%2e/x", "/sales/x"),
        # percent-decoding happens once to judge a segment; what is forwarded keeps the client's
        # encoding, so the child decodes exactly what the edge judged
        ("/sales/a%20b", "/sales/a%20b"),
        ("/sales/caf%C3%A9", "/sales/caf%C3%A9"),
        ("/sales/100%25", "/sales/100%25"),
    ],
)
def test_normalise_resolves_dot_segments_and_slashes_without_re_encoding(
    raw: str, normalised: str
) -> None:
    assert normalise_path(raw) == normalised


@pytest.mark.parametrize(
    "raw",
    [
        # encoded slash and backslash, in either case
        "/sales/a%2fb",
        "/sales/a%2Fb",
        "/sales/a%5cb",
        "/sales/a%5Cb",
        # a literal backslash
        "/sales/a\\b",
        # NUL, literal and encoded; other control characters
        "/sales/a\x00b",
        "/sales/a%00b",
        "/sales/a%0ab",
        "/sales/a\nb",
        "/sales/a b",
        # a path that changes after one more decode: %252e is %2e once and '.' twice
        "/sales/%252e%252e/hr",
        "/sales/%2541",
        # malformed or non-UTF-8 percent-encoding cannot be judged, so it is not forwarded
        "/sales/%zz",
        "/sales/%",
        "/sales/%4",
        "/sales/%ff",
        # not an origin-form path
        "",
        "sales/x",
        "*",
        # '..' above the root: no client sends it after its own normalisation
        "/..",
        "/sales/../../hr",
        "/%2e%2e/hr",
        # a fragment marker or a non-ASCII byte in the request target
        "/sales/a#b",
        "/sales/café",
    ],
)
def test_normalise_rejects_ambiguous_paths(raw: str) -> None:
    with pytest.raises(AmbiguousPath):
        normalise_path(raw)


def test_ambiguous_path_message_names_a_reason_and_never_echoes_the_path() -> None:
    path_with_marker = "/sales/x%2fsecret-token-123"
    with pytest.raises(AmbiguousPath) as caught:
        normalise_path(path_with_marker)
    assert "secret-token-123" not in str(caught.value)
    assert "slash" in str(caught.value)


# --- prefix matching ----------------------------------------------------------------------------


def test_prefix_selects_the_app_and_strips_the_prefix() -> None:
    selection = select("/sales/api/orders", "limit=5")
    assert selection == Selection(
        app_slug="sales", upstream_path="/api/orders", query_string="limit=5"
    )
    assert selection.upstream_target == "/api/orders?limit=5"


def test_upstream_target_has_no_question_mark_without_a_query() -> None:
    selection = select("/sales/api")
    assert isinstance(selection, Selection)
    assert selection.upstream_target == "/api"


def test_the_query_string_is_kept_exactly() -> None:
    selection = select("/sales/q", "a=1&b=%2F&a=2&empty=")
    assert isinstance(selection, Selection)
    assert selection.query_string == "a=1&b=%2F&a=2&empty="


@pytest.mark.parametrize("path", ["/sales", "/sales/"])
def test_the_bare_prefix_reaches_the_root_of_the_app(path: str) -> None:
    selection = select(path)
    assert isinstance(selection, Selection)
    assert selection.app_slug == "sales"
    assert selection.upstream_path == "/"


def test_the_whole_first_segment_is_matched() -> None:
    # '/sales-old/x' must reach 'sales-old', and never 'sales' for being its textual prefix.
    selection = select("/sales-old/x")
    assert isinstance(selection, Selection)
    assert selection.app_slug == "sales-old"


@pytest.mark.parametrize("path", ["/sale/x", "/sales-new/x", "/salesx", "/Sales/x", "/hr2/x"])
def test_a_lookalike_slug_matches_nothing(path: str) -> None:
    assert select(path) == NoMatch()


def test_an_unknown_slug_and_the_root_match_nothing() -> None:
    assert select("/unknown/x") == NoMatch()
    assert select("/") == NoMatch()


def test_a_path_that_normalises_to_another_app_is_matched_as_that_app() -> None:
    # The match is made on the normalised path, so the app that is authorised is the app that is
    # reached: '/sales/../hr/x' is a request for 'hr'.
    selection = select("/sales/../hr/x")
    assert isinstance(selection, Selection)
    assert (selection.app_slug, selection.upstream_path) == ("hr", "/x")
    assert select("//hr/x") == select("/hr/x")


def test_an_encoded_first_segment_is_decoded_once_for_the_match() -> None:
    selection = select("/%73ales/x")
    assert isinstance(selection, Selection)
    assert selection.app_slug == "sales"
    # '%2573ales' is '%73ales' after one decode, which is not a slug (and changes on a second).
    with pytest.raises(AmbiguousPath):
        select("/%2573ales/x")


def test_the_reserved_segment_matches_no_app_even_when_a_row_claims_it() -> None:
    apps = Apps("_appmux", "sales")
    request = RoutingRequest(raw_path="/_appmux/api/v1/me", query_string="")
    assert SELECTOR.select(request, apps) == NoMatch()
    assert SELECTOR.select(RoutingRequest(raw_path="/%5Fappmux/x", query_string=""), apps) == (
        NoMatch()
    )


def test_a_child_path_with_encoded_characters_is_forwarded_as_the_client_sent_it() -> None:
    selection = select("/sales/files/a%20b/caf%C3%A9.txt")
    assert isinstance(selection, Selection)
    assert selection.upstream_path == "/files/a%20b/caf%C3%A9.txt"


def test_a_control_character_in_the_query_is_refused() -> None:
    with pytest.raises(AmbiguousPath):
        select("/sales/x", "a=1\r\nHost: evil")
    with pytest.raises(AmbiguousPath):
        select("/sales/x", "a=\x00")


def test_ambiguous_input_raises_instead_of_not_matching() -> None:
    # '400', not '404': a path nobody can judge must not be told apart as "no such app".
    with pytest.raises(AmbiguousPath):
        select("/sales/a%2fb")


def test_prefix_is_the_only_selector_of_v1() -> None:
    assert set(SELECTORS) == {"prefix"}
    assert SELECTORS["prefix"] is PrefixSelector
    assert PrefixSelector().name == "prefix"


def test_the_selector_rewrites_the_response_for_the_app_it_chose() -> None:
    selection = select("/sales/login")
    assert isinstance(selection, Selection)
    rewritten = SELECTOR.rewrite_response(
        selection,
        [("Location", "/dashboard"), ("Set-Cookie", "sid=1; Path=/; Domain=example.com")],
    )
    assert rewritten == [("Location", "/sales/dashboard"), ("Set-Cookie", "sid=1; Path=/sales/")]
