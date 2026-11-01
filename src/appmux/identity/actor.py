"""Forwarded headers to an ``Actor`` (design 7.1, edge contract).

Pure: it takes the request headers and does no I/O. The Databricks proxy overwrites the identity
headers and ``X-Real-Ip`` (spike 8), so the edge trusts them; one that appears on two lines did not
come straight from the proxy and the request is refused. ``X-Forwarded-For`` is not read: the proxy
appends to a client-supplied value, so none of its entries is known to be the client. Group
membership is not here: it is looked up with the visitor's own token and cached elsewhere.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import SecretStr

# Lower case: header names are folded before they are compared.
EMAIL_HEADER: Final = "x-forwarded-email"
USER_HEADER: Final = "x-forwarded-user"
PREFERRED_USERNAME_HEADER: Final = "x-forwarded-preferred-username"
ACCESS_TOKEN_HEADER: Final = "x-forwarded-access-token"  # noqa: S105 - a header name, not a credential
REAL_IP_HEADER: Final = "x-real-ip"

# The proxy writes each of these once and replaces any client copy, so two lines mean the request
# was not forwarded by the proxy alone and no line can be trusted over the other.
_SINGLE_LINE_HEADERS: Final = frozenset(
    {EMAIL_HEADER, USER_HEADER, PREFERRED_USERNAME_HEADER, ACCESS_TOKEN_HEADER, REAL_IP_HEADER}
)

# Optional whitespace around a field value (RFC 9110); any other whitespace is part of the value.
_FIELD_WHITESPACE: Final = " \t"

HeaderLines = Mapping[str, str] | Sequence[tuple[str, str]]


class MissingIdentity(Exception):
    """The request carries no usable ``X-Forwarded-Email``; the edge answers ``401``.

    The proxy always sets the header, so its absence means the edge was reached another way.
    """


class AmbiguousIdentity(MissingIdentity):
    """An identity header appears on more than one line, so no value of it can be trusted.

    The message names the header and never a value: ``X-Forwarded-Access-Token`` is a credential.
    """

    def __init__(self, header: str) -> None:
        self.header = header
        super().__init__(
            f"request carries {header} on more than one line; the Databricks proxy writes it "
            "once, so the request did not arrive through the proxy alone"
        )


@dataclass(frozen=True, slots=True)
class Actor:
    """The person making a request, as far as the request headers say.

    ``visitor_token`` is the person's own access token, present only when user authorization is
    on for the appmux App. It is a ``SecretStr`` so it cannot reach a log or an audit row through
    ``repr``, ``str`` or an f-string; call ``get_secret_value()`` at the one place that forwards it.
    ``client_address`` is the canonical form of ``X-Real-Ip``, or ``None`` when the proxy sent no
    usable address.
    """

    email: str
    user: str | None = None
    preferred_username: str | None = None
    client_address: str | None = None
    visitor_token: SecretStr | None = None


def actor_from_headers(headers: HeaderLines) -> Actor:
    """Build the ``Actor`` of a request.

    ``headers`` is a mapping (a Starlette ``Headers`` yields every line from ``items()``) or a
    sequence of ``(name, value)`` pairs; names match in any case. Raises ``MissingIdentity`` when
    the request has no email and ``AmbiguousIdentity`` when an identity header is on two lines.
    """
    lines = headers.items() if isinstance(headers, Mapping) else headers
    single_values: dict[str, str] = {}
    for name, value in lines:
        key = name.lower()
        if key not in _SINGLE_LINE_HEADERS:
            continue
        if key in single_values:
            raise AmbiguousIdentity(key.title())
        single_values[key] = value

    email = _text(single_values.get(EMAIL_HEADER))
    if email is None:
        raise MissingIdentity(
            "request has no X-Forwarded-Email; the Databricks proxy always sets it, "
            "so the request did not arrive through the proxy"
        )
    token = _text(single_values.get(ACCESS_TOKEN_HEADER))
    return Actor(
        email=email.lower(),
        user=_text(single_values.get(USER_HEADER)),
        preferred_username=_text(single_values.get(PREFERRED_USERNAME_HEADER)),
        client_address=_client_address(single_values.get(REAL_IP_HEADER)),
        visitor_token=None if token is None else SecretStr(token),
    )


def _text(value: str | None) -> str | None:
    """The trimmed value, or ``None`` when the header is absent or blank."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _client_address(real_ip: str | None) -> str | None:
    """``X-Real-Ip`` as a canonical IP address, or ``None`` when it is absent or not one.

    The address is informational, so an unusable value gives ``None`` and the request proceeds. A
    value is accepted only when it is printable ASCII with no zone id, because the result goes to
    logs and audit rows. ``ipaddress`` takes any text after the ``%`` of an IPv6 zone id (spaces,
    ESC, newline), so the ``%`` test is what keeps that text out. ``ipaddress`` already refuses a
    control or non-ASCII character elsewhere; the explicit test keeps the guarantee from resting
    on the parser's internals.
    """
    if real_ip is None:
        return None
    candidate = real_ip.strip(_FIELD_WHITESPACE)
    if not (candidate.isascii() and candidate.isprintable()) or "%" in candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        # Not an address (a name, a port suffix, a list): the address is informational, so the
        # request goes on without one rather than carrying unvalidated text into logs.
        return None
