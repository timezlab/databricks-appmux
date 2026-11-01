"""Response header rewriting: ``Location`` and ``Set-Cookie`` (FR-019, T073).

``rewrite_response_headers`` is pure: header lines in, header lines out. Response bodies never
pass through it, which is the contract's "bodies untouched": the function has no body to change.
"""

from __future__ import annotations

import pytest

from appmux.rewrite.response import OPEN_MODE_COOKIE_NAME, rewrite_response_headers

SLUG = "sales"


def rewrite(*lines: tuple[str, str]) -> list[tuple[str, str]]:
    return rewrite_response_headers(list(lines), slug=SLUG)


# --- Location -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/login", "/sales/login"),
        ("/", "/sales/"),
        ("/a/b?next=%2Fx#top", "/sales/a/b?next=%2Fx#top"),
        # the part after the first segment is the child's own and is never decoded or reshaped
        ("/files/a%20b", "/sales/files/a%20b"),
    ],
)
def test_a_location_beginning_with_a_slash_gets_the_prefix(location: str, expected: str) -> None:
    assert rewrite(("Location", location)) == [("Location", expected)]


@pytest.mark.parametrize(
    "location",
    [
        "https://login.example.com/start",
        "http://example.com/x",
        # a network-path reference leaves the host: it is not a path of this app
        "//login.example.com/start",
        "relative/path",
        "?page=2",
        "#top",
        "",
    ],
)
def test_any_other_location_is_left_as_the_child_wrote_it(location: str) -> None:
    assert rewrite(("Location", location)) == [("Location", location)]


def test_the_location_header_name_keeps_its_spelling() -> None:
    assert rewrite(("location", "/x")) == [("location", "/sales/x")]


# --- Set-Cookie ---------------------------------------------------------------------------------


def test_path_is_forced_to_the_app_and_domain_is_dropped_with_other_attributes_kept() -> None:
    rewritten = rewrite(
        (
            "Set-Cookie",
            "sid=abc123; Path=/; Domain=example.com; Secure; HttpOnly; SameSite=Lax; Max-Age=3600",
        )
    )
    assert len(rewritten) == 1
    name, value = rewritten[0]
    assert name == "Set-Cookie"
    parts = [part.strip() for part in value.split(";")]
    assert parts[0] == "sid=abc123"
    assert sorted(parts[1:]) == sorted(
        ["Secure", "HttpOnly", "SameSite=Lax", "Max-Age=3600", "Path=/sales/"]
    )


@pytest.mark.parametrize(
    "cookie",
    [
        "sid=1; Path=/api",
        "sid=1; path=/api/v2/",
        "sid=1; PATH=/",
        "sid=1; Path=/; Path=/other",
        "sid=1",
        "sid=1; Secure",
    ],
)
def test_path_ends_as_the_one_prefix_path_whatever_the_child_sent(cookie: str) -> None:
    ((_, value),) = rewrite(("Set-Cookie", cookie))
    paths = [part.strip() for part in value.split(";") if part.strip().lower().startswith("path")]
    assert paths == ["Path=/sales/"]


@pytest.mark.parametrize("attribute", ["Domain=example.com", "domain=.example.com", "DOMAIN=a.b"])
def test_domain_is_dropped_in_any_case(attribute: str) -> None:
    ((_, value),) = rewrite(("Set-Cookie", f"sid=1; {attribute}; Path=/"))
    assert "domain" not in value.lower()


def test_the_cookie_value_is_not_touched_even_when_it_looks_like_an_attribute() -> None:
    ((_, value),) = rewrite(("Set-Cookie", "token=Path=/x=Domain=y; Path=/"))
    assert value.split(";")[0] == "token=Path=/x=Domain=y"


def test_every_set_cookie_line_is_rewritten_on_its_own() -> None:
    rewritten = rewrite(
        ("Set-Cookie", "a=1; Path=/"),
        ("Content-Type", "text/html"),
        ("Set-Cookie", "b=2; Path=/x; Domain=example.com"),
    )
    assert rewritten == [
        ("Set-Cookie", "a=1; Path=/sales/"),
        ("Content-Type", "text/html"),
        ("Set-Cookie", "b=2; Path=/sales/"),
    ]


@pytest.mark.parametrize(
    "name",
    [OPEN_MODE_COOKIE_NAME, OPEN_MODE_COOKIE_NAME.upper(), OPEN_MODE_COOKIE_NAME.lower()],
)
def test_a_child_cookie_named_like_the_edges_open_mode_cookie_is_dropped(name: str) -> None:
    assert OPEN_MODE_COOKIE_NAME == "__Host-appmux_open"
    rewritten = rewrite(
        ("Set-Cookie", f"{name}=as-user; Path=/; Secure; HttpOnly; SameSite=Lax"),
        ("Set-Cookie", "kept=1; Path=/"),
    )
    assert rewritten == [("Set-Cookie", "kept=1; Path=/sales/")]


def test_a_cookie_whose_name_merely_starts_like_it_is_kept() -> None:
    rewritten = rewrite(("Set-Cookie", f"{OPEN_MODE_COOKIE_NAME}_x=1; Path=/"))
    assert rewritten == [("Set-Cookie", f"{OPEN_MODE_COOKIE_NAME}_x=1; Path=/sales/")]


def test_a_cookie_named_with_spaces_around_the_name_is_still_dropped() -> None:
    assert rewrite(("Set-Cookie", f" {OPEN_MODE_COOKIE_NAME} = x; Path=/")) == []


# --- everything else ----------------------------------------------------------------------------


def test_other_headers_pass_unchanged_in_their_order_and_spelling() -> None:
    lines = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("X-Custom", "Path=/ is just text here"),
        ("content-length", "12"),
        ("Content-Encoding", "gzip"),
        ("Content-Location", "/not-rewritten"),
    ]
    assert rewrite(*lines) == lines


def test_the_input_is_not_changed() -> None:
    lines = [("Location", "/x"), ("Set-Cookie", "a=1; Domain=example.com")]
    before = list(lines)
    rewrite_response_headers(lines, slug=SLUG)
    assert lines == before


@pytest.mark.parametrize(
    "name",
    ["Connection", "Keep-Alive", "Transfer-Encoding", "Trailer", "Upgrade", "Proxy-Authenticate"],
)
def test_hop_by_hop_headers_do_not_reach_the_client(name: str) -> None:
    assert rewrite((name, "x"), ("Content-Type", "text/plain")) == [("Content-Type", "text/plain")]


def test_a_header_named_by_connection_does_not_reach_the_client() -> None:
    lines = [("Connection", "close, X-Hop"), ("X-Hop", "1"), ("X-Keep", "2")]
    assert rewrite(*lines) == [("X-Keep", "2")]
