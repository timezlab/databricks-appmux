"""The WebSocket relay between a browser and a child's unix socket (design 7, FR-027).

``relay_websocket`` opens a WebSocket to the child first and only then accepts the browser's, so a
child that is down, refuses the handshake or does not answer is reported to the edge (502, the
child's own refusal status, 504) before the browser has been told that the upgrade worked.

Once both sides are open, two pumps copy messages (text stays text, binary stays binary, in order)
until either side ends. Whoever ends first decides how the other is closed:

* the browser leaves with code ``c``: the child is closed with ``c``; a code that must never be put
  on the wire (1005 "no status", 1006 "abnormal", 1015) becomes 1001 "going away";
* the child closes with a code and reason: the browser gets the same pair; a child that closed
  without a status gets 1000; a child that vanished without closing (1006) gets 1011
  "upstream closed", because the browser's connection did nothing wrong;
* a message over ``max_message_bytes`` in either direction closes both sides with 1009;
* ``connections.close_unauthorized`` (access withdrawn) closes both sides with the code and reason
  it passes, and each side is closed once only.

The stream is registered with ``OpenConnections`` for as long as it is open and unregistered in a
``finally``, also when the relay fails or is cancelled, so the registry never holds a dead entry.

``Host`` and ``Origin`` of the browser's handshake are the child's too (the edge contract passes
``Host`` through). A ``Host`` that is not a host name or address falls back to ``localhost``. The
other headers are sent as given; ``rewrite`` already removed the handshake headers of the browser's
own upgrade, and ``websockets`` writes the child's.

``websockets`` parses the handshake URI again, so two details of the target do not survive that hop:
an empty query (``/ws?``) reaches the child as ``/ws``, and a non-ASCII byte in a query is written
UTF-8-encoded rather than as the browser sent it. Neither can change the path the child sees, which
``routing`` judged; both are the same request for every application.

The browser side is a ``ClientSocket`` that ``edge`` adapts a Starlette ``WebSocket`` to, so this
module needs no ASGI server and does not import ``edge``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol, TypeVar

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatusCode
from websockets.legacy.client import WebSocketClientProtocol, unix_connect

from appmux.identity.actor import Actor
from appmux.upstream.client import UpstreamError, UpstreamTimeout, UpstreamUnreachable

# The edge's own uvicorn accepts 16 MiB messages from a browser; the child hop uses the same bound.
DEFAULT_MAX_MESSAGE_BYTES: Final = 16 * 1024 * 1024
DEFAULT_OPEN_TIMEOUT_S: Final = 10.0
# How long a close waits for the peer's answering close frame before the connection is dropped. The
# peer is a process on the same machine, so a short wait is enough; it also adds to the time a
# failed handshake takes (``open_timeout_s`` plus this) before the edge can answer 504.
_CLOSE_TIMEOUT_S: Final = 2.0

# The websockets library logs, at DEBUG, every handshake header it sends (the visitor's e-mail and
# address and, when it is forwarded, the access token) and the payload of every frame. Giving the
# connection a logger pinned at INFO keeps `isEnabledFor(DEBUG)` false whatever the root level is,
# so those lines are never built, let alone written.
_WIRE_LOGGER: Final = logging.getLogger("appmux.upstream.websocket.wire")
_WIRE_LOGGER.setLevel(logging.INFO)

_FALLBACK_HOST: Final = "localhost"
# A host name, IPv4 address or bracketed IPv6 literal, with an optional port: what a Host header may
# hold. Anything else would be interpolated into the handshake's URI, so it is not used.
_HOST_HEADER: Final = re.compile(r"(?:[A-Za-z0-9_.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?")

_NORMAL_CLOSURE: Final = 1000
_GOING_AWAY: Final = 1001
_NO_STATUS_RECEIVED: Final = 1005
_MESSAGE_TOO_BIG: Final = 1009
_INTERNAL_ERROR: Final = 1011
_FIRST_APPLICATION_CODE: Final = 3000
_LAST_APPLICATION_CODE: Final = 4999
# The codes RFC 6455 lets an endpoint send (1004, 1005, 1006 and 1015 are never put on the wire).
_SENDABLE_CODES: Final = frozenset(
    {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}
)
_UPSTREAM_CLOSED_REASON: Final = "upstream closed"
_GOING_AWAY_REASON: Final = "going away"
_TOO_BIG_REASON: Final = "message too big"

_IdT = TypeVar("_IdT")


class ClientDisconnected(Exception):
    """The browser's side ended; ``code`` is the close code it reported (1005 if it gave none)."""

    def __init__(self, code: int = _NO_STATUS_RECEIVED) -> None:
        super().__init__(f"the client disconnected with code {code}")
        self.code = code


