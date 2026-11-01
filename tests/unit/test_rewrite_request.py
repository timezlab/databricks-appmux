"""Request header rewriting: the headers a child receives (edge contract, design 7.1 step 5).

``rewrite_request_headers`` is pure. The five identity headers and ``X-Forwarded-Prefix`` are
written from the ``Actor``, the request id and the slug; every client copy is dropped first, in any
case and however many times it is repeated. ``X-Forwarded-Access-Token`` is forwarded only when the
caller's decision says so.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable

import pytest
from pydantic import SecretStr
from starlette.datastructures import Headers

from appmux.identity.actor import Actor
from appmux.rewrite.request import MalformedHeader, rewrite_request_headers

# Obviously fake values. FORGED marks every value a client supplies, so one assertion can prove
# that none of them reached the child.
FORGED = "forged-by-the-client"
VISITOR_VALUE_MARKER = "placeholder-visitor-token-0001"
REQUEST_ID = "11111111-2222-3333-4444-555555555555"

ACTOR = Actor(
    email="bob@example.com",
    user="1234567890@1111111111111111",
    preferred_username="bob.preferred",
    client_address="198.51.100.9",
    visitor_token=SecretStr(VISITOR_VALUE_MARKER),
)

# Header (canonical spelling) -> the value the child must receive for the ACTOR above.
IDENTITY_VALUES = {
    "X-Forwarded-Email": ACTOR.email,
    "X-Forwarded-User": ACTOR.user,
    "X-Forwarded-Preferred-Username": ACTOR.preferred_username,
    "X-Real-Ip": ACTOR.client_address,
    "X-Request-Id": REQUEST_ID,
}

HeaderList = list[tuple[str, str]]


def rewrite(
    headers: HeaderList,
    *,
    actor: Actor = ACTOR,
    slug: str = "sales",
    request_id: str | None = REQUEST_ID,
    forward_access_token: bool = False,
) -> HeaderList:
    return rewrite_request_headers(
        headers,
        actor=actor,
        slug=slug,
        request_id=request_id,
        forward_access_token=forward_access_token,
    )


def values_of(headers: HeaderList, name: str) -> list[str]:
    """Every value sent under ``name``, matched in any case, in order."""
    return [value for key, value in headers if key.lower() == name.lower()]


def names_of(headers: HeaderList, name: str) -> list[str]:
    """The spelling of every line sent under ``name``."""
    return [key for key, _ in headers if key.lower() == name.lower()]


def alternating_case(name: str) -> str:
    return "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(name))


SPELLINGS: dict[str, Callable[[str], str]] = {
    "canonical": lambda name: name,
    "lower": str.lower,
    "upper": str.upper,
    "mixed": alternating_case,
}


def the_proxys_request() -> HeaderList:
    """What the edge receives from the Databricks proxy for ``GET /sales/api/orders``."""
    return [
        ("Host", "appmux.example.invalid"),
        ("X-Forwarded-Email", ACTOR.email),
        ("X-Forwarded-User", str(ACTOR.user)),
        ("X-Forwarded-Preferred-Username", str(ACTOR.preferred_username)),
        ("X-Real-Ip", str(ACTOR.client_address)),
        ("X-Request-Id", REQUEST_ID),
        ("X-Forwarded-For", "192.0.2.66,198.51.100.9,203.0.113.10"),
        ("Accept", "application/json"),
    ]


def test_a_proxy_request_gets_the_table_of_the_contract() -> None:
    result = rewrite(the_proxys_request())

    assert result == [
        ("Host", "appmux.example.invalid"),
        ("X-Forwarded-For", "192.0.2.66,198.51.100.9,203.0.113.10"),
        ("Accept", "application/json"),
        ("X-Forwarded-Email", "bob@example.com"),
        ("X-Forwarded-User", "1234567890@1111111111111111"),
        ("X-Forwarded-Preferred-Username", "bob.preferred"),
        ("X-Real-Ip", "198.51.100.9"),
        ("X-Request-Id", REQUEST_ID),
        ("X-Forwarded-Prefix", "/sales"),
    ]


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("header", IDENTITY_VALUES)
class TestIdentityForgery:
    """A client copy of an identity header never reaches the child, whatever its spelling."""

    def test_one_forged_line_is_replaced(self, header: str, spelling: str) -> None:
        forged = SPELLINGS[spelling](header)

        result = rewrite([("Accept", "*/*"), (forged, FORGED)])

        assert values_of(result, header) == [IDENTITY_VALUES[header]]
        assert names_of(result, header) == [header]
        assert not any(FORGED in value for _, value in result)

    def test_repeated_forged_lines_are_all_dropped(self, header: str, spelling: str) -> None:
        forged = SPELLINGS[spelling](header)
        incoming = [
            (forged, FORGED + "-1"),
            ("Accept", "*/*"),
            (header.lower(), FORGED + "-2"),
            (forged, FORGED + "-3"),
            (header.upper(), FORGED + "-4"),
        ]

        result = rewrite(incoming)

        assert values_of(result, header) == [IDENTITY_VALUES[header]]
        assert not any(FORGED in value for _, value in result)
        assert values_of(result, "Accept") == ["*/*"]


@pytest.mark.parametrize(
    ("actor", "request_id", "absent_header"),
    [
        (Actor(email="bob@example.com", user=None), REQUEST_ID, "X-Forwarded-User"),
        (
            Actor(email="bob@example.com", preferred_username=None),
            REQUEST_ID,
            "X-Forwarded-Preferred-Username",
        ),
        (Actor(email="bob@example.com", client_address=None), REQUEST_ID, "X-Real-Ip"),
        (ACTOR, None, "X-Request-Id"),
    ],
)
def test_a_value_the_proxy_did_not_send_is_not_invented_from_a_client_copy(
    actor: Actor, request_id: str | None, absent_header: str
) -> None:
    result = rewrite([(absent_header.lower(), FORGED)], actor=actor, request_id=request_id)

    assert values_of(result, absent_header) == []
    assert not any(FORGED in value for _, value in result)


@pytest.mark.parametrize(
    ("actor", "request_id", "absent_header"),
    [
        (Actor(email="bob@example.com", user=""), REQUEST_ID, "X-Forwarded-User"),
        (
            Actor(email="bob@example.com", preferred_username=""),
            REQUEST_ID,
            "X-Forwarded-Preferred-Username",
        ),
        (Actor(email="bob@example.com", client_address=""), REQUEST_ID, "X-Real-Ip"),
        # The edge passes the proxy's X-Request-Id as it read it; an empty one means "absent".
        (ACTOR, "", "X-Request-Id"),
    ],
)
def test_an_empty_value_is_left_out_not_written_as_an_empty_header(
    actor: Actor, request_id: str | None, absent_header: str
) -> None:
    result = rewrite([(absent_header.lower(), FORGED)], actor=actor, request_id=request_id)

    assert names_of(result, absent_header) == []
    assert not any(FORGED in value for _, value in result)


def test_an_empty_visitor_token_is_not_written_as_an_empty_header() -> None:
    actor = Actor(email="bob@example.com", visitor_token=SecretStr(""))

    result = rewrite([("X-Forwarded-Access-Token", FORGED)], actor=actor, forward_access_token=True)

    assert names_of(result, "X-Forwarded-Access-Token") == []
    assert not any(FORGED in value for _, value in result)


def test_the_actors_email_is_the_one_the_child_sees() -> None:
    """``Actor.email`` is the lower-cased form; the child gets exactly what the actor holds."""
    actor = Actor(email="bob@example.com")

    result = rewrite([("X-Forwarded-Email", "Bob@Example.COM")], actor=actor)

    assert values_of(result, "X-Forwarded-Email") == ["bob@example.com"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_forwarded_prefix_is_the_slug_and_replaces_client_copies(spelling: str) -> None:
    forged = SPELLINGS[spelling]("X-Forwarded-Prefix")

    result = rewrite([(forged, "/hr"), (forged, "/admin")], slug="sales-eu")

    assert values_of(result, "X-Forwarded-Prefix") == ["/sales-eu"]
    assert names_of(result, "X-Forwarded-Prefix") == ["X-Forwarded-Prefix"]


class TestAccessToken:
    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_it_is_removed_unless_the_decision_allows_it(self, spelling: str) -> None:
        forged = SPELLINGS[spelling]("X-Forwarded-Access-Token")
        proxys_copy = [("X-Forwarded-Access-Token", VISITOR_VALUE_MARKER)]

        result = rewrite([*proxys_copy, (forged, FORGED)], forward_access_token=False)

        assert values_of(result, "X-Forwarded-Access-Token") == []
        assert not any(VISITOR_VALUE_MARKER in value or FORGED in value for _, value in result)

    @pytest.mark.parametrize("spelling", SPELLINGS)
    def test_it_is_the_visitors_token_when_the_decision_allows_it(self, spelling: str) -> None:
        forged = SPELLINGS[spelling]("X-Forwarded-Access-Token")

        result = rewrite(
            [(forged, FORGED), ("X-Forwarded-Access-Token", FORGED + "-2"), (forged, FORGED)],
            forward_access_token=True,
        )

        assert values_of(result, "X-Forwarded-Access-Token") == [VISITOR_VALUE_MARKER]
        assert names_of(result, "X-Forwarded-Access-Token") == ["X-Forwarded-Access-Token"]
        assert not any(FORGED in value for _, value in result)

    def test_allowing_it_without_a_token_forwards_none_and_never_a_client_copy(self) -> None:
        actor = Actor(email="bob@example.com", visitor_token=None)

        result = rewrite(
            [("x-forwarded-access-token", FORGED)], actor=actor, forward_access_token=True
        )

        assert values_of(result, "X-Forwarded-Access-Token") == []
        assert not any(FORGED in value for _, value in result)

    def test_the_token_is_taken_from_the_actor_not_from_the_incoming_lines(self) -> None:
        result = rewrite(
            [("X-Forwarded-Access-Token", "a-different-token")], forward_access_token=True
        )

        assert values_of(result, "X-Forwarded-Access-Token") == [VISITOR_VALUE_MARKER]

    def test_it_is_the_last_line_written_after_the_prefix(self) -> None:
        result = rewrite(the_proxys_request(), forward_access_token=True)

        assert result == [
            ("Host", "appmux.example.invalid"),
            ("X-Forwarded-For", "192.0.2.66,198.51.100.9,203.0.113.10"),
            ("Accept", "application/json"),
            ("X-Forwarded-Email", "bob@example.com"),
            ("X-Forwarded-User", "1234567890@1111111111111111"),
            ("X-Forwarded-Preferred-Username", "bob.preferred"),
            ("X-Real-Ip", "198.51.100.9"),
            ("X-Request-Id", REQUEST_ID),
            ("X-Forwarded-Prefix", "/sales"),
            ("X-Forwarded-Access-Token", VISITOR_VALUE_MARKER),
        ]

    def test_the_decision_has_no_default_so_a_caller_that_omits_it_fails_loudly(self) -> None:
        """A default of ``True`` would hand every visitor's token to every child."""
        with pytest.raises(TypeError, match="forward_access_token"):
            rewrite_request_headers([], actor=ACTOR, slug="sales", request_id=REQUEST_ID)


