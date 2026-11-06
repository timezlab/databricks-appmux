"""The app routes: register, the app page, egress, rename and owner (T094, T066, T107).

``POST /apps`` (``app.register``, design 5.2 step 1) is for a platform admin only. It checks, in
order: identity and same-origin (the guard of ``api.py``); ``app.register`` (a denial is ``403``);
the body against ``RegisterAppRequest`` (``400`` naming the field); that the admin's own access
token arrived (``403 user_token_missing``: the platform fields are written under that token, never
the shared service principal's); then the ``apps`` row, its first ``app_config`` row and the
``app.register`` audit row in one transaction. A taken slug is ``409 slug_taken`` and nothing is
written. The app starts stopped with no release. The answer is the ``AppDetail`` of the new app.

``GET /apps/{app}`` (``app.view``, ``CAN_MANAGE``) answers the app page. An app the caller cannot
open looks exactly like a missing one (``AdminContext.require``).

Only a platform admin edits an app's egress list: an app admin sees it on the app page, read-only
(``egress.edit`` needs ``CAN_MANAGE`` on the platform, and the app's owner is not that). The rules
a request meets, in order:

1. identity and same-origin: the guard of ``api.py``;
2. ``egress.edit`` on the app: a person who cannot open the app gets the same ``404`` as for a
   missing one, a person who can open it but is not a platform admin gets ``403``;
3. the body against ``PutAppEgressBody`` (``version`` and a list of ``EgressRule``): ``400``;
4. the list against the closed targets and the container's real address (``EgressEditor``): ``400``
   naming the rule, or ``503`` while the address is unknown;
5. ``version`` against the app row: a stale one is ``409`` and nothing changes;
6. the write of ``apps.egress`` and its ``egress.edit`` audit row in one transaction;
7. the call that tells the app's pump to read the list (``503 egress_not_applied`` if it fails,
   with the list saved: saving it again retries).

The answer is the ``AppDetail`` after the write, so ``version`` is the new one. ``build_app_detail``
is the one place that turns registry rows into that shape, for the other routes that answer with it.
What it cannot fill yet is marked where it is filled.

``PATCH /apps/{app}`` (``app.edit_platform_fields``) and ``PUT /apps/{app}/owner``
(``app.set_owner``) are a platform admin's: the person who is not one gets ``403``, and the write
runs under the admin's own token like a registration. Both need the ``apps`` ``version`` that was
read. A field that already has the value sent is not a change: if nothing differs the answer is the
current page and nothing is written or recorded. A ``slug`` in the body is the rename (FR-010): a
taken slug is ``409 slug_taken``, and the two audit rows (``app.edit_platform_fields`` and
``app.rename``, ``{"from", "to"}``) are written under the new slug in the transaction of the
change. After the write ``on_renamed(old, new)`` tells the reconciler, which restarts a running
app under the new name through the swap and leaves a stopped one alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from pydantic import SecretStr, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext, parse_body
from appmux.admin.errors import (
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    Unavailable,
    validation_error,
)
from appmux.admin.identity import AppStateReader, state_from_desired
from appmux.admin.platform import EgressEditor
from appmux.admin.schemas import (
    AppConfigView,
    AppDetail,
    AppGrant,
    DelegationStatus,
    EnvEntry,
    PutAppEgressBody,
    RegisterAppRequest,
    ReleaseSummary,
    RequirementsResponse,
    ResourceView,
    Runtime,
    SetOwnerRequest,
    UpdateAppRequest,
)
from appmux.authz.actions import Action
from appmux.identity.actor import Actor
from appmux.reconciler.rename import SlugTaken, check_new_slug
from appmux.registry.apps import owner_change, platform_fields_change, register_app_changes
from appmux.registry.audit import AuditDetail, ChangedFields
from appmux.registry.errors import RowAlreadyExists
from appmux.registry.models import App, AppConfig, Release
from appmux.registry.models import EnvEntry as RegistryEnvEntry
from appmux.registry.platform import default_memory_budget_mb
from appmux.registry.protocol import Change
from appmux.registry.snapshot import RegistrySnapshot
from appmux.registry.writers import WriteKind
from appmux.settings import PlatformSettings

_SHARED_SP_DELEGATION_KEY = "shared_sp_delegation_allowed"
_Field = TypeVar("_Field", str, int)


def _runtime_of(release: Release | None) -> Runtime | None:
    detected = release.detected.runtime if release is not None else None
    if detected == "python":
        return "python"
    if detected == "node":
        return "node"
    return None


def release_summary(release: Release, *, is_target: bool) -> ReleaseSummary:
    return ReleaseSummary(
        release_no=release.release_no,
        status=release.status,
        status_reason=release.status_reason,
        status_at=release.status_at,
        name=release.label,
        tags=list(release.tags),
        is_target=is_target,
        locked_by=release.locked_by,
        locked_at=release.locked_at,
        created_by=release.created_by,
        created_at=release.created_at,
    )


def env_entries(entries: Iterable[RegistryEnvEntry]) -> list[EnvEntry]:
    """The contract's ``EnvEntry`` list for registry env entries (a release's or an app's)."""
    return [
        EnvEntry(name=entry.name, value=entry.value, value_from=entry.value_from)
        for entry in entries
    ]


