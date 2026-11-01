"""The request pipeline for ``/<slug>/...`` (design 3.4 and 7.1, contracts/edge-http.md).

One request goes through six steps in a fixed order, and a step that refuses ends the request with
the status the contract gives it:

1. identity: ``actor_from_headers`` (``401`` without one forwarded identity);
2. routing: ``PrefixSelector`` on the raw path (``400`` if it is ambiguous, ``404`` for no app);
3. authorization: ``APP_OPEN`` (``CAN_USE``). A path that names no app is asked the same question
   about no app, and an app the visitor may not open gets the very same ``404``, so neither the
   answer nor the work behind it tells a stranger which slugs exist;
4. route: an app with no route answers ``503`` with a page that names its state, to a visitor who
   may open it and to nobody else;
5. caps (FR-020): a body over ``max_request_body_mb`` is ``413`` (by its declared length at once,
   otherwise while it streams), more than ``max_concurrent_requests`` in flight is ``429`` with
   ``Retry-After: 1``, a child that stays silent for ``request_timeout_s`` is ``504``;
6. rewrite and forward: ``rewrite_request_headers`` on the way in, the child's answer streamed back
   through ``rewrite_response`` on the way out. ``502`` when the child is unreachable.

Nothing holds a whole body. The request body goes to the child chunk by chunk through ``CappedBody``
and the response comes back the same way. A WebSocket upgrade takes steps 1 to 4 and then hands over
to ``relay_websocket``; it does not use one of the app's request places, because a stream that lasts
for hours would starve the app of the places its requests need.

A browser that leaves is noticed (``_Inbound``): the request to the child is dropped, so a child
that streams to nobody sees the hang-up and the app's place is freed. The ASGI ``receive`` serves
the body first and the wait for a disconnect after it, and two readers would steal each other's
messages, so ``_Inbound`` is the only reader.

Logging. A line names an app (a registered slug) and what happened. It never carries a token, a
body, an e-mail address, the client's address, a request path or an exception message from the
child's side. The visitor's token is read out of its ``SecretStr`` in one place only
(``rewrite_request_headers``), and only when the token policy said so.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import quote

from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.websockets import WebSocket, WebSocketDisconnect

from appmux.admin.api import Authorizer
from appmux.admin.identity import AppStateReader
from appmux.authz.actions import Action
from appmux.edge.connections import OpenConnections
from appmux.edge.gate import Receive, Scope, Send
from appmux.edge.status_pages import (
    BAD_GATEWAY_STATUS,
    GATEWAY_TIMEOUT_STATUS,
    UNAVAILABLE_STATUS,
    WEBSOCKET_TRY_AGAIN_LATER,
    app_unavailable_page,
    app_unavailable_retry_after_s,
    bad_gateway_page,
    gateway_timeout_page,
    plain_text_headers,
    status_page_headers,
)
from appmux.identity.actor import Actor, HeaderLines, MissingIdentity, actor_from_headers
from appmux.reconciler.route_table import Route
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.protocol import Registry
from appmux.registry.snapshot import RegistrySnapshot
from appmux.rewrite.request import MalformedHeader, rewrite_request_headers
from appmux.routing.normalise import AmbiguousPath
from appmux.routing.selector import NoMatch, PrefixSelector, RoutingRequest, Selection
from appmux.settings import PlatformSettings
from appmux.upstream.caps import (
    AppCaps,
    CappedBody,
    InFlightLimiter,
    RequestBodyTooLarge,
)
from appmux.upstream.client import (
    UpstreamClient,
    UpstreamError,
    UpstreamRequest,
    UpstreamResponse,
    UpstreamTimeout,
    UpstreamUnreachable,
)
from appmux.upstream.websocket import ClientDisconnected, UpstreamRejected, relay_websocket

logger = logging.getLogger(__name__)

# nginx's "client closed request": the browser left before the exchange ended. It is a 4xx for the
# aggregator and never an error of the app.
CLIENT_CLOSED_REQUEST_STATUS: Final = 499
_RETRY_AFTER_BUSY_S: Final = 1
_REGISTRY_RETRY_AFTER_S: Final = 5
_ENCODING: Final = "latin-1"
_FIRST_CLIENT_ERROR: Final = 400
_FIRST_SERVER_ERROR: Final = 500
_LAST_ERROR: Final = 599
_TOO_MANY_REQUESTS: Final = 429
_WEBSOCKET_POLICY_VIOLATION: Final = 1008
_DENIAL_EXTENSION: Final = "websocket.http.response"

_CAP_SETTING_KEYS: Final = ("max_concurrent_requests", "max_request_body_mb", "request_timeout_s")

# What a client is told. Each text says what failed and what to do; none echoes a path, a header or
# a value, and none carries a message from an exception.
_NOT_FOUND_TEXT: Final = "not found"
_UNAUTHORIZED_TEXT: Final = (
    "no identity was forwarded with this request; open the app through its Databricks App URL "
    "so the platform can sign you in"
)
_AMBIGUOUS_PATH_TEXT: Final = (
    "the request path is ambiguous and was refused; send a plain path without encoded slashes, "
    "backslashes or control characters"
)
_BAD_HEADER_TEXT: Final = (
    "a request header cannot be forwarded safely and the request was refused; "
    "check the header names and values"
)
_CANNOT_FORWARD_TEXT: Final = (
    "the request could not be passed on to the app; check its headers and body"
)
_TOO_LARGE_TEXT: Final = (
    "the request body is larger than this app accepts; send less data, "
    "or ask a platform admin to raise max_request_body_mb"
)
_TOO_MANY_TEXT: Final = "this app is handling as many requests as it accepts; try again in a moment"
_REGISTRY_TEXT: Final = (
    "appmux cannot read its registry right now, so it cannot tell which apps exist; "
    "try again in a moment"
)
_CHILD_REFUSED_TEXT: Final = "the app refused the WebSocket connection"


class RouteLookup(Protocol):
    """The part of the route table the edge reads: it never opens or closes a route."""

    def lookup(self, slug: str) -> Route | None: ...


class ForwardTokenPolicy(Protocol):
    """Whether the child receives the visitor's own access token (``edge.open_mode`` implements it).

    Called only for a request that is about to be forwarded. An answer other than ``True``, and an
    exception, mean "do not forward".
    """

    async def __call__(self, actor: Actor, app_slug: str, headers: HeaderLines) -> bool: ...


class MetricsSink(Protocol):
    """What the pipeline feeds; ``metrics.aggregate.MetricsAggregator`` implements it."""

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
    ) -> None: ...

    def observe_in_flight(self, app_slug: str, current: int) -> None: ...

    def websocket_opened(self, app_slug: str) -> None: ...

    def websocket_closed(self, app_slug: str) -> None: ...


class ClientGone(Exception):
    """The browser's side of the connection ended before the exchange did."""


