"""The WebSocket relay between a browser and a child's unix socket (design 7, FR-027, T084).

The child is a real ``websockets`` server on a unix socket in ``tmp_path``. The browser side is a
queue-backed stand-in that implements the ``ClientSocket`` protocol the edge will adapt Starlette
to, so the relay is tested without an ASGI server. No test opens a network socket.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from websockets.legacy.server import WebSocketServerProtocol, unix_serve

from appmux.edge.connections import (
    ACCESS_REVOKED_CLOSE_CODE,
    RELEASE_REPLACED_CLOSE_CODE,
    RELEASE_REPLACED_CLOSE_REASON,
    OpenConnections,
)
from appmux.identity.actor import Actor
from appmux.upstream.client import UpstreamTimeout, UpstreamUnreachable
from appmux.upstream.websocket import (
    ClientDisconnected,
    UpstreamRejected,
    relay_websocket,
)

pytestmark = pytest.mark.anyio

ALICE = Actor(email="alice@example.com")
TIMEOUT_S = 5.0
Handler = Callable[[WebSocketServerProtocol], Awaitable[None]]


class FakeClientSocket:
    """The browser side: what the edge's adapter of a Starlette ``WebSocket`` offers the relay."""

    def __init__(self) -> None:
        self.accepted_with: list[str | None] = []
        self.sent: list[str | bytes] = []
        self.closed_with: list[tuple[int, str]] = []
        self._incoming: asyncio.Queue[str | bytes | ClientDisconnected] = asyncio.Queue()
        self._closed = asyncio.Event()

    def browser_sends(self, message: str | bytes) -> None:
        self._incoming.put_nowait(message)

    def browser_leaves(self, code: int = 1000) -> None:
        self._incoming.put_nowait(ClientDisconnected(code))

    async def accept(self, subprotocol: str | None) -> None:
        self.accepted_with.append(subprotocol)

    async def receive(self) -> str | bytes:
        item = await self._incoming.get()
        if isinstance(item, ClientDisconnected):
            raise item
        return item

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int, reason: str) -> None:
        self.closed_with.append((code, reason))
        self._closed.set()
        self._incoming.put_nowait(ClientDisconnected(code))  # a closed socket ends receive()


@dataclass
class Child:
    path: Path
    handler: Handler
    handshakes: list[WebSocketServerProtocol] = field(default_factory=list)
    close_codes: list[int] = field(default_factory=list)


@contextlib.asynccontextmanager
async def serving(socket_path: Path, handler: Handler, **options: object) -> AsyncIterator[Child]:
    child = Child(socket_path, handler)

    async def wrapped(connection: WebSocketServerProtocol) -> None:
        child.handshakes.append(connection)
        try:
            await handler(connection)
        finally:
            await connection.wait_closed()
            child.close_codes.append(connection.close_code or 0)

    async with unix_serve(wrapped, str(socket_path), **options):
        yield child


async def echo(connection: WebSocketServerProtocol) -> None:
    async for message in connection:
        await connection.send(message)


async def wait_until(condition: Callable[[], object]) -> None:
    """Return once ``condition()`` holds; fail the test after ``TIMEOUT_S`` instead of hanging."""
    async with asyncio.timeout(TIMEOUT_S):
        # The state is the fakes' own lists, which have no event to await.
        while not condition():  # noqa: ASYNC110 - polling state this test file owns
            await asyncio.sleep(0.01)


@pytest.fixture
def socket_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("w") / "c.sock"


async def run_relay(
    client: FakeClientSocket,
    socket_path: Path,
    connections: OpenConnections | None = None,
    **options: object,
) -> None:
    arguments: dict[str, object] = {
        "target": "/ws/stream?x=1",
        "headers": [("Host", "apps.example.com"), ("X-Forwarded-Prefix", "/sales")],
        "subprotocols": [],
        "open_timeout_s": TIMEOUT_S,
        "release_no": 1,
        **options,
    }
    await asyncio.wait_for(
        relay_websocket(
            client,
            socket_path=socket_path,
            connections=connections or OpenConnections(),
            actor=ALICE,
            slug="sales",
            **arguments,
        ),
        TIMEOUT_S,
    )


