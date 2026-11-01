"""The request inbox routes (T145; US8; design 9.4; contracts/admin-api.md, Requests).

Five routes. What each needs, in the order the checks run (identity and same-origin come first, from
the guard of ``api.py``):

* ``GET /requests``: any visitor. A visitor sees their own requests, ``CAN_MANAGE`` on an app adds
  that app's, a platform admin sees all; oldest first, no filter and no paging in v1 (TD-007). A row
  whose kind this version does not know is left out, because ``RequestRow.kind`` cannot carry it.
* ``POST /requests`` (``request.create``): the body against ``CreatePermissionRequest``; then
  ``authorize()`` (any visitor for ``register``, ``CAN_MANAGE`` on the app for the others; an app
  the person cannot open is ``404``); then the rules of the catalogue (``check_new_request``). The
  appmux service principal inserts the row and ``request.create`` in one transaction. A request
  grants nothing (FR-045): no ``apps`` row is read or written.
* ``POST /requests/{id}/withdraw`` (``request.withdraw``): a request the person may not see is
  ``404``; only the asker, only while pending (``403 not_the_asker``, ``409 not_pending``).
* ``POST /requests/{id}/approve`` and ``/deny`` (``request.decide``, platform admins only): the
  answer is written under the admin's own role (``403 user_token_missing`` without their token).
  Approving re-checks the request as it stands now (``plan_approval``), then writes what it grants
  and the answer in one transaction, so every field applies or none does (FR-042).

A rename is followed by the ``RenameCascade`` seam (``reconciler/rename.py``): the row is saved
first, then the running app is moved to the new slug. When the move fails the answer is
``503 rename_not_applied`` and the rename stays saved; pressing Restart on the app retries it. A
process with no cascade wired refuses a rename before saving anything (``503 rename_unavailable``).
Every refusal has a ``code`` the UI may branch on; the table is in ``request_catalogue.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from pydantic import SecretStr, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext, parse_body
from appmux.admin.authorizer import Access
from appmux.admin.errors import Conflict, Forbidden, NotFound, Unavailable, validation_error
from appmux.admin.identity import AccessSource
from appmux.admin.request_catalogue import (
    KINDS,
    asks_without_app,
    check_can_withdraw,
    check_new_request,
    current_values,
    pending_request_conflict,
    plan_approval,
    require_known_kind,
    require_pending,
    requires_reason,
)
from appmux.admin.schemas import (
    CreatePermissionRequest,
    DecidePermissionRequest,
    RequestList,
    RequestRow,
)
from appmux.authz.actions import Action
from appmux.authz.policy import authorize
from appmux.identity.actor import Actor
from appmux.registry.audit import AuditDetail, AuditEntry
from appmux.registry.errors import RowAlreadyExists
from appmux.registry.models import App, PermissionRequest
from appmux.registry.requests import (
    Outcome,
    answer_audit,
    approval_changes,
    create_request_change,
    deny_audit,
    deny_changes,
    taken_slug_note,
    withdraw_change,
)
from appmux.registry.snapshot import RegistrySnapshot

logger = logging.getLogger(__name__)


class RenameCascadeFailed(Exception):
    """The running app could not be moved to its new slug; the rename itself is saved."""


class RenameCascade(Protocol):
    """What happens to a running app after its slug changed (``reconciler/rename.py``)."""

    async def renamed(self, old_slug: str, new_slug: str) -> None:
        """Move the app to ``new_slug``; raise ``RenameCascadeFailed`` if the move fails."""
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def admin_token_of(actor: Actor) -> SecretStr:
    """The platform admin's own access token, which the platform fields are written under.

    Never the appmux service principal's: the database then records who changed what with a role
    only a platform admin holds (design 7.3).
    """
    if actor.visitor_token is None:
        raise Forbidden(
            "appmux did not receive your Databricks access token, so it cannot save the change as "
            "you; ask a platform admin to turn on user authorization for the appmux App, then "
            "open this page again",
            code="user_token_missing",
        )
    return actor.visitor_token


def request_row(
    snapshot: RegistrySnapshot, row: PermissionRequest, *, app_version: int | None = None
) -> RequestRow:
    """The contract's ``RequestRow`` for a stored request."""
    payload = {key: value for key, value in row.payload.model_dump().items() if value is not None}
    return RequestRow.model_validate(
        {
            "request_id": row.request_id,
            "app_slug": row.app_slug,
            "kind": row.kind,
            "payload": payload,
            "reason": row.reason,
            "requested_by": row.requested_by,
            "requested_at": row.requested_at,
            "status": row.status,
            "closed_by": row.closed_by,
            "closed_at": row.closed_at,
            "closed_note": row.closed_note,
            "current": current_values(snapshot, row),
            "requires_reason": requires_reason(row.kind, payload),
            "app_version": app_version,
        }
    )