def caps_from_snapshot(snapshot: RegistrySnapshot) -> AppCaps:
    """The caps in force: the platform settings of ``snapshot``, else the documented defaults.

    A stored value the settings model refuses (the admin API cannot write one, but a row can be
    edited in the database) is replaced by the default and logged, never applied: a cap read as
    ``0`` or as ``"120"`` would turn every app off or on without limit.
    """
    defaults = PlatformSettings().model_dump()
    values: dict[str, int] = {}
    for key in _CAP_SETTING_KEYS:
        row = snapshot.setting(key)
        if row is None:
            values[key] = defaults[key]
            continue
        try:
            values[key] = PlatformSettings.model_validate({key: row.value}).model_dump()[key]
        except ValidationError:
            logger.error("the platform setting %r holds an invalid value; its default is used", key)
            values[key] = defaults[key]
    return AppCaps(**values)


# --- what the pipeline answers with --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Refusal:
    """An answer the edge makes itself, with what the metrics need to know about it."""

    status: int
    body: bytes
    headers: list[tuple[bytes, bytes]]
    app_slug: str | None = None
    upstream_error: bool = False
    limited: bool = False


def _plain(
    status: int,
    text: str,
    *,
    app_slug: str | None = None,
    retry_after_s: int | None = None,
    limited: bool = False,
) -> _Refusal:
    body = text.encode("ascii")
    headers = plain_text_headers(body, retry_after_s=retry_after_s)
    return _Refusal(status, body, headers, app_slug, limited=limited)