def _shared_sp_delegation_allowed(snapshot: RegistrySnapshot) -> bool:
    row = snapshot.setting(_SHARED_SP_DELEGATION_KEY)
    if row is not None and isinstance(row.value, bool):
        return row.value
    return PlatformSettings().shared_sp_delegation_allowed


RestartFailureReader = Callable[[str], str | None]
"""Why the last restart of an app did not come up, by slug; ``None`` when nothing failed."""


def no_restart_failure(slug: str) -> str | None:
    """The reader when no reconciler runs: no copy was started, so none can have failed."""
    return None


def build_app_detail(
    snapshot: RegistrySnapshot,
    app: App,
    config: AppConfig,
    *,
    app_state: AppStateReader = state_from_desired,
    restart_failure: RestartFailureReader = no_restart_failure,
) -> AppDetail:
    """The contract's ``AppDetail`` for ``app``, from ``snapshot`` and the app's own rows.

    ``app`` and ``config`` are passed in so a route that has just written ``app`` shows the stored
    row, not whatever the snapshot held before it caught up. Two fields are not computed yet:
    ``restart_to_apply`` is ``False`` and ``requirements`` is empty, because the reconciler (which
    knows when the app last started) and the requirements panel (which reads the release's
    ``databricks.yml``) are later tasks. ``state`` comes from ``app_state``: by default the one the
    registry alone supports (``state_from_desired``), in the server the reconciler's.
    ``restart_failure`` comes from ``restart_failure``: in the server, why the copy the last
    Restart started beside the running one did not come up (TD-028); by default nothing failed.
    """
    live = sorted(
        (release for release in snapshot.releases_for(app.slug) if release.deleted_at is None),
        key=lambda release: release.release_no,
        reverse=True,
    )
    target = next(
        (release for release in live if release.release_no == config.target_release_no), None
    )
    active = next((release for release in live if release.status == "active"), None)
    return AppDetail(
        slug=app.slug,
        display_name=app.display_name,
        owner=app.owner,
        source_path=app.source_path,
        memory_budget_mb=app.memory_budget_mb,
        state=app_state(app.slug, config.desired_state),
        runtime=_runtime_of(active),
        restart_to_apply=False,
        restart_failure=restart_failure(app.slug),
        version=app.version,
        created_by=app.created_by,
        created_at=app.created_at,
        updated_by=app.updated_by,
        updated_at=app.updated_at,
        releases=[
            release_summary(release, is_target=release.release_no == config.target_release_no)
            for release in live
        ],
        requirements=RequirementsResponse(
            release_no=config.target_release_no, items=[], unreferenced_offers=[]
        ),
        resources=[
            ResourceView(
                key=resource.key,
                type=resource.type,
                target=resource.target,
                permission=resource.permission,
                access_as=resource.access_as,
                check_result=resource.check_result,
                attached_by=resource.attached_by,
                attached_at=resource.attached_at,
            )
            for resource in snapshot.resources_for(app.slug)
        ],
        config=AppConfigView(
            desired_state=config.desired_state,
            target_release_no=config.target_release_no,
            env=env_entries(target.config.env if target is not None else ()),
            wheel_sources=list(target.config.wheel_sources) if target is not None else [],
            health_path=config.health_path,
            health_deadline_s=config.health_deadline_s,
            own_sp_client_id_key=config.own_sp_client_id_key,
            own_sp_client_secret_key=config.own_sp_client_secret_key,
            version=config.version,
            updated_by=config.updated_by,
            updated_at=config.updated_at,
        ),
        delegation=DelegationStatus(
            obo_delegated=app.obo_delegated,
            shared_sp_delegated=app.shared_sp_delegated,
            shared_sp_delegation_allowed=_shared_sp_delegation_allowed(snapshot),
        ),
        egress=list(app.egress),
        grants=[
            AppGrant(
                type=grant.principal_type,
                name=grant.principal,
                permission=grant.permission,
                granted_by=grant.granted_by,
                granted_at=grant.granted_at,
            )
            for grant in snapshot.grants_for(app.slug)
        ],
    )


