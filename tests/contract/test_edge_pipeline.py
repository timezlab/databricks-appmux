"""The edge request pipeline against a stub child (T078; contracts/edge-http.md; design 3.4, 7.1).

A request to ``/<slug>/...`` goes identity -> routing -> authz -> rewrite -> upstream -> rewrite.
These tests drive the whole edge application (``create_edge_app``) through the ASGI interface and
let the "child" be a small server on a unix socket in ``tmp_path`` (HTTP by hand, WebSocket with
``websockets``), so what reaches the child is what the child's own bytes show. Nothing opens a TCP
port.

What is pinned, by the contract's own table:

* ``401`` without a (single) forwarded identity, ``400`` for an ambiguous path or a header that
  cannot be forwarded, ``404`` for an unknown app and for a denied one (the same answer after the
  same work), ``503`` for an app with no route (only for a visitor who may open it, and only that
  app), ``413`` over the body cap (also with no ``Content-Length``), ``429`` with ``Retry-After: 1``
  over the concurrency cap, ``502`` and ``504`` for a child that is gone or silent;
* the headers the child receives, the headers the browser receives, streaming both ways without
  holding a body, WebSocket pass-through, and the numbers the metrics aggregator is fed;
* no error body, log record or metric carries a token, an e-mail, a path or a stack trace.

What this cannot show: a real sandboxed child, the platform proxy, and how uvicorn passes
``raw_path`` and a client's disconnect. The driver below follows the ASGI rules (``receive`` blocks
after the body until the response is over or the client leaves) so a pipeline that ignores a
disconnect fails here; the real server is covered by the integration tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote

import pytest
from edge_harness import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    FRANK,
    MOUNT,
    NOW,
    VISITOR_TOKEN,
    App,
    FakeGroups,
    Message,
    Scope,
    ScriptedCanary,
    canary_text,
    scope_of,
    seeded_registry,
)
from stub_registry import StubRegistry
from websockets.datastructures import Headers
from websockets.legacy.server import WebSocketServerProtocol, unix_serve

from appmux.edge.app import create_edge_app
from appmux.edge.connections import OpenConnections
from appmux.identity.actor import Actor
from appmux.reconciler.route_table import Route, RouteTable
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.snapshot import RegistrySnapshot
from appmux.sandbox.self_check import SelfCheckState
from appmux.upstream.client import UpstreamClient

pytestmark = pytest.mark.anyio

TIMEOUT_S: Final = 5.0
ONE_MIB: Final = 1024 * 1024
CLIENT_ADDRESS: Final = "203.0.113.9"
REQUEST_ID: Final = "req-7"
BODY_MARKER: Final = b"private-request-body-marker"
HEAD_END: Final = b"\r\n\r\n"

# --- the stub child (HTTP) -----------------------------------------------------------------------


@dataclass
class Received:
    method: str
    target: str
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key.lower() == name.lower()]


class Connection:
    """One accepted connection of the child, as a handler sees it."""

    def __init__(
        self, child: Child, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self._child = child

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    def peer_gone(self) -> bool:
        """Whether the edge hung up (or the test is over); a handler that waits polls this."""
        gone = self.reader.at_eof() or self._child.stopping.is_set()
        if gone:
            self._child.hung_up.set()
        return gone


Handler = Callable[[Received, Connection], Awaitable[None]]


def http_response(status: str = "200 OK", headers: Sequence[str] = (), body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status}", *headers]
    if not any(
        line.lower().startswith(("content-length", "transfer-encoding")) for line in headers
    ):
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def chunk(data: bytes) -> bytes:
    return b"%x\r\n" % len(data) + data + b"\r\n"


LAST_CHUNK: Final = b"0\r\n\r\n"


async def read_chunked(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    while True:
        size_line = await reader.readline()
        if not size_line:
            # EOF before the last chunk: the sender hung up mid-body. A real server rejects this,
            # so the stub must not record it as a complete request.
            raise asyncio.IncompleteReadError(b"", None)
        size = int(size_line.strip(), 16)
        if size == 0:
            await reader.readline()
            return
        data = await reader.readexactly(size)
        await reader.readexactly(2)
        yield data


async def read_request(reader: asyncio.StreamReader, *, read_body: bool) -> Received:
    head = await reader.readuntil(HEAD_END)
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    pairs = [line.split(":", 1) for line in lines[1:] if line]
    headers = [(name, value.strip()) for name, value in pairs]
    lowered = {name.lower(): value for name, value in headers}
    body = b""
    if read_body and "content-length" in lowered:
        body = await reader.readexactly(int(lowered["content-length"]))
    elif read_body and lowered.get("transfer-encoding") == "chunked":
        body = b"".join([piece async for piece in read_chunked(reader)])
    return Received(method, target, headers, body)


class Child:
    """A server on a unix socket that records the requests it parsed and answers by a handler."""

    def __init__(self, path: Path, handler: Handler, *, read_body: bool = True) -> None:
        self.path = path
        self.handler = handler
        self.read_body = read_body
        self.received: list[Received] = []
        self.hung_up = asyncio.Event()
        self.stopping = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))

    async def stop(self) -> None:
        self.stopping.set()
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        connection = Connection(self, reader, writer)
        try:
            while True:
                request = await read_request(reader, read_body=self.read_body)
                self.received.append(request)
                await self.handler(request, connection)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # the edge went away: nothing more to serve on this connection
        finally:
            writer.close()


def answer(body: bytes = b"ok", status: str = "200 OK", headers: Sequence[str] = ()) -> Handler:
    async def handle(request: Received, connection: Connection) -> None:
        await connection.send(http_response(status, headers, body))

    return handle


def scripted(*handlers: Handler) -> Handler:
    """One handler per request, in order; the last one answers every request after that."""
    remaining = list(handlers)

    async def handle(request: Received, connection: Connection) -> None:
        await (remaining.pop(0) if len(remaining) > 1 else remaining[0])(request, connection)

    return handle


def hold_until(gate: asyncio.Event) -> Handler:
    async def handle(request: Received, connection: Connection) -> None:
        await gate.wait()
        await connection.send(http_response(body=b"late"))

    return handle


async def hang_up(request: Received, connection: Connection) -> None:
    connection.writer.close()


async def speak_garbage(request: Received, connection: Connection) -> None:
    await connection.send(b"this is not http\r\n\r\n")


async def stay_silent(request: Received, connection: Connection) -> None:
    while not connection.peer_gone():  # noqa: ASYNC110 - polling the child's own socket state
        await asyncio.sleep(0.01)


async def die_mid_body(request: Received, connection: Connection) -> None:
    await connection.send(http_response(headers=["Content-Length: 100"]) + b"x" * 10)
    connection.writer.close()


async def endless_stream(request: Received, connection: Connection) -> None:
    head = http_response(headers=["Content-Type: text/event-stream", "Transfer-Encoding: chunked"])
    await connection.send(head)
    while not connection.peer_gone():
        with contextlib.suppress(ConnectionError):
            await connection.send(chunk(b"data: tick\n\n"))
        await asyncio.sleep(0.01)


# --- the ASGI driver -----------------------------------------------------------------------------

_DISCONNECT: Final = object()


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode()

    def header(self, name: str) -> str | None:
        values = self.headers_named(name)
        return values[0] if values else None

    def headers_named(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key == name.lower()]


class Exchange:
    """One HTTP exchange with the ``receive`` rules of a real server.

    The body is delivered, then ``receive`` blocks until the response is over or ``disconnect`` is
    called, as uvicorn does; a pipeline that never looks for the disconnect cannot learn of it.
    """

    def __init__(
        self, app: App, scope: Scope, *, body: Sequence[bytes] = (), hold_body: bool = False
    ) -> None:
        self.messages: list[Message] = []
        self.body_messages_taken = 0
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        for piece in body:
            self._queue.put_nowait(piece)
        if not hold_body:
            self._queue.put_nowait(None)
        self._body_ended = False
        self._over = asyncio.Event()
        self._started = asyncio.Event()
        self._bodies: asyncio.Queue[bytes] = asyncio.Queue()
        self.task = asyncio.create_task(app(scope, self.receive, self.send))

    # --- what the server side offers the app ---

    async def receive(self) -> Message:
        if not self._body_ended:
            item = await self._queue.get()
            self.body_messages_taken += 1
            if item is _DISCONNECT:
                self._body_ended = True
                return {"type": "http.disconnect"}
            if item is None:
                self._body_ended = True
                return {"type": "http.request", "body": b"", "more_body": False}
            assert isinstance(item, bytes)
            return {"type": "http.request", "body": item, "more_body": True}
        await self._over.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        self.messages.append(message)
        if message["type"] == "http.response.start":
            self._started.set()
        elif message["type"] == "http.response.body":
            if message.get("body"):
                self._bodies.put_nowait(message["body"])
            if not message.get("more_body", False):
                self._over.set()

    # --- what a test does ---

    def feed(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def end_body(self) -> None:
        self._queue.put_nowait(None)

    def disconnect(self) -> None:
        self._queue.put_nowait(_DISCONNECT)
        self._over.set()

    @property
    def completed(self) -> bool:
        return any(
            message["type"] == "http.response.body" and not message.get("more_body", False)
            for message in self.messages
        )

    async def next_body(self) -> bytes:
        return await asyncio.wait_for(self._bodies.get(), TIMEOUT_S)

    async def started(self) -> int:
        await asyncio.wait_for(self._started.wait(), TIMEOUT_S)
        return self._start()["status"]

    async def finished(self) -> None:
        await asyncio.wait_for(self.task, TIMEOUT_S)

    async def answer(self) -> Answer:
        await self.finished()
        start = self._start()
        return Answer(
            status=start["status"],
            headers=tuple((n.decode().lower(), v.decode()) for n, v in start["headers"]),
            body=b"".join(
                m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
            ),
        )

    def _start(self) -> Message:
        return next(m for m in self.messages if m["type"] == "http.response.start")


class SocketExchange:
    """One WebSocket exchange: the browser side of a handshake and what follows it."""

    def __init__(self, app: App, scope: Scope) -> None:
        self.sent: list[Message] = []
        self._incoming: asyncio.Queue[Message] = asyncio.Queue()
        self._incoming.put_nowait({"type": "websocket.connect"})
        self._outgoing: asyncio.Queue[Message] = asyncio.Queue()
        self.task = asyncio.create_task(app(scope, self.receive, self.send))

    async def receive(self) -> Message:
        return await self._incoming.get()

    async def send(self, message: Message) -> None:
        self.sent.append(message)
        self._outgoing.put_nowait(message)

    def browser_sends(self, *, text: str | None = None, data: bytes | None = None) -> None:
        message: Message = {"type": "websocket.receive"}
        message["text" if text is not None else "bytes"] = text if text is not None else data
        self._incoming.put_nowait(message)

    def browser_leaves(self, code: int = 1000) -> None:
        self._incoming.put_nowait({"type": "websocket.disconnect", "code": code})

    async def next_sent(self) -> Message:
        return await asyncio.wait_for(self._outgoing.get(), TIMEOUT_S)

    async def finished(self) -> None:
        await asyncio.wait_for(self.task, TIMEOUT_S)


async def wait_until(condition: Callable[[], object]) -> None:
    """Return once ``condition()`` holds; fail the test after ``TIMEOUT_S`` instead of hanging."""
    async with asyncio.timeout(TIMEOUT_S):
        # The state is the rig's own (a child's list, a counter): there is no event to await.
        while not condition():  # noqa: ASYNC110 - polling state this test file owns
            await asyncio.sleep(0.01)


# --- what the pipeline reports to ----------------------------------------------------------------


class CountingGroups(FakeGroups):
    def __init__(self) -> None:
        self.lookups = 0

    async def groups_for(self, actor: Actor) -> frozenset[str] | None:
        self.lookups += 1
        return await super().groups_for(actor)


@dataclass
class RecordingMetrics:
    requests: list[dict[str, Any]] = field(default_factory=list)
    in_flight: list[tuple[str, int]] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def record_request(
        self,
        app_slug: str | None,
        *,
        status: int,
        latency_ms: float,
        bytes_in: int = 0,
        bytes_out: int = 0,
        upstream_error: bool = False,
        limited: bool = False,
    ) -> None:
        self.requests.append(
            {
                "app_slug": app_slug,
                "status": status,
                "latency_ms": latency_ms,
                "bytes_in": bytes_in,
                "bytes_out": bytes_out,
                "upstream_error": upstream_error,
                "limited": limited,
            }
        )

    def observe_in_flight(self, app_slug: str, current: int) -> None:
        self.in_flight.append((app_slug, current))

    def websocket_opened(self, app_slug: str) -> None:
        self.opened.append(app_slug)

    def websocket_closed(self, app_slug: str) -> None:
        self.closed.append(app_slug)


# --- the rig -------------------------------------------------------------------------------------


def proxy_headers(email: str) -> list[tuple[str, str]]:
    local_part = email.split("@")[0]
    return [
        ("X-Forwarded-Email", email),
        ("X-Forwarded-User", f"user-{local_part}"),
        ("X-Forwarded-Preferred-Username", local_part),
        ("X-Real-Ip", CLIENT_ADDRESS),
        ("X-Request-Id", REQUEST_ID),
        ("X-Forwarded-Access-Token", VISITOR_TOKEN),
    ]


def http_scope(
    method: str,
    target: str,
    *,
    email: str | None,
    headers: Sequence[tuple[str, str]] = (),
    scope_type: str = "http",
) -> Scope:
    raw_path, _, query = target.partition("?")
    lines = [*([] if email is None else proxy_headers(email)), *headers]
    scope = scope_of(method, unquote(raw_path), lines, scope_type=scope_type, raw_path=raw_path)
    scope["query_string"] = query.encode("latin-1")
    return scope


class Rig:
    def __init__(self, socket_dir: Path) -> None:
        self.socket_dir = socket_dir
        self.registry: StubRegistry = seeded_registry()
        self.routes = RouteTable()
        self.connections = OpenConnections()
        self.metrics = RecordingMetrics()
        self.groups = CountingGroups()
        self.upstream = UpstreamClient()
        self._stack = contextlib.AsyncExitStack()
        self._exchanges: list[Exchange | SocketExchange] = []
        self._app: App | None = None

    @property
    def app(self) -> App:
        if self._app is None:
            self._app = self.build()
        return self._app

    def build(self, **overrides: object) -> App:
        options: dict[str, Any] = {
            "registry": self.registry,
            "group_source": self.groups,
            "routes": self.routes,
            "connections": self.connections,
            "metrics": self.metrics,
            "upstream_client": self.upstream,
            **overrides,
        }
        return create_edge_app(**options)

    async def serve(self, slug: str, handler: Handler, *, read_body: bool = True) -> Child:
        child = Child(self.socket_dir / f"{slug}.sock", handler, read_body=read_body)
        await child.start()
        self._stack.push_async_callback(child.stop)
        self.routes.open_route(slug, Route(release_no=1, socket_path=child.path))
        return child

    async def serve_websocket(
        self,
        slug: str,
        handler: Callable[[WebSocketServerProtocol], Awaitable[None]],
        **options: object,
    ) -> list[tuple[str, Headers]]:
        """A WebSocket child; the result lists the handshakes it saw as (path, headers)."""
        handshakes: list[tuple[str, Headers]] = []

        async def wrapped(connection: WebSocketServerProtocol) -> None:
            handshakes.append((connection.path, connection.request_headers))
            await handler(connection)

        path = self.socket_dir / f"{slug}.sock"
        await self._stack.enter_async_context(unix_serve(wrapped, str(path), **options))
        self.routes.open_route(slug, Route(release_no=1, socket_path=path))
        return handshakes

    def set_setting(self, key: str, value: object) -> None:
        self.registry.seed(
            "platform_settings", {"key": key, "value": value, "updated_by": DANA, "updated_at": NOW}
        )

    def start(
        self,
        method: str,
        target: str,
        *,
        email: str | None = BOB,
        headers: Sequence[tuple[str, str]] = (),
        body: Sequence[bytes] = (),
        hold_body: bool = False,
        app: App | None = None,
    ) -> Exchange:
        scope = http_scope(method, target, email=email, headers=headers)
        exchange = Exchange(app or self.app, scope, body=body, hold_body=hold_body)
        self._exchanges.append(exchange)
        return exchange

    async def request(
        self,
        method: str,
        target: str,
        *,
        email: str | None = BOB,
        headers: Sequence[tuple[str, str]] = (),
        body: Sequence[bytes] = (),
        app: App | None = None,
    ) -> Answer:
        exchange = self.start(method, target, email=email, headers=headers, body=body, app=app)
        return await exchange.answer()

    def open_websocket(
        self,
        target: str,
        *,
        email: str | None = BOB,
        headers: Sequence[tuple[str, str]] = (),
        subprotocols: Sequence[str] = (),
        can_refuse_with_status: bool = True,
        app: App | None = None,
    ) -> SocketExchange:
        scope = http_scope("GET", target, email=email, headers=headers, scope_type="websocket")
        scope["subprotocols"] = list(subprotocols)
        scope["extensions"] = {"websocket.http.response": {}} if can_refuse_with_status else {}
        exchange = SocketExchange(app or self.app, scope)
        self._exchanges.append(exchange)
        return exchange

    def assert_clean(self, answer: Answer) -> None:
        """No token, e-mail, path or trace in what a visitor is told."""
        leaks = (
            VISITOR_TOKEN,
            ALICE,
            BOB,
            CAROL,
            DANA,
            FRANK,
            "Traceback",
            "Errno",
            ".sock",
            str(self.socket_dir),
            "Exception",
        )
        for leak in leaks:
            assert leak not in answer.text, f"{leak!r} reached the visitor"

    async def close(self) -> None:
        for exchange in self._exchanges:
            exchange.task.cancel()
        await asyncio.gather(*(e.task for e in self._exchanges), return_exceptions=True)
        await self._stack.aclose()
        await self.upstream.aclose()


@pytest.fixture
def socket_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    # AF_UNIX paths are limited to about 100 characters, so the directory name stays short.
    return tmp_path_factory.mktemp("e")


@pytest.fixture
async def rig(socket_dir: Path) -> AsyncIterator[Rig]:
    instance = Rig(socket_dir)
    try:
        yield instance
    finally:
        await instance.close()


# --- 401: identity -------------------------------------------------------------------------------


async def test_a_request_without_a_forwarded_identity_is_refused_401(rig: Rig) -> None:
    child = await rig.serve("sales", answer())

    reply = await rig.request("GET", "/sales/x", email=None)

    assert reply.status == 401
    assert child.received == []
    rig.assert_clean(reply)


async def test_a_repeated_identity_header_is_refused_401(rig: Rig) -> None:
    # The proxy writes X-Forwarded-Email once; a second line means no line can be trusted.
    child = await rig.serve("sales", answer())

    reply = await rig.request("GET", "/sales/x", headers=[("X-Forwarded-Email", FRANK)])

    assert reply.status == 401
    assert child.received == []


# --- 404 and routing -----------------------------------------------------------------------------


async def test_an_authorized_visitor_reaches_the_child_without_the_prefix(rig: Rig) -> None:
    child = await rig.serve("sales", answer(body=b"hello"))

    reply = await rig.request("GET", "/sales/api/orders?limit=5&q=a%20b")

    assert (reply.status, reply.body) == (200, b"hello")
    assert [(r.method, r.target) for r in child.received] == [
        ("GET", "/api/orders?limit=5&q=a%20b")
    ]


@pytest.mark.parametrize(("path", "upstream_target"), [("/sales", "/"), ("/sales/", "/")])
async def test_the_app_root_reaches_the_child_as_slash(
    rig: Rig, path: str, upstream_target: str
) -> None:
    child = await rig.serve("sales", answer())

    reply = await rig.request("GET", path)

    assert reply.status == 200
    assert [r.target for r in child.received] == [upstream_target]


async def test_a_slug_matches_as_a_whole_first_segment(rig: Rig) -> None:
    child = await rig.serve("sales", answer())

    reply = await rig.request("GET", "/sales-old/x")

    assert reply.status == 404
    assert child.received == []


async def test_an_unknown_app_and_a_denied_one_answer_the_same_404_after_the_same_work(
    rig: Rig,
) -> None:
    docs = await rig.serve("docs", answer())
    base = await rig.request("GET", "/_appmux/not-a-page")

    before = rig.groups.lookups
    unknown = await rig.request("GET", "/nowhere/x")
    unknown_lookups = rig.groups.lookups - before
    before = rig.groups.lookups
    denied = await rig.request("GET", "/docs/x")  # bob has no grant on docs
    denied_lookups = rig.groups.lookups - before

    assert unknown.status == denied.status == 404
    assert unknown.body == denied.body == base.body
    assert unknown.headers == denied.headers
    assert unknown.header("cache-control") == base.header("cache-control")
    assert unknown_lookups == denied_lookups >= 1
    assert docs.received == []


# An App's service principal reaches a child with its application id in X-Forwarded-Email
# (docs/reference/mcp-and-a2a.md); these two ids are made up (T188, ADR 0020).
SALES_BOT: Final = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab"
OTHER_BOT: Final = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def seed_service_principal_grant(rig: Rig, slug: str, application_id: str) -> None:
    rig.registry.seed(
        "app_grants",
        {
            "app_slug": slug,
            "principal_type": "service_principal",
            "principal": application_id,
            "permission": "CAN_USE",
            "granted_by": ALICE,
            "granted_at": NOW,
        },
    )


@pytest.mark.parametrize("forwarded", [SALES_BOT, SALES_BOT.upper()], ids=["lower", "upper"])
async def test_a_service_principal_with_a_grant_reaches_the_child(rig: Rig, forwarded: str) -> None:
    seed_service_principal_grant(rig, "sales", SALES_BOT)
    child = await rig.serve("sales", answer())

    reply = await rig.request("GET", "/sales/x", email=forwarded)

    assert reply.status == 200
    assert len(child.received) == 1


async def test_an_application_id_with_no_grant_gets_the_same_404_as_an_unknown_app(
    rig: Rig,
) -> None:
    # Regression for T188: a new grant type must not open a door for an id no grant names.
    seed_service_principal_grant(rig, "sales", SALES_BOT)
    sales = await rig.serve("sales", answer())
    docs = await rig.serve("docs", answer())

    unknown = await rig.request("GET", "/nowhere/x", email=OTHER_BOT)
    other_bot = await rig.request("GET", "/sales/x", email=OTHER_BOT)
    no_grant = await rig.request("GET", "/docs/x", email=SALES_BOT)

    assert unknown.status == other_bot.status == no_grant.status == 404
    assert unknown.body == other_bot.body == no_grant.body
    assert unknown.headers == other_bot.headers == no_grant.headers
    assert sales.received == [] and docs.received == []


@pytest.mark.parametrize(
    "target",
    [
        "/sales/a%2Fb",
        "/sales/a%5Cb",
        "/sales/%252e%252e/x",
        "/sales/a%00b",
        "/sales/%2e%2e%2fx",
        "/sales/../../x",
        "/sales/x?a=1\r\nX-Evil: 1",
    ],
)
async def test_an_ambiguous_target_is_refused_400_and_never_echoed(rig: Rig, target: str) -> None:
    child = await rig.serve("sales", answer())

    reply = await rig.request("GET", target)

    assert reply.status == 400
    assert child.received == []
    assert target.partition("?")[0] not in reply.text and "X-Evil" not in reply.text
    rig.assert_clean(reply)


async def test_dot_segments_are_resolved_before_the_app_is_picked_and_authorized(
    rig: Rig,
) -> None:
    sales = await rig.serve("sales", answer())
    docs = await rig.serve("docs", answer())

    reaches_sales = await rig.request("GET", "/docs/../sales/x")  # bob may open sales
    denied = await rig.request("GET", "/sales/../docs/x")  # but not docs

    assert reaches_sales.status == 200 and [r.target for r in sales.received] == ["/x"]
    assert denied.status == 404 and docs.received == []


async def test_the_admin_paths_are_still_the_admin_api_and_cannot_be_reached_as_an_app(
    rig: Rig,
) -> None:
    child = await rig.serve("sales", answer())

    me = await rig.request("GET", f"{MOUNT}/me")
    sneaked = await rig.request("GET", "/sales/../_appmux/api/v1/me")

    assert me.status == 200 and json.loads(me.body)["email"] == BOB
    assert sneaked.status == 404 and sneaked.text == "not found"
    assert child.received == []


# --- 503: an app with no route -------------------------------------------------------------------


async def test_an_app_with_no_route_answers_503_with_a_page_naming_its_state(rig: Rig) -> None:
    reply = await rig.request("GET", "/sales/x")

    assert reply.status == 503
    assert reply.header("content-type") == "text/html; charset=utf-8"
    assert "<code>state</code>: starting" in reply.text and "sales" in reply.text
    assert reply.header("retry-after") == "5"
    assert reply.header("content-security-policy") == "default-src 'none'; frame-ancestors 'none'"
    rig.assert_clean(reply)


async def test_a_stopped_app_says_so_and_does_not_ask_the_client_to_hurry(rig: Rig) -> None:
    rig.registry.seed(
        "app_config",
        {"app_slug": "sales", "desired_state": "stopped", "updated_by": DANA, "updated_at": NOW},
    )

    reply = await rig.request("GET", "/sales/x")

    assert reply.status == 503
    assert "<code>state</code>: stopped" in reply.text
    assert reply.header("retry-after") is None


class UnloadedRegistry(StubRegistry):
    """A registry whose first snapshot never loaded: reading ``snapshot`` raises."""

    @property
    def snapshot(self) -> RegistrySnapshot:
        raise RegistryUnavailable


async def test_a_registry_with_no_snapshot_answers_503_and_forwards_nothing(rig: Rig) -> None:
    child = await rig.serve("sales", answer())
    app = rig.build(registry=UnloadedRegistry(clock=lambda: NOW))

    reply = await rig.request("GET", "/sales/x", app=app)
    anonymous = await rig.request("GET", "/sales/x", email=None, app=app)

    assert reply.status == 503 and reply.header("retry-after") == "5"
    assert anonymous.status == 401  # identity is checked before the registry is asked
    assert child.received == []
    rig.assert_clean(reply)


async def test_only_the_app_without_a_route_is_affected(rig: Rig) -> None:
    docs = await rig.serve("docs", answer(body=b"docs"))

    sales = await rig.request("GET", "/sales/x")
    served = await rig.request("GET", "/docs/x", email=FRANK)

    assert sales.status == 503
    assert (served.status, served.body) == (200, b"docs")
    assert len(docs.received) == 1


async def test_a_visitor_who_may_not_open_the_app_gets_404_not_its_state(rig: Rig) -> None:
    unknown = await rig.request("GET", "/nowhere/x", email=FRANK)

    reply = await rig.request("GET", "/sales/x", email=FRANK)  # frank has no grant on sales

    assert reply.status == 404
    assert reply.body == unknown.body


# --- 413 and 429: the caps -----------------------------------------------------------------------


async def test_a_declared_body_over_the_cap_is_refused_413_before_the_child_hears_of_it(
    rig: Rig,
) -> None:
    rig.set_setting("max_request_body_mb", 1)
    child = await rig.serve("sales", answer())

    reply = await rig.request(
        "POST", "/sales/up", headers=[("Content-Length", str(ONE_MIB + 1))], body=[b"x"]
    )

    assert reply.status == 413
    assert child.received == []
    rig.assert_clean(reply)


async def test_a_streamed_body_over_the_cap_is_refused_413_and_reading_stops(rig: Rig) -> None:
    rig.set_setting("max_request_body_mb", 1)
    child = await rig.serve("sales", answer())
    pieces = [b"x" * (400 * 1024)] * 4  # 1.6 MiB, no Content-Length

    exchange = rig.start(
        "POST", "/sales/up", headers=[("Transfer-Encoding", "chunked")], body=pieces
    )
    reply = await exchange.answer()

    assert reply.status == 413
    assert child.received == []  # the child never got a complete request
    assert exchange.body_messages_taken < len(pieces) + 1  # the end of the body was never read


async def test_a_body_of_exactly_the_cap_passes(rig: Rig) -> None:
    rig.set_setting("max_request_body_mb", 1)
    child = await rig.serve("sales", answer())
    half = b"x" * (ONE_MIB // 2)

    reply = await rig.request(
        "POST", "/sales/up", headers=[("Transfer-Encoding", "chunked")], body=[half, half]
    )

    assert reply.status == 200
    assert len(child.received[0].body) == ONE_MIB


async def test_requests_over_the_cap_get_429_with_retry_after_and_only_for_that_app(
    rig: Rig,
) -> None:
    rig.set_setting("max_concurrent_requests", 2)
    gate = asyncio.Event()
    sales = await rig.serve("sales", hold_until(gate))
    await rig.serve("docs", answer())
    held = [rig.start("GET", "/sales/a"), rig.start("GET", "/sales/b")]
    await wait_until(lambda: len(sales.received) == 2)

    refused = await rig.request("GET", "/sales/c")
    other_app = await rig.request("GET", "/docs/x", email=FRANK)
    gate.set()
    finished = [await exchange.answer() for exchange in held]
    admitted_again = await rig.request("GET", "/sales/d")

    assert refused.status == 429 and refused.header("retry-after") == "1"
    assert len(sales.received) == 3  # the refused one never reached the child
    assert other_app.status == 200
    assert [reply.status for reply in finished] == [200, 200]
    assert admitted_again.status == 200
    rig.assert_clean(refused)


async def test_a_place_is_given_back_whichever_way_a_request_ends(rig: Rig) -> None:
    rig.set_setting("max_concurrent_requests", 1)
    rig.set_setting("max_request_body_mb", 1)
    await rig.serve("sales", scripted(hang_up, speak_garbage, answer()))

    unreachable = await rig.request("GET", "/sales/a")
    garbled = await rig.request("GET", "/sales/b")
    too_big = await rig.request(
        "POST", "/sales/c", headers=[("Content-Length", str(ONE_MIB + 1))], body=[b"x"]
    )
    served = await rig.request("GET", "/sales/d")

    assert [r.status for r in (unreachable, garbled, too_big, served)] == [502, 502, 413, 200]


async def test_a_client_that_leaves_a_stream_frees_its_place_and_the_child_sees_the_hangup(
    rig: Rig,
) -> None:
    rig.set_setting("max_concurrent_requests", 1)
    sales = await rig.serve("sales", scripted(endless_stream, answer()))
    stream = rig.start("GET", "/sales/events")
    assert await stream.started() == 200
    assert (await stream.next_body()).startswith(b"data:")
    assert (await rig.request("GET", "/sales/other")).status == 429  # the stream holds the place

    stream.disconnect()
    await stream.finished()
    await asyncio.wait_for(sales.hung_up.wait(), TIMEOUT_S)

    assert (await rig.request("GET", "/sales/after")).status == 200
    assert not stream.completed  # nothing pretended the stream ended cleanly


async def test_a_client_that_leaves_during_an_upload_frees_its_place(rig: Rig) -> None:
    rig.set_setting("max_concurrent_requests", 1)

    async def read_until_hangup(request: Received, connection: Connection) -> None:
        # The body bytes already sent sit unread in the buffer, and `at_eof()` is false until they
        # are read, so read to the end: `b""` is the edge hanging up.
        while await connection.reader.read(4096):
            pass
        connection.peer_gone()

    sales = await rig.serve("sales", scripted(read_until_hangup, answer()), read_body=False)
    upload = rig.start(
        "POST",
        "/sales/up",
        headers=[("Transfer-Encoding", "chunked")],
        body=[b"part"],
        hold_body=True,
    )
    await wait_until(lambda: len(sales.received) == 1)

    upload.disconnect()
    await upload.finished()
    await asyncio.wait_for(sales.hung_up.wait(), TIMEOUT_S)

    assert (await rig.request("GET", "/sales/after")).status == 200
    assert not upload.completed


async def test_a_client_that_leaves_before_the_child_answers_frees_its_place(rig: Rig) -> None:
    rig.set_setting("max_concurrent_requests", 1)

    async def never_answers(request: Received, connection: Connection) -> None:
        while await connection.reader.read(4096):  # `b""` is the edge hanging up
            pass
        connection.peer_gone()

    sales = await rig.serve("sales", scripted(never_answers, answer()))
    waiting = rig.start("GET", "/sales/slow")  # no body: nothing else tells the edge it left
    await wait_until(lambda: len(sales.received) == 1)

    waiting.disconnect()
    await waiting.finished()
    await asyncio.wait_for(sales.hung_up.wait(), TIMEOUT_S)

    assert (await rig.request("GET", "/sales/after")).status == 200
    assert not waiting.completed


# --- 502 and 504 ---------------------------------------------------------------------------------


async def test_a_child_nobody_listens_for_answers_502_with_a_page_that_names_no_path(
    rig: Rig,
) -> None:
    rig.routes.open_route("sales", Route(release_no=1, socket_path=rig.socket_dir / "gone.sock"))

    reply = await rig.request("GET", "/sales/x")

    assert reply.status == 502
    assert "sales" in reply.text and reply.header("content-type") == "text/html; charset=utf-8"
    rig.assert_clean(reply)


@pytest.mark.parametrize("misbehaviour", [hang_up, speak_garbage])
async def test_a_child_that_hangs_up_or_does_not_speak_http_answers_502(
    rig: Rig, misbehaviour: Handler
) -> None:
    await rig.serve("sales", misbehaviour)

    reply = await rig.request("GET", "/sales/x")

    assert reply.status == 502
    rig.assert_clean(reply)


async def test_a_child_that_does_not_answer_in_time_gets_504_and_the_app_recovers(
    rig: Rig,
) -> None:
    rig.set_setting("request_timeout_s", 1)
    rig.set_setting("max_concurrent_requests", 1)
    await rig.serve("sales", scripted(stay_silent, answer()))

    slow = await rig.request("GET", "/sales/x")
    recovered = await rig.request("GET", "/sales/y")

    assert slow.status == 504
    assert "sales" in slow.text and slow.header("retry-after") is None
    assert recovered.status == 200
    rig.assert_clean(slow)


async def test_a_child_that_dies_mid_body_never_gets_a_complete_response(rig: Rig) -> None:
    await rig.serve("sales", die_mid_body)

    exchange = rig.start("GET", "/sales/x")
    await exchange.finished()

    assert await exchange.started() == 200  # the status was already sent; it cannot be taken back
    assert not exchange.completed  # so the response is left unfinished and the browser sees it


# --- what the child receives ---------------------------------------------------------------------


async def test_the_child_receives_the_proxy_identity_and_the_prefix_and_nothing_forged(
    rig: Rig,
) -> None:
    child = await rig.serve("sales", answer())

    await rig.request(
        "GET",
        "/sales/x",
        headers=[
            ("Host", "apps.example.com"),
            ("X-Forwarded-Prefix", "/evil"),
            ("X-Appmux-Role", "admin"),
            ("Authorization", "Bearer client-supplied"),
            ("X-Forwarded-For", "198.51.100.1"),
            ("Connection", "keep-alive, X-Hop"),
            ("X-Hop", "1"),
            ("Upgrade", "h2c"),
        ],
    )

    seen = child.received[0]
    assert seen.header("x-forwarded-prefix") == ["/sales"]
    assert seen.header("x-forwarded-email") == [BOB]
    assert seen.header("x-real-ip") == [CLIENT_ADDRESS]
    assert seen.header("x-request-id") == [REQUEST_ID]
    assert seen.header("host") == ["apps.example.com"]
    assert seen.header("authorization") == ["Bearer client-supplied"]
    assert seen.header("x-forwarded-for") == ["198.51.100.1"]
    assert seen.header("x-appmux-role") == ["admin"]
    assert seen.header("x-hop") == [] and seen.header("upgrade") == []
    assert seen.header("x-forwarded-access-token") == []  # not forwarded unless a policy says so


async def test_the_access_token_is_forwarded_only_when_the_policy_says_so(rig: Rig) -> None:
    child = await rig.serve("sales", answer())
    asked: list[tuple[str, str, str | None]] = []

    def policy_returning(verdict: bool | Exception) -> App:
        async def policy(actor: Actor, slug: str, headers: Any) -> bool:
            asked.append((actor.email, slug, headers.get("cookie")))
            if isinstance(verdict, Exception):
                raise verdict
            return verdict

        return rig.build(forward_token_policy=policy)

    cookie = [("Cookie", "k=v")]
    yes = await rig.request("GET", "/sales/a", headers=cookie, app=policy_returning(True))
    no = await rig.request("GET", "/sales/b", headers=cookie, app=policy_returning(False))
    broken = await rig.request(
        "GET", "/sales/c", headers=cookie, app=policy_returning(RuntimeError("boom"))
    )

    assert [r.status for r in (yes, no, broken)] == [200, 200, 200]
    assert [r.header("x-forwarded-access-token") for r in child.received] == [
        [VISITOR_TOKEN],
        [],
        [],
    ]
    assert asked == [(BOB, "sales", "k=v")] * 3


async def test_the_token_policy_is_not_asked_about_a_request_that_cannot_be_served(
    rig: Rig,
) -> None:
    asked: list[str] = []

    async def policy(actor: Actor, slug: str, headers: Any) -> bool:
        asked.append(slug)
        return True

    app = rig.build(forward_token_policy=policy)

    no_route = await rig.request("GET", "/sales/x", app=app)  # allowed, but no route
    denied = await rig.request("GET", "/docs/x", app=app)  # not allowed
    anonymous = await rig.request("GET", "/sales/x", email=None, app=app)

    assert [no_route.status, denied.status, anonymous.status] == [503, 404, 401]
    assert asked == []


@pytest.mark.parametrize(
    "bad_headers",
    [
        [("Host", "first-host.example"), ("Host", "second-host.example")],
        [("Content-Length", "3"), ("Transfer-Encoding", "chunked")],
        [("X-Probe", "a\r\nX-Evil: 1")],
        [("Bad Name", "v")],
    ],
)
async def test_a_header_that_cannot_be_forwarded_is_refused_400_and_nothing_is_forwarded(
    rig: Rig, bad_headers: list[tuple[str, str]]
) -> None:
    child = await rig.serve("sales", answer())

    reply = await rig.request("POST", "/sales/x", headers=bad_headers, body=[b"abc"])

    assert reply.status == 400
    assert child.received == []
    for value in ("first-host.example", "second-host.example", "X-Evil"):
        assert value not in reply.text
    rig.assert_clean(reply)


async def test_a_request_with_no_body_goes_out_with_no_framing(rig: Rig) -> None:
    child = await rig.serve("sales", answer())

    await rig.request("GET", "/sales/x")

    seen = child.received[0]
    assert seen.header("content-length") == [] and seen.header("transfer-encoding") == []
    assert seen.body == b""


async def test_a_body_with_a_length_is_forwarded_with_that_length(rig: Rig) -> None:
    child = await rig.serve("sales", answer())

    await rig.request("POST", "/sales/x", headers=[("Content-Length", "5")], body=[b"hel", b"lo"])

    seen = child.received[0]
    assert seen.body == b"hello"
    assert seen.header("content-length") == ["5"] and seen.header("transfer-encoding") == []


async def test_a_chunked_body_is_forwarded_whole(rig: Rig) -> None:
    child = await rig.serve("sales", answer())

    await rig.request(
        "PUT", "/sales/x", headers=[("Transfer-Encoding", "chunked")], body=[b"hel", b"lo"]
    )

    seen = child.received[0]
    assert seen.body == b"hello" and seen.header("content-length") == []


# --- what the browser receives -------------------------------------------------------------------


async def test_the_response_headers_are_rewritten_and_the_body_is_not(rig: Rig) -> None:
    body = gzip.compress(b"compressed payload")
    await rig.serve(
        "sales",
        answer(
            body=body,
            status="302 Found",
            headers=[
                "Location: /login",
                "Set-Cookie: sid=1; Domain=evil.example; Path=/; HttpOnly",
                "Set-Cookie: theme=dark; Path=/app",
                "Keep-Alive: timeout=5",
                "Content-Encoding: gzip",
            ],
        ),
    )

    reply = await rig.request("GET", "/sales/x")

    assert reply.status == 302
    assert reply.header("location") == "/sales/login"
    cookies = reply.headers_named("set-cookie")
    assert len(cookies) == 2
    assert all("Path=/sales/" in cookie and "Domain" not in cookie for cookie in cookies)
    assert "HttpOnly" in cookies[0]
    assert reply.header("keep-alive") is None
    assert reply.header("content-encoding") == "gzip"
    assert reply.body == body


async def test_a_head_request_gets_the_childs_headers_and_no_body(rig: Rig) -> None:
    async def head_answer(request: Received, connection: Connection) -> None:
        await connection.send(http_response(headers=["Content-Length: 5", "X-Seen: yes"]))

    await rig.serve("sales", head_answer)

    reply = await rig.request("HEAD", "/sales/x")

    assert reply.status == 200 and reply.body == b"" and reply.header("x-seen") == "yes"


# --- streaming -----------------------------------------------------------------------------------


async def test_a_response_streams_to_the_browser_before_the_child_has_finished(rig: Rig) -> None:
    gate = asyncio.Event()

    async def two_parts(request: Received, connection: Connection) -> None:
        head = http_response(headers=["Content-Type: text/plain", "Transfer-Encoding: chunked"])
        await connection.send(head + chunk(b"first"))
        await gate.wait()
        await connection.send(chunk(b"second") + LAST_CHUNK)

    await rig.serve("sales", two_parts)
    exchange = rig.start("GET", "/sales/x")

    assert await exchange.started() == 200
    assert await exchange.next_body() == b"first"
    assert not exchange.completed  # the child is still holding the rest
    gate.set()
    reply = await exchange.answer()

    assert reply.body == b"firstsecond"


async def test_a_request_body_streams_to_the_child_before_the_browser_has_finished(
    rig: Rig,
) -> None:
    first_seen = asyncio.Event()

    async def read_in_pieces(request: Received, connection: Connection) -> None:
        pieces: list[bytes] = []
        async for piece in read_chunked(connection.reader):
            pieces.append(piece)
            first_seen.set()
        await connection.send(http_response(body=b"|".join(pieces)))

    await rig.serve("sales", read_in_pieces, read_body=False)
    upload = rig.start(
        "POST",
        "/sales/up",
        headers=[("Transfer-Encoding", "chunked")],
        body=[b"alpha"],
        hold_body=True,
    )

    await asyncio.wait_for(
        first_seen.wait(), TIMEOUT_S
    )  # the child has it; the browser is not done
    assert not upload.completed
    upload.feed(b"beta")
    upload.end_body()
    reply = await upload.answer()

    assert reply.body == b"alpha|beta"


# --- WebSocket -----------------------------------------------------------------------------------


async def echo(connection: WebSocketServerProtocol) -> None:
    async for message in connection:
        await connection.send(message)


async def test_a_websocket_is_relayed_in_both_directions_and_registered_while_open(
    rig: Rig,
) -> None:
    handshakes = await rig.serve_websocket("sales", echo)
    socket = rig.open_websocket("/sales/ws/stream?x=1")

    assert (await socket.next_sent())["type"] == "websocket.accept"
    socket.browser_sends(text="ping")
    assert (await socket.next_sent())["text"] == "ping"
    socket.browser_sends(data=b"\x00\x01")
    assert (await socket.next_sent())["bytes"] == b"\x00\x01"
    assert len(rig.connections) == 1 and rig.metrics.opened == ["sales"]
    socket.browser_leaves()
    await socket.finished()

    path, headers = handshakes[0]
    assert path == "/ws/stream?x=1"
    assert headers["X-Forwarded-Prefix"] == "/sales" and headers["X-Forwarded-Email"] == BOB
    assert VISITOR_TOKEN not in json.dumps(list(headers.raw_items()))
    assert len(rig.connections) == 0 and rig.metrics.closed == ["sales"]


async def test_the_subprotocol_the_child_picks_is_the_one_the_browser_gets(rig: Rig) -> None:
    await rig.serve_websocket("sales", echo, subprotocols=["chat"])
    socket = rig.open_websocket("/sales/ws", subprotocols=["superchat", "chat"])

    accepted = await socket.next_sent()
    socket.browser_leaves()
    await socket.finished()

    assert accepted["subprotocol"] == "chat"


@pytest.mark.parametrize(
    ("email", "target", "status"),
    [
        (None, "/sales/ws", 401),
        (BOB, "/nowhere/ws", 404),
        (FRANK, "/sales/ws", 404),  # frank may not open sales: the same answer as an unknown app
        (BOB, "/sales/a%2Fb", 400),
    ],
)
async def test_a_websocket_handshake_is_refused_with_the_status_of_a_request(
    rig: Rig, email: str | None, target: str, status: int
) -> None:
    handshakes = await rig.serve_websocket("sales", echo)
    socket = rig.open_websocket(target, email=email)

    start = await socket.next_sent()
    await socket.finished()

    assert (start["type"], start["status"]) == ("websocket.http.response.start", status)
    assert handshakes == [] and len(rig.connections) == 0


async def test_a_websocket_to_an_app_with_no_route_is_refused_503(rig: Rig) -> None:
    socket = rig.open_websocket("/sales/ws")

    start = await socket.next_sent()
    await socket.finished()

    assert (start["type"], start["status"]) == ("websocket.http.response.start", 503)


async def test_a_refusal_without_the_status_extension_closes_before_accepting(rig: Rig) -> None:
    socket = rig.open_websocket("/nowhere/ws", can_refuse_with_status=False)

    first = await socket.next_sent()
    await socket.finished()

    assert first["type"] == "websocket.close"


async def test_a_child_that_refuses_the_handshake_is_passed_on_and_an_absent_one_is_a_502(
    rig: Rig,
) -> None:
    async def forbid(path: str, request_headers: Any) -> tuple[HTTPStatus, list[Any], bytes]:
        return HTTPStatus.FORBIDDEN, [], b"no"

    await rig.serve_websocket("sales", echo, process_request=forbid)
    refused = rig.open_websocket("/sales/ws")
    rig.routes.open_route("docs", Route(release_no=1, socket_path=rig.socket_dir / "gone.sock"))
    absent = rig.open_websocket("/docs/ws", email=FRANK)

    refused_start = await refused.next_sent()
    absent_start = await absent.next_sent()

    assert refused_start["status"] == 403
    assert absent_start["status"] == 502


async def test_a_websocket_does_not_use_up_a_place_for_requests(rig: Rig) -> None:
    async def plain_http(
        path: str, request_headers: Any
    ) -> tuple[HTTPStatus, list[Any], bytes] | None:
        if request_headers.get("Upgrade", "").lower() == "websocket":
            return None
        return HTTPStatus.OK, [], b"plain"

    rig.set_setting("max_concurrent_requests", 1)
    await rig.serve_websocket("sales", echo, process_request=plain_http)
    socket = rig.open_websocket("/sales/ws")
    await socket.next_sent()

    reply = await rig.request("GET", "/sales/page")
    socket.browser_leaves()
    await socket.finished()

    assert (reply.status, reply.body) == (200, b"plain")


# --- the metrics ---------------------------------------------------------------------------------


async def test_a_served_request_is_counted_for_its_app_with_its_bytes(rig: Rig) -> None:
    await rig.serve("sales", answer(body=b"hello"))

    await rig.request("POST", "/sales/x", headers=[("Content-Length", "3")], body=[b"abc"])

    [counted] = rig.metrics.requests
    assert counted["app_slug"] == "sales" and counted["status"] == 200
    assert (counted["bytes_in"], counted["bytes_out"]) == (3, 5)
    assert counted["latency_ms"] >= 0
    assert (counted["upstream_error"], counted["limited"]) == (False, False)
    assert rig.metrics.in_flight == [("sales", 1), ("sales", 0)]


async def test_a_refusal_before_an_app_is_known_is_counted_for_no_app(rig: Rig) -> None:
    await rig.request("GET", "/nowhere/x")
    await rig.request("GET", "/sales/x", email=None)

    assert [(m["app_slug"], m["status"]) for m in rig.metrics.requests] == [
        (None, 404),
        (None, 401),
    ]
    assert rig.metrics.in_flight == []


async def test_the_edges_own_refusals_are_marked_for_the_aggregator(rig: Rig) -> None:
    rig.set_setting("max_request_body_mb", 1)
    rig.set_setting("request_timeout_s", 1)
    await rig.serve("sales", scripted(hang_up, stay_silent))

    await rig.request(
        "POST", "/sales/a", headers=[("Content-Length", str(ONE_MIB + 1))], body=[b"x"]
    )
    await rig.request("GET", "/sales/b")  # the child hangs up: 502
    await rig.request("GET", "/sales/c")  # the child stays silent: 504

    flags = [(m["status"], m["limited"], m["upstream_error"]) for m in rig.metrics.requests]
    assert flags == [(413, True, False), (502, False, True), (504, False, True)]
    assert {m["app_slug"] for m in rig.metrics.requests} == {"sales"}


async def test_a_429_is_marked_as_limited(rig: Rig) -> None:
    rig.set_setting("max_concurrent_requests", 1)
    gate = asyncio.Event()
    sales = await rig.serve("sales", hold_until(gate))
    held = rig.start("GET", "/sales/a")
    await wait_until(lambda: len(sales.received) == 1)

    await rig.request("GET", "/sales/b")
    gate.set()
    await held.answer()

    refused = [m for m in rig.metrics.requests if m["status"] == 429]
    assert [(m["app_slug"], m["limited"]) for m in refused] == [("sales", True)]
    assert max(count for _, count in rig.metrics.in_flight) == 1


async def test_the_edge_works_without_a_metrics_sink(rig: Rig) -> None:
    await rig.serve("sales", answer())

    reply = await rig.request("GET", "/sales/x", app=rig.build(metrics=None))

    assert reply.status == 200


# --- the self-check gate still wraps the pipeline ------------------------------------------------


async def test_a_failed_self_check_still_stops_every_app_request(rig: Rig) -> None:
    child = await rig.serve("sales", answer())
    state = SelfCheckState(ScriptedCanary(canary_text(failing={"pid_isolation": "marker"})))
    await state.run()
    gated = rig.build(self_check=state)

    reply = await rig.request("GET", "/sales/x", app=gated)
    another = await rig.request("POST", "/sales/y", app=gated, body=[b"abc"])

    assert reply.status == another.status == 503
    assert "marker" not in reply.text
    assert child.received == []


# --- logs ----------------------------------------------------------------------------------------


async def test_nothing_the_pipeline_logs_carries_a_token_a_body_or_an_address(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rig.set_setting("max_request_body_mb", 1)
    await rig.serve("sales", scripted(answer(), hang_up, answer()))
    rig.routes.open_route("docs", Route(release_no=1, socket_path=rig.socket_dir / "gone.sock"))

    await rig.request(
        "POST", "/sales/a", headers=[("Content-Length", str(len(BODY_MARKER)))], body=[BODY_MARKER]
    )
    await rig.request("GET", "/sales/b")  # 502
    await rig.request("GET", "/nowhere/c")  # 404
    await rig.request("GET", "/docs/d", email=FRANK)  # 502, no listener
    await rig.request("GET", "/sales/e", email=FRANK)  # denied
    await rig.request(
        "POST", "/sales/f", headers=[("Content-Length", str(ONE_MIB + 1))], body=[BODY_MARKER]
    )

    logged = "\n".join(
        f"{record.getMessage()} {record.exc_text or ''}" for record in caplog.records
    )
    for secret in (VISITOR_TOKEN, BODY_MARKER.decode(), BOB, FRANK, ALICE, CLIENT_ADDRESS):
        assert secret not in logged
    assert "Traceback" not in logged


async def test_a_websocket_handshake_is_not_logged_with_the_token_it_forwards(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    async def forward(actor: Actor, slug: str, headers: Any) -> bool:
        return True

    await rig.serve_websocket("sales", echo)
    socket = rig.open_websocket("/sales/ws", app=rig.build(forward_token_policy=forward))
    assert (await socket.next_sent())["type"] == "websocket.accept"
    socket.browser_sends(text="ping")
    assert (await socket.next_sent())["text"] == "ping"
    socket.browser_leaves()
    await socket.finished()

    # The stub child is a `websockets` server in this same process and logs what it receives; only
    # the edge's side (its own loggers and its connection to the child) is under test.
    edge_side = [r for r in caplog.records if not r.name.startswith("websockets.server")]
    logged = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in edge_side)
    for secret in (VISITOR_TOKEN, BOB, CLIENT_ADDRESS):
        assert secret not in logged
