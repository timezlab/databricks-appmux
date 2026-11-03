"""The admin JSON API as a Starlette sub-application, and the seam route modules plug into.

``create_admin_app`` returns the application the edge mounts at ``/_appmux/api/v1`` (the factory
does not know the mount path). It carries no route of its own: each route module is a
``RouteProvider`` whose ``routes(context)`` returns Starlette ``Route`` objects, so a module adds
its routes without editing this file.

A guard around every route applies, in this order (design 7.3, contracts/admin-api.md Conventions):

1. identity from the forwarded headers: none, or a repeated identity header, is ``401``;
2. on every method but ``GET``, ``HEAD`` and ``OPTIONS`` (compared upper-cased), ``Sec-Fetch-Site:
   same-origin`` exactly once; anything else is ``403``;
3. the route. An ``AdminError``, a ``RegistryError`` or any other exception becomes the one error
   body of ``errors.py``.

Every response carries ``Cache-Control: no-store`` and ``X-Content-Type-Options: nosniff``, whatever
the route set. The router does not redirect a path with or without a trailing slash: a mismatch is
the ordinary ``404`` (a redirect would be built from the ``Host`` header and, for a change, would
be taken for an unaudited success).

A mutating route must record an audit entry. ``AdminContext.write`` is the only way a route writes,
it refuses a write without audit entries, and the guard replaces the answer of a mutating route
that finished with a success and recorded none by a ``500`` that it logs, so a change cannot
succeed unrecorded. A route that really changes nothing says so with ``AdminContext.skip_audit``.

Authorization stays in ``authz``: the caller injects an ``Authorizer`` and this module only asks.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Protocol, TypeVar

from pydantic import AwareDatetime, BaseModel, SecretStr, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from appmux.admin.errors import (
    AdminError,
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    Unauthorized,
    Unavailable,
    error_response,
    validation_error,
)
from appmux.authz.actions import Action
from appmux.identity.actor import Actor, MissingIdentity, actor_from_headers
from appmux.registry.audit import AuditAction, AuditDetail, AuditEntry
from appmux.registry.errors import ChangedByOthers
from appmux.registry.metric_steps import MetricBreakdownRow
from appmux.registry.models import AuditLogRow, MetricRow, RowModel
from appmux.registry.protocol import Change, Registry
from appmux.registry.snapshot import RegistrySnapshot

logger = logging.getLogger(__name__)

# Methods that change nothing. Any other verb, in any letter case, is treated as mutating: a list
# of mutating verbs would let an unlisted or lower-case verb (a class-based endpoint hands the
# lower-cased verb to its handler) past the same-origin and audit rules.
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
# The largest JSON body a route reads. The admin bodies are small lists and short texts.
MAX_BODY_BYTES: Final = 1024 * 1024

_STATE_KEY: Final = "appmux.admin.request"
_SAME_ORIGIN: Final = "same-origin"
_NO_STORE_HEADERS: Final = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

BodyT = TypeVar("BodyT", bound=BaseModel)


class Authorizer(Protocol):
    """Answers whether ``actor`` may do ``action`` (a value of ``authz.actions.Action``) on an app.

    ``app_slug`` is ``None`` for a platform action or one with no app yet. Only an answer that is
    exactly ``True`` allows; the adapter from ``authz.authorize`` is ``admin/authorizer.py``.
    """

    async def __call__(self, actor: Actor, action: str, app_slug: str | None) -> bool: ...


class RouteProvider(Protocol):
    """A route module: given the context, the routes it serves under the API's mount path."""

    def routes(self, context: AdminContext) -> Sequence[Route]: ...


@dataclass(slots=True)
class _RequestState:
    """What the guard knows about one request, kept in the ASGI scope."""

    actor: Actor
    is_audited: bool = False
    audit_skip_reason: str | None = None


def _is_mutating(method: str) -> bool:
    return method.upper() not in SAFE_METHODS


def _state_of(request: Request) -> _RequestState:
    state = request.scope.get(_STATE_KEY)
    if not isinstance(state, _RequestState):
        raise RuntimeError(
            "the request did not pass the admin guard; build routes with the factory"
        )
    return state


