"""``python -m appmux``: validate the settings, bind the port, then start up in the background.

The order is the one of design 5.6 and FR-008:

1. ``EnvironmentSettings.from_environment()``; a ``SettingsError`` names every faulty variable and
   stops the start before anything binds.
2. uvicorn starts on ``DATABRICKS_APP_PORT`` with the ``Gate`` of ``edge/gate.py`` as its ASGI
   application, so the platform's probe of ``/`` is answered from the first moment.
3. A background task, started only once uvicorn reports the port bound, connects to the registry,
   loads the first snapshot, runs the start-up steps, builds the application and opens the gate.
   Any failure puts the gate in its *blocked* phase with a reason from a fixed list. After the
   gate opens the same task keeps the registry snapshot fresh until it is cancelled.
4. On ``SIGTERM`` uvicorn shuts down and runs the gate's shutdown hooks: first the one that stops
   the children (``Service.stop``, within ``CHILD_STOP_BUDGET_S``), then the one that cancels the
   background task, whose cleanup closes the registry.

Nothing slow is imported at the top of this module: the registry client (psycopg), the Databricks
SDK, httpx and the application are imported in a worker thread by the background task, after the
port is bound. ``STARTUP_STEPS`` is for steps that need nothing from the wiring; the steps that do
(the content store's folder, the self-check of T067, the children of T095) come from the real
wiring itself, because the edge needs the state they run into.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import sys
import tempfile
import threading
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from ipaddress import IPv4Address
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import uvicorn

from appmux.edge.gate import ASGIApplication, BlockReason, Gate, Phase
from appmux.registry.errors import RegistryError, RegistryUnavailable
from appmux.registry.protocol import Registry
from appmux.settings import EnvironmentSettings, SettingsError

if TYPE_CHECKING:
    import httpx
    from databricks.sdk.core import Config
    from pydantic import SecretStr
    from starlette.types import ASGIApp

    from appmux.admin.platform import ClosedTargetsSource
    from appmux.sandbox.self_check import CanaryRunner, SelfCheckState

logger = logging.getLogger(__name__)

# The address the platform's proxy reaches the container on.
LISTEN_HOST: Final = "0.0.0.0"  # noqa: S104 - the proxy is on another interface; the port is the platform's
# The platform allows 15 s from SIGTERM to the end of the process (design 5.6). The time is split
# in three that run one after the other, plus a margin: uvicorn's wait for open connections, the
# children's stop (the first hook), and the last metrics flush (the second hook). The split is the
# owner's choice of 2026-10-31 (TD-016 (5)); a connection still open after
# ``GRACEFUL_SHUTDOWN_S`` is cut, which is sooner than it would be with a longer wait, because the
# minutes of metrics in memory are worth more than the last seconds of a slow request.
PLATFORM_SHUTDOWN_S: Final = 15
GRACEFUL_SHUTDOWN_S: Final = 6
METRICS_FLUSH_BUDGET_S: Final = 4.0
# Left unused so the last hook and the exit are not racing the platform's kill.
SHUTDOWN_MARGIN_S: Final = 1
# What the children get to stop in: whatever the other two and the margin leave.
CHILD_STOP_BUDGET_S: Final = (
    PLATFORM_SHUTDOWN_S - GRACEFUL_SHUTDOWN_S - METRICS_FLUSH_BUDGET_S - SHUTDOWN_MARGIN_S
)
# No document or spike records that the platform injects PGPORT or PGUSER for a Lakebase database
# resource, and EnvironmentSettings reads neither. The first start of the dev App on Free Edition
# (2026-10-31) connected to port 5432 as the service principal's OAuth client id with its workspace
# token as the password and loaded the registry, so these two values work there; whether the
# platform also injects the variables, and whether the role has this name elsewhere, is not known.
LAKEBASE_PORT: Final = 5432
# How often the start-up looks at whether uvicorn has bound the port.
BIND_POLL_INTERVAL_S: Final = 0.05
EXIT_BAD_SETTINGS: Final = 2


class ManagedRegistry(Registry, Protocol):
    """A registry the start-up owns: the ``Registry`` operations plus the two lifecycle calls."""

    async def poll(self) -> None:
        """Reload the snapshot forever; ends only when cancelled."""
        ...

    async def close(self) -> None: ...


# A start-up step runs after the first snapshot is loaded and before the gate opens. It raises to
# keep the gate shut. The tuple holds the steps that are independent of the wiring; it is empty
# because every step so far needs a part of it (see `_load_wiring`).
StartupStep = Callable[[Registry], Awaitable[None]]
STARTUP_STEPS: Final[tuple[StartupStep, ...]] = ()

ApplicationBuilder = Callable[[Registry], ASGIApplication]


@dataclass(frozen=True, slots=True)
class BuilderWithSteps:
    """An application builder with the start-up steps its application depends on.

    The real wiring returns one so that the self-check step and the edge it builds share the same
    ``SelfCheckState``: the step runs the canary into the state before the gate opens, and the edge
    answers from it. It is called like any ``ApplicationBuilder``.
    """

    build: ApplicationBuilder
    steps: tuple[StartupStep, ...]

    def __call__(self, registry: Registry) -> ASGIApplication:
        return self.build(registry)


def _log_failure(stage: str, exc: Exception) -> None:
    # The message of an exception can quote a host or a credential, so the log gets the stage, the
    # class and the stack frames (code locations, never values) and not the message.
    frames = "".join(traceback.format_tb(exc.__traceback__))
    logger.error("start-up failed during %s (%s)\n%s", stage, type(exc).__name__, frames)


async def start_application(
    gate: Gate,
    *,
    registry: ManagedRegistry,
    build: ApplicationBuilder,
    steps: Sequence[StartupStep] | None = None,
) -> None:
    """Load the registry, run ``steps``, build the application and open ``gate``; then poll.

    Returns when the gate was blocked, or when cancelled (``SIGTERM``) after it opened. The gate is
    opened only after every earlier stage succeeded; a failure blocks it with a ``BlockReason``
    chosen from the stage and the error class. A cancellation propagates and leaves the gate as it
    was. The registry is closed on the way out. ``steps`` defaults to ``STARTUP_STEPS``, read when
    called.
    """
    try:
        await _start(gate, registry, build, STARTUP_STEPS if steps is None else steps)
        if gate.phase is Phase.OPEN:
            await registry.poll()
    finally:
        await _close_quietly(registry)


async def _start(
    gate: Gate,
    registry: ManagedRegistry,
    build: ApplicationBuilder,
    steps: Sequence[StartupStep],
) -> None:
    try:
        await registry.refresh()
    except RegistryUnavailable as exc:
        _log_failure("registry load", exc)
        gate.block(BlockReason.REGISTRY_UNREACHABLE)
        return
    except RegistryError as exc:
        _log_failure("registry load", exc)
        gate.block(BlockReason.REGISTRY_REFUSED)
        return
    except Exception as exc:
        # Operational boundary: whatever else the first load raised must close the gate, not
        # kill the task and leave the gate answering "starting" for ever.
        _log_failure("registry load", exc)
        gate.block(BlockReason.STARTUP_FAILED)
        return

    try:
        for step in steps:
            await step(registry)
        application = build(registry)
    except Exception as exc:
        # Operational boundary, same reason: a failed step or build keeps the gate shut.
        _log_failure("start-up steps", exc)
        gate.block(BlockReason.STARTUP_FAILED)
        return
    gate.open(application)


async def _close_quietly(registry: ManagedRegistry) -> None:
    try:
        await registry.close()
    except Exception as exc:
        # The process is stopping or blocked; a failed close changes nothing the caller can do.
        logger.error("closing the registry failed (%s)", type(exc).__name__)


async def stop_background(task: asyncio.Task[None]) -> None:
    """Cancel the start-up task and wait until it has cleaned up; safe to call twice."""
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- the real wiring: imported after the port is bound ----------------------------------------


def _load_wiring(
    settings: EnvironmentSettings, gate: Gate
) -> tuple[ManagedRegistry, ApplicationBuilder]:
    """Import and construct the registry client and the application builder.

    Runs in a worker thread: the imports (psycopg, the Databricks SDK, httpx) take long enough to
    delay the bind if they ran on the event loop. Makes no network call.

    The edge and the reconciler share one route table, one set of open connections, one upstream
    client and one workspace client; ``Service`` (``assembly.py``) holds them and the reconciler.
    The start-up steps are, in order: the content store's folder, the self-check, then the
    children (only if the self-check passed), the metrics writer and the daily clean-up of the
    content store. ``Service.stop`` is the first shutdown hook, so the children stop while the
    registry is still open. The edge is given the service as the control of the reconciler
    (restart, rename, the state of an app), its memory books for ``GET /platform``, and the
    aggregator the pipeline counts requests into.
    """
    from appmux.assembly import Service
    from appmux.edge.app import create_edge_app
    from appmux.edge.connections import OpenConnections
    from appmux.edge.open_mode import OpenModeCookies
    from appmux.identity.groups import GroupLookup
    from appmux.reconciler.route_table import RouteTable
    from appmux.registry.client import RegistryClient
    from appmux.sandbox.self_check import SelfCheckState
    from appmux.upstream.client import UpstreamClient

    service_principal_token = _service_principal_token(settings)
    service_principal_token_async = _as_async_token(service_principal_token)
    registry = RegistryClient(
        host=settings.pghost,
        port=LAKEBASE_PORT,
        database=settings.appmux_lakebase_database,
        user=settings.databricks_client_id,  # worked on Free Edition, see LAKEBASE_PORT
        token_provider=service_principal_token_async,
    )

    self_check = SelfCheckState(_canary_runner(settings))
    workspace = _workspace_client(settings)
    routes = RouteTable()
    upstream = UpstreamClient()
    connections = OpenConnections()
    service = Service(
        settings=settings,
        registry=registry,
        workspace=workspace,
        service_principal_token=service_principal_token,
        service_principal_token_async=service_principal_token_async,
        routes=routes,
        upstream=upstream,
        closer=connections,
        is_self_check_passing=lambda: self_check.is_passing,
        container_ip=_container_ip,
        stop_budget_s=CHILD_STOP_BUDGET_S,
        metrics_stop_budget_s=METRICS_FLUSH_BUDGET_S,
    )
    gate.add_shutdown_hook(service.stop, first=True)
    ui_app = _ui_application(settings.ui_dir)

    def build(candidate: Registry) -> ASGIApplication:
        return create_edge_app(
            registry=candidate,
            group_source=GroupLookup(workspace),
            bootstrap_admins=settings.appmux_bootstrap_admins,
            self_check=self_check,
            closed_targets=_closed_targets(settings),
            egress_reloader=service.egress_reloader,
            ui_app=ui_app,
            routes=routes,
            upstream_client=upstream,
            connections=connections,
            release_updater=service.updater,
            on_release_created=service.wake,
            attach_checker=service.attach_checker,
            source_store=service.content_store,
            open_mode_cookies=OpenModeCookies(),
            metrics=service.metrics,
            app_state=service.run_state,
            restart_failure=service.last_restart_failure,
            app_control=service,
            rename_cascade=service,
            memory=service.memory_ledger,
            workspace_client=workspace,
            platform_token=service_principal_token_async,
            principal_directory=service.principal_directory,
            app_output=service,
            child_setup=service.child_setup,
        )

    steps = (
        service.ensure_store_folder,
        _self_check_step(self_check),
        service.start_children,
        service.start_metrics,
        service.start_cleanup,
    )
    return registry, BuilderWithSteps(build, steps)


def _workspace_client(settings: EnvironmentSettings) -> httpx.AsyncClient:
    """The one client every call to the workspace goes through (groups, store, secrets, checks).

    It sends only a path and the caller's own ``Authorization`` header, and follows no redirect.
    """
    import httpx

    host = settings.databricks_host
    base_url = host if "://" in host else f"https://{host}"
    return httpx.AsyncClient(base_url=base_url, follow_redirects=False)


def _ui_application(directory: Path) -> ASGIApp:
    """The admin UI served from the export at ``directory``, or a ``503`` stand-in if it cannot be.

    Reads the whole export, so it runs where the other imports do (a worker thread). A broken
    export is logged with its cause and costs only the UI: the admin API and the proxy do not need
    it, and stopping the process would interrupt every app it hosts (constitution III).
    """
    from appmux.edge.ui_static import (
        UiExportError,
        create_ui_app,
        load_ui_export,
        unavailable_ui_app,
    )

    try:
        return create_ui_app(load_ui_export(directory))
    except UiExportError as exc:
        logger.error("the admin UI is not served: %s", exc)
        return unavailable_ui_app()


def _canary_runner(settings: EnvironmentSettings) -> CanaryRunner:
    """The function that runs the canary in a real sandbox and returns what it printed.

    ``sandbox/canary.py`` does the work: it plants what the canary is tested against, starts it
    through the launcher and returns the one JSON object it printed. This only gathers the facts
    of this container for it: appmux's own environment (``sys.prefix``: the parent environment a
    child sees read-only), the container address and port, and the platform's default settings.
    Design 6.4 runs ``self_check()`` with the default rules and the default CPU pin, so the registry
    is not asked.

    Container-only: nothing here has run in a sandbox yet. Any failure raises, and ``self_check``
    records a failed result, so the edge serves nothing but ``/_appmux/`` (FR-013). Answering
    "passed" for a check that never ran would be the one wrong choice.
    """
    from appmux.sandbox.self_check import CanaryUnavailable
    from appmux.settings import PlatformSettings

    async def run_in_sandbox() -> str:
        from appmux.sandbox.canary import CanaryConfig, run_canary

        container_ip = _container_ip()
        if container_ip is None:
            raise CanaryUnavailable(
                "the container's address is unknown, so the canary has nothing to test against"
            )
        defaults = PlatformSettings()
        config = CanaryConfig(
            # A path that does not exist yet: run_canary makes it, uses it and removes it.
            work_dir=Path(tempfile.gettempdir()) / f"appmux-canary-{secrets.token_hex(8)}",
            app_environment=Path(sys.prefix),
            container_ip=container_ip,
            appmux_port=settings.databricks_app_port,
            global_rules=tuple(defaults.egress_global),
            pin_cpu=defaults.child_cpu_pin,
        )
        return await run_canary(config)

    return run_in_sandbox


# --- the container's address, for the egress editor (T066) ------------------------------------

ROUTE_TABLE_PATH: Final = "/proc/net/route"
# RTF_UP of <linux/route.h>: the route is usable.
_ROUTE_IS_UP: Final = 0x1
_ROUTE_TABLE_MIN_FIELDS: Final = 8  # Iface Destination Gateway Flags RefCnt Use Metric Mask
_MAX_IPV4_INTEGER: Final = 0xFFFFFFFF
# A UDP connect() sends nothing; it only makes the kernel pick the source address of the route.
# The port is the discard service's and is never contacted.
_ROUTE_PROBE_PORT: Final = 9


def _default_gateway(route_table: str) -> IPv4Address | None:
    """The gateway of the default route in the text of ``/proc/net/route``, or ``None``.

    Each line is ``Iface Destination Gateway Flags RefCnt Use Metric Mask ...`` with the
    addresses in hexadecimal, as the little-endian bytes of the integer (``0102000A`` is
    ``10.0.2.1``). The default route has destination and mask ``0``, the ``UP`` flag and a gateway.
    A line that is not a route (the header) or does not parse is skipped.
    """
    for line in route_table.splitlines():
        fields = line.split()
        if len(fields) < _ROUTE_TABLE_MIN_FIELDS:
            continue
        try:
            destination, gateway, flags, mask = (int(fields[i], 16) for i in (1, 2, 3, 7))
        except ValueError:
            continue
        is_default = destination == 0 and mask == 0 and flags & _ROUTE_IS_UP
        if is_default and 0 < gateway <= _MAX_IPV4_INTEGER:
            return IPv4Address(gateway.to_bytes(4, "little"))
    return None


def _container_ip() -> IPv4Address | None:
    """The address this container's default route leaves from, or ``None`` if it cannot be read.

    Container-only: it reads ``/proc/net/route`` and asks the kernel which source address it
    would use towards the gateway, which no unit test can reproduce. A failure is logged with the
    exception's class only (an OS error text can hold a path) and answered as unknown, so the
    egress editor refuses to save instead of checking a list against a guess.
    """
    try:
        with open(ROUTE_TABLE_PATH, encoding="ascii") as route_table:
            gateway = _default_gateway(route_table.read())
        if gateway is None:
            logger.warning(
                "no default route in %s; the container address is unknown", ROUTE_TABLE_PATH
            )
            return None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((str(gateway), _ROUTE_PROBE_PORT))
            return IPv4Address(probe.getsockname()[0])
    except (OSError, ValueError) as exc:
        logger.warning("cannot learn the container address (%s)", type(exc).__name__)
        return None


def _closed_targets(settings: EnvironmentSettings) -> ClosedTargetsSource:
    """The source the egress editor asks for ``@container_ip`` and ``@appmux_port``.

    The address is looked up on first use and kept once found; while it cannot be found every
    call looks again, so a save made after the network came up works without a restart.
    """
    from appmux.admin.platform import ClosedTargets

    container_ip: IPv4Address | None = None

    def source() -> ClosedTargets | None:
        nonlocal container_ip
        if container_ip is None:
            container_ip = _container_ip()
        if container_ip is None:
            return None
        return ClosedTargets(container_ip=container_ip, appmux_port=settings.databricks_app_port)

    return source


def _self_check_step(state: SelfCheckState) -> StartupStep:
    """The step that runs the self-check into ``state`` after the registry loads (T067).

    It does not raise on a failed result, so the gate still opens: the edge itself then answers
    ``503`` with the failed checks on every path but ``/_appmux/`` (``SelfCheckGate``), which
    keeps the admin pages up for a platform admin to read the result and run it again. A blocked
    startup gate would answer ``503`` there too and never reopen.
    """

    async def run_self_check(registry: Registry) -> None:
        await state.run()

    return run_self_check


def _service_principal_token(settings: EnvironmentSettings) -> Callable[[], str]:
    """Mint the service principal's OAuth token with the Databricks SDK; cached by the SDK.

    The SDK is imported and its ``Config`` built on first use because both block (and ``Config``
    may call the workspace). The callable blocks, so ``_as_async_token`` runs it in a worker
    thread; the content store calls it directly, as its ``token_source`` is synchronous (a refresh
    once an hour then holds the event loop for as long as the workspace takes, TD-015). A failure
    raises ``RuntimeError`` with a fixed message: the registry client turns a failing provider into
    ``RegistryUnavailable``. It is not exercised by the tests, which make no network call.
    """
    cached: list[Config] = []
    # The registry and the secret reader call it from worker threads and the store from the loop;
    # without the lock two first calls could each build a ``Config``.
    lock = threading.Lock()

    def authenticate() -> str:
        from databricks.sdk.core import Config

        with lock:
            if not cached:
                cached.append(
                    Config(
                        host=settings.databricks_host,
                        client_id=settings.databricks_client_id,
                        client_secret=settings.databricks_client_secret.get_secret_value(),
                        auth_type="oauth-m2m",
                    )
                )
            config = cached[0]
        scheme, _, token = config.authenticate().get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise RuntimeError("the Databricks SDK returned no bearer token")
        return token

    return authenticate


def _as_async_token(token: Callable[[], str]) -> Callable[[], Awaitable[SecretStr]]:
    """``token`` for callers on the event loop that must not wait for it: it runs in a thread."""
    from pydantic import SecretStr

    async def provide() -> SecretStr:
        return SecretStr(await asyncio.to_thread(token))

    return provide


async def _start_with_real_wiring(gate: Gate, settings: EnvironmentSettings) -> None:
    try:
        registry, build = await asyncio.to_thread(_load_wiring, settings, gate)
    except Exception as exc:
        # Operational boundary: an import or construction failure closes the gate.
        _log_failure("loading the registry client", exc)
        gate.block(BlockReason.STARTUP_FAILED)
        return
    # The steps are read at call time, as `start_application` reads `STARTUP_STEPS`.
    steps = (*STARTUP_STEPS, *build.steps) if isinstance(build, BuilderWithSteps) else None
    await start_application(gate, registry=registry, build=build, steps=steps)


# --- the server -------------------------------------------------------------------------------


def server_config(settings: EnvironmentSettings, gate: Gate) -> uvicorn.Config:
    """uvicorn's configuration: the gate on the platform's port.

    ``proxy_headers`` is off so uvicorn leaves the client address alone (the edge reads
    ``X-Real-Ip`` itself, spike 8), and the access log is off because a query string may carry a
    token.
    """
    return uvicorn.Config(
        gate,
        host=LISTEN_HOST,
        port=settings.databricks_app_port,
        lifespan="on",
        proxy_headers=False,
        access_log=False,
        server_header=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        log_level="info",
    )


class _BindState(Protocol):
    @property
    def started(self) -> bool: ...


async def _start_once_bound(server: _BindState, gate: Gate, settings: EnvironmentSettings) -> None:
    # uvicorn runs the lifespan startup before it binds, so a task started at once could reach the
    # registry first. Waiting for ``started`` makes "port bound, then registry" a guarantee.
    # uvicorn exposes the bind only as a plain flag, so there is no event to await.
    while not server.started:  # noqa: ASYNC110 - polling a flag we do not own
        await asyncio.sleep(BIND_POLL_INTERVAL_S)
    await _start_with_real_wiring(gate, settings)


def stop_background_on_shutdown(gate: Gate, startup: asyncio.Task[None]) -> None:
    """Cancel ``startup`` (and so close the registry) when the server shuts down.

    uvicorn runs the gate's shutdown hooks on ``SIGTERM``, inside its graceful shutdown. After a
    signal uvicorn re-raises it once ``serve()`` returns, so the process ends before any
    ``finally`` of ``serve()`` runs; this hook is the only cleanup on that path.
    """
    gate.add_shutdown_hook(partial(stop_background, startup))


async def serve(settings: EnvironmentSettings) -> None:
    """Run uvicorn with the gate and the background start-up until the process is told to stop."""
    gate = Gate()
    server = uvicorn.Server(server_config(settings, gate))
    startup = asyncio.create_task(_start_once_bound(server, gate, settings), name="appmux-startup")
    stop_background_on_shutdown(gate, startup)
    try:
        await server.serve()
    finally:
        # A bind failure ends serve() with SystemExit before any lifespan shutdown.
        await stop_background(startup)


def run_server(settings: EnvironmentSettings) -> None:
    asyncio.run(serve(settings))


def main(
    environ: Mapping[str, str] | None = None,
    *,
    run: Callable[[EnvironmentSettings], None] = run_server,
) -> int:
    """Validate the environment and run the server; the return value is the exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        settings = EnvironmentSettings.from_environment(environ)
    except SettingsError as exc:
        logger.error("%s", exc)
        return EXIT_BAD_SETTINGS
    run(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
