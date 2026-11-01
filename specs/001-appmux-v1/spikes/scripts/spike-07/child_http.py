"""Spike 7, runs inside the sandbox: a trivial child. Not part of appmux.

A keep-alive HTTP/1.1 server on 127.0.0.1 that answers every request with the same 200-byte JSON body. It is written with raw
`asyncio` streams so the measurement shows the edge and the relay, not a web framework.

usage: child_http.py <port>            (listens on 127.0.0.1)
       child_http.py unix:<path>       (listens on a unix socket: the baseline that has no relay and no sandbox)
"""
from __future__ import annotations

import asyncio
import sys

BODY = b'{"app": "trivial-child", "padding": "' + b"x" * 150 + b'"}'
RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(BODY)).encode() + b"\r\n\r\n" + BODY
HEAD_END = b"\r\n\r\n"


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            head = await reader.readuntil(HEAD_END)
            for line in head.split(b"\r\n"):
                if line[:15].lower() == b"content-length:":
                    await reader.readexactly(int(line[15:]))
            writer.write(RESPONSE)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass  # the client closed the keep-alive connection
    finally:
        writer.close()


async def main() -> None:
    if sys.argv[1].startswith("unix:"):
        server = await asyncio.start_unix_server(handle, sys.argv[1][5:], backlog=1024)
    else:
        server = await asyncio.start_server(handle, "127.0.0.1", int(sys.argv[1]), backlog=1024)
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
