"""Status pages the edge itself answers with (contracts/edge-http.md, "Status codes the edge itself
produces").

A status page is plain HTML with no script, no style and no external reference, so it works under
``default-src 'none'``. It is built from fixed text and from names this build knows: never from an
exception, a path, a header or any text a child or the canary produced, because the visitor who
reads it may be a stranger to the platform. What a canary printed about a failure (its ``detail``)
belongs to the platform admin's page.

``render_status_page`` and ``status_page_headers`` are the parts every status page shares;
``self_check_failed_page`` is the page of a failed self-check (design 6.4, FR-013); the pages of
one app that cannot answer (``app_unavailable_page`` for ``503``, ``bad_gateway_page`` for ``502``
and ``gateway_timeout_page`` for ``504``) are the request pipeline's (design 7.1). A short
plain-text answer (``404``, ``429``, ``413``) uses ``plain_text_headers``.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Final

from appmux.sandbox.self_check import CANARY_CHECK_NAME, CHECKS

# The status of a page the edge shows because it cannot serve (service unavailable).
UNAVAILABLE_STATUS: Final = 503
# The child answered nothing usable, or did not answer in time (contracts/edge-http.md).
BAD_GATEWAY_STATUS: Final = 502
GATEWAY_TIMEOUT_STATUS: Final = 504
# How long a client is told to wait before it asks again. A failed self-check ends only when a
# platform admin runs it again or the App restarts, so the client has no reason to hurry.
SELF_CHECK_RETRY_AFTER_S: Final = 30
# An app that is coming up may be serving in seconds; a stopped or crashed one waits for a person.
APP_STARTING_RETRY_AFTER_S: Final = 5
# 1013 is "try again later" (RFC 6455 registry); sent before accept it answers the upgrade with 403.
WEBSOCKET_TRY_AGAIN_LATER: Final = 1013

_UNKNOWN_CHECK_NAME: Final = "unknown"
_STARTING: Final = "starting"
# What a visitor is told about an app with no route, by the state the app is in. The states are the
# run states of the launcher (`stopped`, `starting`, `crashed`, `no_release`); `running` never
# reaches a page because an app that runs has a route.
_APP_STATE_ADVICE: Final = {
    "stopped": "An admin has stopped this app. Ask its owner to start it again.",
    _STARTING: (
        "The app is starting or being updated, and no version of it is serving yet. "
        "Try again in a minute."
    ),
    "crashed": (
        "The app crashed and is not running. Ask its owner to read its log or to activate "
        "another release."
    ),
    "no_release": (
        "The app has no release to serve: none has passed its health check yet. Waiting will not "
        "change that. Ask its owner to update it or to activate a release."
    ),
}


def render_status_page(heading: str, detail: str, items: Sequence[tuple[str, str]] = ()) -> bytes:
    """A page with a heading, a paragraph and an optional list of (name, explanation) pairs."""
    entries = "".join(
        f"<li><code>{html.escape(name)}</code>: {html.escape(text)}</li>" for name, text in items
    )
    listing = f"<ul>{entries}</ul>" if entries else ""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        '<head><meta charset="utf-8"><title>appmux</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>\n'
        f"<body><h1>{html.escape(heading)}</h1><p>{html.escape(detail)}</p>{listing}</body>\n"
        "</html>\n"
    ).encode()


def _shared_headers(
    body: bytes, content_type: bytes, retry_after_s: int | None
) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"content-type", content_type),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
    ]
    if retry_after_s is not None:
        headers.append((b"retry-after", str(retry_after_s).encode("ascii")))
    return headers


def status_page_headers(body: bytes, *, retry_after_s: int | None) -> list[tuple[bytes, bytes]]:
    """The ASGI headers of a status page whose body is ``body``.

    ``retry_after_s`` is how long the client is told to wait; ``None`` leaves the header out (a page
    that no waiting will change).
    """
    headers = _shared_headers(body, b"text/html; charset=utf-8", retry_after_s)
    headers.append((b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"))
    return headers


def plain_text_headers(
    body: bytes, *, retry_after_s: int | None = None
) -> list[tuple[bytes, bytes]]:
    """The ASGI headers of a short plain-text answer (``404``, ``413``, ``429``, ``401``)."""
    return _shared_headers(body, b"text/plain; charset=utf-8", retry_after_s)


def _failed_check_item(name: str) -> tuple[str, str]:
    # Only ids this build defines reach the page; any other name (which no code path produces)
    # is shown as "unknown", so a name can never carry text from outside.
    if name in CHECKS:
        return name, f"a sandboxed app must not be able to {CHECKS[name]}, and it could"
    if name == CANARY_CHECK_NAME:
        return name, "the canary did not produce a trustworthy result"
    return _UNKNOWN_CHECK_NAME, "the self-check reported a result this build does not know"


def self_check_failed_page(names: Sequence[str]) -> bytes:
    """The page every route but ``/_appmux/`` shows while the self-check is not a pass.

    ``names`` are the ids of the failed checks (``SelfCheckState.failed_names``).
    """
    return render_status_page(
        "appmux cannot serve apps right now",
        "appmux checks at start-up that its sandbox holds, and that check failed, so no app is "
        "served. Ask a platform admin to open the appmux platform page and run the self-check "
        "again, or to read the App's log. The failed checks:",
        [_failed_check_item(name) for name in names],
    )


def app_unavailable_state(state: str) -> str:
    """The state a page names: one of this build's four, and ``starting`` for any other text."""
    return state if state in _APP_STATE_ADVICE else _STARTING


def app_unavailable_page(slug: str, state: str) -> bytes:
    """The ``503`` page of an app that has no route: its name, its state and what to do about it.

    ``slug`` is the slug of a registered app (the pipeline answers this page only for a visitor who
    may open the app, so it tells nothing to a stranger). ``state`` is a run state, shown only if
    it is one of this build's own words.
    """
    state = app_unavailable_state(state)
    return render_status_page(
        f"{slug} is not available right now",
        _APP_STATE_ADVICE[state],
        [("state", state)],
    )


def app_unavailable_retry_after_s(state: str) -> int | None:
    """How long to tell the client to wait: only an app that is coming up will change soon."""
    return APP_STARTING_RETRY_AFTER_S if app_unavailable_state(state) == _STARTING else None


def bad_gateway_page(slug: str) -> bytes:
    """The ``502`` page: the app is there, but appmux got no usable answer from it."""
    return render_status_page(
        f"{slug} did not answer correctly",
        "appmux could not get a valid answer from the app. Try again in a moment; if it keeps "
        "happening, ask the app's owner to read its log.",
    )


def gateway_timeout_page(slug: str) -> bytes:
    """The ``504`` page: the app did not answer within the platform's time limit."""
    return render_status_page(
        f"{slug} took too long to answer",
        "The app did not answer within the platform's time limit for a request. Try again; if it "
        "keeps happening, ask the app's owner to look at what the request does.",
    )
