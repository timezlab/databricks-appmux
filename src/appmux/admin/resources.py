"""Resources of an app: the requirements panel, attach, detach, and the attach check (T127).

Three routes (contracts/admin-api.md):

* ``GET /apps/{app}/requirements`` (``app.view``): the keys the target release needs
  (``value_from`` of its manifest and of its config), whether each is attached, the offer a
  ``databricks.yml`` made for it, and the offers nothing refers to.
* ``PUT /apps/{app}/resources/{key}`` (``resource.attach``): runs the attach check with the token
  of the person who attaches, then stores the resource and who attached it. A secret is stored as
  a ``scope/key`` reference; its value never passes through appmux here.
* ``DELETE /apps/{app}/resources/{key}`` (``resource.detach``): removes it.

The rules of an attach live in ``registry/resources.py`` (``verify_attach``); the route only maps
them to answers: a refusal is ``400`` with a stable ``code`` and nothing is written, a check that
could not run is ``503`` ``check_unavailable`` (it is never taken as a pass), a key that is the
app's own service principal key is ``409`` ``in_use`` when its resource would go or stop being a
secret.

``DatabricksAttachChecker`` is the adapter that asks Databricks. The calls are those measured in
spike 16 (2026-10-30, Free Edition workspace only); how an answer is read is proven against a
stubbed transport, not against a live workspace. It sends only to the base URL of the client it
was given (the workspace host from settings, never a URL from a request), carries the attacher's
token in the one header that needs it, follows no redirect, and shows the token nowhere: not in a
message, not in a log, not in an exception.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import quote

import httpx
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext, parse_body
from appmux.admin.errors import BadRequest, Conflict, NotFound, Unavailable
from appmux.admin.schemas import (
    AttachResourceRequest,
    BundleResource,
    Requirement,
    RequirementsResponse,
    ResourceView,
)
from appmux.appyaml.requirements import required_keys, suggest_resource_type
from appmux.authz.actions import Action
from appmux.registry.audit import AuditDetail
from appmux.registry.models import App, AppConfig, AppResource
from appmux.registry.resources import (
    RESOURCE_TYPES,
    SECRET_TYPE,
    VERIFIABLE_TYPES,
    AttachChecker,
    AttachCheckUnavailable,
    AttachProbe,
    AttachRefused,
    CheckVerdict,
    check_warehouse_id,
    delete_resource_change,
    is_resource_key,
    is_valid_target,
    own_sp_key_in_use,
    put_resource_change,
    shared_sp_delegation_allowed,
    verify_attach,
)
from appmux.registry.snapshot import RegistrySnapshot

STATEMENTS_PATH: Final = "/api/2.0/sql/statements"
# Databricks takes 5 to 50 seconds here; CANCEL stops a statement that outlives the wait, so a
# check never leaves a query running on someone's warehouse.
STATEMENT_WAIT: Final = "50s"
_REST_TIMEOUT_S: Final = 15.0
_STATEMENT_TIMEOUT_S: Final = 60.0  # the 50 s wait plus room for the answer
_MAX_READ_CHARS: Final = 4096
_BUNDLE_SOURCE: Final = "databricks.yml"

CHECK_UNAVAILABLE_MESSAGE: Final = (
    "Databricks did not answer the access check, so nothing was attached; try again in a moment"
)

# Resource types checked by one GET: the path in front of the (quoted) target.
_REST_PATHS: Final = {
    "serving_endpoint": "/api/2.0/serving-endpoints/",
    "uc_table": "/api/2.1/unity-catalog/tables/",
    "uc_connection": "/api/2.1/unity-catalog/connections/",
    "genie_space": "/api/2.0/genie/spaces/",
}
_VOLUME_PATH: Final = "/api/2.0/fs/directories"


class DatabricksAttachChecker:
    """The ``AttachChecker`` that asks the workspace, with the attacher's own token."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def check(self, probe: AttachProbe) -> CheckVerdict:
        # Both are caller defects: ``verify_attach`` answers a type with no check by itself and
        # refuses a target of the wrong form before it calls the adapter.
        if probe.resource_type not in VERIFIABLE_TYPES:
            raise ValueError("no attach check exists for this resource type")
        if not is_valid_target(probe.resource_type, probe.target):
            raise ValueError("the target is not of the form its check needs")
        match probe.resource_type:
            case "secret":
                return await self._secret(probe)
            case "sql_warehouse":
                return await self._statement(probe, probe.target, "SELECT 1", own_warehouse=True)
            case "uc_function":
                return await self._function(probe)
            case "uc_volume":
                return await self._read(probe, f"{_VOLUME_PATH}{quote(probe.target, safe='/')}")
            case "serving_endpoint":
                return await self._read(
                    probe, _REST_PATHS["serving_endpoint"] + quote(probe.target, safe="")
                )
            case "uc_table" | "uc_connection" | "genie_space":
                return await self._read(
                    probe, _REST_PATHS[probe.resource_type] + quote(probe.target, safe="")
                )
            case _:  # a verifiable type this match does not list: the catalogue grew
                raise ValueError("no attach check exists for this resource type")

    # --- the statement checks: the answer is in ``status.state``, whatever the HTTP status says

    async def _secret(self, probe: AttachProbe) -> CheckVerdict:
        scope, key = probe.target.split("/", 1)
        # The charset of a secret target (``is_valid_target``) has no quote, so the literals are
        # plain; the statement asks for the length of the value, never the value.
        statement = f"SELECT length(secret('{scope}', '{key}'))"
        return await self._statement(probe, probe.warehouse_id, statement, own_warehouse=False)

    async def _function(self, probe: AttachProbe) -> CheckVerdict:
        catalog, schema, name = probe.target.split(".")
        statement = f"DESCRIBE FUNCTION `{catalog}`.`{schema}`.`{name}`"
        return await self._statement(probe, probe.warehouse_id, statement, own_warehouse=False)

    async def _statement(
        self, probe: AttachProbe, warehouse_id: str | None, statement: str, *, own_warehouse: bool
    ) -> CheckVerdict:
        if warehouse_id is None:
            raise AttachRefused(
                "check_warehouse_unset",
                "the attach_check_warehouse_id setting is not set, so this access cannot be "
                "checked; ask a platform admin to set it",
            )
        response = await self._send(
            probe.token.get_secret_value(),
            "POST",
            STATEMENTS_PATH,
            timeout_s=_STATEMENT_TIMEOUT_S,
            json={
                "warehouse_id": warehouse_id,
                "statement": statement,
                "wait_timeout": STATEMENT_WAIT,
                "on_wait_timeout": "CANCEL",
            },
        )
        if response.status_code in (400, 403, 404):
            if names_a_missing_scope(response):
                return CheckVerdict.UNVERIFIED
            if own_warehouse:
                return CheckVerdict.NO_ACCESS
            raise AttachRefused(
                "check_warehouse_denied",
                "you cannot use the SQL warehouse of the attach_check_warehouse_id setting, so "
                "your access cannot be checked; ask a platform admin for CAN_USE on it",
            )
        if response.status_code != 200:
            raise AttachCheckUnavailable(CHECK_UNAVAILABLE_MESSAGE)
        state = _statement_state(response)
        if state == "SUCCEEDED":
            return CheckVerdict.OK
        if state == "FAILED":
            return CheckVerdict.NO_ACCESS
        # PENDING, RUNNING, CANCELED, CLOSED, or an answer with no state: nothing is known.
        raise AttachCheckUnavailable(CHECK_UNAVAILABLE_MESSAGE)

    # --- the read checks: a 200 proves the attacher reaches the object

    async def _read(self, probe: AttachProbe, path: str) -> CheckVerdict:
        response = await self._send(
            probe.token.get_secret_value(), "GET", path, timeout_s=_REST_TIMEOUT_S
        )
        if response.status_code == 200:
            if probe.resource_type == "serving_endpoint":
                return _serving_endpoint_verdict(response, probe.permission)
            return CheckVerdict.OK
        if response.status_code in (400, 403, 404):
            return (
                CheckVerdict.UNVERIFIED
                if names_a_missing_scope(response)
                else (CheckVerdict.NO_ACCESS)
            )
        raise AttachCheckUnavailable(CHECK_UNAVAILABLE_MESSAGE)

    async def _send(
        self,
        token: str,
        method: str,
        path: str,
        *,
        timeout_s: float,
        json: dict[str, str] | None = None,
    ) -> httpx.Response:
        response = await send_with_token(
            self._client, token, method, path, timeout_s=timeout_s, json=json
        )
        if response is None:
            raise AttachCheckUnavailable(CHECK_UNAVAILABLE_MESSAGE)
        return response