HOP_BY_HOP_HEADERS = [
    "Connection",
    "Keep-Alive",
    "Proxy-Authenticate",
    "Proxy-Authorization",
    "Proxy-Connection",
    "TE",
    "Trailer",
    "Transfer-Encoding",
    "Upgrade",
]

# The client's WebSocket handshake belongs to one connection; the relay makes its own to the child.
WEBSOCKET_HANDSHAKE_HEADERS = [
    "Sec-WebSocket-Key",
    "Sec-WebSocket-Version",
    "Sec-WebSocket-Extensions",
    "Sec-WebSocket-Protocol",
    "Sec-WebSocket-Accept",
]


class TestHopByHop:
    @pytest.mark.parametrize("spelling", SPELLINGS)
    @pytest.mark.parametrize("header", [*HOP_BY_HOP_HEADERS, *WEBSOCKET_HANDSHAKE_HEADERS])
    def test_a_per_connection_header_is_stripped(self, header: str, spelling: str) -> None:
        incoming = [
            ("Accept", "*/*"),
            (SPELLINGS[spelling](header), "x"),
            ("Accept-Language", "en"),
        ]

        result = rewrite(incoming)

        assert values_of(result, header) == []
        assert values_of(result, "Accept") == ["*/*"]
        assert values_of(result, "Accept-Language") == ["en"]

    def test_a_header_named_in_connection_is_stripped_across_lines_and_cases(self) -> None:
        incoming = [
            ("Connection", "close, X-Hop-One"),
            ("X-Hop-One", "1"),
            ("connection", " keep-alive ,x-HOP-two,, "),
            ("x-hop-two", "2"),
            ("X-Hop-Two", "2b"),
            ("X-End-To-End", "3"),
        ]

        result = rewrite(incoming)

        assert values_of(result, "X-Hop-One") == []
        assert values_of(result, "X-Hop-Two") == []
        assert values_of(result, "X-End-To-End") == ["3"]
        assert values_of(result, "Connection") == []

    def test_a_connection_line_may_name_content_length_and_it_is_then_removed(self) -> None:
        """RFC 9110 section 7.6.1 has no exception for it; the upstream client frames the body."""
        incoming = [
            ("Connection", "Content-Length"),
            ("Content-Length", "12"),
            ("Content-Type", "application/json"),
        ]

        result = rewrite(incoming)

        assert values_of(result, "Content-Length") == []
        assert values_of(result, "Content-Type") == ["application/json"]

    def test_naming_a_proxy_owned_header_in_connection_does_not_take_it_from_the_child(
        self,
    ) -> None:
        incoming = [
            (
                "Connection",
                "X-Forwarded-Email, X-Forwarded-Prefix, X-Forwarded-Access-Token, X-Request-Id",
            )
        ]

        result = rewrite(incoming, forward_access_token=True)

        assert values_of(result, "X-Forwarded-Email") == [ACTOR.email]
        assert values_of(result, "X-Forwarded-Prefix") == ["/sales"]
        assert values_of(result, "X-Forwarded-Access-Token") == [VISITOR_VALUE_MARKER]
        assert values_of(result, "X-Request-Id") == [REQUEST_ID]

    def test_end_to_end_headers_are_not_stripped(self) -> None:
        incoming = [
            ("Content-Type", "application/json"),
            ("Content-Length", "12"),
            ("Cookie", "a=1"),
            ("Authorization", "Bearer placeholder"),
            ("Expect", "100-continue"),
        ]

        assert rewrite(incoming)[: len(incoming)] == incoming