# --- messages -----------------------------------------------------------------------------------


async def test_text_and_binary_frames_travel_both_ways_in_order(socket_path: Path) -> None:
    client = FakeClientSocket()
    async with serving(socket_path, echo):
        relay = asyncio.create_task(run_relay(client, socket_path))
        for message in ("one", b"\x00\x01\x02", "three"):
            client.browser_sends(message)
        await wait_until(lambda: len(client.sent) >= 3)
        client.browser_leaves()
        await relay
    assert client.sent == ["one", b"\x00\x01\x02", "three"]


async def test_a_message_from_the_child_that_nobody_asked_for_reaches_the_browser(
    socket_path: Path,
) -> None:
    async def greets(connection: WebSocketServerProtocol) -> None:
        await connection.send("welcome")
        await connection.send(b"\xff")
        await connection.wait_closed()

    client = FakeClientSocket()
    async with serving(socket_path, greets):
        relay = asyncio.create_task(run_relay(client, socket_path))
        await wait_until(lambda: len(client.sent) >= 2)
        client.browser_leaves()
        await relay
    assert client.sent == ["welcome", b"\xff"]


# --- the handshake ------------------------------------------------------------------------------


async def test_the_child_sees_the_target_the_host_and_the_forwarded_headers(
    socket_path: Path,
) -> None:
    client = FakeClientSocket()
    async with serving(socket_path, echo) as child:
        relay = asyncio.create_task(
            run_relay(
                client,
                socket_path,
                headers=[
                    ("Host", "apps.example.com"),
                    ("Origin", "https://apps.example.com"),
                    ("X-Forwarded-Prefix", "/sales"),
                    ("Cookie", "a=1"),
                ],
            )
        )
        await wait_until(lambda: bool(client.accepted_with))
        client.browser_leaves()
        await relay
    (connection,) = child.handshakes
    assert connection.path == "/ws/stream?x=1"
    seen = connection.request_headers
    assert seen.get_all("Host") == ["apps.example.com"]
    assert seen.get_all("Origin") == ["https://apps.example.com"]
    assert seen.get_all("X-Forwarded-Prefix") == ["/sales"]
    assert seen.get_all("Cookie") == ["a=1"]


@pytest.mark.parametrize(
    "target",
    ["/ws", "/ws/a%20b/caf%C3%A9?x=1&y=%2F", "/s;p=1/%7Euser?q=a|b&r={c}"],
)
async def test_the_target_reaches_the_child_as_the_edge_judged_it(
    socket_path: Path, target: str
) -> None:
    client = FakeClientSocket()
    async with serving(socket_path, echo) as child:
        relay = asyncio.create_task(run_relay(client, socket_path, target=target))
        await wait_until(lambda: bool(client.accepted_with))
        client.browser_leaves()
        await relay
    assert child.handshakes[0].path == target


async def test_the_subprotocol_the_child_picks_is_the_one_the_browser_is_given(
    socket_path: Path,
) -> None:
    client = FakeClientSocket()
    async with serving(socket_path, echo, subprotocols=["chat"]):
        relay = asyncio.create_task(
            run_relay(client, socket_path, subprotocols=["superchat", "chat"])
        )
        await wait_until(lambda: bool(client.accepted_with))
        client.browser_leaves()
        await relay
    assert client.accepted_with == ["chat"]


async def test_a_child_that_picks_no_subprotocol_leaves_the_browser_with_none(
    socket_path: Path,
) -> None:
    client = FakeClientSocket()
    async with serving(socket_path, echo):
        relay = asyncio.create_task(run_relay(client, socket_path, subprotocols=["chat"]))
        await wait_until(lambda: bool(client.accepted_with))
        client.browser_leaves()
        await relay
    assert client.accepted_with == [None]


