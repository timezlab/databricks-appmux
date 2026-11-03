"""Activate, edit the config, start, stop, restart and delete (T107, T128; FR-006, FR-009).

Six routes of ``contracts/admin-api.md``. Each one decides from the registry snapshot, writes
rows (plus the audit row, in one transaction) and tells the reconciler to look; the reconciler
makes the rows true (design 5.2). None of them reads a workspace folder, and only the delete needs
the person's token; the others write as the appmux service principal.

``POST /apps/{app}/releases/{id}/activate`` (``release.activate``, ``CAN_MANAGE``) makes any live
release run again from what is stored. Before anything is written it runs the requirements check
(``reconciler/activate.py``): a release whose resource keys are not attached answers
``409 requirements_missing`` with every missing key in the message. A release on its way up is
``409 release_in_progress``; a deleted or unknown one is ``404``. The answer is the release as
``ReleaseDetail``, ``pending`` until the reconciler has swapped to it.

``PUT /apps/{app}/config`` (``config.edit``, ``CAN_MANAGE``) replaces the whole editable config
and needs the ``version`` that was read. Env entries and wheel sources belong to a release, so a
change of either creates a **new release with the same snapshot** that goes through the same swap
as an Update; the health check and the own-SP keys belong to the app and are written on
``app_config`` alone (``registry/operations.py``). The own-SP keys must name attached resources of
type ``secret`` (the database refuses anything else; this route answers ``400`` first). Env
values reach neither the answer nor the audit row.

``POST /apps/{app}/start|stop|restart`` (``app.start`` ...) change what is wanted and answer the
state the reconciler reports (``AppStateResponse``). Stop and Start write ``desired_state``;
Restart of a running app writes nothing, it asks the reconciler for a fresh copy beside the
running one. Start and Restart of an app that has no release to run are
``409 no_release``. Start of a crashed app is a restart: its desired state was ``running``
already, so nothing would otherwise happen.

``DELETE /apps/{app}`` (``app.delete``, platform ``CAN_MANAGE``) answers ``204``. The app's route
closes and its child stops **before** its rows go (``AppControl.stop_app``), so a child never
outlives its registration; then one registry write deletes the app (its config, releases,
resources, grants and requests go with it by the foreign keys) together with the audit row, which
stays, as do the app's metrics. It is a platform write, so it is made as the person (their own
token), never as the service principal. Everything that can refuse it is checked before the stop
(permission, token, registry up). A write that fails after the stop leaves the app stopped and
answers the usual error; nothing is started again to make up for it, because a second failing
write would leave neither state. A repeated delete finds no app and answers ``404``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext, parse_body
from appmux.admin.errors import BadRequest, Conflict, NotFound, validation_error
from appmux.admin.releases import build_release_detail
from appmux.admin.requests import admin_token_of
from appmux.admin.schemas import AppStateResponse, ConfigChangeResult, UpdateConfigRequest
from appmux.authz.actions import Action
from appmux.reconciler.activate import ActivationRefused, plan_activation
from appmux.reconciler.app_control import AppControl
from appmux.registry.audit import AuditDetail, ChangedFields
from appmux.registry.models import AppConfig, EnvEntry
from appmux.registry.operations import ConfigEditRefused, desired_state_change, plan_config_edit
from appmux.registry.protocol import Change
from appmux.registry.snapshot import RegistrySnapshot
from appmux.registry.writers import WriteKind

# Release numbers are small positive integers; anything else is no release, whatever it is.
_RELEASE_NO_PATTERN: Final = re.compile(r"[1-9][0-9]{0,9}")
_RUNNABLE_STATUSES: Final = frozenset({"active", "pending"})
_OWN_SP_KEY_TYPE: Final = "secret"


def _stored_config(snapshot: RegistrySnapshot, slug: str) -> AppConfig:
    config = snapshot.app_config(slug)
    if config is None:
        raise NotFound("no such app")
    return config


def _require_something_to_run(snapshot: RegistrySnapshot, config: AppConfig) -> None:
    """``409 no_release`` unless an active release exists or the target is waiting to be built."""
    releases = [r for r in snapshot.releases_for(config.app_slug) if r.deleted_at is None]
    has_active = any(release.status == "active" for release in releases)
    has_target = any(
        release.release_no == config.target_release_no and release.status in _RUNNABLE_STATUSES
        for release in releases
    )
    if not (has_active or has_target):
        raise Conflict(
            "this app has no release that can run yet; press Update, or activate a release, "
            "then try again",
            code="no_release",
        )


def _require_attached_secrets(
    snapshot: RegistrySnapshot, slug: str, keys: dict[str, str | None]
) -> None:
    """``400`` naming each own-SP field whose key is not an attached ``secret`` resource."""
    attached = {resource.key: resource.type for resource in snapshot.resources_for(slug)}
    wrong = [
        field
        for field, key in keys.items()
        if key is not None and attached.get(key) != _OWN_SP_KEY_TYPE
    ]
    if wrong:
        raise BadRequest(
            f"the request is not valid: {', '.join(wrong)} must name an attached resource of "
            "type secret; attach it on the Resources panel first"
        )


def _config_detail(
    config: AppConfig, body: UpdateConfigRequest, release_no: int | None
) -> AuditDetail:
    """The audit detail of a config edit: the release it made, and the health check it changed."""
    is_path_changed = body.health_path != config.health_path
    is_deadline_changed = body.health_deadline_s != config.health_deadline_s
    has_health_change = is_path_changed or is_deadline_changed
    try:
        return AuditDetail(
            release=release_no,
            before=ChangedFields(
                health_path=config.health_path if is_path_changed else None,
                health_deadline_s=config.health_deadline_s if is_deadline_changed else None,
            )
            if has_health_change
            else None,
            after=ChangedFields(
                health_path=body.health_path if is_path_changed else None,
                health_deadline_s=body.health_deadline_s if is_deadline_changed else None,
            )
            if has_health_change
            else None,
        )
    except ValidationError as exc:
        raise validation_error(exc) from exc


class OperationRoutes:
    """The route provider of activate, the config edit, start, stop, restart and delete.

    ``control`` is the reconciler's side of the routes (``AppControl``): it is woken after a write,
    answers the state of an app and stops an app that is about to be deleted.
    """

    def __init__(self, control: AppControl) -> None:
        self._control = control

    def routes(self, context: AdminContext) -> Sequence[Route]:
        control = self._control

        async def activate(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.RELEASE_ACTIVATE, slug)
            raw_release_no = request.path_params["release_id"]
            if _RELEASE_NO_PATTERN.fullmatch(raw_release_no) is None:
                raise NotFound("no such release")
            release_no = int(raw_release_no)
            context.require_registry()

            actor = context.actor(request)
            try:
                activation = plan_activation(
                    context.registry.snapshot, slug, release_no, updated_by=actor.email
                )
            except ActivationRefused as refusal:
                if refusal.kind == "not_found":
                    raise NotFound(refusal.message) from refusal
                raise Conflict(refusal.message, code=refusal.code) from refusal

            entry = context.audit_entry(
                request, "release.activate", app_slug=slug, detail=AuditDetail(release=release_no)
            )
            await context.write(request, activation.changes, [entry])
            control.wake(slug)
            if activation.is_serving and control.run_state(slug, "running") == "crashed":
                control.request_restart(slug)

            snapshot = context.registry.snapshot
            stored = snapshot.release(slug, release_no) or activation.release
            app = snapshot.app(slug)
            if app is None:
                # Deleted or renamed by another request while this one waited on its write: the
                # activation is committed and audited, and the same request now would be a 404.
                raise NotFound("no such app")
            detail = build_release_detail(
                stored, is_target=True, memory_budget_mb=app.memory_budget_mb
            )
            return JSONResponse(detail.model_dump(mode="json"))

        async def put_config(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.CONFIG_EDIT, slug)
            body = await parse_body(request, UpdateConfigRequest)
            context.require_registry()

            snapshot = context.registry.snapshot
            config = _stored_config(snapshot, slug)
            context.require_version(config.version, body.version)
            _require_attached_secrets(
                snapshot,
                slug,
                {
                    "own_sp_client_id_key": body.own_sp_client_id_key,
                    "own_sp_client_secret_key": body.own_sp_client_secret_key,
                },
            )

            actor = context.actor(request)
            try:
                edit = plan_config_edit(
                    snapshot,
                    config,
                    env=[
                        EnvEntry(name=e.name, value=e.value, value_from=e.value_from)
                        for e in body.env
                    ],
                    wheel_sources=body.wheel_sources,
                    health_path=body.health_path,
                    health_deadline_s=body.health_deadline_s,
                    own_sp_client_id_key=body.own_sp_client_id_key,
                    own_sp_client_secret_key=body.own_sp_client_secret_key,
                    updated_by=actor.email,
                )
            except ConfigEditRefused as refusal:
                raise Conflict(refusal.message, code=refusal.code) from refusal

            entry = context.audit_entry(
                request,
                "config.edit",
                app_slug=slug,
                detail=_config_detail(config, body, edit.release_no),
            )
            rows = await context.write(request, edit.changes, [entry])
            stored = rows[-1]
            if not isinstance(stored, AppConfig):
                raise RuntimeError("the registry did not return the stored app settings")
            if edit.release_no is not None:
                control.wake(slug)
            result = ConfigChangeResult(config_version=stored.version, release_no=edit.release_no)
            return JSONResponse(result.model_dump(mode="json"))

        async def run_app(
            request: Request, audit_action: Literal["app.start", "app.restart"]
        ) -> Response:
            slug = request.path_params["app"]
            await context.require(
                request,
                Action.APP_START if audit_action == "app.start" else Action.APP_RESTART,
                slug,
            )
            context.require_registry()
            snapshot = context.registry.snapshot
            config = _stored_config(snapshot, slug)
            _require_something_to_run(snapshot, config)

            actor = context.actor(request)
            is_stopped = config.desired_state == "stopped"
            changes = (
                [desired_state_change(config, "running", updated_by=actor.email)]
                if is_stopped
                else []
            )
            entry = context.audit_entry(request, audit_action, app_slug=slug)
            await context.write(request, changes, [entry])
            if is_stopped:
                control.wake(slug)
            elif audit_action == "app.restart" or control.run_state(slug, "running") == "crashed":
                control.request_restart(slug)
            return _state_response(control, slug, "running")

        async def start(request: Request) -> Response:
            return await run_app(request, "app.start")

        async def restart(request: Request) -> Response:
            return await run_app(request, "app.restart")

        async def stop(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_STOP, slug)
            context.require_registry()
            config = _stored_config(context.registry.snapshot, slug)

            actor = context.actor(request)
            is_running = config.desired_state != "stopped"
            changes = (
                [desired_state_change(config, "stopped", updated_by=actor.email)]
                if is_running
                else []
            )
            entry = context.audit_entry(request, "app.stop", app_slug=slug)
            await context.write(request, changes, [entry])
            control.wake(slug)
            return _state_response(control, slug, "stopped")

        async def delete_app(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_DELETE, slug)
            token = admin_token_of(context.actor(request))
            context.require_registry()
            entry = context.audit_entry(request, "app.delete", app_slug=slug)

            # Stop first and write second: a failed write leaves a stopped app (a person can
            # start it), while the other order would leave a running child with no registration.
            await control.stop_app(slug)
            await context.write(
                request,
                [Change(WriteKind.APP_DELETE, key={"slug": slug})],
                [entry],
                admin_token=token,
            )
            control.wake(slug)
            return Response(status_code=204)

        return [
            Route("/apps/{app}/releases/{release_id}/activate", activate, methods=["POST"]),
            Route("/apps/{app}/config", put_config, methods=["PUT"]),
            Route("/apps/{app}/start", start, methods=["POST"]),
            Route("/apps/{app}/stop", stop, methods=["POST"]),
            Route("/apps/{app}/restart", restart, methods=["POST"]),
            Route("/apps/{app}", delete_app, methods=["DELETE"]),
        ]


def _state_response(
    control: AppControl, slug: str, desired_state: Literal["running", "stopped"]
) -> Response:
    state = AppStateResponse(
        state=control.run_state(slug, desired_state), desired_state=desired_state
    )
    return JSONResponse(state.model_dump(mode="json"))