class RegistryView:
    """What a route may use of the registry directly: reads only.

    Writes go through ``AdminContext.write`` so that none can skip its audit entry.
    """

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    @property
    def snapshot(self) -> RegistrySnapshot:
        """The latest good snapshot; raises ``RegistryUnavailable`` before the first load."""
        return self._registry.snapshot

    @property
    def last_error(self) -> str | None:
        return self._registry.last_error

    async def read_audit(
        self, *, app_slug: str | None, start: AwareDatetime, end: AwareDatetime, limit: int
    ) -> tuple[AuditLogRow, ...]:
        """``Registry.read_audit``."""
        return await self._registry.read_audit(app_slug=app_slug, start=start, end=end, limit=limit)

    async def read_metrics(
        self, *, app_slug: str | None, start: AwareDatetime, end: AwareDatetime, limit: int
    ) -> tuple[MetricRow, ...]:
        """``Registry.read_metrics``."""
        return await self._registry.read_metrics(
            app_slug=app_slug, start=start, end=end, limit=limit
        )

    async def read_metric_steps(
        self,
        *,
        app_slug: str | None,
        start: AwareDatetime,
        end: AwareDatetime,
        step: timedelta,
        limit: int,
    ) -> tuple[MetricRow, ...]:
        """``Registry.read_metric_steps``."""
        return await self._registry.read_metric_steps(
            app_slug=app_slug, start=start, end=end, step=step, limit=limit
        )

    async def read_metric_breakdown(
        self, *, start: AwareDatetime, end: AwareDatetime, step: timedelta, top: int
    ) -> tuple[MetricBreakdownRow, ...]:
        """``Registry.read_metric_breakdown``."""
        return await self._registry.read_metric_breakdown(start=start, end=end, step=step, top=top)


class AdminContext:
    """Everything a route needs, and nothing else.

    Built once. What belongs to one request (the actor) is read from the ``request`` argument.
    """

    def __init__(self, *, registry: Registry, authorizer: Authorizer) -> None:
        self._registry = registry
        self._authorizer = authorizer
        self.registry = RegistryView(registry)

    def actor(self, request: Request) -> Actor:
        """The person making the request, from the forwarded headers."""
        return _state_of(request).actor

    async def require(self, request: Request, action: Action, app_slug: str | None = None) -> None:
        """Return when the actor may do ``action``; otherwise raise.

        With an ``app_slug``, an app that does not exist and an app the actor cannot open both
        raise ``NotFound`` with the same body and after the same work (the authorizer is asked
        about ``app.open``, then the snapshot is looked up, whatever either answers), so neither
        the body nor the time it takes tells a stranger which slugs exist. An app the actor can
        open but may not act on raises ``Forbidden``. Without an app (a platform action) a denial
        is ``Forbidden``.
        """
        actor = self.actor(request)
        if app_slug is not None:
            can_open = await self._allows(actor, Action.APP_OPEN, app_slug)
            exists = self.registry.snapshot.app(app_slug) is not None
            if not (can_open and exists):
                raise NotFound("no such app")
            if action is Action.APP_OPEN:
                return
        if not await self._allows(actor, action, app_slug):
            raise Forbidden(f"you are not allowed to do '{action.value}' here")

    async def _allows(self, actor: Actor, action: Action, app_slug: str | None) -> bool:
        # Fail closed: only the value True allows (not a truthy object); an authorizer that
        # raises propagates and the guard answers 500.
        return await self._authorizer(actor, action.value, app_slug) is True

    def require_registry(self) -> None:
        """Raise ``Unavailable`` while the registry's latest read or write failed.

        For a route whose answer must be fresh. A route that may serve the last snapshot (the
        launcher) does not call it; a route that writes or reads the database gets
        ``RegistryUnavailable`` from the registry itself.
        """
        if self._registry.last_error is not None:
            raise Unavailable()

    @staticmethod
    def require_version(current: int, sent: int) -> None:
        """Raise the ``409`` of a stale ``version`` (contracts/admin-api.md, Conventions).

        The registry checks ``expected_version`` again inside the write; this is the early check
        for a route that has the row in hand.
        """
        if current != sent:
            raise Conflict(ChangedByOthers.default_message, code=ChangedByOthers.code)

    def audit_entry(
        self,
        request: Request,
        action: AuditAction,
        *,
        app_slug: str | None = None,
        detail: AuditDetail | None = None,
        reason: str | None = None,
    ) -> AuditEntry:
        """An audit entry for the actor of ``request``.

        A field the audit model refuses (a reason shaped like a credential, a name that is not a
        slug) raises ``BadRequest`` naming the field and never its value.
        """
        try:
            return AuditEntry(
                actor=self.actor(request).email,
                action=action,
                app_slug=app_slug,
                detail=detail if detail is not None else AuditDetail(),
                reason=reason,
            )
        except ValidationError as exc:
            raise validation_error(exc) from exc

    async def write(
        self,
        request: Request,
        changes: Sequence[Change],
        audit: Sequence[AuditEntry],
        *,
        admin_token: SecretStr | None = None,
    ) -> tuple[RowModel | None, ...]:
        """``Registry.write`` for a mutating route: the changes and their audit entries together.

        ``audit`` must name the actor of the request and must not be empty; a violation is a
        defect of the route, raised as ``ValueError`` (the guard answers ``500`` and logs it) and
        nothing is written. ``changes`` may be empty: the write then records the audit entries
        alone (viewing a release's source changes no row) and counts as audited. ``admin_token`` is
        for the kinds only a platform admin's role may write (``resolve_identity``); the route
        passes ``actor.visitor_token``.
        """
        state = _state_of(request)
        if not audit:
            raise ValueError("a mutating route must record at least one audit entry")
        if any(entry.actor != state.actor.email for entry in audit):
            raise ValueError("an audit entry must name the actor of the request")
        rows = await self._registry.write(changes, audit, admin_token=admin_token)
        state.is_audited = True
        return rows

    def skip_audit(self, request: Request, reason: str) -> None:
        """Declare that this mutating call changed no registry row, so it has nothing to record.

        For a ``POST`` that only computes (a self-check) or finds nothing to do (an Update whose
        digest equals a live release's). The reason is required and logged.
        """
        if not reason.strip():
            raise ValueError("skip_audit needs the reason the call changes nothing")
        _state_of(request).audit_skip_reason = reason
        logger.info("admin call declared no audit entry: %s", reason)


