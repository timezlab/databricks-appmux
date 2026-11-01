"""Spike 7, runs on the probe app (outside the sandbox): a stand-in for the edge, to measure what a Python HTTP front can carry.
Not part of appmux, and not the edge: the real one is chosen in the plan and also does identity, routing, ACL and metrics, all
of which cost more than this.

HTTP/1.1 keep-alive on both sides, one upstream connection per client connection. Per request it does what the edge must always
do: read the head, drop the client's own `X-Forwarded-*` headers, add the identity headers, forward head and body, relay the
response head and body. Standard library `asyncio` only.

usage: edge_standin.py <listen-port> <upstream-unix-socket>
"""
from __future__ import annotations

import asyncio
import sys

IDENTITY_HEADERS = b"X-Forwarded-Email: visitor@example.invalid\r\nX-Forwarded-User: visitor\r\nX-Forwarded-Prefix: /sales\r\n"
HEAD_END = b"\r\n\r\n"
STREAM_LIMIT_BYTES = 1 << 20


def content_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        if line[:15].lower() == b"content-length:":
            return int(line[15:])
    return 0


def rewrite_request_head(head: bytes) -> bytes:
    lines = head[: -len(HEAD_END)].split(b"\r\n")
    kept = [line for line in lines if not line.lower().startswith(b"x-forwarded-")]
    return b"\r\n".join(kept) + b"\r\n" + IDENTITY_HEADERS + b"\r\n"


async def relay_message(source: asyncio.StreamReader, sink: asyncio.StreamWriter, rewrite: bool) -> None:
    head = await source.readuntil(HEAD_END)
    sink.write(rewrite_request_head(head) if rewrite else head)
    length = content_length(head)
    if length:
        sink.write(await source.readexactly(length))
    await sink.drain()


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    upstream_writer = None
    try:
        upstream_reader, upstream_writer = await asyncio.open_unix_connection(UPSTREAM, limit=STREAM_LIMIT_BYTES)
        while True:
            await relay_message(client_reader, upstream_writer, rewrite=True)
            await relay_message(upstream_reader, client_writer, rewrite=False)
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
        pass  # either side closed: the normal end of a keep-alive connection
    finally:
        client_writer.close()
        if upstream_writer:
            upstream_writer.close()


async def main() -> None:
    server = await asyncio.start_server(handle, "127.0.0.1", int(sys.argv[1]), limit=STREAM_LIMIT_BYTES, backlog=1024)
    await server.serve_forever()


UPSTREAM = sys.argv[2] if len(sys.argv) > 2 else ""

if __name__ == "__main__":
    asyncio.run(main())
