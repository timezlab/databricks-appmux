"""The admin UI's Content-Security-Policy (design 7.3, FR-021, contracts/edge-http.md, spike 19).

All children and the admin UI share one browser origin, so the UI is hardened: no framing
(``frame-ancestors 'none'``), no cross-origin window sharing (``Cross-Origin-Opener-Policy:
same-origin``), no CORS, and a strict policy. A Next.js static export cannot start without the
inline scripts it writes into every page (the ``_R_`` bootstrap and the ``self.__next_f`` flight
data), so the policy is ``script-src 'self'`` plus the SHA-256 of each inline script of the page
being sent. Never ``'unsafe-inline'`` and never ``'unsafe-eval'``.

This module is the pure part:

* ``scan_page`` finds a page's inline scripts and hashes each from its exact text, and finds what
  the policy has no way to allow (an inline ``style`` attribute, an event-handler attribute, a
  ``<style>`` element), so the server can refuse such an export instead of serving a page that
  renders wrong or stays dead in the browser;
* ``build_csp`` and ``security_headers`` write the header values;
* ``UiSecurityHeadersMiddleware`` gives the same headers to responses the UI server does not make
  (the admin API's, which also live under ``/_appmux/``).

The page is parsed with the standard library's ``HTMLParser`` instead of searched with a regular
expression: text that only looks like an attribute inside a script, a comment or a quoted value
must not be taken for one. Nothing here reads a file or a request.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# The directives that do not depend on the page; the page's own ``script-src`` follows them.
FIXED_DIRECTIVES: Final = (
    "default-src 'none'",
    "style-src 'self'",
    "connect-src 'self'",
    "img-src 'self'",
    "font-src 'self'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
)
OPENER_POLICY: Final = "same-origin"

# The HTML parser turns CR LF and a lone CR into LF before a script's text is hashed, so the text
# is hashed that way too (a policy holding the hash of the CR LF text blocks the script).
_NEWLINES: Final = re.compile(r"\r\n?")


def script_source(text: str) -> str:
    """The CSP source expression for the inline script whose text is ``text``."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def build_csp(script_sources: Iterable[str] = ()) -> str:
    """The header value for a page whose inline scripts have these ``script_source`` values."""
    script_directive = " ".join(("script-src", "'self'", *script_sources))
    return "; ".join((*FIXED_DIRECTIVES, script_directive))


BASE_CSP: Final = build_csp()


def security_headers(csp: str) -> dict[str, str]:
    """The headers every response under ``/_appmux/`` carries. There is no CORS header."""
    return {
        "Content-Security-Policy": csp,
        "Cross-Origin-Opener-Policy": OPENER_POLICY,
        "X-Content-Type-Options": "nosniff",
    }


@dataclass(frozen=True, slots=True)
class PageScan:
    """What a page holds that the policy cares about.

    ``script_sources``: one ``script_source`` per distinct inline script, in document order.
    ``problems``: one line each for what no policy of this shape can allow, naming the line.
    """

    script_sources: tuple[str, ...]
    problems: tuple[str, ...]


class _PageScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.script_texts: list[str] = []
        self.problems: list[str] = []
        self._open_script: list[str] | None = None
        self._open_script_line = 0

    def _problem(self, message: str) -> None:
        self.problems.append(f"line {self.getpos()[0]}: {message}")

    def _inspect(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        # ``style-src 'self'`` blocks a style attribute and a <style> element; ``script-src``
        # without 'unsafe-inline' blocks every on* attribute. Hashes cannot allow an attribute.
        if tag == "style":
            self._problem("an inline <style> element")
        for name, _ in attributes:
            if name == "style":
                self._problem(f"<{tag}> has an inline style attribute")
            elif name.startswith("on"):
                self._problem(f"<{tag}> has the inline event handler '{name}'")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)
        if tag == "script" and all(name != "src" for name, _ in attrs):
            self._open_script = []
            self._open_script_line = self.getpos()[0]

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect(tag, attrs)
        if tag == "script":
            # A browser ignores the slash and reads everything up to </script> as the script's
            # text, while this parser would not: refuse it rather than hash the wrong text.
            self._problem("a self-closing <script> tag")

    def handle_data(self, data: str) -> None:
        if self._open_script is not None:
            self._open_script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._open_script is not None:
            self.script_texts.append(_NEWLINES.sub("\n", "".join(self._open_script)))
            self._open_script = None

    def finish(self) -> None:
        self.close()
        if self._open_script is not None:
            self.problems.append(f"line {self._open_script_line}: <script> is never closed")


def scan_page(html: bytes) -> PageScan:
    """Find the inline scripts of ``html`` and what the policy cannot allow in it.

    A script is inline when it has no ``src``; its hash is taken from the exact text between the
    tags (white space and all), whatever its ``type`` or ``id``. Raises ``UnicodeDecodeError`` when
    the page is not UTF-8: the browser would decode it by another rule, so no hash could be trusted.
    """
    scanner = _PageScanner()
    scanner.feed(html.decode("utf-8"))
    scanner.finish()
    sources = dict.fromkeys(script_source(text) for text in scanner.script_texts)
    return PageScan(script_sources=tuple(sources), problems=tuple(scanner.problems))


class UiSecurityHeadersMiddleware:
    """Adds the UI's security headers to a response that lacks them (an inline-script-free policy).

    For the admin API, which the contract also puts under ``/_appmux/``: it answers JSON, so the
    policy without hashes is the right one. A header the inner application already set is kept.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in security_headers(BASE_CSP).items():
                    headers.setdefault(name, value)
            await send(message)

        await self._app(scope, receive, send_with_headers)