class UpstreamRejected(UpstreamError):
    """The child answered the handshake with an HTTP status instead of upgrading (``status``)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"the child refused the WebSocket handshake with status {status}")
        self.status = status


class ClientSocket(Protocol):
    """The browser's side of the stream, as the edge's adapter of a Starlette ``WebSocket``."""

    async def accept(self, subprotocol: str | None) -> None: ...

    async def receive(self) -> str | bytes:
        """The next message; raises ``ClientDisconnected`` when the browser leaves or is closed."""
        ...

    async def send_text(self, data: str) -> None:
        """Send a text message; may raise ``ClientDisconnected`` if the browser has just left."""
        ...

    async def send_bytes(self, data: bytes) -> None: ...

    async def close(self, code: int, reason: str) -> None: ...


class _StreamHandle(Protocol):
    async def close(self, code: int, reason: str) -> None: ...


class ConnectionRegistry(Protocol[_IdT]):
    """What the relay needs of ``edge.connections.OpenConnections`` (not imported: layering)."""

    def register(
        self, actor: Actor, slug: str, handle: _StreamHandle, *, release_no: int | None = None
    ) -> _IdT: ...

    def unregister(self, connection_id: _IdT) -> None: ...


async def relay_websocket(
    client: ClientSocket,
    *,
    socket_path: Path,
    target: str,
    headers: Sequence[tuple[str, str]],
    subprotocols: Sequence[str],
    connections: ConnectionRegistry[_IdT],
    actor: Actor,
    slug: str,
    release_no: int,
    open_timeout_s: float = DEFAULT_OPEN_TIMEOUT_S,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> None:
    """Connect to the child, accept the browser, and relay until one side ends.

    ``release_no`` is the release whose copy is at ``socket_path``; the registry keeps it so a swap
    can close the streams of the copy it replaces.
    """
    host, origin, extra_headers = _handshake_headers(headers)
    upstream = await _connect(
        socket_path,
        f"ws://{host}{target}",
        subprotocols=subprotocols,
        origin=origin,
        extra_headers=extra_headers,
        open_timeout_s=open_timeout_s,
        max_message_bytes=max_message_bytes,
    )
    relay = _Relay(client, upstream, max_message_bytes)
    connection_id = connections.register(actor, slug, relay, release_no=release_no)
    try:
        await client.accept(upstream.subprotocol)
        await relay.run()
    finally:
        connections.unregister(connection_id)
        await relay.close(_GOING_AWAY, _GOING_AWAY_REASON)


class _Relay:
    """The two sockets of one stream, closed once each, whoever asks first."""

    def __init__(
        self, client: ClientSocket, upstream: WebSocketClientProtocol, max_message_bytes: int
    ) -> None:
        self._client = client
        self._upstream = upstream
        self._max_message_bytes = max_message_bytes
        self._client_closed = False

    async def run(self) -> None:
        pumps = [
            asyncio.create_task(self._browser_to_child()),
            asyncio.create_task(self._child_to_browser()),
        ]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
        for pump in pumps:
            if not pump.cancelled() and (error := pump.exception()) is not None:
                raise error

    async def close(self, code: int, reason: str) -> None:
        """Close both sides; this is what ``OpenConnections`` calls when access is withdrawn."""
        try:
            await self._close_client(code, reason)
        finally:
            await self._close_upstream(code, reason)

    async def _close_client(self, code: int, reason: str) -> None:
        if self._client_closed:
            return
        self._client_closed = True
        # Best effort during teardown: a browser that is already gone must not stop the child's
        # side from being closed, and there is nobody left to tell.
        with contextlib.suppress(Exception):
            await self._client.close(code, reason)

    async def _close_upstream(self, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):  # same reason as the browser side
            await self._upstream.close(_sendable_code(code), reason)

    async def _browser_to_child(self) -> None:
        try:
            while True:
                message = await self._client.receive()
                if _size_of(message) > self._max_message_bytes:
                    await self.close(_MESSAGE_TOO_BIG, _TOO_BIG_REASON)
                    return
                await self._upstream.send(message)
        except ClientDisconnected as left:
            await self._close_upstream(left.code, "")
        except ConnectionClosed:
            return  # the child's side ended; _child_to_browser reports it to the browser

    async def _child_to_browser(self) -> None:
        try:
            while True:
                message = await self._upstream.recv()
                if isinstance(message, str):
                    await self._client.send_text(message)
                else:
                    await self._client.send_bytes(message)
        except ConnectionClosed as closed:
            code, reason = _close_for_browser(closed)
            await self._close_client(code, reason)
        except ClientDisconnected as left:  # a send found the browser already gone
            await self._close_upstream(left.code, "")


async def _connect(
    socket_path: Path,
    uri: str,
    *,
    subprotocols: Sequence[str],
    origin: str | None,
    extra_headers: list[tuple[str, str]],
    open_timeout_s: float,
    max_message_bytes: int,
) -> WebSocketClientProtocol:
    try:
        return await unix_connect(
            str(socket_path),
            uri,
            subprotocols=list(subprotocols) or None,
            origin=origin,
            extra_headers=extra_headers,
            open_timeout=open_timeout_s,
            close_timeout=_CLOSE_TIMEOUT_S,
            max_size=max_message_bytes,
            ping_interval=None,
            compression=None,
            logger=_WIRE_LOGGER,
        )
    except TimeoutError as exc:  # before OSError: it is a subclass of it
        raise UpstreamTimeout("the child did not finish the WebSocket handshake in time") from exc
    except InvalidStatusCode as exc:
        raise UpstreamRejected(exc.status_code) from exc
    except (InvalidHandshake, OSError, EOFError) as exc:
        raise UpstreamUnreachable("the child is not reachable for a WebSocket") from exc


def _handshake_headers(
    headers: Sequence[tuple[str, str]],
) -> tuple[str, str | None, list[tuple[str, str]]]:
    """The URI host, the ``Origin`` and the remaining headers to send."""
    host = _FALLBACK_HOST
    origin: str | None = None
    saw_host = False
    rest: list[tuple[str, str]] = []
    for name, value in headers:
        folded = name.lower()
        if folded == "host" and not saw_host:
            saw_host = True
            if _HOST_HEADER.fullmatch(value) is not None:
                host = value
        elif folded == "origin" and origin is None:
            origin = value
        elif folded not in {"host", "origin"}:
            rest.append((name, value))
    return host, origin, rest


def _size_of(message: str | bytes) -> int:
    return len(message) if isinstance(message, bytes) else len(message.encode("utf-8"))


def _sendable_code(code: int) -> int:
    """``code`` if it may be put on the wire, else 1001 (the peer went away)."""
    if code in _SENDABLE_CODES or _FIRST_APPLICATION_CODE <= code <= _LAST_APPLICATION_CODE:
        return code
    return _GOING_AWAY


def _close_for_browser(closed: ConnectionClosed) -> tuple[int, str]:
    """The close code and reason to give the browser when the child's side ended."""
    if closed.rcvd is not None:  # the child closed, with a status or without one
        if closed.rcvd.code == _NO_STATUS_RECEIVED:
            return _NORMAL_CLOSURE, ""
        if _sendable_code(closed.rcvd.code) == closed.rcvd.code:
            return closed.rcvd.code, closed.rcvd.reason
    elif closed.sent is not None and closed.sent.code == _MESSAGE_TOO_BIG:
        return _MESSAGE_TOO_BIG, _TOO_BIG_REASON  # we ended it: the child's message was too big
    return _INTERNAL_ERROR, _UPSTREAM_CLOSED_REASON