def _not_found() -> _Refusal:
    # One answer for "no such app" and "not yours": the same bytes, so neither can be told apart.
    return _plain(404, _NOT_FOUND_TEXT)


def _too_large(slug: str) -> _Refusal:
    return _plain(413, _TOO_LARGE_TEXT, app_slug=slug, limited=True)


def _too_many_requests(slug: str) -> _Refusal:
    return _plain(
        _TOO_MANY_REQUESTS,
        _TOO_MANY_TEXT,
        app_slug=slug,
        retry_after_s=_RETRY_AFTER_BUSY_S,
        limited=True,
    )


def _bad_gateway(slug: str) -> _Refusal:
    body = bad_gateway_page(slug)
    headers = status_page_headers(body, retry_after_s=None)
    return _Refusal(BAD_GATEWAY_STATUS, body, headers, slug, upstream_error=True)


def _gateway_timeout(slug: str) -> _Refusal:
    body = gateway_timeout_page(slug)
    headers = status_page_headers(body, retry_after_s=None)
    return _Refusal(GATEWAY_TIMEOUT_STATUS, body, headers, slug, upstream_error=True)


def _child_refused(status: int, slug: str) -> _Refusal:
    # The child's own refusal of a handshake reaches the browser with its status; a status that is
    # not an error (a redirect, say) is not a handshake answer the browser can use.
    if not _FIRST_CLIENT_ERROR <= status <= _LAST_ERROR:
        return _bad_gateway(slug)
    return _plain(status, _CHILD_REFUSED_TEXT, app_slug=slug)


def _failure_refusal(failure: Exception, slug: str) -> _Refusal:
    """The answer for a request that could not be exchanged with the child."""
    if isinstance(failure, RequestBodyTooLarge):
        return _too_large(slug)
    if isinstance(failure, UpstreamTimeout):
        logger.warning("the child of app %r did not answer in time", slug)
        return _gateway_timeout(slug)
    if isinstance(failure, UpstreamUnreachable):
        logger.warning("the child of app %r is unreachable or did not speak HTTP", slug)
        return _bad_gateway(slug)
    logger.warning("a request for app %r could not be written to its child", slug)
    return _plain(400, _CANNOT_FORWARD_TEXT, app_slug=slug)


async def _send_refusal(scope: Scope, send: Send, refusal: _Refusal) -> None:
    await send(
        {"type": "http.response.start", "status": refusal.status, "headers": refusal.headers}
    )
    body = b"" if scope["method"] == "HEAD" else refusal.body
    await send({"type": "http.response.body", "body": body})


async def _refuse_websocket(scope: Scope, send: Send, refusal: _Refusal) -> None:
    """Refuse a handshake with the refusal's status when the server can carry one.

    Without the ASGI ``websocket.http.response`` extension the only refusal there is, a close
    before ``accept``, answers ``403``; the code then only tells a log which kind it was.
    """
    if _DENIAL_EXTENSION in (scope.get("extensions") or {}):
        await send(
            {
                "type": "websocket.http.response.start",
                "status": refusal.status,
                "headers": refusal.headers,
            }
        )
        await send({"type": "websocket.http.response.body", "body": refusal.body})
        return
    busy = refusal.status >= _FIRST_SERVER_ERROR or refusal.status == _TOO_MANY_REQUESTS
    code = WEBSOCKET_TRY_AGAIN_LATER if busy else _WEBSOCKET_POLICY_VIOLATION
    await send({"type": "websocket.close", "code": code})


# --- reading the request -------------------------------------------------------------------------


