"""Spike 7, runs inside the sandbox: the inbound relay of design section 6.1. Not part of appmux.

Listens on a unix socket in the run directory and forwards each connection to the child's server on 127.0.0.1, both ways.
Standard library `asyncio` only.

usage: relay.py <unix-socket-path> <child-port>
"""
from __future__ import annotations

import asyncio
import os
import sys

COPY_BYTES = 1 << 16


async def copy(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while data := await source.read(COPY_BYTES):
            sink.write(data)
            await sink.drain()
    except ConnectionError:
        pass  # the other side vanished: closing both ends below is the response
    finally:
        sink.close()


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    try:
        child_reader, child_writer = await asyncio.open_connection("127.0.0.1", CHILD_PORT)
    except OSError:
        client_writer.close()
        return
    await asyncio.gather(copy(client_reader, child_writer), copy(child_reader, client_writer))


async def main() -> None:
    if os.path.exists(sys.argv[1]):
        os.unlink(sys.argv[1])
    server = await asyncio.start_unix_server(handle, sys.argv[1], backlog=1024)
    os.chmod(sys.argv[1], 0o600)
    await server.serve_forever()


CHILD_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 0

if __name__ == "__main__":
    asyncio.run(main())
