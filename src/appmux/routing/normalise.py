"""Path normalisation: the one reading of a request path that every later step trusts (FR-017).

Routing, authorization and the child must agree on what path a request names, or a request can
be judged for one app and served by another (``/sales/../hr/x``, ``/sales/%2e%2e/hr``). So this
module reads the raw request path once and either returns a path that cannot be read two ways or
raises ``AmbiguousPath``, which the edge answers with ``400`` and forwards nothing.

The input is the raw path of the request line, without the query: the ASGI ``raw_path`` read as
latin-1, not the already-decoded ``path``. The output keeps the client's own percent-encoding
(each segment is decoded once to be judged, never re-encoded), so the child decodes exactly the
bytes this module judged.

A segment is refused when, after one percent-decoding, it holds ``/``, ``\\``, a control character
or invalid UTF-8, or when it would change under a second decoding (``%252e`` is ``%2e`` once and
``.`` twice, so two components that decode differently would disagree about it). ``.`` and ``..``
segments, written plainly or encoded, are resolved as RFC 3986 section 5.2.4 does; a ``..`` above
the root is refused rather than clamped, because no client sends one after its own normalisation.

Error messages name the rule that failed and never echo the path.
"""

from __future__ import annotations

import re
from typing import Final

_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")
# A decoded segment that still holds one would be read differently by a second decoder.
_PERCENT_ESCAPE: Final = re.compile(r"%[0-9A-Fa-f]{2}")
_FIRST_PRINTABLE_ASCII: Final = 0x21
_LAST_PRINTABLE_ASCII: Final = 0x7E
_FORBIDDEN_LITERALS: Final = frozenset("\\#?")
_FORBIDDEN_DECODED: Final = frozenset("/\\")
_DELETE: Final = 0x7F
_FIRST_PRINTABLE: Final = 0x20


class AmbiguousPath(ValueError):
    """The request path (or query) can be read in more than one way, or not at all."""


def normalise_path(raw_path: str) -> str:
    """Resolve slashes and dot segments of ``raw_path``; raise ``AmbiguousPath`` if it is unsafe."""
    if not raw_path.startswith("/"):
        raise AmbiguousPath("the request path does not start with a slash")
    for character in raw_path:
        code = ord(character)
        if not _FIRST_PRINTABLE_ASCII <= code <= _LAST_PRINTABLE_ASCII:
            raise AmbiguousPath("the request path holds a space, control or non-ASCII character")
        if character in _FORBIDDEN_LITERALS:
            raise AmbiguousPath("the request path holds a backslash, '#' or '?'")

    kept: list[str] = []
    last_was_dot_segment = False
    for segment in raw_path.split("/"):
        if segment == "":
            last_was_dot_segment = False
            continue  # '//' collapses
        decoded = decode_segment(segment)
        if decoded == ".":
            last_was_dot_segment = True
        elif decoded == "..":
            if not kept:
                raise AmbiguousPath("the request path climbs above the root")
            kept.pop()
            last_was_dot_segment = True
        else:
            kept.append(segment)
            last_was_dot_segment = False

    if not kept:
        return "/"
    trailing_slash = raw_path.endswith("/") or last_was_dot_segment
    return "/" + "/".join(kept) + ("/" if trailing_slash else "")


def decode_segment(segment: str) -> str:
    """Percent-decode one path segment once and check what it became.

    Raises ``AmbiguousPath`` for malformed or non-UTF-8 encoding, a decoded slash or backslash, a
    control character, or a result that would decode further.
    """
    raw = bytearray()
    index = 0
    while index < len(segment):
        character = segment[index]
        if character == "%":
            digits = segment[index + 1 : index + 3]
            if len(digits) != 2 or not all(digit in _HEX_DIGITS for digit in digits):
                raise AmbiguousPath("the request path holds malformed percent-encoding")
            raw.append(int(digits, 16))
            index += 3
        else:
            raw.append(ord(character))
            index += 1
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AmbiguousPath("the request path holds percent-encoding that is not UTF-8") from exc
    if any(character in _FORBIDDEN_DECODED for character in decoded):
        raise AmbiguousPath("the request path holds an encoded slash or backslash")
    if any(ord(character) < _FIRST_PRINTABLE or ord(character) == _DELETE for character in decoded):
        raise AmbiguousPath("the request path holds an encoded control character")
    if _PERCENT_ESCAPE.search(decoded):
        raise AmbiguousPath("the request path is percent-encoded twice")
    return decoded
