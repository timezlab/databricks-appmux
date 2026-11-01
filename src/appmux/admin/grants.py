"""``PUT /apps/{app}/grants`` and ``DELETE /apps/{app}/grants/{type}/{name}`` (T041, ``grant.app``).

The rules, in the order a request meets them:

1. identity and same-origin: the guard of ``api.py``;
2. ``grant.app`` on the app (``CAN_MANAGE``): a person who cannot even open the app gets the same
   ``404`` as for a missing one, a person who can open it but not manage it gets ``403``;
3. the body or the path against the schema (``GrantRequest``): ``everyone`` is named ``*`` and is
   ``CAN_USE`` only, a user is a lower-case email, the type is one of three: ``400``;
4. the owner is always ``CAN_MANAGE`` and is not a grant row (data model rule 1): naming the owner
   with a lower level, or removing the owner, is ``400``. ``authz.check_grant_change`` repeats the
   rule as the policy's own check, so the two cannot drift apart;
5. the write and its ``grant.app`` audit row in one transaction.

There is no ``version`` here. The contract's ``version`` belongs to the routes that change an
``apps`` or ``app_config`` row; a grant is a row of its own, keyed by (app, type, principal), so two
writers of the same principal end as the later one (``PUT`` replaces the whole value it names).
A ``PUT`` of the level a principal already has writes and records nothing (``skip_audit``). A
``DELETE`` of a grant that is not there is ``404`` (``no_such_grant``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext, parse_body
from appmux.admin.errors import BadRequest, NotFound
from appmux.admin.schemas import AppGrant as AppGrantBody
from appmux.admin.schemas import (
    GrantRequest,
    OwnerGrantRefused,
    refuse_lowering_owner,
    refuse_removing_owner,
)
from appmux.authz.actions import Action
from appmux.authz.policy import GrantChange, Permission, check_grant_change
from appmux.registry.audit import AuditDetail
from appmux.registry.grants import delete_grant_change, find_grant, put_grant_change
from appmux.registry.models import App, AppGrant


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _detail(principal_type: str, name: str, **fields: Any) -> AuditDetail:
    """The audit detail of a grant change.

    The only caller-controlled part of it is the principal (``type`` and ``name`` of the request),
    so a refusal by the audit model (a control character, a length over 255, a credential shape)
    is reported against ``name``, a field the caller sent, never against ``principal``, which they
    did not. The message never repeats the value.
    """
    try:
        return AuditDetail.model_validate({"principal": f"{principal_type}:{name}", **fields})
    except ValidationError as exc:
        raise BadRequest(
            "the request is not valid: name is not valid "
            "(no control characters, 1 to 255 characters, nothing shaped like a credential)"
        ) from exc


def _path_target(request: Request) -> GrantRequest:
    """The ``{type}/{name}`` of a ``DELETE`` path as a ``GrantRequest`` (the same rules as ``PUT``).

    A refusal names the path parameter, not a body: a ``DELETE`` has none.
    """
    kind = request.path_params["type"]
    name = request.path_params["name"]
    try:
        return GrantRequest.model_validate({"type": kind, "name": name, "permission": "CAN_USE"})
    except ValidationError as exc:
        field = "type" if kind not in ("user", "group", "everyone") else "name"
        raise BadRequest(f"the request is not valid: {field} in the path is not valid") from exc


def _body_of(grant: AppGrant) -> dict[str, object]:
    body = AppGrantBody(
        type=grant.principal_type,
        name=grant.principal,
        permission=grant.permission,
        granted_by=grant.granted_by,
        granted_at=grant.granted_at,
    )
    return body.model_dump(mode="json")


def _app_of(context: AdminContext, slug: str) -> App:
    # `require` found the app a moment ago; a snapshot that moved on can have dropped it.
    app = context.registry.snapshot.app(slug)
    if app is None:
        raise NotFound("no such app")
    return app


def _refuse_owner_change(owner: str, change: GrantChange) -> None:
    refusal = check_grant_change(owner, change)
    if refusal is not None:
        raise BadRequest(f"the grant is refused by the rule '{refusal.value}'", code=refusal.value)


class GrantRoutes:
    """The route provider of the two grant routes."""

    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def put_grant(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.GRANT_APP, slug)
            body = await parse_body(request, GrantRequest)
            app = _app_of(context, slug)
            try:
                refuse_lowering_owner(body, owner=app.owner)
            except OwnerGrantRefused as exc:
                raise BadRequest(str(exc), code="owner_protected") from exc
            _refuse_owner_change(
                app.owner, GrantChange(body.type, body.name, Permission(body.permission))
            )

            actor = context.actor(request)
            existing = find_grant(context.registry.snapshot, slug, body.type, body.name)
            change = put_grant_change(
                existing,
                app_slug=slug,
                principal_type=body.type,
                principal=body.name,
                permission=body.permission,
                granted_by=actor.email,
                granted_at=self._clock(),
            )
            if change is None:
                context.skip_audit(request, "the principal already holds that level")
                if existing is None:  # put_grant_change plans nothing only for a stored grant
                    raise RuntimeError("no change was planned for a grant that does not exist")
                return JSONResponse(_body_of(existing))

            entry = context.audit_entry(
                request,
                "grant.app",
                app_slug=slug,
                detail=_detail(
                    body.type,
                    existing.principal if existing else body.name,
                    permission=body.permission,
                    **({"from": existing.permission} if existing else {}),
                ),
            )
            (stored,) = await context.write(request, [change], [entry])
            if not isinstance(stored, AppGrant):
                raise RuntimeError("the registry did not return the stored grant")
            return JSONResponse(_body_of(stored))

        async def delete_grant(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.GRANT_APP, slug)
            target = _path_target(request)
            app = _app_of(context, slug)
            try:
                refuse_removing_owner(target.type, target.name, owner=app.owner)
            except OwnerGrantRefused as exc:
                raise BadRequest(str(exc), code="owner_protected") from exc
            _refuse_owner_change(app.owner, GrantChange(target.type, target.name, None))

            # A name the audit log cannot hold is a 400 at once; no grant can carry such a name.
            _detail(target.type, target.name)
            existing = find_grant(context.registry.snapshot, slug, target.type, target.name)
            if existing is None:
                raise NotFound("that principal has no grant on this app", code="no_such_grant")
            entry = context.audit_entry(
                request,
                "grant.app",
                app_slug=slug,
                detail=_detail(
                    existing.principal_type,
                    existing.principal,
                    permission=existing.permission,
                    to="removed",
                ),
            )
            await context.write(request, [delete_grant_change(existing)], [entry])
            return Response(status_code=204)

        return [
            Route("/apps/{app}/grants", put_grant, methods=["PUT"]),
            Route("/apps/{app}/grants/{type}/{name:path}", delete_grant, methods=["DELETE"]),
        ]
