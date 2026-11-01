"""``PUT`` and ``DELETE /apps/{app}/delegation/{kind}`` (T145; US8; design 8.4 and 9.4).

A platform admin grants or revokes a delegation directly, with no request (``delegation.grant`` and
``delegation.revoke``). ``kind`` is ``obo`` or ``shared_sp``; any other word is ``404``. The rules a
call meets, in order:

1. identity and same-origin: the guard of ``api.py``;
2. the permission on the app: a person who cannot open it gets the same ``404`` as for a missing
   one, one who can but is not a platform admin gets ``403``;
3. the reason and the ``apps`` ``version``: the ``PUT`` sends them as JSON, the ``DELETE`` as query
   parameters (nothing shows that a ``DELETE`` body survives the Databricks proxy). A bad one is
   ``400``, a stale ``version`` is ``409 changed_by_others``;
4. that the call changes something: granting what is held is ``409 already_delegated``, revoking
   what is not held is ``409 not_delegated``;
5. a grant of ``shared_sp`` while the platform master switch is off is
   ``409 shared_sp_not_allowed``; a revoke is never refused for it, because it only reduces access;
6. the ``apps`` update (under the admin's own token, ``403 user_token_missing`` without it) and the
   audit row, with the reason, in one transaction.

The answer is the ``AppDetail`` after the write. The change takes effect at once for the app's next
start: the credentials of a child are built when it starts (design 8.5), so a running app keeps the
ones it has until it is restarted, which the page says next to the flag.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext, parse_body
from appmux.admin.apps import build_app_detail
from appmux.admin.errors import Conflict, NotFound, Unavailable, validation_error
from appmux.admin.request_catalogue import DELEGATION_COLUMNS, shared_sp_allowed
from appmux.admin.requests import admin_token_of
from appmux.admin.schemas import DelegationReasonRequest
from appmux.authz.actions import Action
from appmux.registry.audit import AuditDetail
from appmux.registry.models import App
from appmux.registry.protocol import Change
from appmux.registry.writers import WriteKind


def _column_of(kind: str) -> str:
    column = DELEGATION_COLUMNS.get(kind)
    if column is None:
        raise NotFound("no such delegation; the kinds are obo and shared_sp")
    return column


class DelegationRoutes:
    """The route provider of the two delegation routes."""

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def grant(request: Request) -> Response:
            slug, kind = request.path_params["app"], request.path_params["kind"]
            column = _column_of(kind)
            await context.require(request, Action.DELEGATION_GRANT, slug)
            body = await parse_body(request, DelegationReasonRequest)
            snapshot = context.registry.snapshot
            app = _app_of(snapshot.app(slug))
            context.require_version(app.version, body.version)
            if getattr(app, column) is True:
                raise Conflict(
                    f"{kind} is already delegated to this app; nothing to grant",
                    code="already_delegated",
                )
            if kind == "shared_sp" and not shared_sp_allowed(snapshot):
                raise Conflict(
                    "the shared service principal is switched off for the platform; turn it on in "
                    "the platform settings first",
                    code="shared_sp_not_allowed",
                )
            return await _write(context, request, app, kind, granting=True, body=body)

        async def revoke(request: Request) -> Response:
            slug, kind = request.path_params["app"], request.path_params["kind"]
            column = _column_of(kind)
            await context.require(request, Action.DELEGATION_REVOKE, slug)
            try:
                body = DelegationReasonRequest.from_query(dict(request.query_params))
            except ValidationError as exc:
                raise validation_error(exc) from exc
            app = _app_of(context.registry.snapshot.app(slug))
            context.require_version(app.version, body.version)
            if getattr(app, column) is not True:
                raise Conflict(
                    f"{kind} is not delegated to this app; nothing to revoke", code="not_delegated"
                )
            return await _write(context, request, app, kind, granting=False, body=body)

        return [
            Route("/apps/{app}/delegation/{kind}", grant, methods=["PUT"]),
            Route("/apps/{app}/delegation/{kind}", revoke, methods=["DELETE"]),
        ]


def _app_of(app: App | None) -> App:
    # ``AdminContext.require`` found the app a moment ago, so a missing one means the snapshot
    # moved on between the two reads.
    if app is None:
        raise NotFound("no such app")
    return app


async def _write(
    context: AdminContext,
    request: Request,
    app: App,
    kind: str,
    *,
    granting: bool,
    body: DelegationReasonRequest,
) -> Response:
    actor = context.actor(request)
    token = admin_token_of(actor)
    change = Change(
        WriteKind.APP_UPDATE,
        key={"slug": app.slug},
        values={DELEGATION_COLUMNS[kind]: granting, "updated_by": actor.email},
        expected_version=body.version,
    )
    entry = context.audit_entry(
        request,
        "delegation.grant" if granting else "delegation.revoke",
        app_slug=app.slug,
        detail=AuditDetail(kind=kind),
        reason=body.reason,
    )
    (row,) = await context.write(request, [change], [entry], admin_token=token)
    if not isinstance(row, App):
        raise RuntimeError("the registry did not return the stored app")
    snapshot = context.registry.snapshot
    config = snapshot.app_config(app.slug)
    if config is None:
        raise Unavailable(
            "this app's settings cannot be read right now; ask a platform admin to check the "
            "registry",
            code="app_config_missing",
        )
    return JSONResponse(build_app_detail(snapshot, row, config).model_dump(mode="json"))