async def test_a_host_header_that_is_not_a_host_falls_back_without_failing(
    socket_path: Path,
) -> None:
    client = FakeClientSocket()
    async with serving(socket_path, echo) as child:
        relay = asyncio.create_task(
            run_relay(client, socket_path, headers=[("Host", "bad host/with space")])
        )
        await wait_until(lambda: bool(client.accepted_with))
        client.browser_leaves()
        await relay
    assert child.handshakes[0].request_headers.get_all("Host") == ["localhost"]


async def test_nothing_listening_is_unreachable_and_the_browser_is_never_accepted(
    socket_path: Path,
) -> None:
    client = FakeClientSocket()
    connections = OpenConnections()
    with pytest.raises(UpstreamUnreachable):
        await run_relay(client, socket_path, connections)
    assert client.accepted_with == []
    assert len(connections) == 0


async def test_a_child_that_refuses_the_handshake_is_reported_with_its_status(
    socket_path: Path,
) -> None:
    async def refuse(path: str, headers: object) -> tuple[int, list[tuple[str, str]], bytes]:
        return 403, [], b"no"

    client = FakeClientSocket()
    async with serving(socket_path, echo, process_request=refuse):
        with pytest.raises(UpstreamRejected) as caught:
            await run_relay(client, socket_path)
    assert caught.value.status == 403
    assert client.accepted_with == []


async def test_a_child_that_never_finishes_the_handshake_times_out(socket_path: Path) -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5)

    server = await asyncio.start_unix_server(silent, path=str(socket_path))
    try:
        with pytest.raises(UpstreamTimeout):
            await run_relay(FakeClientSocket(), socket_path, open_timeout_s=0.2)
    finally:
        server.close()
        await server.wait_closed()


# --- closing ------------------------------------------------------------------------------------


async def test_the_browser_leaving_closes_the_childs_side_with_the_same_code(
    socket_path: Path,
) -> None:
    client = FakeClientSocket()
    async with serving(socket_path, echo) as child:
        relay = asyncio.create_task(run_relay(client, socket_path))
        await wait_until(lambda: bool(client.accepted_with))
        client.browser_leaves(1001)
        await relay
        await wait_until(lambda: bool(child.close_codes))
    assert child.close_codes == [1001]


async def test_the_child_closing_closes_the_browser_with_its_code_and_reason(
    socket_path: Path,
) -> None:
    async def says_goodbye(connection: WebSocketServerProtocol) -> None:
        await connection.close(4001, "session over")

    client = FakeClientSocket()
    async with serving(socket_path, says_goodbye):
        await run_relay(client, socket_path)
    assert client.closed_with == [(4001, "session over")]


async def test_a_child_that_dies_without_closing_gives_the_browser_a_server_error_code(
    socket_path: Path,
) -> None:
    async def dies(connection: WebSocketServerProtocol) -> None:
        connection.transport.abort()

    client = FakeClientSocket()
    async with serving(socket_path, dies):
        await run_relay(client, socket_path)
    assert client.closed_with == [(1011, "upstream closed")]


async def test_a_browser_that_is_gone_when_a_message_arrives_ends_the_stream_cleanly(
    socket_path: Path,
) -> None:
    class GoneBrowser(FakeClientSocket):
        async def send_text(self, data: str) -> None:
            raise ClientDisconnected(1001)

    async def sends_text(connection: WebSocketServerProtocol) -> None:
        await connection.send("anyone there?")
        await connection.wait_closed()

    client = GoneBrowser()
    async with serving(socket_path, sends_text) as child:
        await run_relay(client, socket_path)
        await wait_until(lambda: bool(child.close_codes))
    assert child.close_codes == [1001]


async def test_a_reserved_close_code_is_never_sent_to_the_other_side(socket_path: Path) -> None:
    # 1005 ("no status") and 1006 ("abnormal") are reported to an application, never sent.
    client = FakeClientSocket()
    async with serving(socket_path, echo) as child:
        relay = asyncio.create_task(run_relay(client, socket_path))
        await wait_until(lambda: bool(client.accepted_with))
        client.browser_leaves(1006)
        await relay
        await wait_until(lambda: bool(child.close_codes))
    assert child.close_codes == [1001]