def _stored_app(snapshot: RegistrySnapshot, slug: str) -> tuple[App, AppConfig]:
    """The app and its settings row, as the snapshot holds them now.

    ``AdminContext.require`` found the app a moment ago, so a missing one means the snapshot moved
    on (``404``). An app with no ``app_config`` row is a registry defect that the person cannot fix
    (``503``): the page cannot be built without it.
    """
    app, config = snapshot.app(slug), snapshot.app_config(slug)
    if app is None:
        raise NotFound("no such app")
    if config is None:
        raise Unavailable(
            "this app's settings cannot be read right now; ask a platform admin to check "
            "the registry",
            code="app_config_missing",
        )
    return app, config


def _register_detail(body: RegisterAppRequest, memory_budget_mb: int) -> AuditDetail:
    """The audit detail of a registration (data model 4.9): the owner and the budget it got.

    The audit model is stricter about an email than the request schema, so a refusal here is
    reported against ``owner``, a field the caller sent. The message never repeats the value.
    """
    try:
        return AuditDetail(owner=body.owner, budget_mb=memory_budget_mb)
    except ValidationError as exc:
        raise BadRequest(
            "the request is not valid: owner is not valid (a plain lower-case address such as "
            "name@example.com)"
        ) from exc


def _admin_token(actor: Actor, *, doing: str, then: str) -> SecretStr:
    """The platform admin's own access token, which the platform fields are written under.

    Never the shared service principal's: the registry gives these writes to the admin's role.
    """
    if actor.visitor_token is None:
        raise Forbidden(
            f"appmux did not receive your Databricks access token, so it cannot {doing} as you; "
            f"ask a platform admin to turn on user authorization for the appmux App, then {then}",
            code="user_token_missing",
        )
    return actor.visitor_token


def _if_different(wanted: _Field | None, stored: _Field) -> _Field | None:
    """``wanted`` when it is given and is not what is stored; otherwise ``None`` (no change)."""
    return wanted if wanted is not None and wanted != stored else None


def _platform_fields_detail(
    app: App,
    *,
    slug: str | None,
    display_name: str | None,
    source_path: str | None,
    memory_budget_mb: int | None,
) -> AuditDetail:
    """The before and after of the fields a PATCH changes (only those; data model 4.9)."""
    try:
        return AuditDetail(
            before=ChangedFields(
                slug=app.slug if slug is not None else None,
                display_name=app.display_name if display_name is not None else None,
                source_path=app.source_path if source_path is not None else None,
                memory_budget_mb=app.memory_budget_mb if memory_budget_mb is not None else None,
            ),
            after=ChangedFields(
                slug=slug,
                display_name=display_name,
                source_path=source_path,
                memory_budget_mb=memory_budget_mb,
            ),
        )
    except ValidationError as exc:
        raise validation_error(exc) from exc