def _answer_text(body: DecidePermissionRequest) -> str | None:
    """The note the asker reads: the admin's note, else their reason (data model 4.3)."""
    for text in (body.note, body.reason):
        if text is not None and text.strip():
            return text.strip()
    return None


def _stored_request(row: object) -> PermissionRequest:
    if not isinstance(row, PermissionRequest):
        raise RuntimeError("the registry did not return the stored request")
    return row


class RequestRoutes:
    """The route provider of the request inbox.

    ``access`` gives a route the snapshot, the authorization data and the groups of the actor (the
    ``RegistryAuthorizer``). ``rename`` is the cascade run after an approved rename; ``clock``
    stamps ``closed_at``, which the registry does not.
    """

    def __init__(
        self,
        access: AccessSource,
        *,
        rename: RenameCascade | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._access = access
        self._rename = rename
        self._clock = clock

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def list_requests(request: Request) -> Response:
            actor = context.actor(request)
            access = await self._access.access_for(actor)
            is_admin = _decides(actor, access)
            ordered = sorted(
                access.snapshot.permission_requests,
                key=lambda row: (row.requested_at, row.request_id),
            )
            items = [
                request_row(access.snapshot, row)
                for row in ordered
                if row.kind in KINDS and (is_admin or _may_see(actor, row, access))
            ]
            return JSONResponse(RequestList(items=items).model_dump(mode="json"))

        async def create(request: Request) -> Response:
            body = await parse_body(request, CreatePermissionRequest)
            app_slug = None if asks_without_app(body.kind) else body.app
            await context.require(request, Action.REQUEST_CREATE, app_slug)
            actor = context.actor(request)
            check_new_request(
                context.registry.snapshot,
                kind=body.kind,
                app=body.app,
                payload=body.payload,
                asker=actor.email,
            )
            change = create_request_change(
                kind=body.kind,
                app_slug=app_slug,
                payload={key: value for key, value in body.payload.items() if value is not None},
                reason=body.reason,
                requested_by=actor.email,
            )
            entry = context.audit_entry(
                request, "request.create", app_slug=app_slug, detail=AuditDetail(kind=body.kind)
            )
            try:
                (row,) = await context.write(request, [change], [entry])
            except RowAlreadyExists as exc:
                # The registry's unique index: another request for the same slot got in first.
                raise pending_request_conflict() from exc
            stored = _stored_request(row)
            return _answer(context, stored)

        async def withdraw(request: Request) -> Response:
            actor = context.actor(request)
            access = await self._access.access_for(actor)
            row = _find(access.snapshot, request)
            if not (_decides(actor, access) or _may_see(actor, row, access)):
                raise NotFound("no such request")
            await context.require(request, Action.REQUEST_WITHDRAW, row.app_slug)
            require_known_kind(row)
            check_can_withdraw(row, actor.email)
            change = withdraw_change(row, closed_by=actor.email, closed_at=self._clock())
            entry = context.audit_entry(
                request,
                "request.withdraw",
                app_slug=row.app_slug,
                detail=AuditDetail(request=row.request_id, kind=row.kind),
            )
            (updated,) = await context.write(request, [change], [entry])
            return _answer(context, _stored_request(updated))

        async def approve(request: Request) -> Response:
            await context.require(request, Action.REQUEST_DECIDE)
            body = await parse_body(request, DecidePermissionRequest)
            actor = context.actor(request)
            token = admin_token_of(actor)
            access = await self._access.access_for(actor)
            snapshot = access.snapshot
            row = _find(snapshot, request)
            asker = Actor(email=row.requested_by.lower())
            asker_can_still_ask = (
                authorize(
                    asker, Action.REQUEST_CREATE, row.app_slug, access.data, groups=None
                ).allowed
                is True
            )
            plan = plan_approval(
                snapshot, row, reason=body.reason, requester_can_manage=asker_can_still_ask
            )
            if plan.rename is not None and self._rename is None:
                raise Unavailable(
                    "this process cannot move a running app to a new slug, so the rename was not "
                    "saved; restart the appmux App, then press Approve again",
                    code="rename_unavailable",
                )
            if plan.outcome is Outcome.SLUG_TAKEN:
                changes = deny_changes(
                    row,
                    closed_by=actor.email,
                    closed_at=self._clock(),
                    note=taken_slug_note(str(plan.taken_slug)),
                )
            else:
                changes = approval_changes(
                    plan,
                    app=snapshot.app(row.app_slug) if row.app_slug is not None else None,
                    approver=actor.email,
                    closed_at=self._clock(),
                    note=_answer_text(body),
                )
            entries = _audit_or_refuse(
                lambda: answer_audit(plan, row, approver=actor.email, reason=body.reason)
            )
            try:
                rows = await context.write(request, changes, entries, admin_token=token)
            except RowAlreadyExists as exc:
                # An app took the slug between the check and the write; the next try finds it.
                raise Conflict(
                    "an app with that slug appeared a moment ago; reload the inbox and press "
                    "Approve again",
                    code="slug_taken",
                ) from exc
            app_version = next((stored.version for stored in rows if isinstance(stored, App)), None)
            if plan.outcome is Outcome.APPLY and plan.rename is not None:
                await self._move_running_app(*plan.rename)
            return _answer(context, _stored_request(rows[-1]), app_version=app_version)

        async def deny(request: Request) -> Response:
            await context.require(request, Action.REQUEST_DECIDE)
            body = await parse_body(request, DecidePermissionRequest)
            actor = context.actor(request)
            token = admin_token_of(actor)
            access = await self._access.access_for(actor)
            row = _find(access.snapshot, request)
            require_known_kind(row)
            require_pending(row)
            changes = deny_changes(
                row, closed_by=actor.email, closed_at=self._clock(), note=_answer_text(body)
            )
            entries = _audit_or_refuse(
                lambda: deny_audit(row, denier=actor.email, reason=body.reason)
            )
            (updated,) = await context.write(request, changes, entries, admin_token=token)
            return _answer(context, _stored_request(updated))

        return [
            Route("/requests", list_requests, methods=["GET"]),
            Route("/requests", create, methods=["POST"]),
            Route("/requests/{request_id:int}/withdraw", withdraw, methods=["POST"]),
            Route("/requests/{request_id:int}/approve", approve, methods=["POST"]),
            Route("/requests/{request_id:int}/deny", deny, methods=["POST"]),
        ]

    async def _move_running_app(self, old_slug: str, new_slug: str) -> None:
        if self._rename is None:
            return
        try:
            await self._rename.renamed(old_slug, new_slug)
        except RenameCascadeFailed as exc:
            # The only record of why: the answer must not repeat the cascade's own text.
            logger.warning(
                "the rename of app %r to %r is saved but the running app was not moved: %s",
                old_slug,
                new_slug,
                exc,
            )
            raise Unavailable(
                "the new slug is saved, but the running app has not moved to it yet; press "
                "Restart on the app to try again, and ask a platform admin to check the server "
                "log if it keeps failing",
                code="rename_not_applied",
            ) from exc


def _answer(
    context: AdminContext, row: PermissionRequest, *, app_version: int | None = None
) -> Response:
    """The request as the registry holds it after the write, with the values current now."""
    shown = request_row(context.registry.snapshot, row, app_version=app_version)
    return JSONResponse(shown.model_dump(mode="json"))


def _find(snapshot: RegistrySnapshot, request: Request) -> PermissionRequest:
    row = snapshot.request(int(request.path_params["request_id"]))
    if row is None:
        raise NotFound("no such request")
    return row


def _decides(actor: Actor, access: Access) -> bool:
    """Whether the actor is a platform admin (``request.decide``), who sees every request."""
    decision = authorize(actor, Action.REQUEST_DECIDE, None, access.data, groups=access.groups)
    return decision.allowed is True


def _may_see(actor: Actor, row: PermissionRequest, access: Access) -> bool:
    """The asker sees their own request, and ``CAN_MANAGE`` on its app sees the app's."""
    if row.requested_by.lower() == actor.email.lower():
        return True
    if row.app_slug is None:
        return False
    decision = authorize(actor, Action.APP_VIEW, row.app_slug, access.data, groups=access.groups)
    return decision.allowed is True


def _audit_or_refuse(build: Callable[[], list[AuditEntry]]) -> list[AuditEntry]:
    """The audit entries, or the ``400`` of a field the audit model refuses (a reason shaped like a
    credential, or too long). The message names the field and never its value."""
    try:
        return build()
    except ValidationError as exc:
        raise validation_error(exc) from exc