def _raw_path(scope: Scope) -> str:
    """The path as the client wrote it (ASGI ``raw_path``, without the query), as text.

    Routing judges the raw form; the decoded ``path`` would hide an encoded slash or dot. A server
    that gives no ``raw_path`` gets its decoded path quoted again, which is the closest form there
    is (it cannot tell ``%2F`` from ``/``, and uvicorn, the only server this edge runs on, always
    sends the raw one).
    """
    raw = scope.get("raw_path")
    if isinstance(raw, bytes):
        return raw.decode(_ENCODING)
    return quote(str(scope["path"]), safe="/")


def _query_string(scope: Scope) -> str:
    return bytes(scope.get("query_string") or b"").decode(_ENCODING)


def _declared_length(headers: Headers) -> int | None:
    value = headers.get("content-length")
    if value is None:
        return None
    text = value.strip()
    return int(text) if text.isascii() and text.isdigit() else None


def _has_body(headers: Headers) -> bool:
    """Whether the request declares a body (HTTP frames one by a length or a transfer coding)."""
    if "transfer-encoding" in headers:
        return True
    value = headers.get("content-length")
    return value is not None and value.strip() != "0"


class _Inbound:
    """The only reader of the ASGI ``receive`` of one HTTP request.

    First the request body, as an async iterator of its non-empty chunks (the body of a request is
    read only as fast as the child takes it). Then, once the body is read, ``wait_for_disconnect``
    returns when the browser has gone. An empty chunk is skipped: in chunked framing it would end
    the body.
    """

    def __init__(self, receive: Receive) -> None:
        self._receive = receive
        self._body_read = asyncio.Event()
        self.gone = False

    def mark_no_body(self) -> None:
        """The request has no body to read, so the wait for a disconnect may begin."""
        self._body_read.set()

    def __aiter__(self) -> _Inbound:
        return self

    async def __anext__(self) -> bytes:
        while not self._body_read.is_set():
            message = await self._receive()
            if message["type"] == "http.disconnect":
                self.gone = True
                raise ClientGone
            if not message.get("more_body", False):
                self._body_read.set()
            chunk: bytes = message.get("body", b"")
            if chunk:
                return chunk
        raise StopAsyncIteration

    async def wait_for_disconnect(self) -> None:
        await self._body_read.wait()
        while not self.gone:
            message = await self._receive()
            # What arrives before the disconnect (an empty last chunk, or a body the child never
            # asked for) is dropped.
            self.gone = message["type"] == "http.disconnect"


# --- the browser's WebSocket as the relay sees it ------------------------------------------------


class _BrowserSocket:
    """A Starlette ``WebSocket`` as the ``ClientSocket`` of ``relay_websocket``."""

    def __init__(self, websocket: WebSocket, *, on_accept: Callable[[], None]) -> None:
        self._websocket = websocket
        self._on_accept = on_accept
        self.accepted = False

    async def accept(self, subprotocol: str | None) -> None:
        await self._websocket.accept(subprotocol)
        self.accepted = True
        self._on_accept()

    async def receive(self) -> str | bytes:
        message = await self._websocket.receive()
        if message["type"] == "websocket.disconnect":
            raise ClientDisconnected(message.get("code", 1005))
        text = message.get("text")
        return text if text is not None else (message.get("bytes") or b"")

    async def send_text(self, data: str) -> None:
        try:
            await self._websocket.send_text(data)
        except (WebSocketDisconnect, RuntimeError) as exc:
            # Starlette reports a browser that left as one of these; the relay ends the stream.
            raise ClientDisconnected(1006) from exc

    async def send_bytes(self, data: bytes) -> None:
        try:
            await self._websocket.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError) as exc:
            raise ClientDisconnected(1006) from exc

    async def close(self, code: int, reason: str) -> None:
        await self._websocket.close(code, reason)


