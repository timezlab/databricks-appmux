"""The request headers a child receives (edge contract, design 7.1 step 5).

``rewrite_request_headers`` is pure: header lines in, header lines out, no I/O, no logging and no
mutation of its input. It does not decide who may see what. ``forward_access_token`` is a decision
the caller computed from ``authz``; this module only carries it out, so it imports neither ``authz``
nor ``routing`` (design 3.4).

The seven headers the proxy owns are never copied from the incoming lines. Every client copy is
dropped, in any case and however often it is repeated, and each is written once from a trusted
argument: the identity values from the ``Actor`` (which ``actor_from_headers`` built from lines it
had already checked), the request id and the slug from the caller. That is why a repeated forgery
has nothing to win: there is no "which line is the proxy's" choice to get wrong. A value the proxy
did not send is left out, never filled from a client line.

The edge's own open-mode cookie (``__Host-appmux_open``, design 9.2) is removed from the ``Cookie``
lines too: it is the edge's, not the child's, and carries the admin's per-app choice. The other
cookie pairs, and any line without it, pass untouched.

A line that cannot be forwarded safely raises ``MalformedHeader`` instead of being repaired,
because every check here sits on an auth path: a name that is not an HTTP token, a value with CR, LF
or NUL (header injection), a second ``Host``, or both ``Content-Length`` and ``Transfer-Encoding``
(two body framings). The edge answers ``400``. An error message names a line's position and, once
its name is known to be a token, the name; it never carries a value, since
``X-Forwarded-Access-Token`` is a credential.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from appmux.identity.actor import (
    ACCESS_TOKEN_HEADER,
    EMAIL_HEADER,
    PREFERRED_USERNAME_HEADER,
    REAL_IP_HEADER,
    USER_HEADER,
    Actor,
    HeaderLines,
)
from appmux.rewrite.response import OPEN_MODE_COOKIE_NAME

# Lower case: header names are folded before they are compared.
REQUEST_ID_HEADER: Final = "x-request-id"
PREFIX_HEADER: Final = "x-forwarded-prefix"

# Written by this module and by nothing else: any client copy is dropped before they are added.
_PROXY_OWNED_HEADERS: Final = frozenset(
    {
        EMAIL_HEADER,
        USER_HEADER,
        PREFERRED_USERNAME_HEADER,
        REAL_IP_HEADER,
        REQUEST_ID_HEADER,
        PREFIX_HEADER,
        ACCESS_TOKEN_HEADER,
    }
)

# RFC 9110 section 7.6.1 plus ``Proxy-Connection`` (sent by old clients). They describe the client's
# connection to the proxy, not the request, so they do not travel on to the child's connection.
_HOP_BY_HOP_HEADERS: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# A WebSocket handshake (``Upgrade``, ``Connection``, ``Sec-WebSocket-*``) belongs to one
# connection. The relay opens its own handshake to the child (design 7.1 step 6), so the client's
# lines are not forwarded and a second copy never reaches the child's handshake.
_WEBSOCKET_HANDSHAKE_PREFIX: Final = "sec-websocket-"

_CONNECTION_HEADER: Final = "connection"
_HOST_HEADER: Final = "host"
_COOKIE_HEADER: Final = "cookie"

# RFC 9112 section 6.1: a request with both has two framings, and the edge's server and the child
# could read different bodies from it. Transfer-Encoding is dropped from the forwarded lines and the
# upstream client frames the body itself, so a client-declared Content-Length must not ride along
# next to a Transfer-Encoding the server may have obeyed instead.
_BODY_FRAMING_HEADERS: Final = frozenset({"content-length", "transfer-encoding"})

# RFC 9110 section 5.6.2: a header name is a token.
_TOKEN: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

# A value holding one of these ends the header line early or starts another (header injection).
_FORBIDDEN_VALUE_CHARACTERS: Final = frozenset("\r\n\x00")

# Optional whitespace around a list element (RFC 9110 section 5.6.1).
_LIST_WHITESPACE: Final = " \t"


class MalformedHeader(ValueError):
    """A request header line cannot be forwarded safely; the edge answers ``400``."""


def rewrite_request_headers(
    headers: HeaderLines,
    *,
    actor: Actor,
    slug: str,
    request_id: str | None,
    forward_access_token: bool,
) -> list[tuple[str, str]]:
    """The header lines to send to the child ``slug``.

    ``headers`` is what the edge received: a sequence of ``(name, value)`` pairs, or a mapping
    whose ``items()`` yields every line (a Starlette ``Headers``). Names match in any case and
    repeated names are kept. The result is a new list: the unrelated lines in their original order
    and spelling, then the lines this module writes, in the contract's order.

    Dropped from the incoming lines: every copy of ``X-Forwarded-Email``, ``-User``,
    ``-Preferred-Username``, ``X-Real-Ip``, ``X-Request-Id``, ``X-Forwarded-Prefix`` and
    ``X-Forwarded-Access-Token``; the hop-by-hop headers and any header named by a ``Connection``
    line; the WebSocket handshake lines; the ``__Host-appmux_open`` pair of a ``Cookie`` line (a
    line that held only that pair goes with it). ``Host`` and every other line, ``X-Forwarded-For``,
    ``X-Forwarded-Host``, ``X-Appmux-*`` and ``Authorization`` among them, pass unchanged.

    Written once each, only when there is a value: the four identity values of ``actor`` (a field
    the proxy did not send is absent), ``request_id`` (the proxy's ``X-Request-Id``, read by the
    caller), and ``X-Forwarded-Prefix: /<slug>``. ``X-Forwarded-Access-Token`` is written, with the
    actor's own token, only when ``forward_access_token`` is true and the actor has one.

    A ``Connection`` line removes every header it names, ``Content-Length`` included (RFC 9110
    section 7.6.1 makes no exception); the upstream client then frames the body itself. Proxy-owned
    headers named there are still written.

    Raises ``MalformedHeader`` for a name that is not an HTTP token, a value with CR, LF or NUL
    (also in a line that would be dropped, and in a value this function would write), more than one
    ``Host`` line, and a request with both ``Content-Length`` and ``Transfer-Encoding``.
    """
    lines = list(headers.items() if isinstance(headers, Mapping) else headers)
    _refuse_malformed_lines(lines)

    removed = _HOP_BY_HOP_HEADERS | _named_by_connection(lines)
    kept: list[tuple[str, str]] = []
    for name, value in lines:
        key = name.lower()
        if (
            key in _PROXY_OWNED_HEADERS
            or key in removed
            or key.startswith(_WEBSOCKET_HANDSHAKE_PREFIX)
        ):
            continue
        if key == _COOKIE_HEADER:
            remaining = _without_open_mode_cookie(value)
            if remaining is None:
                continue
            value = remaining
        kept.append((name, value))
    written = _proxy_lines(
        actor=actor,
        slug=slug,
        request_id=request_id,
        forward_access_token=forward_access_token,
    )
    return [*kept, *written]


def _without_open_mode_cookie(line: str) -> str | None:
    """A ``Cookie`` line without the edge's cookie; ``None`` when no other pair is left.

    A line that does not carry the cookie is returned as it is, byte for byte. The name is compared
    without case: no child needs a lookalike, and a browser sends the case it was given.
    """
    pairs = line.split(";")
    others = [
        pair
        for pair in pairs
        if pair.partition("=")[0].strip().lower() != OPEN_MODE_COOKIE_NAME.lower()
    ]
    if len(others) == len(pairs):
        return line
    remaining = [pair.strip() for pair in others if pair.strip()]
    return "; ".join(remaining) if remaining else None


def _refuse_malformed_lines(lines: list[tuple[str, str]]) -> None:
    host_lines = 0
    framing_headers_seen: set[str] = set()
    for position, (name, value) in enumerate(lines, start=1):
        if not _TOKEN.fullmatch(name):
            # The name is not echoed: it may hold the very bytes that make it unsafe.
            raise MalformedHeader(
                f"request header line {position} has a name that is not an HTTP token; "
                "the request is refused"
            )
        _refuse_unsafe_value(name, value, f"request header line {position}")
        key = name.lower()
        if key == _HOST_HEADER:
            host_lines += 1
            if host_lines > 1:
                raise MalformedHeader(
                    f"request header line {position} is a second Host line; the request is refused"
                )
        elif key in _BODY_FRAMING_HEADERS:
            framing_headers_seen.add(key)
            if framing_headers_seen == _BODY_FRAMING_HEADERS:
                raise MalformedHeader(
                    f"request header line {position} gives the body a second framing, since the "
                    "request has both Content-Length and Transfer-Encoding; the request is refused"
                )


def _refuse_unsafe_value(name: str, value: str, where: str) -> None:
    if not _FORBIDDEN_VALUE_CHARACTERS.isdisjoint(value):
        raise MalformedHeader(
            f"{where} ({name}) has a CR, LF or NUL in its value; the request is refused"
        )


def _named_by_connection(lines: list[tuple[str, str]]) -> frozenset[str]:
    """Header names that any ``Connection`` line lists: per RFC 9110 they are hop-by-hop too."""
    named: set[str] = set()
    for name, value in lines:
        if name.lower() != _CONNECTION_HEADER:
            continue
        for element in value.split(","):
            token = element.strip(_LIST_WHITESPACE).lower()
            if token:
                named.add(token)
    return frozenset(named)


def _proxy_lines(
    *, actor: Actor, slug: str, request_id: str | None, forward_access_token: bool
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str | None]] = [
        (EMAIL_HEADER, actor.email),
        (USER_HEADER, actor.user),
        (PREFERRED_USERNAME_HEADER, actor.preferred_username),
        (REAL_IP_HEADER, actor.client_address),
        (REQUEST_ID_HEADER, request_id),
        (PREFIX_HEADER, f"/{slug}"),
    ]
    if forward_access_token and actor.visitor_token is not None:
        # The one place the token leaves its SecretStr.
        candidates.append((ACCESS_TOKEN_HEADER, actor.visitor_token.get_secret_value()))

    written: list[tuple[str, str]] = []
    for key, value in candidates:
        if not value:
            continue
        name = key.title()
        _refuse_unsafe_value(name, value, "a header this module writes")
        written.append((name, value))
    return written
