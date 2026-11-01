"""The inbound relay of design 6.1: a unix socket in front of the child's loopback port.

These tests run the relay for real, on this machine, between a unix socket and a TCP server on
127.0.0.1: the byte-level behaviour (both directions, half-close, a child that is not listening
yet, many connections) does not depend on namespaces. That the relay runs in the child's network
namespace and that the edge reaches it is for the container run.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import stat
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from appmux.sandbox.relay import bind_relay_socket, serve_relay

pytestmark = pytest.mark.anyio

TIMEOUT_S = 10
STOP_TIMEOUT_S = 2


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    writer.close()


@contextlib.asynccontextmanager
async def echo_server() -> AsyncIterator[int]:
    """A TCP server on 127.0.0.1 that echoes everything and half-closes after the client does."""

    server = await asyncio.start_server(_echo, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def accepts_connections(path: Path) -> bool:
    try:
        _, writer = await asyncio.open_unix_connection(str(path))
    except ConnectionRefusedError:
        return False
    writer.close()
    return True


async def wait_until_accepting(path: Path) -> None:
    async with asyncio.timeout(TIMEOUT_S):
        while True:
            if await accepts_connections(path):
                return
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def running_relay(
    tmp_path: Path, child_port: int, *, is_waiting_for_accept: bool = True
) -> AsyncIterator[Path]:
    """The relay as the launcher starts it: a bound socket that does not listen yet.

    The relay starts listening once the child's port accepts, so a test that talks to a child that
    is up waits for that first; one that checks the refusal does not.
    """
    path = tmp_path / "relay.sock"
    listener = bind_relay_socket(path, is_listening=False)
    task = asyncio.create_task(serve_relay(listener, child_port))
    try:
        if is_waiting_for_accept:
            await wait_until_accepting(path)
        yield path
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        listener.close()


async def test_bytes_flow_both_ways_through_the_relay(tmp_path: Path) -> None:
    async with echo_server() as child_port, running_relay(tmp_path, child_port) as path:
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"GET / HTTP/1.1\r\n\r\n")
        await writer.drain()

        assert (
            await asyncio.wait_for(reader.readexactly(18), TIMEOUT_S) == b"GET / HTTP/1.1\r\n\r\n"
        )
        writer.close()


async def test_a_half_closed_client_still_receives_the_rest_of_the_answer(tmp_path: Path) -> None:
    async with echo_server() as child_port, running_relay(tmp_path, child_port) as path:
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"request")
        await writer.drain()
        writer.write_eof()

        assert await asyncio.wait_for(reader.read(), TIMEOUT_S) == b"request"
        writer.close()


async def test_a_large_body_arrives_intact(tmp_path: Path) -> None:
    payload = os.urandom(3 * 1024 * 1024)
    async with echo_server() as child_port, running_relay(tmp_path, child_port) as path:
        reader, writer = await asyncio.open_unix_connection(str(path))

        async def send() -> None:
            writer.write(payload)
            await writer.drain()
            writer.write_eof()

        sender = asyncio.create_task(send())
        received = await asyncio.wait_for(reader.read(), TIMEOUT_S)
        await sender

        assert received == payload
        writer.close()


async def test_many_connections_do_not_share_bytes(tmp_path: Path) -> None:
    async with echo_server() as child_port, running_relay(tmp_path, child_port) as path:

        async def exchange(number: int) -> bytes:
            reader, writer = await asyncio.open_unix_connection(str(path))
            message = f"connection-{number}".encode() * 100
            writer.write(message)
            await writer.drain()
            writer.write_eof()
            answer = await reader.read()
            writer.close()
            assert answer == message
            return answer

        answers = await asyncio.wait_for(
            asyncio.gather(*(exchange(number) for number in range(40))), TIMEOUT_S
        )

        assert len(set(answers)) == 40


def unused_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_a_child_that_has_not_bound_its_port_has_its_connections_refused(
    tmp_path: Path,
) -> None:
    # Design 5.5: the health check of a slow starter reads "connection refused" until the port is
    # bound. A relay that accepted first and hung up would read as "closed before an answer".
    async with running_relay(tmp_path, unused_loopback_port(), is_waiting_for_accept=False) as path:
        await asyncio.sleep(0.2)  # long enough for the relay to have tried the port several times

        with pytest.raises(ConnectionRefusedError):
            await asyncio.open_unix_connection(str(path))


async def test_the_relay_starts_accepting_once_the_child_binds_its_port(tmp_path: Path) -> None:
    port = unused_loopback_port()
    async with running_relay(tmp_path, port, is_waiting_for_accept=False) as path:
        assert not await accepts_connections(path)

        child = await asyncio.start_server(_echo, "127.0.0.1", port)
        try:
            await wait_until_accepting(path)
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b"ping")
            await writer.drain()

            assert await asyncio.wait_for(reader.readexactly(4), TIMEOUT_S) == b"ping"
            writer.close()
        finally:
            child.close()
            await child.wait_closed()


def test_a_socket_bound_without_listening_refuses_connections(tmp_path: Path) -> None:
    path = tmp_path / "relay.sock"
    listener = bind_relay_socket(path, is_listening=False)
    try:
        with (
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client,
            pytest.raises(ConnectionRefusedError),
        ):
            client.connect(str(path))
    finally:
        listener.close()


async def test_stopping_the_relay_ends_the_connections_it_still_holds(tmp_path: Path) -> None:
    async with echo_server() as child_port:
        path = tmp_path / "relay.sock"
        listener = bind_relay_socket(path, is_listening=False)
        relay = asyncio.create_task(serve_relay(listener, child_port))
        try:
            await wait_until_accepting(path)
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b"x")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(1), TIMEOUT_S) == b"x"

            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await relay

            assert await asyncio.wait_for(reader.read(), STOP_TIMEOUT_S) == b""
            writer.close()
        finally:
            listener.close()


async def test_the_relay_keeps_serving_after_a_client_vanishes_mid_request(tmp_path: Path) -> None:
    async with echo_server() as child_port, running_relay(tmp_path, child_port) as path:
        _, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"partial")
        writer.transport.abort()

        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b"again")
        await writer.drain()

        assert await asyncio.wait_for(reader.readexactly(5), TIMEOUT_S) == b"again"
        writer.close()


def test_the_socket_is_private_to_the_owner_and_replaces_a_stale_one(tmp_path: Path) -> None:
    path = tmp_path / "relay.sock"
    path.write_text("left behind by a crashed launcher")

    listener = bind_relay_socket(path)
    try:
        assert stat.S_ISSOCK(path.stat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert listener.getsockname() == str(path)
    finally:
        listener.close()
