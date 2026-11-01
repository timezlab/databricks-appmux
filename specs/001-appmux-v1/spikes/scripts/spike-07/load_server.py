"""Spike 7, runs on the probe app (outside the sandbox): the far end of the child's outbound traffic. Not part of appmux.

An echo server (ping-pong messages) and a bulk server (sends or swallows a stated number of bytes) on 0.0.0.0, in their own
process so that the driver's Python does not compete with them for the interpreter lock.

usage: load_server.py <echo-port> <bulk-port>
"""
from __future__ import annotations

import asyncio
import struct
import sys

BULK_HEADER = struct.Struct(">cQ")
BULK_CHUNK = b"\0" * (1 << 20)
READ_BYTES = 1 << 20


async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(READ_BYTES):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass  # the client went away: nothing left to echo to
    finally:
        writer.close()


async def bulk(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        direction, size = BULK_HEADER.unpack(await reader.readexactly(BULK_HEADER.size))
        if direction == b"D":
            sent = 0
            while sent < size:
                chunk = BULK_CHUNK[: min(len(BULK_CHUNK), size - sent)]
                writer.write(chunk)
                await writer.drain()
                sent += len(chunk)
        else:
            remaining = size
            while remaining:
                remaining -= len(await reader.readexactly(min(READ_BYTES, remaining)))
            writer.write(b"k")
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass  # the client stopped early: the driver sees that on its side
    finally:
        writer.close()


async def main() -> None:
    servers = [await asyncio.start_server(echo, "0.0.0.0", int(sys.argv[1])), await asyncio.start_server(bulk, "0.0.0.0", int(sys.argv[2]))]
    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
