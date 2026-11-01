"""The startup gate: the ASGI application uvicorn starts with (design 5.6, FR-008, research D17).

The gate lets the port be bound before anything slow runs. The Databricks proxy probes ``/`` while
a deployment starts, and a container that answers nothing then risks being restarted, so the gate
answers at once from static pages while the background start-up connects to the registry. It has
three phases:

* ``starting``: ``GET`` and ``HEAD`` on ``/`` answer ``200`` with a static page; every other path
  and method, ``/_appmux/`` included, answers ``503`` with the same page;
* ``blocked``: every path, ``/`` included, answers ``503`` with a page that names the reason;
* ``open``: every request goes to the top-level application given to ``open``.

This module imports the standard library only, so nothing slow loads before the bind; a test
imports it in a subprocess whose import system refuses everything else. The pages are built from
fixed text. They carry no identity, registry or environment data, and the reason of a blocked gate
is a member of ``BlockReason``: a fixed list that no exception message can enter (a message may
quote a host name or a credential). Anything that closes the gate chooses a member, never a string.

Closing is always allowed and reopening is not: ``open`` works only from ``starting``, so a gate
that was blocked (or opened and then blocked) stays shut until the process restarts.

The gate also owns the ASGI lifespan, so the application behind it never sees one. Work that must
stop on ``SIGTERM`` registers a hook with ``add_shutdown_hook``; uvicorn runs the hooks, in order,
when it shuts down.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from enum import Enum, StrEnum
from typing import Any, Final

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApplication = Callable[[Scope, Receive, Send], Awaitable[None]]
ShutdownHook = Callable[[], Awaitable[None]]

_STARTING_RETRY_AFTER_S: Final = "5"
_BLOCKED_RETRY_AFTER_S: Final = "30"
# 1013 is "try again later" (RFC 6455 registry); sent before accept it answers the upgrade with 403.
_WEBSOCKET_TRY_AGAIN_LATER: Final = 1013


class Phase(StrEnum):
    STARTING = "starting"
    BLOCKED = "blocked"
    OPEN = "open"


class BlockReason(Enum):
    """Why a blocked gate stays shut: a fixed list, each with the text its page shows.

    The text is written here and nowhere else, so a page can never carry an exception message.
    """

    REGISTRY_UNREACHABLE = (
        "The registry cannot be reached",
        "appmux could not connect to its Lakebase registry, so it cannot tell which apps exist. "
        "Check that the Lakebase database is running and attached to this App, then restart it.",
    )
    REGISTRY_REFUSED = (
        "The registry refused appmux",
        "appmux reached its Lakebase registry, but the database did not accept its login or its "
        "privileges. Ask the Lakebase project owner to apply the grants, then restart the App.",
    )
    STARTUP_FAILED = (
        "appmux could not finish starting",
        "A start-up step failed. The App's log names the step and the kind of error. "
        "Fix the cause, then restart the App.",
    )

    @property
    def title(self) -> str:
        title: str = self.value[0]
        return title

    @property
    def advice(self) -> str:
        advice: str = self.value[1]
        return advice


def _page(heading: str, detail: str) -> bytes:
    # No script, no style and no external reference: the page works under ``default-src 'none'``.
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        '<head><meta charset="utf-8"><title>appmux</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>\n'
        f"<body><h1>{html.escape(heading)}</h1><p>{html.escape(detail)}</p></body>\n"
        "</html>\n"
    ).encode()


_STARTING_PAGE: Final = _page(
    "appmux is starting",
    "The service is starting and will be ready in a few minutes. Try again shortly.",
)
_BLOCKED_PAGES: Final = {reason: _page(reason.title, reason.advice) for reason in BlockReason}


class Gate:
    """The ASGI application that answers while appmux starts and hands over once it is ready."""

    def __init__(self) -> None:
        self._phase = Phase.STARTING
        self._reason: BlockReason | None = None
        self._application: ASGIApplication | None = None
        self._shutdown_hooks: list[ShutdownHook] = []

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def reason(self) -> BlockReason | None:
        """Why the gate is blocked; ``None`` in any other phase."""
        return self._reason

    def open(self, application: ASGIApplication) -> None:
        """Send every request to ``application`` from now on; only a ``starting`` gate opens."""
        if self._phase is Phase.OPEN:
            raise RuntimeError("the gate is already open")
        if self._phase is Phase.BLOCKED:
            raise RuntimeError("the gate is blocked and stays blocked until the process restarts")
        self._application = application
        self._phase = Phase.OPEN

    def block(self, reason: BlockReason) -> None:
        """Answer ``503`` on every path with the page of ``reason``; allowed in any phase."""
        if not isinstance(reason, BlockReason):
            raise TypeError("the reason of a blocked gate must be a BlockReason")
        self._phase = Phase.BLOCKED
        self._reason = reason
        self._application = None

    def add_shutdown_hook(self, hook: ShutdownHook, *, first: bool = False) -> None:
        """Run ``hook`` when the server shuts down (``SIGTERM``), in registration order.

        ``first`` puts it ahead of every hook added so far: for work that must finish before an
        earlier-registered hook tears down what it uses (the children stop before the registry
        closes), when only the later code knows it exists.
        """
        if first:
            self._shutdown_hooks.insert(0, hook)
        else:
            self._shutdown_hooks.append(hook)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
        elif self._phase is Phase.OPEN and self._application is not None:
            await self._application(scope, receive, send)
        elif scope_type == "http":
            await self._answer_http(scope, send)
        elif scope_type == "websocket":
            await send({"type": "websocket.close", "code": _WEBSOCKET_TRY_AGAIN_LATER})
        # Any other scope type is not served before the gate opens.

    async def _answer_http(self, scope: Scope, send: Send) -> None:
        is_head = scope["method"] == "HEAD"
        if self._phase is Phase.STARTING:
            is_probe = scope["path"] == "/" and scope["method"] in ("GET", "HEAD")
            status = 200 if is_probe else 503
            body, retry_after = _STARTING_PAGE, _STARTING_RETRY_AFTER_S
        else:
            # ``block`` sets the reason; a missing one still fails closed.
            reason = self._reason or BlockReason.STARTUP_FAILED
            status, body, retry_after = 503, _BLOCKED_PAGES[reason], _BLOCKED_RETRY_AFTER_S
        headers = [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
        ]
        if status == 503:
            headers.append((b"retry-after", retry_after.encode("ascii")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if is_head else body})

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self._run_shutdown_hooks()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _run_shutdown_hooks(self) -> None:
        for hook in tuple(self._shutdown_hooks):
            try:
                await hook()
            except Exception as exc:
                # A hook that fails must not keep the next one from running: the process is
                # stopping and every hook is best effort. Only the class is logged, because the
                # message of an error from a hook could quote a credential.
                name = getattr(hook, "__qualname__", type(hook).__name__)
                logger.error("shutdown hook %s failed (%s)", name, type(exc).__name__)