async def parse_body(request: Request, model: type[BodyT]) -> BodyT:
    """The JSON body of ``request`` as ``model``.

    ``400`` when the body is not ``application/json``, is larger than ``MAX_BODY_BYTES``, is not
    JSON, or fails the model. The message names fields and never repeats the submitted text.
    """
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise BadRequest("the body must be sent as application/json")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise BadRequest(f"the body is larger than {MAX_BODY_BYTES // 1024} KiB")
        chunks.append(chunk)
    try:
        return model.model_validate_json(b"".join(chunks))
    except ValidationError as exc:
        raise validation_error(exc) from exc


class _AdminGuard:
    """Pure ASGI middleware: identity, same-origin, error bodies, headers and the audit rule."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            # The admin API has no websocket route; refusing before accept answers 403.
            await send({"type": "websocket.close", "code": 1008})
            return

        started = False
        replaced = False

        async def forward(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in _NO_STORE_HEADERS.items():
                    headers[name] = value
            await send(message)

        state: _RequestState | None = None

        async def guarded_send(message: Message) -> None:
            nonlocal replaced
            if replaced:
                return
            if (
                message["type"] == "http.response.start"
                and state is not None
                and _is_mutating(scope["method"])
                and message["status"] < 400
                and not state.is_audited
                and state.audit_skip_reason is None
            ):
                replaced = True
                logger.error(
                    "mutating route %s %r answered %s without recording an audit entry; "
                    "the answer was replaced by a 500",
                    scope["method"],
                    scope["path"],
                    message["status"],
                )
                await error_response(AdminError())(scope, receive, forward)
                return
            await forward(message)

        try:
            refusal = self._refusal(scope)
            if isinstance(refusal, Exception):
                await error_response(refusal)(scope, receive, forward)
                return
            state = refusal
            scope[_STATE_KEY] = state
            await self._app(scope, receive, guarded_send)
        except Exception as exc:
            # An operational boundary: whatever a route raises must leave as the one error body,
            # never as a server traceback. ``error_response`` logs an unexpected exception once.
            if replaced:
                logger.exception("a route failed after its answer was replaced")
                return
            if started:
                raise
            await error_response(exc)(scope, receive, forward)

    @staticmethod
    def _refusal(scope: Scope) -> _RequestState | Exception:
        """The state of an accepted request, or the error that refuses it."""
        request = Request(scope)
        try:
            actor = actor_from_headers(request.headers)
        except MissingIdentity:  # also AmbiguousIdentity, its subclass: neither can be trusted
            return Unauthorized()
        if _is_mutating(scope["method"]):
            sent = request.headers.getlist("sec-fetch-site")
            if sent != [_SAME_ORIGIN]:
                return Forbidden(
                    "a change must come from the appmux admin page itself "
                    "(Sec-Fetch-Site: same-origin)",
                    code="cross_origin",
                )
        return _RequestState(actor=actor)


def _http_exception(request: Request, exc: Exception) -> Response:
    # Unknown route and wrong method: the router raises these; they answer in the error body.
    if not isinstance(exc, HTTPException):
        raise exc
    return error_response(exc)


def create_admin_app(
    *,
    registry: Registry,
    authorizer: Authorizer,
    route_providers: Sequence[RouteProvider] = (),
) -> Starlette:
    """The admin API application; the caller mounts it at ``/_appmux/api/v1``.

    Every route of every provider goes behind the guard. A provider that returns anything but a
    Starlette ``Route`` raises ``TypeError`` here, at startup, rather than serving a path the
    guard cannot see the method of.
    """
    context = AdminContext(registry=registry, authorizer=authorizer)
    routes: list[Route] = []
    for provider in route_providers:
        for route in provider.routes(context):
            if not isinstance(route, Route):
                raise TypeError(
                    f"{type(provider).__name__}.routes() must return starlette Route objects, "
                    f"not {type(route).__name__}"
                )
            routes.append(route)
    application = Starlette(
        routes=routes,
        middleware=[Middleware(_AdminGuard)],
        exception_handlers={HTTPException: _http_exception},
    )
    # Off: a path with the other trailing slash is an ordinary 404 in the error body. The default
    # answers 307 with a Location built from the Host header, and the audit rule turns the 307 of a
    # change into a 500 and an ERROR log line that anyone signed in could trigger.
    application.router.redirect_slashes = False
    return application