# --- the pipeline --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Admitted:
    """A request that passed steps 1 to 4: who, which app, where, under which caps."""

    actor: Actor
    selection: Selection
    route: Route
    caps: AppCaps
    headers: Headers
    upstream_headers: list[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: int
    bytes_in: int = 0
    bytes_out: int = 0
    upstream_error: bool = False
    limited: bool = False


@dataclass(slots=True)
class _Transfer:
    bytes_out: int = 0


class RequestPipeline:
    """The ASGI application of ``/<slug>/...``: HTTP and WebSocket, for the edge's router."""

    def __init__(
        self,
        *,
        registry: Registry,
        authorizer: Authorizer,
        routes: RouteLookup,
        upstream: UpstreamClient,
        connections: OpenConnections,
        app_state: AppStateReader,
        forward_token_policy: ForwardTokenPolicy | None = None,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._registry = registry
        self._authorizer = authorizer
        self._routes = routes
        self._upstream = upstream
        self._connections = connections
        self._app_state = app_state
        self._token_policy = forward_token_policy
        self._metrics = metrics
        self._selector = PrefixSelector()
        self._limiter = InFlightLimiter()
        self._caps: tuple[RegistrySnapshot, AppCaps] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await self._serve_websocket(scope, receive, send)
        else:
            await self._serve_http(scope, receive, send)

    # --- steps 1 to 4: shared by HTTP and WebSocket ---

    async def _admit(self, scope: Scope) -> _Admitted | _Refusal:
        headers = Headers(scope=scope)
        try:
            actor = actor_from_headers(headers)
        except MissingIdentity:
            return _plain(401, _UNAUTHORIZED_TEXT)
        try:
            snapshot = self._registry.snapshot
        except RegistryUnavailable:
            return _plain(UNAVAILABLE_STATUS, _REGISTRY_TEXT, retry_after_s=_REGISTRY_RETRY_AFTER_S)
        try:
            chosen = self._selector.select(
                RoutingRequest(_raw_path(scope), _query_string(scope)), snapshot
            )
        except AmbiguousPath:
            return _plain(400, _AMBIGUOUS_PATH_TEXT)
        if isinstance(chosen, NoMatch):
            # Asked for no app, the authorizer does the work it does for an app (the visitor's
            # groups are looked up), so a 404 for a slug that does not exist costs what a 404 for
            # a slug that is not yours costs. The answer is discarded: no app is not open.
            await self._authorizer(actor, Action.APP_OPEN, None)
            return _not_found()
        slug = chosen.app_slug
        if await self._authorizer(actor, Action.APP_OPEN, slug) is not True:
            logger.info("a visitor was denied access to app %r", slug)
            return _not_found()
        route = self._routes.lookup(slug)
        if route is None:
            return self._no_route(snapshot, slug)
        forward_token = await self._may_forward_token(actor, slug, headers)
        try:
            upstream_headers = rewrite_request_headers(
                headers,
                actor=actor,
                slug=slug,
                request_id=headers.get("x-request-id"),
                forward_access_token=forward_token,
            )
        except MalformedHeader:
            return _plain(400, _BAD_HEADER_TEXT, app_slug=slug)
        return _Admitted(
            actor=actor,
            selection=chosen,
            route=route,
            caps=self._caps_for(snapshot),
            headers=headers,
            upstream_headers=upstream_headers,
        )

    def _no_route(self, snapshot: RegistrySnapshot, slug: str) -> _Refusal:
        config = snapshot.app_config(slug)
        # An app with no config row reads as stopped, as it does in the launcher (GET /apps).
        desired_state = "stopped" if config is None else config.desired_state
        state = self._app_state(slug, desired_state)
        body = app_unavailable_page(slug, state)
        headers = status_page_headers(body, retry_after_s=app_unavailable_retry_after_s(state))
        return _Refusal(UNAVAILABLE_STATUS, body, headers, slug)

    async def _may_forward_token(self, actor: Actor, slug: str, headers: Headers) -> bool:
        if self._token_policy is None:
            return False
        try:
            return await self._token_policy(actor, slug, headers) is True
        except Exception:
            # An operational boundary of an auth decision: whatever went wrong must end as "do not
            # forward the token", never as a failed request and never as a default of "forward".
            logger.exception(
                "the token policy failed; the access token is not forwarded (app %r)", slug
            )
            return False

    def _caps_for(self, snapshot: RegistrySnapshot) -> AppCaps:
        cached = self._caps
        if cached is not None and cached[0] is snapshot:
            return cached[1]
        caps = caps_from_snapshot(snapshot)
        self._caps = (snapshot, caps)
        return caps

    # --- HTTP ---

    async def _serve_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.perf_counter()
        admission = await self._admit(scope)
        if isinstance(admission, _Refusal):
            await self._refuse_http(scope, send, admission, started)
            return
        slug = admission.selection.app_slug
        caps = admission.caps
        declared = _declared_length(admission.headers)
        if declared is not None and declared > caps.max_request_body_bytes:
            await self._refuse_http(scope, send, _too_large(slug), started)
            return
        permit = self._limiter.try_acquire(slug, caps.max_concurrent_requests)
        if permit is None:
            await self._refuse_http(scope, send, _too_many_requests(slug), started)
            return
        self._observe_in_flight(slug, self._limiter.in_flight(slug))
        try:
            outcome = await self._forward(scope, receive, send, admission)
        finally:
            self._observe_in_flight(slug, permit.release())
        self._count(slug, outcome, started)

    async def _refuse_http(
        self, scope: Scope, send: Send, refusal: _Refusal, started: float
    ) -> None:
        self._count(refusal.app_slug, _outcome_of(refusal), started)
        await _send_refusal(scope, send, refusal)

    async def _forward(
        self, scope: Scope, receive: Receive, send: Send, admitted: _Admitted
    ) -> _Outcome:
        slug = admitted.selection.app_slug
        inbound = _Inbound(receive)
        body: CappedBody | None = None
        if _has_body(admitted.headers):
            body = CappedBody(inbound, max_bytes=admitted.caps.max_request_body_bytes)
        else:
            inbound.mark_no_body()
        request = UpstreamRequest(
            method=scope["method"],
            target=admitted.selection.upstream_target,
            headers=admitted.upstream_headers,
            body=body,
        )

        def bytes_in() -> int:
            return 0 if body is None else body.received_bytes

        watcher = asyncio.create_task(inbound.wait_for_disconnect())
        response: UpstreamResponse | None = None
        try:
            try:
                response = await self._send_unless_client_leaves(admitted, request, watcher)
            except ClientGone:
                return _Outcome(CLIENT_CLOSED_REQUEST_STATUS, bytes_in=bytes_in())
            except (RequestBodyTooLarge, UpstreamError) as failure:
                refusal = _failure_refusal(failure, slug)
                await _send_refusal(scope, send, refusal)
                return _outcome_of(refusal, bytes_in=bytes_in())
            return await self._stream_response(
                send,
                response,
                admitted,
                watcher,
                head_only=scope["method"] == "HEAD",
                bytes_in=bytes_in(),
            )
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            if response is not None:
                await response.aclose()

    async def _send_unless_client_leaves(
        self, admitted: _Admitted, request: UpstreamRequest, watcher: asyncio.Task[None]
    ) -> UpstreamResponse:
        """The child's answer head, or ``ClientGone`` if the browser leaves first."""
        exchange = asyncio.create_task(
            self._upstream.send(
                admitted.route.socket_path,
                request,
                timeout_s=float(admitted.caps.request_timeout_s),
            )
        )
        try:
            await asyncio.wait({exchange, watcher}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not exchange.done():
                exchange.cancel()
                [abandoned] = await asyncio.gather(exchange, return_exceptions=True)
                if isinstance(abandoned, UpstreamResponse):
                    await abandoned.aclose()  # the answer came in as the browser left
        if not exchange.done() or exchange.cancelled():
            raise ClientGone
        return exchange.result()

    async def _stream_response(
        self,
        send: Send,
        response: UpstreamResponse,
        admitted: _Admitted,
        watcher: asyncio.Task[None],
        *,
        head_only: bool,
        bytes_in: int,
    ) -> _Outcome:
        if watcher.done():  # the browser left while the child was answering
            return _Outcome(CLIENT_CLOSED_REQUEST_STATUS, bytes_in=bytes_in)
        lines = self._selector.rewrite_response(admitted.selection, response.headers)
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": [(n.encode(_ENCODING), v.encode(_ENCODING)) for n, v in lines],
            }
        )
        transfer = _Transfer()
        pump = asyncio.create_task(_pump_body(send, response, transfer, head_only=head_only))
        pump_finished = False
        try:
            await asyncio.wait({pump, watcher}, return_when=asyncio.FIRST_COMPLETED)
            pump_finished = pump.done()
        finally:
            if not pump.done():
                pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        if not pump_finished:
            return _Outcome(CLIENT_CLOSED_REQUEST_STATUS, bytes_in, transfer.bytes_out)
        if pump.result():
            return _Outcome(response.status, bytes_in, transfer.bytes_out)
        # The status is sent and cannot be taken back. The response is left unfinished, which the
        # server turns into a closed connection, so the browser sees a broken body, not a short one.
        logger.warning("the child of app %r ended its response early", admitted.selection.app_slug)
        return _Outcome(response.status, bytes_in, transfer.bytes_out, upstream_error=True)

    # --- WebSocket ---

    async def _serve_websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = time.perf_counter()
        admission = await self._admit(scope)
        if isinstance(admission, _Refusal):
            self._count(admission.app_slug, _outcome_of(admission), started)
            await _refuse_websocket(scope, send, admission)
            return
        slug = admission.selection.app_slug
        browser = _BrowserSocket(
            WebSocket(scope, receive, send), on_accept=lambda: self._websocket_opened(slug)
        )
        refusal: _Refusal | None = None
        try:
            await relay_websocket(
                browser,
                socket_path=admission.route.socket_path,
                target=admission.selection.upstream_target,
                headers=admission.upstream_headers,
                subprotocols=scope.get("subprotocols") or [],
                connections=self._connections,
                actor=admission.actor,
                slug=slug,
                release_no=admission.route.release_no,
                open_timeout_s=float(admission.caps.request_timeout_s),
            )
        except UpstreamRejected as rejected:
            refusal = _child_refused(rejected.status, slug)
        except UpstreamTimeout:
            refusal = _gateway_timeout(slug)
        except UpstreamError:
            refusal = _bad_gateway(slug)
        finally:
            if browser.accepted and self._metrics is not None:
                self._metrics.websocket_closed(slug)
        if refusal is None:
            return
        if browser.accepted:
            # The browser was told the upgrade worked, so there is no status left to give it.
            logger.warning("the WebSocket of app %r failed after it was accepted", slug)
            return
        logger.warning("the WebSocket handshake with app %r was refused (%d)", slug, refusal.status)
        self._count(slug, _outcome_of(refusal), started)
        await _refuse_websocket(scope, send, refusal)

    # --- metrics ---

    def _websocket_opened(self, slug: str) -> None:
        if self._metrics is not None:
            self._metrics.websocket_opened(slug)

    def _observe_in_flight(self, slug: str, current: int) -> None:
        if self._metrics is not None:
            self._metrics.observe_in_flight(slug, current)

    def _count(self, slug: str | None, outcome: _Outcome, started: float) -> None:
        if self._metrics is not None:
            self._metrics.record_request(
                slug,
                status=outcome.status,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                bytes_in=outcome.bytes_in,
                bytes_out=outcome.bytes_out,
                upstream_error=outcome.upstream_error,
                limited=outcome.limited,
            )


def _outcome_of(refusal: _Refusal, *, bytes_in: int = 0) -> _Outcome:
    return _Outcome(
        refusal.status,
        bytes_in=bytes_in,
        upstream_error=refusal.upstream_error,
        limited=refusal.limited,
    )


async def _pump_body(
    send: Send, response: UpstreamResponse, transfer: _Transfer, *, head_only: bool
) -> bool:
    """Pass the child's body on, chunk by chunk. ``False`` when the child's side broke off."""
    try:
        if not head_only:
            # Not wrapped in ``aclosing``: the caller closes the response, which ends the stream
            # this loop reads, and the generator is finalised with it.
            async for chunk in response.body():
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    transfer.bytes_out += len(chunk)
    except UpstreamError:
        return False
    await send({"type": "http.response.body", "body": b"", "more_body": False})
    return True