async def send_with_token(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    path: str,
    *,
    timeout_s: float,
    json: Mapping[str, object] | None = None,
    params: Mapping[str, str] | None = None,
) -> httpx.Response | None:
    """One request to the workspace made with a person's token, or ``None`` if it got no answer.

    The one place that sends a person's token to Databricks from the admin API (the attach check
    and the resource suggestions both use it): only the token in the one header that needs it,
    whatever other credential the client carries, no redirect followed, and ``None`` instead of an
    exception from inside ``except`` because the transport error's text is not ours to vouch for
    and this request carried a bearer token, so it must not stay behind as the new error's
    ``__context__``.
    """
    try:
        return await client.request(
            method,
            path,
            json=json,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            auth=None,
            follow_redirects=False,
            timeout=timeout_s,
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        return None


def names_a_missing_scope(response: httpx.Response) -> bool:
    """Whether a refusal says the App lacks the OAuth scope, rather than that access is denied."""
    text = response.text[:_MAX_READ_CHARS].lower()
    return "required scope" in text or "invalid scope" in text


def _statement_state(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    status = body.get("status") if isinstance(body, dict) else None
    state = status.get("state") if isinstance(status, dict) else None
    return state if isinstance(state, str) else None


def _serving_endpoint_verdict(response: httpx.Response, requested: str) -> CheckVerdict:
    """The answer names the level the caller holds; compare it with the one asked for."""
    try:
        body = response.json()
    except ValueError:
        return CheckVerdict.OK
    held = body.get("permission_level") if isinstance(body, dict) else None
    if not isinstance(held, str):
        return CheckVerdict.OK  # no level reported: reach is all that was proven
    levels = RESOURCE_TYPES["serving_endpoint"].levels
    if held not in levels:
        return CheckVerdict.UNVERIFIED
    return (
        CheckVerdict.OK if levels.index(held) >= levels.index(requested) else CheckVerdict.NO_ACCESS
    )


# --- views -----------------------------------------------------------------------------------


def resource_view(resource: AppResource) -> ResourceView:
    """The contract's ``ResourceView`` of a stored resource (a secret as a reference)."""
    return ResourceView(
        key=resource.key,
        type=resource.type,
        target=resource.target,
        permission=resource.permission,
        access_as=resource.access_as,
        check_result=resource.check_result,
        attached_by=resource.attached_by,
        attached_at=resource.attached_at,
    )


def build_requirements(snapshot: RegistrySnapshot, slug: str) -> RequirementsResponse:
    """The requirements panel of an app: the target release's keys against the app's resources.

    ``release_no`` is the target release, or ``None`` before the first Update (or when the target
    was deleted, which leaves nothing to read keys from).
    """
    config = snapshot.app_config(slug)
    release = None
    if config is not None and config.target_release_no is not None:
        candidate = snapshot.release(slug, config.target_release_no)
        if candidate is not None and candidate.deleted_at is None:
            release = candidate
    if release is None:
        return RequirementsResponse(release_no=None, items=[], unreferenced_offers=[])

    attached = {resource.key for resource in snapshot.resources_for(slug)}
    offers: dict[str, BundleResource] = {}
    for stored_offer in release.detected.bundle_resources:
        offers.setdefault(
            stored_offer.key,
            BundleResource(
                key=stored_offer.key,
                type=stored_offer.type,
                target=stored_offer.target,
                permission=stored_offer.permission,
            ),
        )
    required = required_keys(release.detected.env, release.config.env)

    items = []
    for key in required:
        is_attached = key in attached
        offer = offers.get(key)
        items.append(
            Requirement(
                key=key,
                state="attached" if is_attached else "missing",
                offer=offer,
                suggested_type=(
                    None if is_attached or offer is not None else suggest_resource_type(key)
                ),
            )
        )
    wanted = set(required)
    return RequirementsResponse(
        release_no=release.release_no,
        items=items,
        unreferenced_offers=[
            offer for key, offer in offers.items() if key not in wanted and key not in attached
        ],
    )


# --- routes ------------------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _stored_app(snapshot: RegistrySnapshot, slug: str) -> tuple[App, AppConfig]:
    # `require` found the app a moment ago; a snapshot that moved on can have dropped it.
    app = snapshot.app(slug)
    config = snapshot.app_config(slug)
    if app is None or config is None:
        raise NotFound("no such app")
    return app, config


def _detail(key: str, body: AttachResourceRequest, check: str) -> AuditDetail:
    """The audit detail of an attach: no target but a secret's reference, never a value.

    The caller controls ``key`` and the secret's target, so a refusal by the audit model (a key
    shaped like a credential, say) is a ``400`` that names no value.
    """
    fields: dict[str, Any] = {"key": key, "type": body.type, "check": check}
    if body.type == SECRET_TYPE:
        fields["target"] = body.target
    else:
        fields["access_as"] = body.access_as
    if body.from_bundle:
        fields["from"] = _BUNDLE_SOURCE
    try:
        return AuditDetail.model_validate(fields)
    except ValidationError as exc:
        raise BadRequest(
            "the request is not valid: the key or the target cannot be recorded "
            "(no control characters, nothing shaped like a credential)"
        ) from exc


class ResourceRoutes:
    """The route provider of the requirements, attach and detach routes."""

    def __init__(self, *, checker: AttachChecker, clock: Callable[[], datetime] = _utc_now) -> None:
        self._checker = checker
        self._clock = clock

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def get_requirements(request: Request) -> Response:
            slug = request.path_params["app"]
            await context.require(request, Action.APP_VIEW, slug)
            snapshot = context.registry.snapshot
            _stored_app(snapshot, slug)
            return JSONResponse(build_requirements(snapshot, slug).model_dump(mode="json"))

        async def put_resource(request: Request) -> Response:
            slug = request.path_params["app"]
            key = request.path_params["key"]
            await context.require(request, Action.RESOURCE_ATTACH, slug)
            if not is_resource_key(key):
                raise BadRequest(
                    "the request is not valid: key in the path is 1 to 63 lower-case letters, "
                    "digits, '.', '_' or '-', starting with a letter or a digit"
                )
            body = await parse_body(request, AttachResourceRequest)
            snapshot = context.registry.snapshot
            app, config = _stored_app(snapshot, slug)
            existing = snapshot.resource(slug, key)
            if existing is not None and own_sp_key_in_use(config, key) and body.type != SECRET_TYPE:
                raise Conflict(
                    "this resource holds the own service principal of the app, so it stays a "
                    "secret; clear the own service principal keys in the configuration first",
                    code="in_use",
                )
            _detail(key, body, "unverified")  # a key the audit log cannot hold is refused at once

            actor = context.actor(request)
            try:
                check = await verify_attach(
                    resource_type=body.type,
                    target=body.target,
                    permission=body.permission,
                    access_as=body.access_as,
                    app=app,
                    config=config,
                    resources=snapshot.resources_for(slug),
                    shared_sp_allowed=shared_sp_delegation_allowed(snapshot),
                    warehouse_id=check_warehouse_id(snapshot),
                    token=actor.visitor_token,
                    checker=self._checker,
                )
            except AttachRefused as exc:
                raise BadRequest(exc.message, code=exc.code) from exc
            except AttachCheckUnavailable as exc:
                raise Unavailable(str(exc), code="check_unavailable") from exc

            change = put_resource_change(
                existing,
                app_slug=slug,
                key=key,
                type_name=body.type,
                target=body.target,
                permission=body.permission,
                access_as=body.access_as,
                check_result=check,
                attached_by=actor.email,
                attached_at=self._clock(),
            )
            entry = context.audit_entry(
                request, "resource.attach", app_slug=slug, detail=_detail(key, body, check)
            )
            (stored,) = await context.write(request, [change], [entry])
            if not isinstance(stored, AppResource):
                raise RuntimeError("the registry did not return the stored resource")
            return JSONResponse(resource_view(stored).model_dump(mode="json"))

        async def delete_resource(request: Request) -> Response:
            slug = request.path_params["app"]
            key = request.path_params["key"]
            await context.require(request, Action.RESOURCE_DETACH, slug)
            snapshot = context.registry.snapshot
            _, config = _stored_app(snapshot, slug)
            existing = snapshot.resource(slug, key) if is_resource_key(key) else None
            if existing is None:
                raise NotFound("this app has no resource under that key", code="no_such_resource")
            if own_sp_key_in_use(config, key):
                raise Conflict(
                    "this resource holds the own service principal of the app; clear the own "
                    "service principal keys in the configuration first",
                    code="in_use",
                )
            entry = context.audit_entry(
                request,
                "resource.detach",
                app_slug=slug,
                detail=AuditDetail(key=existing.key, type=existing.type),
            )
            await context.write(request, [delete_resource_change(existing)], [entry])
            return Response(status_code=204)

        return [
            Route("/apps/{app}/requirements", get_requirements, methods=["GET"]),
            Route("/apps/{app}/resources/{key}", put_resource, methods=["PUT"]),
            Route("/apps/{app}/resources/{key}", delete_resource, methods=["DELETE"]),
        ]


__all__ = [
    "CHECK_UNAVAILABLE_MESSAGE",
    "DatabricksAttachChecker",
    "ResourceRoutes",
    "build_requirements",
    "resource_view",
]
