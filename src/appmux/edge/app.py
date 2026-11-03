"""The top-level application the gate opens to (design 3.4, contracts/edge-http.md).

``/_appmux/api/v1`` goes to the admin API (``admin.api.create_admin_app`` with the route providers
that exist: identity, grants, the platform routes, the app and egress routes, the request inbox,
delegation, audit and metrics, and, for each part the start-up gives, the control, release, resource
source-viewer and folder-browser routes), ``/`` and ``/_appmux/...``
go to the UI application when one is given (``edge.ui_static``), and every other path goes to the
request pipeline (``edge.pipeline``): ``/<slug>/...`` is a request for a child app. The pipeline
exists only when the caller gives a route table; without one those paths answer ``404``, which is
for code that builds the edge alone.

The self-check gate (``SelfCheckGate``) wraps the whole application. While the latest self-check is
not a pass (design 6.4, FR-013) every request answers ``503`` with a status page that names the
failed checks, except the ones for ``/_appmux/``, so a platform admin can still read the result and
run it again (``POST /platform/self-check``). It is separate from the startup gate (``gate.py``)
because that gate never reopens and answers ``503`` on ``/_appmux/`` too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Final

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount
from starlette.types import ASGIApp

from appmux.admin.api import RouteProvider, create_admin_app
from appmux.admin.apps import AppRoutes
from appmux.admin.authorizer import GroupSource, make_authorizer
from appmux.admin.delegation import DelegationRoutes
from appmux.admin.grants import GrantRoutes
from appmux.admin.identity import AppStateReader, IdentityRoutes, state_from_desired
from appmux.admin.metrics import MetricsRoutes
from appmux.admin.operations import OperationRoutes
from appmux.admin.platform import (
    ClosedTargetsSource,
    EgressEditor,
    EgressReloader,
    MemorySource,
    PlatformRoutes,
)
from appmux.admin.releases import ReleaseRoutes
from appmux.admin.requests import RenameCascade, RequestRoutes
from appmux.admin.resources import ResourceRoutes
from appmux.admin.source_viewer import SnapshotStore, SourceViewerRoutes
from appmux.admin.workspace_browse import WorkspaceBrowseRoutes
from appmux.edge.connections import OpenConnections
from appmux.edge.gate import ASGIApplication, Receive, Scope, Send
from appmux.edge.open_mode import OpenModeCookies, OpenModeTokenPolicy
from appmux.edge.pipeline import ForwardTokenPolicy, MetricsSink, RequestPipeline, RouteLookup
from appmux.edge.status_pages import (
    SELF_CHECK_RETRY_AFTER_S,
    UNAVAILABLE_STATUS,
    WEBSOCKET_TRY_AGAIN_LATER,
    self_check_failed_page,
    status_page_headers,
)
from appmux.edge.ui_static import ui_routes
from appmux.packages.store import WorkspaceHttpApi
from appmux.reconciler.app_control import AppControl
from appmux.reconciler.ports import ReleaseUpdater
from appmux.registry.protocol import Registry
from appmux.registry.resources import AttachChecker
from appmux.sandbox.self_check import SelfCheckState
from appmux.upstream.client import UpstreamClient

if TYPE_CHECKING:
    import httpx

ADMIN_API_MOUNT: Final = "/_appmux/api/v1"
ADMIN_PATH_ROOT: Final = "/_appmux"
_NOT_FOUND_HEADERS: Final = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
# A percent-encoded dot, slash or backslash in the raw path: a later layer may decode it again into
# a dot segment or a separator.
_ENCODED_PATH_TRICKS: Final = (b"%2e", b"%2f", b"%5c")


def _not_found(request: Request, exc: Exception) -> Response:
    # Short text, no path echoed (contracts/edge-http.md).
    body = "" if request.method == "HEAD" else "not found"
    return PlainTextResponse(body, status_code=404, headers=_NOT_FOUND_HEADERS)


def is_admin_path(scope: Scope) -> bool:
    """Whether the request is for ``/_appmux`` or a path under ``/_appmux/``, exactly as written.

    The self-check gate lets these through, so the rule is strict: a path that a later layer could
    read as something else after normalising it, and so reach an app without the self-check
    having passed, is not an admin path. Not admin paths: a different case, a dot or empty segment
    (``/_appmux/../x``, ``/_appmux//x``), a backslash or control character, and a raw path with an
    encoded dot, slash or backslash. An encoded character of another kind (``%40``) is fine.
    """
    path = scope["path"]
    if path != ADMIN_PATH_ROOT and not path.startswith(f"{ADMIN_PATH_ROOT}/"):
        return False
    if "\\" in path or not path.isprintable():
        return False
    # One trailing slash is the path of the UI's root; any other empty segment is a `//`.
    segments = path.removesuffix("/")[1:].split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return False
    raw_path = scope.get("raw_path")
    return not (
        isinstance(raw_path, bytes)
        and any(trick in raw_path.lower() for trick in _ENCODED_PATH_TRICKS)
    )


def is_edge_own_path(path: str) -> bool:
    """Whether ``path`` is the launcher (``/``) or the edge's own URL space (``/_appmux...``).

    It reads the path as written, with no normalising, and a path that is not exactly the edge's own
    goes to the pipeline, which resolves it and refuses what it cannot judge. That keeps the two
    sets apart: nothing the self-check gate lets through as an admin path can reach an app, because
    ``is_admin_path`` accepts only a path that starts with ``/_appmux`` as written.
    """
    return path in ("", "/", ADMIN_PATH_ROOT) or path.startswith(f"{ADMIN_PATH_ROOT}/")


class EdgeRouter:
    """The launcher and ``/_appmux...`` go to the base application; every other path to an app."""

    def __init__(self, base: ASGIApplication, pipeline: ASGIApplication) -> None:
        self._base = base
        self._pipeline = pipeline

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and not is_edge_own_path(scope["path"]):
            await self._pipeline(scope, receive, send)
        else:
            await self._base(scope, receive, send)


class SelfCheckGate:
    """Answers ``503`` with the failed checks while the self-check is not a pass; else passes on.

    The state is read on every request, so a run of ``POST /platform/self-check`` takes effect on
    the next one. Before the first run has finished nothing is passing, which closes the gate.
    """

    def __init__(self, application: ASGIApplication, state: SelfCheckState) -> None:
        self._application = application
        self._state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if (
            scope_type not in ("http", "websocket")
            or self._state.is_passing
            or is_admin_path(scope)
        ):
            await self._application(scope, receive, send)
        elif scope_type == "websocket":
            await send({"type": "websocket.close", "code": WEBSOCKET_TRY_AGAIN_LATER})
        else:
            await self._refuse(scope, send)

    async def _refuse(self, scope: Scope, send: Send) -> None:
        body = self_check_failed_page(self._state.failed_names)
        headers = status_page_headers(body, retry_after_s=SELF_CHECK_RETRY_AFTER_S)
        await send(
            {"type": "http.response.start", "status": UNAVAILABLE_STATUS, "headers": headers}
        )
        await send(
            {"type": "http.response.body", "body": b"" if scope["method"] == "HEAD" else body}
        )


def create_edge_app(
    *,
    registry: Registry,
    group_source: GroupSource,
    bootstrap_admins: Iterable[str] = (),
    self_check: SelfCheckState | None = None,
    closed_targets: ClosedTargetsSource | None = None,
    egress_reloader: EgressReloader | None = None,
    ui_app: ASGIApp | None = None,
    routes: RouteLookup | None = None,
    upstream_client: UpstreamClient | None = None,
    connections: OpenConnections | None = None,
    metrics: MetricsSink | None = None,
    forward_token_policy: ForwardTokenPolicy | None = None,
    app_state: AppStateReader = state_from_desired,
    release_updater: ReleaseUpdater | None = None,
    on_release_created: Callable[[str], None] | None = None,
    attach_checker: AttachChecker | None = None,
    source_store: SnapshotStore | None = None,
    open_mode_cookies: OpenModeCookies | None = None,
    app_control: AppControl | None = None,
    rename_cascade: RenameCascade | None = None,
    memory: MemorySource | None = None,
    workspace_client: httpx.AsyncClient | None = None,
) -> ASGIApplication:
    """The application behind an open gate.

    ``registry`` must already hold its first snapshot. ``group_source`` looks up a visitor's
    Databricks groups (``identity.groups.GroupLookup``); ``bootstrap_admins`` are the emails that
    count as platform admins only while the registry's ``platform_admins`` is unset.

    ``self_check`` is the state the start-up ran the canary into (``__main__`` always passes it).
    With ``None`` the edge is not gated and ``POST /platform/self-check`` answers ``503``: that is
    for code that builds the edge alone, never for the running service.

    ``closed_targets`` answers what ``@container_ip`` and ``@appmux_port`` stand for in this
    container (``None`` while unknown), and ``egress_reloader`` tells the children's pumps to read
    a saved egress list (``None`` for the global list, else an app slug). Without
    ``closed_targets`` the egress routes answer ``503``: a list that cannot be checked is never
    saved. Without ``egress_reloader`` a save changes the registry and signals nothing.

    ``ui_app`` serves ``/`` and ``/_appmux/...`` (``create_ui_app`` of a loaded export, or
    ``unavailable_ui_app``). Without it those paths answer ``404``: that is for code that builds
    the edge alone.

    The admin routes that depend on a part of the running service are mounted only when the
    start-up gives that part; without it the path answers ``404``, which is for code that builds
    the edge alone:

    * ``release_updater`` (the Update flow) mounts ``POST /apps/{app}/releases`` and the release
      page; ``on_release_created`` is told the app's slug after an Update wrote a release (the
      running service passes ``Reconciler.wake``);
    * ``app_control`` (the reconciler) mounts activate, the config edit, start, stop, restart and
      delete, and is told when a rename by edit moved an app (``app_renamed``);
    * ``memory`` (the reconciler's memory books) is what ``GET /platform`` reads; without it that
      route answers ``503``, never a made-up figure;
    * ``attach_checker`` mounts the requirements, attach and detach routes;
    * ``source_store`` (the content store) mounts the read-only source viewer;
    * ``workspace_client`` (the client every call to the workspace goes through) mounts the
      folder browser, which reads a folder with the visitor's own token;
    * ``open_mode_cookies`` is the one writer and reader of the Open as a user cookie: the
      ``POST /apps/{app}/open-mode`` route sets it, and, unless ``forward_token_policy`` is given,
      the pipeline reads the same instance to decide whether a child receives an admin's token.
      Without it that route answers ``503`` and no token is forwarded.

    The request inbox, delegation, ``GET /audit`` and ``GET /metrics`` need no part of the service
    and are always mounted; ``rename_cascade`` is what an approved rename request runs after the
    row is saved (without it the app keeps running under its old slug until a restart).

    ``routes`` is the route table the request pipeline reads (``RouteTable``; the edge never opens
    or closes a route). With it, ``/<slug>/...`` is served by the pipeline; without it those paths
    answer ``404``. The other arguments belong to the pipeline and are ignored without ``routes``:

    * ``upstream_client`` and ``connections`` are the client to the children and the registry of
      open streams. The running service passes the ones it shares (the reconciler ``forget``s a
      socket path on the client, and the sweeper closes streams through ``connections``); left out,
      the pipeline makes its own, which nothing else can reach.
    * ``metrics`` is fed with every request the pipeline decides (``None`` records nothing).
    * ``forward_token_policy`` decides whether a child receives the visitor's access token (see
      ``edge.open_mode``); without one, and without ``open_mode_cookies``, the token is never
      forwarded.
    * ``app_state`` names the state shown on the page of an app that has no route. It is also
      what ``GET /apps`` reports, so a visitor reads one state in both places.
    """
    # Read once: the authorizer and the platform routes both need the list, and an iterator
    # given here would be empty for the second.
    admins = tuple(bootstrap_admins)
    authorizer = make_authorizer(registry, group_source, bootstrap_admins=admins)
    egress = EgressEditor(closed_targets=closed_targets, reloader=egress_reloader)
    providers: list[RouteProvider] = [
        IdentityRoutes(authorizer, app_state=app_state, open_mode_cookies=open_mode_cookies),
        GrantRoutes(),
        PlatformRoutes(
            self_check=self_check, egress=egress, bootstrap_admins=admins, memory=memory
        ),
        AppRoutes(
            egress=egress,
            app_state=app_state,
            on_renamed=None if app_control is None else app_control.app_renamed,
        ),
        RequestRoutes(authorizer, rename=rename_cascade),
        DelegationRoutes(),
        MetricsRoutes(),
    ]
    if app_control is not None:
        providers.append(OperationRoutes(app_control))
    if release_updater is not None:
        providers.append(
            ReleaseRoutes(release_updater)
            if on_release_created is None
            else ReleaseRoutes(release_updater, on_release_created=on_release_created)
        )
    if attach_checker is not None:
        providers.append(ResourceRoutes(checker=attach_checker))
    if source_store is not None:
        providers.append(SourceViewerRoutes(source_store))
    if workspace_client is not None:
        providers.append(WorkspaceBrowseRoutes(WorkspaceHttpApi(workspace_client)))
    admin_app = create_admin_app(
        registry=registry, authorizer=authorizer, route_providers=providers
    )
    # redirect_slashes is off: Starlette would answer ``/_appmux/api/v1`` with a 307 to a URL
    # built from the Host header and the scope's scheme (http behind the TLS-terminating proxy).
    # A trailing-slash mismatch is a plain 404 (D5).
    application = Starlette(
        routes=[
            Mount(ADMIN_API_MOUNT, app=admin_app),
            *([] if ui_app is None else ui_routes(ui_app)),
        ],
        exception_handlers={404: _not_found},
    )
    application.router.redirect_slashes = False
    edge: ASGIApplication = application
    if forward_token_policy is None and open_mode_cookies is not None:
        forward_token_policy = OpenModeTokenPolicy(authorizer, open_mode_cookies)
    if routes is not None:
        pipeline = RequestPipeline(
            registry=registry,
            authorizer=authorizer,
            routes=routes,
            upstream=upstream_client if upstream_client is not None else UpstreamClient(),
            connections=connections if connections is not None else OpenConnections(),
            app_state=app_state,
            forward_token_policy=forward_token_policy,
            metrics=metrics,
        )
        edge = EdgeRouter(application, pipeline)
    if self_check is None:
        return edge
    return SelfCheckGate(edge, self_check)