class TestPassThrough:
    def test_other_forwarded_and_appmux_and_authorization_headers_pass_unchanged(self) -> None:
        incoming = [
            ("X-Forwarded-For", "192.0.2.66,198.51.100.9"),
            ("x-forwarded-host", "appmux.example.invalid"),
            ("X-Forwarded-Proto", "https"),
            ("X-Appmux-Trace", "abc"),
            ("authorization", "Bearer placeholder-client-credential"),
        ]

        assert rewrite(incoming)[: len(incoming)] == incoming

    def test_host_passes_through_unchanged_and_in_place(self) -> None:
        incoming = [
            ("Accept", "*/*"),
            ("host", "appmux.example.invalid"),
            ("Accept-Language", "en"),
        ]

        assert rewrite(incoming)[:3] == incoming

    def test_two_host_lines_are_refused(self) -> None:
        with pytest.raises(MalformedHeader):
            rewrite([("Host", "appmux.example.invalid"), ("host", "other.example.invalid")])

    def test_unrelated_headers_keep_their_order_spelling_and_repeats(self) -> None:
        incoming = [
            ("Accept", "a"),
            ("X-Forwarded-Email", FORGED),
            ("Cookie", "one=1"),
            ("Connection", "close"),
            ("x-appmux-trace", "t"),
            ("Cookie", "two=2"),
            ("X-Forwarded-Prefix", FORGED),
            ("ACCEPT-ENCODING", "gzip"),
        ]

        result = rewrite(incoming)

        assert result[:5] == [
            ("Accept", "a"),
            ("Cookie", "one=1"),
            ("x-appmux-trace", "t"),
            ("Cookie", "two=2"),
            ("ACCEPT-ENCODING", "gzip"),
        ]

    def test_a_starlette_headers_object_with_repeated_lines_works(self) -> None:
        headers = Headers(
            raw=[
                (b"x-forwarded-email", FORGED.encode()),
                (b"x-forwarded-email", FORGED.encode()),
                (b"accept", b"*/*"),
            ]
        )

        result = rewrite_request_headers(
            headers, actor=ACTOR, slug="sales", request_id=REQUEST_ID, forward_access_token=False
        )

        assert values_of(result, "X-Forwarded-Email") == [ACTOR.email]
        assert values_of(result, "accept") == ["*/*"]

    def test_a_mapping_works(self) -> None:
        result = rewrite_request_headers(
            {"accept": "*/*", "X-FORWARDED-EMAIL": FORGED},
            actor=ACTOR,
            slug="sales",
            request_id=REQUEST_ID,
            forward_access_token=False,
        )

        assert values_of(result, "X-Forwarded-Email") == [ACTOR.email]
        assert values_of(result, "accept") == ["*/*"]