def _detail_or_bad_request(**fields: object) -> AuditDetail:
    """An ``AuditDetail`` of fields the caller sent; a refusal names the field, not the value."""
    try:
        return AuditDetail.model_validate(fields)
    except ValidationError as exc:
        raise validation_error(exc) from exc


class AppRoutes:
    """The route provider of the app routes: register, the app page, egress, rename and owner.

    ``egress`` is shared with ``PlatformRoutes``; the default has no closed targets, so the route
    answers ``503`` until a real one is given. ``app_state`` answers ``AppDetail.state`` and
    ``restart_failure`` answers ``AppDetail.restart_failure`` (the reconciler's in the server).
    ``on_renamed`` receives the old and the new slug after a rename
    was written (the server wires it to ``Reconciler.app_renamed``); by default nobody is told.
    """

    def __init__(
        self,
        *,
        egress: EgressEditor | None = None,
        app_state: AppStateReader = state_from_desired,
        restart_failure: RestartFailureReader = no_restart_failure,
        on_renamed: Callable[[str, str], None] | None = None,
    ) -> None:
        self._egress = egress if egress is not None else EgressEditor()
        self._app_state = app_state
        self._restart_failure = restart_failure
        self._on_renamed = on_renamed

    def _detail(self, snapshot: RegistrySnapshot, app: App, config: AppConfig) -> AppDetail:
        return build_app_detail(
            snapshot,
            app,
            config,
            app_state=self._app_state,
            restart_failure=self._restart_failure,
        )

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def register(request: Request) -> Response:
            await context.require(request, Action.APP_REGISTER)
            body = await parse_body(request, RegisterAppRequest)
            actor = context.actor(request)
            admin_token = _admin_token(actor, doing="save the app", then="open this page again")
            # Left out, the budget is the platform default as the snapshot holds it now.
            memory_budget_mb = (
                body.memory_budget_mb
                if body.memory_budget_mb is not None
                else default_memory_budget_mb(context.registry.snapshot)
            )

            changes = register_app_changes(
                slug=body.slug,
                display_name=body.display_name,
                owner=body.owner,
                source_path=body.source_path,
                memory_budget_mb=memory_budget_mb,
                registered_by=actor.email,
            )
            entry = context.audit_entry(
                request,
                "app.register",
                app_slug=body.slug,
                detail=_register_detail(body, memory_budget_mb),
            )
            try:
                rows = await context.write(request, changes, [entry], admin_token=admin_token)
            except RowAlreadyExists as exc:
                raise Conflict(f"slug '{body.slug}' is already taken", code="slug_taken") from exc
            app, config = rows
            if not isinstance(app, App) or not isinstance(config, AppConfig):
                raise RuntimeError("the registry did not return the stored app")
            detail = self._detail(context.registry.snapshot, app, config)
            return JSONResponse(detail.model_dump(mode="json"))

        async def get_app(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_VIEW, slug)
            snapshot = context.registry.snapshot
            app, config = _stored_app(snapshot, slug)
            detail = self._detail(snapshot, app, config)
            return JSONResponse(detail.model_dump(mode="json"))

        async def put_egress(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.EGRESS_EDIT, slug)
            body = await parse_body(request, PutAppEgressBody)
            stored = self._egress.checked(body.rules)

            app, config = _stored_app(context.registry.snapshot, slug)
            context.require_version(app.version, body.version)

            actor = context.actor(request)
            change = Change(
                WriteKind.APP_UPDATE,
                key={"slug": slug},
                values={"egress": stored, "updated_by": actor.email},
                expected_version=body.version,
            )
            entry = context.audit_entry(request, "egress.edit", app_slug=slug, detail=AuditDetail())
            (row,) = await context.write(
                request, [change], [entry], admin_token=actor.visitor_token
            )
            if not isinstance(row, App):
                raise RuntimeError("the registry did not return the stored app")
            await self._egress.apply(slug)
            detail = self._detail(context.registry.snapshot, row, config)
            return JSONResponse(detail.model_dump(mode="json"))

        async def patch_app(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_EDIT_PLATFORM_FIELDS, slug)
            body = await parse_body(request, UpdateAppRequest)
            admin_token = _admin_token(
                context.actor(request), doing="save the change", then="try again"
            )
            snapshot = context.registry.snapshot
            app, config = _stored_app(snapshot, slug)
            context.require_version(app.version, body.version)

            new_slug = _if_different(body.slug, app.slug)
            display_name = _if_different(body.display_name, app.display_name)
            source_path = _if_different(body.source_path, app.source_path)
            memory_budget_mb = _if_different(body.memory_budget_mb, app.memory_budget_mb)
            if (new_slug, display_name, source_path, memory_budget_mb) == (None,) * 4:
                context.skip_audit(request, "every field sent already has that value")
                detail = self._detail(snapshot, app, config)
                return JSONResponse(detail.model_dump(mode="json"))
            if new_slug is not None:
                try:
                    check_new_slug(snapshot, slug, new_slug)
                except SlugTaken as exc:
                    raise Conflict(
                        f"slug '{new_slug}' is already taken", code="slug_taken"
                    ) from exc

            actor = context.actor(request)
            change = platform_fields_change(
                app,
                updated_by=actor.email,
                slug=new_slug,
                display_name=display_name,
                source_path=source_path,
                memory_budget_mb=memory_budget_mb,
            )
            # History follows the app: both rows are filed under the slug it has from now on.
            filed_under = new_slug if new_slug is not None else slug
            entries = [
                context.audit_entry(
                    request,
                    "app.edit_platform_fields",
                    app_slug=filed_under,
                    detail=_platform_fields_detail(
                        app,
                        slug=new_slug,
                        display_name=display_name,
                        source_path=source_path,
                        memory_budget_mb=memory_budget_mb,
                    ),
                )
            ]
            if new_slug is not None:
                entries.append(
                    context.audit_entry(
                        request,
                        "app.rename",
                        app_slug=filed_under,
                        detail=_detail_or_bad_request(**{"from": slug, "to": new_slug}),
                    )
                )
            try:
                (row,) = await context.write(request, [change], entries, admin_token=admin_token)
            except RowAlreadyExists as exc:
                if new_slug is None:
                    raise
                raise Conflict(f"slug '{new_slug}' is already taken", code="slug_taken") from exc
            if not isinstance(row, App):
                raise RuntimeError("the registry did not return the stored app")
            if new_slug is not None and self._on_renamed is not None:
                self._on_renamed(slug, new_slug)

            snapshot = context.registry.snapshot
            stored_config = snapshot.app_config(row.slug) or config.model_copy(
                update={"app_slug": row.slug}
            )
            detail = self._detail(snapshot, row, stored_config)
            return JSONResponse(detail.model_dump(mode="json"))

        async def put_owner(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_SET_OWNER, slug)
            body = await parse_body(request, SetOwnerRequest)
            admin_token = _admin_token(
                context.actor(request), doing="save the change", then="try again"
            )
            snapshot = context.registry.snapshot
            app, config = _stored_app(snapshot, slug)
            context.require_version(app.version, body.version)
            if body.owner == app.owner:
                context.skip_audit(request, "the app already has that owner")
                detail = self._detail(snapshot, app, config)
                return JSONResponse(detail.model_dump(mode="json"))

            actor = context.actor(request)
            entry = context.audit_entry(
                request,
                "app.set_owner",
                app_slug=slug,
                detail=_detail_or_bad_request(owner=body.owner),
            )
            (row,) = await context.write(
                request,
                [owner_change(app, body.owner, updated_by=actor.email)],
                [entry],
                admin_token=admin_token,
            )
            if not isinstance(row, App):
                raise RuntimeError("the registry did not return the stored app")
            detail = self._detail(context.registry.snapshot, row, config)
            return JSONResponse(detail.model_dump(mode="json"))

        return [
            Route("/apps", register, methods=["POST"]),
            Route("/apps/{app}", get_app, methods=["GET"]),
            Route("/apps/{app}", patch_app, methods=["PATCH"]),
            Route("/apps/{app}/owner", put_owner, methods=["PUT"]),
            Route("/apps/{app}/egress", put_egress, methods=["PUT"]),
        ]
