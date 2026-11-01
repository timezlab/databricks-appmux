"""The HTTP client from the edge to a child's unix socket (design 3.1, edge contract, FR-020).

The edge forwards a request and streams the answer back, so this client is deliberately thin: it
sends exactly what it is given and returns exactly what the child answers, and it never decides
anything about the request.

* The request target is written to the request line as given. ``routing`` judged that target
  and ``authz`` authorized it, so anything that re-encodes it (a URL parser normalising ``|`` or
  turning latin-1 bytes into UTF-8 escapes) would hand the child a path nobody checked. That is why
  this module talks to ``httpcore`` (the layer ``httpx`` is built on, installed with it) and builds
  the request line itself, instead of passing a URL through ``httpx``. The one change is a byte
  outside printable ASCII (a client sent UTF-8 in a query unescaped): HTTP/1.1 cannot carry it in a
  request target and ``h11`` refuses to write it, so it goes out as the percent escape a browser
  would have sent, which the child decodes to the same bytes.
* No header is added. ``User-Agent``, ``Accept``, ``Accept-Encoding`` and ``Connection`` are the
  caller's to send; a client default would change what a child sees. The one exception is a missing
  ``Host``: HTTP/1.1 requires one and ``h11`` refuses to write a request without it, so a request
  that came without one gets ``localhost``. Edge requests always carry the browser's ``Host``.
* The response is not decoded and a redirect is not followed: the body is the raw bytes the child
  sent, with its ``Content-Encoding``, and a ``Location`` goes to the browser to act on.
* A body with a ``Content-Length`` is streamed with that length; a body without one is sent
  ``Transfer-Encoding: chunked``. Neither is read into memory.

One connection pool per socket path, because a pool keys connections by origin and every child is
``localhost``; ``forget`` drops a pool when the path is reused by a new process (a restart of the
child must not be answered from a connection to the old one).

Failure mapping, so the edge can answer ``502`` or ``504`` without knowing the HTTP library:
a socket nobody listens on, a connection closed before an answer, a reply that is not HTTP and a
connection that dies mid-body are ``UpstreamUnreachable``; no answer within ``timeout_s`` (also a
stall between two chunks of a body) is ``UpstreamTimeout``. ``timeout_s`` bounds each wait, not the
whole response: a long stream that keeps sending is not cut.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpcore

_NON_ASCII_BYTE: Final = re.compile(rb"[\x80-\xff]")
_TARGET_ENCODING: Final = "latin-1"  # ASGI hands the raw path and query over as latin-1 text
_FALLBACK_HOST: Final = "localhost"
_ORIGIN_SCHEME: Final = b"http"


class UpstreamError(Exception):
    """The request could not be exchanged with the child."""


class UpstreamUnreachable(UpstreamError):
    """The child is not there, hung up, or did not speak HTTP (the edge answers 502)."""


class UpstreamTimeout(UpstreamError):
    """The child did not answer in time (the edge answers 504)."""


@dataclass(frozen=True, slots=True)
class UpstreamRequest:
    method: str
    target: str  # path and query exactly as the edge judged them, latin-1 text
    headers: Sequence[tuple[str, str]]
    body: AsyncIterator[bytes] | None = None


class UpstreamResponse:
    """The status line and headers of the child's answer, with its body still on the wire."""

    def __init__(self, response: httpcore.Response) -> None:
        self._response = response
        self.status: int = response.status
        self.headers: list[tuple[str, str]] = [
            (name.decode(_TARGET_ENCODING), value.decode(_TARGET_ENCODING))
            for name, value in response.headers
        ]

    async def body(self) -> AsyncIterator[bytes]:
        """The raw body in the pieces the child produced; the connection is closed by ``aclose``."""
        stream = self._response.stream
        if not isinstance(stream, AsyncIterable):
            raise UpstreamError("the upstream response has no streaming body")
        try:
            async for chunk in stream:
                yield chunk
        except httpcore.TimeoutException as exc:
            raise UpstreamTimeout("the child stopped sending its response") from exc
        except (httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise UpstreamUnreachable("the child's connection ended mid-response") from exc

    async def aclose(self) -> None:
        await self._response.aclose()


class UpstreamClient:
    def __init__(self) -> None:
        self._pools: dict[Path, httpcore.AsyncConnectionPool] = {}

    async def send(
        self, socket_path: Path, request: UpstreamRequest, *, timeout_s: float
    ) -> UpstreamResponse:
        """Send ``request`` to the child on ``socket_path``; return once the headers are in."""
        outgoing = httpcore.Request(
            request.method,
            httpcore.URL(
                scheme=_ORIGIN_SCHEME,
                host=_FALLBACK_HOST,
                port=None,
                target=_wire_target(request.target),
            ),
            headers=_wire_headers(request),
            content=request.body,
            extensions={
                "timeout": {
                    "connect": timeout_s,
                    "read": timeout_s,
                    "write": timeout_s,
                    "pool": timeout_s,
                }
            },
        )
        try:
            response = await self._pool(socket_path).handle_async_request(outgoing)
        except httpcore.TimeoutException as exc:
            raise UpstreamTimeout("the child did not answer in time") from exc
        except httpcore.LocalProtocolError as exc:
            # Our side could not write the request (an illegal header, a body that does not match
            # its Content-Length): not the child's fault, so neither a 502 nor a 504.
            raise UpstreamError("the request could not be written to the child") from exc
        except (httpcore.NetworkError, httpcore.ProtocolError) as exc:
            raise UpstreamUnreachable("the child is not reachable or did not answer") from exc
        return UpstreamResponse(response)

    async def forget(self, socket_path: Path) -> None:
        """Drop the connections to ``socket_path``; call when a new process may own the path."""
        pool = self._pools.pop(socket_path, None)
        if pool is not None:
            await pool.aclose()

    async def aclose(self) -> None:
        pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            await pool.aclose()

    def _pool(self, socket_path: Path) -> httpcore.AsyncConnectionPool:
        pool = self._pools.get(socket_path)
        if pool is None:
            # No connection cap: the per-app concurrency limit (caps) decides how many requests
            # are in flight, so a second limit here would only turn into a pool timeout.
            pool = httpcore.AsyncConnectionPool(uds=str(socket_path), max_connections=None)
            self._pools[socket_path] = pool
        return pool


def _wire_headers(request: UpstreamRequest) -> list[tuple[bytes, bytes]]:
    names = {name.lower() for name, _ in request.headers}
    headers = [
        (name.encode(_TARGET_ENCODING), value.encode(_TARGET_ENCODING))
        for name, value in request.headers
    ]
    if "host" not in names:
        headers.append((b"Host", _FALLBACK_HOST.encode("ascii")))
    if request.body is not None and "content-length" not in names:
        headers.append((b"Transfer-Encoding", b"chunked"))
    return headers


def _wire_target(target: str) -> bytes:
    raw = target.encode(_TARGET_ENCODING)
    return _NON_ASCII_BYTE.sub(lambda match: b"%%%02X" % match.group()[0], raw)