class TestInputIsNeverMutated:
    def test_the_input_list_and_its_pairs_are_unchanged(self) -> None:
        incoming = [
            *the_proxys_request(),
            ("X-Forwarded-Email", FORGED),
            ("Connection", "close, X-Hop"),
            ("X-Hop", "1"),
            ("X-Forwarded-Access-Token", VISITOR_VALUE_MARKER),
        ]
        before = copy.deepcopy(incoming)

        result = rewrite(incoming, forward_access_token=True)

        assert incoming == before
        assert result is not incoming

    def test_changing_the_result_does_not_change_the_input(self) -> None:
        incoming = [("Accept", "*/*")]

        result = rewrite(incoming)
        result.clear()

        assert incoming == [("Accept", "*/*")]

    def test_a_mapping_input_is_unchanged(self) -> None:
        incoming = {"accept": "*/*", "X-Forwarded-Email": FORGED, "Connection": "close"}
        before = dict(incoming)

        rewrite_request_headers(
            incoming, actor=ACTOR, slug="sales", request_id=REQUEST_ID, forward_access_token=True
        )

        assert incoming == before


# Values that would end the header line and start a new one (or cut the value short).
LINE_BREAKING_VALUES = [
    "carriage\rreturn",
    "line\nfeed",
    "both\r\nX-Injected: 1",
    "trailing\r\n",
    "nul\x00byte",
]


