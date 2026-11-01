"""The inbound relay of design 6.1: from the edge's unix socket into the child's loopback.

The child's server listens on ``127.0.0.1`` inside its own network namespace, which nothing
outside can reach. A relay *inside* the namespace accepts connections on a unix socket and
forwards each one to ``127.0.0.1:<port>``, both ways; the edge connects to that socket.

The socket is bound by the launcher, outside the sandbox, and inherited by the relay
(:func:`bind_relay_socket`), so the child's run directory need not be shared with appmux and can
be a size-capped ``tmpfs`` (``run_dir``, TD-004). The child never holds the descriptor: the
launcher closes every descriptor above 2 in the process that becomes the child.

The relay starts listening only once the child's port accepts a connection. Until then a connect
to the socket is refused, which is what the health clock of a slow starter has to read ("connection
refused", design 5.5): a relay that accepted first and hung up when the port refused would show as
"connection closed before an answer", which says nothing about the port.

This module runs inside the sandbox, as a process the launcher forks after it has built the
root, so it imports only the standard library: nothing of appmux is on disk there.

Ported from ``specs/001-appmux-v1/spikes/scripts/spike-07/relay.py``. Two differences from the
prototype: the socket is passed in rather than bound here, and an end-of-stream from one side is
forwarded as a half-close (``write_eof``) instead of closing the other side, so a client that
shuts down its writing half still receives the whole answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from pathlib import Path
from typing import Final

COPY_BYTES: Final = 1 << 16
ACCEPT_BACKLOG: Final = 1024
LOOPBACK: Final = "127.0.0.1"
RELAY_SOCKET_MODE: Final = 0o600
# How often the relay tries the child's port while it is not bound yet. A bound port is noticed
# within this time, which is also the most a health poll can still read "refused" after the bind.
CHILD_PORT_POLL_INTERVAL_S: Final = 0.05


def bind_relay_socket(path: Path, *, is_listening: bool = True) -> socket.socket:
    """Bind a unix socket at ``path``, readable by this user only, and listen on it by default.

    A socket left by a crashed launcher is replaced; any other file at the path is replaced too,
    because the path is inside a directory the launcher created for this one child.

    A socket that is bound but does not listen refuses every connect (``ECONNREFUSED``). The relay
    socket is bound like that, and :func:`serve_relay` makes it listen when the child's port is up.
    """
    path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, RELAY_SOCKET_MODE)
        if is_listening:
            listener.listen(ACCEPT_BACKLOG)
    except OSError:
        listener.close()
        raise
    return listener


async def _copy(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while data := await source.read(COPY_BYTES):
            sink.write(data)
            await sink.drain()
        if sink.can_write_eof():
            sink.write_eof()
    except (ConnectionError, OSError):
        # The other side vanished: closing both ends in the caller is the response.
        sink.close()


async def forward_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    child_port: int,
) -> None:
    """Connect to the child and copy both ways until both directions have ended."""
    try:
        child_reader, child_writer = await asyncio.open_connection(LOOPBACK, child_port)
    except OSError:
        # The child stopped listening after it had started: the client sees a closed connection,
        # which the edge turns into its own error. (Before the first start the socket refuses.)
        client_writer.close()
        return
    try:
        await asyncio.gather(_copy(client_reader, child_writer), _copy(child_reader, client_writer))
    finally:
        child_writer.close()
        client_writer.close()
        with contextlib.suppress(OSError):
            await asyncio.gather(
                child_writer.wait_closed(), client_writer.wait_closed(), return_exceptions=True
            )


async def _wait_until_child_port_accepts(child_port: int) -> None:
    """Return when ``127.0.0.1:child_port`` accepts a connection; try again until it does."""
    while True:
        try:
            _, writer = await asyncio.open_connection(LOOPBACK, child_port)
        except OSError:
            await asyncio.sleep(CHILD_PORT_POLL_INTERVAL_S)
            continue
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return


async def serve_relay(listener: socket.socket, child_port: int) -> None:
    """Forward the inherited ``listener`` to ``127.0.0.1:child_port`` forever.

    ``listener`` is bound; it starts listening (the server below calls ``listen``) when the child's
    port first accepts a connection, and a socket that was already listening is simply served.
    """
    await _wait_until_child_port_accepts(child_port)
    connections: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = asyncio.current_task()
        if connection is None:  # cannot happen: asyncio runs a stream callback in a task
            raise RuntimeError("the relay's connection handler runs outside a task")
        connections.add(connection)
        try:
            await forward_connection(reader, writer, child_port=child_port)
        finally:
            connections.discard(connection)

    server = await asyncio.start_unix_server(handle, sock=listener, backlog=ACCEPT_BACKLOG)
    try:
        async with server:
            await server.serve_forever()
    finally:
        # Stopping the relay ends the connections it holds, so none outlives it half-closed.
        for connection in connections:
            connection.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
