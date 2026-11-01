"""The response headers a browser receives from a child (edge contract, design 7.1 step 7).

``rewrite_response_headers`` is pure: header lines in, header lines out, no I/O and no mutation of
its input. A response body never passes through it (the contract: "bodies untouched"), so the
function has no body to change.

What changes, and why:

* ``Location`` that begins with a single ``/`` gets the app's prefix. The child believes it lives
  at ``/``; the browser reaches it at ``/<slug>/``. An absolute URL, a network-path reference
  (``//host``), a relative reference and an empty value are the child's own and stay.
* ``Set-Cookie`` loses its ``Domain`` and gets ``Path=/<slug>/`` whatever it sent, so a cookie of
  one app is never sent to another app on the same host. Every other attribute is kept as the
  child wrote it. A cookie named like the edge's open-mode cookie is dropped: only the edge sets it.
* The hop-by-hop headers (RFC 9110 section 7.6.1) and any header the child's ``Connection`` line
  names describe the child's connection to appmux, not the response, so they do not travel on.

A cookie that needs ``Path=/`` (any ``__Host-`` cookie) cannot survive the path rewrite: the browser
rejects it. That limit is the price of the prefix model (design 7.2).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

# Same name the edge uses for its own cookie (edge contract, "Open as a user").
OPEN_MODE_COOKIE_NAME: Final = "__Host-appmux_open"

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
_DROPPED_COOKIE_ATTRIBUTES: Final = frozenset({"domain", "path"})


def rewrite_response_headers(
    lines: Sequence[tuple[str, str]], *, slug: str
) -> list[tuple[str, str]]:
    """The header lines to send to the browser for a response of app ``slug``."""
    named_by_connection = _names_in_connection_headers(lines)
    prefix = f"/{slug}"
    rewritten: list[tuple[str, str]] = []
    for name, value in lines:
        folded = name.lower()
        if folded in _HOP_BY_HOP_HEADERS or folded in named_by_connection:
            continue
        if folded == "location":
            rewritten.append((name, _prefixed_location(value, prefix)))
        elif folded == "set-cookie":
            cookie = _rewritten_cookie(value, prefix)
            if cookie is not None:
                rewritten.append((name, cookie))
        else:
            rewritten.append((name, value))
    return rewritten


def _names_in_connection_headers(lines: Sequence[tuple[str, str]]) -> frozenset[str]:
    return frozenset(
        token.strip().lower()
        for name, value in lines
        if name.lower() == "connection"
        for token in value.split(",")
        if token.strip()
    )


def _prefixed_location(value: str, prefix: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return prefix + value
    return value


def _rewritten_cookie(value: str, prefix: str) -> str | None:
    """The cookie line with ``Domain`` removed and ``Path`` forced; ``None`` to drop the line."""
    name_and_value, *attributes = value.split(";")
    cookie_name = name_and_value.partition("=")[0].strip()
    if cookie_name.lower() == OPEN_MODE_COOKIE_NAME.lower():
        return None
    kept = [
        attribute.strip()
        for attribute in attributes
        if attribute.partition("=")[0].strip().lower() not in _DROPPED_COOKIE_ATTRIBUTES
        and attribute.strip()
    ]
    return "; ".join([name_and_value.strip(), *kept, f"Path={prefix}/"])
