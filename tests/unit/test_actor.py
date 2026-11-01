"""Forwarded headers become an ``Actor``; a request without an identity is refused.

Design 7.1 and the edge contract: the Databricks proxy overwrites the identity headers and
``X-Real-Ip`` (spike 8), so the client address is ``X-Real-Ip``. It appends to
``X-Forwarded-For`` after a client-supplied value, so that header is never the client address.
An identity header that appears twice is ambiguous and the request is refused.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence

import pytest
from starlette.datastructures import Headers

from appmux.identity.actor import Actor, AmbiguousIdentity, MissingIdentity, actor_from_headers

# An obviously fake token; tests check that it never shows up in a repr.
VISITOR_VALUE_MARKER = "placeholder-visitor-token-0001"

FULL_HEADERS: Mapping[str, str] = {
    "X-Forwarded-Email": "bob@example.com",
    "X-Forwarded-User": "1234567890@1111111111111111",
    "X-Forwarded-Preferred-Username": "bob@example.com",
    # The shape spike 8 measured: the client-supplied value first, then the real client and the
    # proxy hops (documentation addresses stand in for the redacted ones).
    "X-Forwarded-For": "192.0.2.66,198.51.100.9,203.0.113.10, 203.0.113.11",
    "X-Forwarded-Access-Token": VISITOR_VALUE_MARKER,
    "X-Real-Ip": "198.51.100.9",
    "X-Request-Id": "11111111-2222-3333-4444-555555555555",
}


def headers_without(name: str) -> dict[str, str]:
    return {key: value for key, value in FULL_HEADERS.items() if key.lower() != name.lower()}


def test_forwarded_headers_become_an_actor() -> None:
    actor = actor_from_headers(FULL_HEADERS)

    assert actor.email == "bob@example.com"
    assert actor.user == "1234567890@1111111111111111"
    assert actor.preferred_username == "bob@example.com"
    assert actor.client_address == "198.51.100.9"
    assert actor.visitor_token is not None
    assert actor.visitor_token.get_secret_value() == VISITOR_VALUE_MARKER


def test_header_names_match_in_any_case() -> None:
    lower_case = {key.lower(): value for key, value in FULL_HEADERS.items()}
    upper_case = {key.upper(): value for key, value in FULL_HEADERS.items()}

    assert actor_from_headers(lower_case) == actor_from_headers(FULL_HEADERS)
    assert actor_from_headers(upper_case) == actor_from_headers(FULL_HEADERS)


def test_a_starlette_headers_object_works_as_the_mapping() -> None:
    actor = actor_from_headers(Headers(dict(FULL_HEADERS)))

    assert actor.email == "bob@example.com"
    assert actor.client_address == "198.51.100.9"


def test_the_email_is_lower_cased_and_trimmed() -> None:
    actor = actor_from_headers({"X-Forwarded-Email": "  Bob@Example.COM "})

    assert actor.email == "bob@example.com"


def test_no_forwarded_email_raises_missing_identity() -> None:
    with pytest.raises(MissingIdentity):
        actor_from_headers(headers_without("X-Forwarded-Email"))


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_forwarded_email_raises_missing_identity(value: str) -> None:
    with pytest.raises(MissingIdentity):
        actor_from_headers({**FULL_HEADERS, "X-Forwarded-Email": value})


def test_no_headers_at_all_raises_missing_identity() -> None:
    with pytest.raises(MissingIdentity):
        actor_from_headers({})


def test_only_the_email_is_required() -> None:
    actor = actor_from_headers({"X-Forwarded-Email": "bob@example.com"})

    assert actor == Actor(
        email="bob@example.com",
        user=None,
        preferred_username=None,
        client_address=None,
        visitor_token=None,
    )


def test_the_visitor_token_is_absent_when_user_authorization_is_off() -> None:
    assert actor_from_headers(headers_without("X-Forwarded-Access-Token")).visitor_token is None


def test_an_empty_token_header_is_no_token() -> None:
    assert (
        actor_from_headers({**FULL_HEADERS, "X-Forwarded-Access-Token": ""}).visitor_token is None
    )


@pytest.mark.parametrize(
    ("real_ip", "expected"),
    [
        ("198.51.100.9", "198.51.100.9"),
        ("  198.51.100.9  ", "198.51.100.9"),
        ("\t198.51.100.9", "198.51.100.9"),
        ("2001:db8::1", "2001:db8::1"),
        ("2001:DB8:0:0:0:0:0:1", "2001:db8::1"),
        ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
    ],
)
def test_client_address_is_the_canonical_form_of_x_real_ip(real_ip: str, expected: str) -> None:
    actor = actor_from_headers({**FULL_HEADERS, "X-Real-Ip": real_ip})

    assert actor.client_address == expected


def test_the_client_address_is_x_real_ip_and_not_a_forwarded_for_entry() -> None:
    # Spike 8: the proxy overwrites X-Real-Ip and appends to X-Forwarded-For, so the rightmost
    # X-Forwarded-For entry is a proxy hop and the leftmost one is whatever the client sent.
    actor = actor_from_headers(FULL_HEADERS)

    assert actor.client_address == "198.51.100.9"
    assert actor.client_address != "203.0.113.11"
    assert actor.client_address != "192.0.2.66"


def test_a_client_supplied_forwarded_for_cannot_choose_the_address() -> None:
    headers = {**FULL_HEADERS, "X-Forwarded-For": "192.0.2.66", "X-Real-Ip": "198.51.100.9"}

    assert actor_from_headers(headers).client_address == "198.51.100.9"


@pytest.mark.parametrize("forwarded_for", ["198.51.100.9", "192.0.2.66, 198.51.100.9", ""])
def test_forwarded_for_alone_gives_no_client_address(forwarded_for: str) -> None:
    headers = {**headers_without("X-Real-Ip"), "X-Forwarded-For": forwarded_for}

    assert actor_from_headers(headers).client_address is None


def test_no_x_real_ip_gives_no_client_address() -> None:
    assert actor_from_headers(headers_without("X-Real-Ip")).client_address is None


@pytest.mark.parametrize(
    "real_ip",
    [
        "",
        "   ",
        "unknown",
        "198.51.100.9:443",
        "[2001:db8::1]:443",
        "999.1.1.1",
        "198.51.100.9, 203.0.113.11",
        "198.51.100.09",
        "198.51.100.9/24",
        # An IPv6 zone id: ``ipaddress`` accepts any text after the percent sign.
        "fe80::1%eth0",
        "fe80::1%\x1b[31mred",
        "fe80::1%x\ny",
        "fe80::1%a b",
        # Control characters and non-ASCII text, which must never reach a log line.
        "198.51.100.9\n",
        "198.51.100.9\r\nX-Injected: 1",
        "198.51.100.9\x00",
        "198.51.100.9\x1b[31m",
        "\u2003198.51.100.9",
        "198.51.100.9\u200b",
        "\u0661\u0669\u0668.\u0665\u0661.\u0661\u0660\u0660.\u0669",
        "\uff11\uff19\uff18.\uff15\uff11.\uff11\uff10\uff10.\uff19",
    ],
)
def test_an_unusable_x_real_ip_gives_no_client_address(real_ip: str) -> None:
    actor = actor_from_headers({**FULL_HEADERS, "X-Real-Ip": real_ip})

    assert actor.client_address is None


def test_a_client_address_never_holds_a_zone_id_or_a_control_character() -> None:
    for real_ip in ("fe80::1%eth0", "fe80::1%\x1b[31mred", "fe80::1%x\ny", "fe80::1%a b"):
        address = actor_from_headers({**FULL_HEADERS, "X-Real-Ip": real_ip}).client_address

        assert address is None or (
            address.isascii() and address.isprintable() and "%" not in address
        )


# --- Ambiguous identity -----------------------------------------------------------------------

# One value per header; the same header twice means the request did not come straight from the
# proxy, which overwrites these (spike 8).
SINGLE_VALUE_HEADERS = [
    "X-Forwarded-Email",
    "X-Forwarded-User",
    "X-Forwarded-Preferred-Username",
    "X-Forwarded-Access-Token",
    "X-Real-Ip",
]
HeaderInput = Mapping[str, str] | Sequence[tuple[str, str]]
FIRST_LINE_MARKER = "first-line-0001"
SECOND_LINE_MARKER = "second-line-0002"


def with_the_header_twice(name: str, first: str, second: str) -> list[tuple[str, str]]:
    """Every header of ``FULL_HEADERS`` as raw lines, with ``name`` on two lines."""
    lines = [(key, value) for key, value in FULL_HEADERS.items() if key.lower() != name.lower()]
    return [*lines, (name.lower(), first), (name.lower(), second)]


def as_starlette_headers(lines: Sequence[tuple[str, str]]) -> Headers:
    return Headers(raw=[(key.lower().encode(), value.encode()) for key, value in lines])


def as_mixed_case_dict(lines: Sequence[tuple[str, str]]) -> dict[str, str]:
    # A plain dict cannot hold one name twice, but it can hold it in two spellings.
    seen: dict[str, int] = {}
    result: dict[str, str] = {}
    for key, value in lines:
        count = seen.get(key.lower(), 0)
        seen[key.lower()] = count + 1
        result[key.upper() if count else key] = value
    return result


@pytest.mark.parametrize("header", SINGLE_VALUE_HEADERS)
@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(as_starlette_headers, id="starlette-headers"),
        pytest.param(list, id="list-of-pairs"),
        pytest.param(as_mixed_case_dict, id="dict-with-two-spellings"),
    ],
)
def test_an_identity_header_on_two_lines_is_refused_naming_the_header(
    header: str, shape: Callable[[Sequence[tuple[str, str]]], HeaderInput]
) -> None:
    lines = with_the_header_twice(header, FIRST_LINE_MARKER, SECOND_LINE_MARKER)

    with pytest.raises(MissingIdentity, match=header) as raised:
        actor_from_headers(shape(lines))

    assert FIRST_LINE_MARKER not in str(raised.value)
    assert SECOND_LINE_MARKER not in str(raised.value)


@pytest.mark.parametrize("header", SINGLE_VALUE_HEADERS)
def test_an_identity_header_repeated_with_the_same_value_is_still_refused(header: str) -> None:
    value = "198.51.100.9" if header == "X-Real-Ip" else "bob@example.com"
    lines = with_the_header_twice(header, value, value)

    with pytest.raises(MissingIdentity, match=header):
        actor_from_headers(as_starlette_headers(lines))


def test_an_ambiguous_identity_is_a_missing_identity_that_names_the_header() -> None:
    lines = with_the_header_twice("X-Forwarded-Email", FIRST_LINE_MARKER, SECOND_LINE_MARKER)

    with pytest.raises(AmbiguousIdentity) as raised:
        actor_from_headers(lines)

    # The edge answers 401 to every MissingIdentity, so an ambiguous request gets it too.
    assert isinstance(raised.value, MissingIdentity)
    assert raised.value.header == "X-Forwarded-Email"


def test_a_second_access_token_line_never_replaces_or_joins_the_first() -> None:
    lines = with_the_header_twice("X-Forwarded-Access-Token", VISITOR_VALUE_MARKER, "other")

    with pytest.raises(MissingIdentity, match="X-Forwarded-Access-Token"):
        actor_from_headers(as_starlette_headers(lines))


def test_a_blank_second_line_of_the_email_is_still_ambiguous() -> None:
    lines = with_the_header_twice("X-Forwarded-Email", "bob@example.com", "")

    with pytest.raises(MissingIdentity, match="X-Forwarded-Email"):
        actor_from_headers(as_starlette_headers(lines))


def test_a_list_of_pairs_works_as_the_headers() -> None:
    actor = actor_from_headers(list(FULL_HEADERS.items()))

    assert actor == actor_from_headers(FULL_HEADERS)


def test_a_header_that_is_not_an_identity_header_may_repeat() -> None:
    lines = [*FULL_HEADERS.items(), ("x-request-id", "other"), ("x-forwarded-for", "192.0.2.1")]

    assert actor_from_headers(as_starlette_headers(lines)) == actor_from_headers(FULL_HEADERS)


def test_the_visitor_token_never_appears_in_a_repr_or_str() -> None:
    actor = actor_from_headers(FULL_HEADERS)

    assert VISITOR_VALUE_MARKER not in repr(actor)
    assert VISITOR_VALUE_MARKER not in str(actor)
    assert VISITOR_VALUE_MARKER not in str(dataclasses.replace(actor, email="x@example.com"))


@pytest.mark.parametrize("field_name", [field.name for field in dataclasses.fields(Actor)])
def test_an_actor_cannot_be_changed(field_name: str) -> None:
    actor = actor_from_headers(FULL_HEADERS)

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(actor, field_name, "mallory@example.com")


def test_two_actors_from_the_same_headers_are_equal_and_hashable() -> None:
    first = actor_from_headers(FULL_HEADERS)
    second = actor_from_headers(FULL_HEADERS)

    assert first == second
    assert len({first, second}) == 1