async def test_a_message_larger_than_the_limit_closes_both_sides_with_1009(
    socket_path: Path,
) -> None:
    async def big(connection: WebSocketServerProtocol) -> None:
        await connection.send("x" * 5000)
        await connection.wait_closed()

    client = FakeClientSocket()
    async with serving(socket_path, big):
        await run_relay(client, socket_path, max_message_bytes=1000)
    assert client.closed_with[0][0] == 1009
    assert client.sent == []


async def test_a_message_from_the_browser_over_the_limit_closes_both_sides_with_1009(
    socket_path: Path,
) -> None:
    client = FakeClientSocket()
    async with serving(socket_path, echo) as child:
        relay = asyncio.create_task(run_relay(client, socket_path, max_message_bytes=1000))
        await wait_until(lambda: bool(client.accepted_with))
        client.browser_sends("x" * 1001)
        await relay
        await wait_until(lambda: bool(child.close_codes))
    assert [code for code, _ in client.closed_with] == [1009]
    assert child.close_codes == [1009]


# --- the registry of open connections -----------------------------------------------------------


async def test_the_stream_is_registered_while_it_is_open_and_forgotten_after(
    socket_path: Path,
) -> None:
    client = FakeClientSocket()
    connections = OpenConnections()
    async with serving(socket_path, echo):
        relay = asyncio.create_task(run_relay(client, socket_path, connections))
        await wait_until(lambda: bool(client.accepted_with))
        assert len(connections) == 1
        client.browser_leaves()
        await relay
    assert len(connections) == 0


async def test_the_stream_is_forgotten_even_when_the_relay_fails(socket_path: Path) -> None:
    async def dies(connection: WebSocketServerProtocol) -> None:
        connection.transport.abort()

    connections = OpenConnections()
    async with serving(socket_path, dies):
        await run_relay(FakeClientSocket(), socket_path, connections)
    assert len(connections) == 0


async def test_withdrawing_access_closes_both_sides_of_the_stream(socket_path: Path) -> None:
    async def deny(actor: Actor, slug: str) -> bool:
        return False

    client = FakeClientSocket()
    connections = OpenConnections()
    async with serving(socket_path, echo) as child:
        relay = asyncio.create_task(run_relay(client, socket_path, connections))
        await wait_until(lambda: bool(client.accepted_with))
        assert await connections.close_unauthorized(deny) == 1
        await relay
        await wait_until(lambda: bool(child.close_codes))
    assert client.closed_with == [(ACCESS_REVOKED_CLOSE_CODE, "access revoked")]
    assert child.close_codes == [ACCESS_REVOKED_CLOSE_CODE]
    assert len(connections) == 0


async def test_a_stream_is_registered_with_its_release_so_a_swap_can_close_it(
    socket_path: Path,
) -> None:
    client = FakeClientSocket()
    connections = OpenConnections()
    async with serving(socket_path, echo) as child:
        relay = asyncio.create_task(run_relay(client, socket_path, connections, release_no=3))
        await wait_until(lambda: bool(client.accepted_with))
        await connections.close_connections("sales", 2)
        assert len(connections) == 1, "another release's walk leaves the stream open"
        await connections.close_connections("sales", 3)
        await relay
        await wait_until(lambda: bool(child.close_codes))
    assert client.closed_with == [(RELEASE_REPLACED_CLOSE_CODE, RELEASE_REPLACED_CLOSE_REASON)]
    assert len(connections) == 0


async def test_the_relay_is_cancellable_and_unregisters(socket_path: Path) -> None:
    client = FakeClientSocket()
    connections = OpenConnections()
    async with serving(socket_path, echo):
        relay = asyncio.create_task(run_relay(client, socket_path, connections))
        await wait_until(lambda: bool(client.accepted_with))
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay
    assert len(connections) == 0