class TestMalformedLinesAreRefused:
    @pytest.mark.parametrize("value", LINE_BREAKING_VALUES)
    @pytest.mark.parametrize(
        "header",
        [
            "Accept",  # unrelated
            "x-forwarded-email",  # a forged copy that would be dropped anyway
            "X-Forwarded-Access-Token",  # a credential
            "Host",
            "Connection",
        ],
    )
    def test_a_value_with_cr_lf_or_nul_is_refused_in_any_line(
        self, header: str, value: str
    ) -> None:
        with pytest.raises(MalformedHeader) as refusal:
            rewrite([("Accept-Language", "en"), (header, value)])

        message = str(refusal.value)
        assert value not in message
        assert "X-Injected" not in message

    def test_the_message_names_the_line_not_the_value(self) -> None:
        with pytest.raises(MalformedHeader) as refusal:
            rewrite(
                [("Accept", "*/*"), ("X-Forwarded-Access-Token", VISITOR_VALUE_MARKER + "\r\n")]
            )

        assert VISITOR_VALUE_MARKER not in str(refusal.value)
        assert "X-Forwarded-Access-Token".lower() in str(refusal.value).lower()

    def test_the_message_gives_the_position_of_a_value_with_a_line_break(self) -> None:
        with pytest.raises(MalformedHeader, match=r"\bline 2\b"):
            rewrite([("Accept", "*/*"), ("Accept-Language", "a\r\nb")])

    def test_the_message_gives_the_position_of_a_name_that_is_not_a_token(self) -> None:
        with pytest.raises(MalformedHeader, match=r"\bline 3\b"):
            rewrite([("Accept", "*/*"), ("Accept-Language", "en"), ("Bad Name", "v")])

    def test_the_message_gives_the_position_of_the_second_host_line(self) -> None:
        incoming = [
            ("Accept", "*/*"),
            ("Host", "appmux.example.invalid"),
            ("Accept-Language", "en"),
            ("host", "other.example.invalid"),
        ]

        with pytest.raises(MalformedHeader, match=r"\bline 4\b"):
            rewrite(incoming)

    @pytest.mark.parametrize("spelling", SPELLINGS)
    @pytest.mark.parametrize("length_first", [True, False])
    def test_content_length_with_transfer_encoding_is_refused(
        self, length_first: bool, spelling: str
    ) -> None:
        """RFC 9112 section 6.1: the server framed the body by one header and the declared length
        of the other would reach the child, so the two readings of the message could disagree."""
        length = (SPELLINGS[spelling]("Content-Length"), "5")
        encoding = (SPELLINGS[spelling]("Transfer-Encoding"), "chunked")
        framing = [length, encoding] if length_first else [encoding, length]

        with pytest.raises(MalformedHeader, match=r"\bline 3\b") as refusal:
            rewrite([("Accept", "*/*"), *framing])

        assert "chunked" not in str(refusal.value)

    @pytest.mark.parametrize("name", ["Bad Name", "", "X:Y", "X\r\nY", "Név", "X-Bad\n"])
    def test_a_name_that_is_not_an_http_token_is_refused(self, name: str) -> None:
        with pytest.raises(MalformedHeader) as refusal:
            rewrite([(name, "value")])

        if name:
            assert name not in str(refusal.value)

    @pytest.mark.parametrize(
        ("actor", "slug", "request_id"),
        [
            (Actor(email="bob@example.com\r\nX-Injected: 1"), "sales", REQUEST_ID),
            (Actor(email="bob@example.com", user="a\nb"), "sales", REQUEST_ID),
            (Actor(email="bob@example.com", preferred_username="a\rb"), "sales", REQUEST_ID),
            (ACTOR, "sales\r\nX-Injected: 1", REQUEST_ID),
            (ACTOR, "sales", "id\r\nX-Injected: 1"),
        ],
    )
    def test_a_value_this_module_would_write_is_checked_too(
        self, actor: Actor, slug: str, request_id: str
    ) -> None:
        with pytest.raises(MalformedHeader) as refusal:
            rewrite([], actor=actor, slug=slug, request_id=request_id)

        assert "X-Injected" not in str(refusal.value)

    def test_the_visitors_token_with_a_line_break_is_refused_without_echoing_it(self) -> None:
        actor = Actor(
            email="bob@example.com", visitor_token=SecretStr(VISITOR_VALUE_MARKER + "\r\nX: 1")
        )

        with pytest.raises(MalformedHeader) as refusal:
            rewrite([], actor=actor, forward_access_token=True)

        assert VISITOR_VALUE_MARKER not in str(refusal.value)

    def test_a_token_with_a_line_break_is_not_checked_when_it_is_not_forwarded(self) -> None:
        actor = Actor(email="bob@example.com", visitor_token=SecretStr("a\r\nb"))

        result = rewrite([], actor=actor, forward_access_token=False)

        assert values_of(result, "X-Forwarded-Access-Token") == []


def test_no_header_value_reaches_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    secret_line = ("X-Forwarded-Access-Token", VISITOR_VALUE_MARKER)
    with caplog.at_level(logging.DEBUG):
        rewrite([*the_proxys_request(), secret_line], forward_access_token=True)
        with pytest.raises(MalformedHeader):
            rewrite([secret_line, ("Accept", "a\r\nb")], forward_access_token=True)

    for value in (VISITOR_VALUE_MARKER, ACTOR.email, REQUEST_ID, "application/json"):
        assert value not in caplog.text
