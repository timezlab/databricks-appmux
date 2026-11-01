"""The upstream HTTP client against a real unix-socket server (design 3.1, FR-020, T084).

The "child" is a small asyncio server that listens on a socket in ``tmp_path``, records the bytes
it receives (so a test can assert exactly what reached the child) and answers with scripted
bytes. No test opens a network socket. Socket paths are short because ``AF_UNIX`` limits them to
about 100 characters.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from appmux.upstream.client import (
    UpstreamClient,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
    UpstreamTimeout,
    UpstreamUnreachable,
)

pytestmark = pytest.mark.anyio


@dataclass
class Received:
    method: str
    target: str
    headers: list[tuple[str, str]]
    body: bytes
    head: bytes

    def header(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key.lower() == name.lower()]


Responder = Callable[[Received, asyncio.StreamWriter], Awaitable[None]]


async def read_request(reader: asyncio.StreamReader) -> Received:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, _ = lines[0].split(" ", 2)
    headers = [tuple(line.split(":", 1)) for line in lines[1:] if line]
    headers = [(name, value.strip()) for name, value in headers]
    lowered = {name.lower(): value for name, value in headers}
    body = b""
    if "content-length" in lowered:
        body = await reader.readexactly(int(lowered["content-length"]))
    elif lowered.get("transfer-encoding") == "chunked":
        while True:
            size = int((await reader.readline()).strip() or b"0", 16)
            if size == 0:
                await reader.readline()
                break
            body += await reader.readexactly(size)
            await reader.readexactly(2)
    return Received(method, target, headers, body, head)


def response(status: str = "200 OK", headers: tuple[str, ...] = (), body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status}", *headers]
    if not any(line.lower().startswith("content-length") for line in headers):
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


async def reply(answer: bytes, writer: asyncio.StreamWriter) -> None:
    writer.write(answer)
    await writer.drain()


@dataclass
class Child:
    """A server on a unix socket that records requests and answers by a responder."""

    path: Path
    responder: Responder
    received: list[Received] = field(default_factory=list)
    _server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request = await read_request(reader)
                self.received.append(request)
                await self.responder(request, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # the client went away: nothing more to serve on this connection
        finally:
            writer.close()


@pytest.fixture
def socket_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("u") / "c.sock"


@contextlib.asynccontextmanager
async def running(socket_path: Path, responder: Responder) -> AsyncIterator[Child]:
    child = Child(socket_path, responder)
    await child.start()
    try:
        yield child
    finally:
        await child.stop()


async def ok(request: Received, writer: asyncio.StreamWriter) -> None:
    await reply(response(body=b"hello"), writer)


@pytest.fixture
async def client() -> AsyncIterator[UpstreamClient]:
    upstream = UpstreamClient()
    try:
        yield upstream
    finally:
        await upstream.aclose()


async def read_all(upstream_response: UpstreamResponse) -> bytes:
    return b"".join([chunk async for chunk in upstream_response.body()])


def request_for(
    target: str = "/",
    *,
    method: str = "GET",
    headers: tuple[tuple[str, str], ...] = (),
    body: AsyncIterator[bytes] | None = None,
) -> UpstreamRequest:
    return UpstreamRequest(method=method, target=target, headers=list(headers), body=body)


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


TIMEOUT_S = 5.0


# --- what reaches the child ---------------------------------------------------------------------


async def test_a_get_reaches_the_child_and_its_answer_comes_back(
    socket_path: Path, client: UpstreamClient
) -> None:
    async with running(socket_path, ok) as child:
        answer = await client.send(
            socket_path, request_for("/api/orders?limit=5"), timeout_s=TIMEOUT_S
        )
        assert answer.status == 200
        assert await read_all(answer) == b"hello"
        await answer.aclose()
    assert [(r.method, r.target) for r in child.received] == [("GET", "/api/orders?limit=5")]


@pytest.mark.parametrize(
    "target",
    [
        "/",
        "/files/a%20b/caf%C3%A9.txt",
        "/a%2Eb/%7Euser/~u",
        "/p!$&'()*+,;=:@x",
        "/search?q=a%20b&q=c&empty=&flag",
        "/search?q=%2F%3F%23",
        '/x?a=1|2&b={c}&d=[e]&f=^g&h="i"&j=<k>&l=`m`',
        "/x?",
    ],
)
async def test_the_request_target_reaches_the_child_exactly_as_the_edge_judged_it(
    socket_path: Path, client: UpstreamClient, target: str
) -> None:
    async with running(socket_path, ok) as child:
        answer = await client.send(socket_path, request_for(target), timeout_s=TIMEOUT_S)
        await answer.aclose()
    assert child.received[0].head.split(b" ")[1] == target.encode("latin-1")


async def test_raw_non_ascii_bytes_in_a_query_are_percent_encoded_not_rejected(
    socket_path: Path, client: UpstreamClient
) -> None:
    # A client may send UTF-8 in a query unescaped (ASGI hands it over as latin-1 text). HTTP/1.1
    # cannot carry such a byte in a request target and h11 refuses to write one, so it is written as
    # the percent escape every browser would have sent: the same bytes after the child decodes.
    async with running(socket_path, ok) as child:
        answer = await client.send(
            socket_path, request_for("/x?name=caf\xc3\xa9&k=\xff"), timeout_s=TIMEOUT_S
        )
        await answer.aclose()
    assert child.received[0].head.split(b" ")[1] == b"/x?name=caf%C3%A9&k=%FF"


async def test_the_forwarded_headers_arrive_unchanged_and_none_is_added(
    socket_path: Path, client: UpstreamClient
) -> None:
    sent = (
        ("Host", "apps.example.com"),
        ("X-Forwarded-Prefix", "/sales"),
        ("Cookie", "a=1; b=2"),
        ("Accept", "text/html"),
        ("X-Dup", "one"),
        ("X-Dup", "two"),
    )
    async with running(socket_path, ok) as child:
        answer = await client.send(socket_path, request_for("/", headers=sent), timeout_s=TIMEOUT_S)
        await answer.aclose()
    received = [(name.lower(), value) for name, value in child.received[0].headers]
    assert received == [(name.lower(), value) for name, value in sent]


async def test_a_request_without_a_host_header_gets_none_invented_from_the_socket_path(
    socket_path: Path, client: UpstreamClient
) -> None:
    async with running(socket_path, ok) as child:
        answer = await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
        await answer.aclose()
    assert str(socket_path) not in child.received[0].head.decode("latin-1")


async def test_a_body_with_a_content_length_is_streamed_with_that_length(
    socket_path: Path, client: UpstreamClient
) -> None:
    async with running(socket_path, ok) as child:
        answer = await client.send(
            socket_path,
            request_for(
                "/upload",
                method="POST",
                headers=(("Content-Length", "11"), ("Content-Type", "text/plain")),
                body=chunks(b"hello ", b"world"),
            ),
            timeout_s=TIMEOUT_S,
        )
        await answer.aclose()
    (received,) = child.received
    assert received.body == b"hello world"
    assert received.header("content-length") == ["11"]
    assert received.header("transfer-encoding") == []


async def test_a_body_without_a_length_is_sent_chunked(
    socket_path: Path, client: UpstreamClient
) -> None:
    async with running(socket_path, ok) as child:
        answer = await client.send(
            socket_path,
            request_for("/upload", method="PUT", body=chunks(b"a" * 100_000, b"b" * 50_000)),
            timeout_s=TIMEOUT_S,
        )
        await answer.aclose()
    (received,) = child.received
    assert received.body == b"a" * 100_000 + b"b" * 50_000
    assert received.header("transfer-encoding") == ["chunked"]


async def test_the_request_body_is_pulled_as_the_child_reads_it(
    socket_path: Path, client: UpstreamClient
) -> None:
    pulled: list[int] = []

    async def counted() -> AsyncIterator[bytes]:
        for index in range(3):
            pulled.append(index)
            yield b"x" * 10

    async with running(socket_path, ok):
        answer = await client.send(
            socket_path,
            request_for("/u", method="POST", headers=(("Content-Length", "30"),), body=counted()),
            timeout_s=TIMEOUT_S,
        )
        await answer.aclose()
    assert pulled == [0, 1, 2]


# --- what comes back ----------------------------------------------------------------------------


async def test_the_status_headers_and_repeated_set_cookie_lines_come_back_in_order(
    socket_path: Path, client: UpstreamClient
) -> None:
    async def cookies(request: Received, writer: asyncio.StreamWriter) -> None:
        await reply(
            response(
                "201 Created",
                ("Set-Cookie: a=1; Path=/", "Content-Type: text/plain", "Set-Cookie: b=2; Path=/x"),
                b"made",
            ),
            writer,
        )

    async with running(socket_path, cookies):
        answer = await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
        assert answer.status == 201
        pairs = [(name.lower(), value) for name, value in answer.headers]
        assert [value for name, value in pairs if name == "set-cookie"] == [
            "a=1; Path=/",
            "b=2; Path=/x",
        ]
        assert ("content-type", "text/plain") in pairs
        await answer.aclose()


async def test_a_redirect_is_returned_as_it_is_never_followed(
    socket_path: Path, client: UpstreamClient
) -> None:
    async def redirect(request: Received, writer: asyncio.StreamWriter) -> None:
        await reply(response("302 Found", ("Location: /login",)), writer)

    async with running(socket_path, redirect) as child:
        answer = await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
        assert answer.status == 302
        assert ("location", "/login") in [(n.lower(), v) for n, v in answer.headers]
        await answer.aclose()
    assert len(child.received) == 1


async def test_a_compressed_body_comes_back_as_the_bytes_the_child_sent(
    socket_path: Path, client: UpstreamClient
) -> None:
    packed = gzip.compress(b"payload " * 50)

    async def compressed(request: Received, writer: asyncio.StreamWriter) -> None:
        await reply(response(headers=("Content-Encoding: gzip",), body=packed), writer)

    async with running(socket_path, compressed):
        answer = await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
        assert await read_all(answer) == packed
        assert ("content-encoding", "gzip") in [(n.lower(), v) for n, v in answer.headers]
        await answer.aclose()


async def test_a_client_that_did_not_ask_for_compression_is_not_given_a_compressed_request(
    socket_path: Path, client: UpstreamClient
) -> None:
    async with running(socket_path, ok) as child:
        answer = await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
        await answer.aclose()
    assert child.received[0].header("accept-encoding") == []


async def test_a_streamed_response_is_passed_on_as_it_is_produced(
    socket_path: Path, client: UpstreamClient
) -> None:
    first_seen = asyncio.Event()

    async def streaming(request: Received, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        writer.write(b"5\r\nfirst\r\n")
        await writer.drain()
        # The second chunk is released only after the test holds the first one: a client that
        # buffers the body would wait here until the timeout.
        await asyncio.wait_for(first_seen.wait(), TIMEOUT_S)
        writer.write(b"6\r\nsecond\r\n0\r\n\r\n")
        await writer.drain()

    async with running(socket_path, streaming):
        answer = await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
        received: list[bytes] = []
        async for chunk in answer.body():
            received.append(chunk)
            if b"first" in b"".join(received):
                first_seen.set()
        assert b"".join(received) == b"firstsecond"
        await answer.aclose()


# --- failures -----------------------------------------------------------------------------------


async def test_a_socket_nobody_listens_on_is_unreachable(
    socket_path: Path, client: UpstreamClient
) -> None:
    with pytest.raises(UpstreamUnreachable):
        await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)


async def test_a_child_that_closes_the_connection_without_answering_is_unreachable(
    socket_path: Path, client: UpstreamClient
) -> None:
    async def hang_up(request: Received, writer: asyncio.StreamWriter) -> None:
        writer.close()

    async with running(socket_path, hang_up):
        with pytest.raises(UpstreamUnreachable) as caught:
            await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
    assert caught.value.__cause__ is not None


async def test_a_child_that_sends_something_that_is_not_http_is_unreachable(
    socket_path: Path, client: UpstreamClient
) -> None:
    async def garbage(request: Received, writer: asyncio.StreamWriter) -> None:
        await reply(b"\x00\x01 not http at all\r\n\r\n", writer)

    async with running(socket_path, garbage):
        with pytest.raises(UpstreamUnreachable):
            await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)


async def test_no_answer_within_the_timeout_is_a_timeout(
    socket_path: Path, client: UpstreamClient
) -> None:
    async def silent(request: Received, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(5)

    async with running(socket_path, silent):
        with pytest.raises(UpstreamTimeout):
            await client.send(socket_path, request_for("/"), timeout_s=0.2)


async def test_a_response_that_stalls_after_its_headers_times_out_while_streaming(
    socket_path: Path, client: UpstreamClient
) -> None:
    async def stalls(request: Received, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nfirst\r\n")
        await writer.drain()
        await asyncio.sleep(5)

    async with running(socket_path, stalls):
        answer = await client.send(socket_path, request_for("/"), timeout_s=0.3)
        with pytest.raises(UpstreamTimeout):
            await read_all(answer)
        await answer.aclose()


async def test_a_child_that_dies_mid_body_breaks_the_stream_with_unreachable(
    socket_path: Path, client: UpstreamClient
) -> None:
    async def dies(request: Received, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial")
        await writer.drain()
        writer.close()

    async with running(socket_path, dies):
        answer = await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
        with pytest.raises(UpstreamUnreachable):
            await read_all(answer)
        await answer.aclose()


async def test_a_header_the_http_layer_cannot_write_is_an_upstream_error_not_a_dead_child(
    socket_path: Path, client: UpstreamClient
) -> None:
    # A value with CR LF would split the request line (header injection). The edge refuses such a
    # header with 400 before this point; if one ever got here it must not read as a 502 or 504.
    async with running(socket_path, ok) as child:
        with pytest.raises(UpstreamError) as caught:
            await client.send(
                socket_path,
                request_for("/", headers=(("X-Bad", "a\r\nInjected: yes"),)),
                timeout_s=TIMEOUT_S,
            )
    assert not isinstance(caught.value, UpstreamUnreachable | UpstreamTimeout)
    assert child.received == []


# --- sockets over time --------------------------------------------------------------------------


async def test_one_client_serves_many_sockets_without_mixing_them_up(
    tmp_path_factory: pytest.TempPathFactory, client: UpstreamClient
) -> None:
    folder = tmp_path_factory.mktemp("m")

    def named(label: bytes) -> Responder:
        async def respond(request: Received, writer: asyncio.StreamWriter) -> None:
            await reply(response(body=label), writer)

        return respond

    async with (
        running(folder / "a.sock", named(b"app-a")),
        running(folder / "b.sock", named(b"app-b")),
    ):
        for _ in range(2):
            first = await client.send(folder / "a.sock", request_for("/"), timeout_s=TIMEOUT_S)
            second = await client.send(folder / "b.sock", request_for("/"), timeout_s=TIMEOUT_S)
            assert (await read_all(first), await read_all(second)) == (b"app-a", b"app-b")
            await first.aclose()
            await second.aclose()


async def test_a_new_child_on_a_reused_socket_path_is_reached_after_forget(
    socket_path: Path, client: UpstreamClient
) -> None:
    async with running(socket_path, ok):
        answer = await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
        await read_all(answer)
        await answer.aclose()
    await asyncio.to_thread(socket_path.unlink, missing_ok=True)

    async def second_life(request: Received, writer: asyncio.StreamWriter) -> None:
        await reply(response(body=b"again"), writer)

    async with running(socket_path, second_life):
        await client.forget(socket_path)
        answer = await client.send(socket_path, request_for("/"), timeout_s=TIMEOUT_S)
        assert await read_all(answer) == b"again"
        await answer.aclose()
