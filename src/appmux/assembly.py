"""The running service's shared parts and the order they start in (T095).

The edge and the reconciler share state: the route table the reconciler opens and the edge reads,
the upstream client, the open connections, the content store the Update flow writes and the builder
reads, the workspace client every outbound call goes through. ``Service`` holds those and offers
the start-up steps and the shutdown hook ``__main__`` plugs in. It makes no network call when it is
built; the steps do.

The steps run in this order, each only after the registry loaded:

1. ``ensure_store_folder``: the content store's folder exists. A store that cannot be created stops
   the start (the gate blocks): without it no Update can store a snapshot and no release can be
   built.
2. the self-check (``__main__`` adds it).
3. ``start_children``: the reconciler starts **only if the self-check passed** (FR-013: with a
   failed self-check no route opens; the platform admin reads the result and restarts). It reads
   the container's memory limit once; a limit nobody states stops the start, because admission
   control would otherwise decide against a guess. The memory watchdog starts with it: once a
   second it sums the resident memory of each running sandbox's process tree and stops the one
   that holds more than its budget with headroom (``sandbox/watchdog.py``, design 5.7, TD-029).
4. ``start_metrics``: the per-minute counters start being written to the registry. The sampler
   reads the container's cgroup and ``/proc`` (``metrics/sources.py``, under ``metrics_root``), the
   reconciler's running children and the registry's app count; the reconciler and the registry
   client count restarts, admission refusals and registry calls into the same aggregator.
5. ``start_cleanup``: the daily clean-up of the content store starts; its first pass is an hour
   after the start and it never blocks the start-up.

The admin routes reach the reconciler through ``Service``: it is their ``AppControl`` (restart,
rename, stop before a delete, the state of an app), their ``RenameCascade`` and the source of the
memory books of ``GET /platform``. Before the reconciler runs (the self-check failed) these answer
from the registry alone and change nothing, because no copy exists to move.

``stop`` is the ``SIGTERM`` hook: the clean-up loop and the memory watchdog are cancelled, the
children are stopped in
parallel within ``stop_budget_s``, the last minute of metrics is written within
``metrics_stop_budget_s``, then the clients close, whatever the stop did.

``edge`` is not imported here (design 3.4: only ``__main__`` imports it); the open connections the
reconciler closes on a swap arrive as a ``ConnectionCloser``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Collection, Mapping
from ipaddress import IPv4Address
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

from pydantic import SecretStr

from appmux.admin.environment import ChildSetup
from appmux.admin.identity import state_from_desired
from appmux.admin.platform import EGRESS_GLOBAL_KEY
from appmux.admin.resources import DatabricksAttachChecker
from appmux.identity.directory import DirectoryLookup
from appmux.metrics.aggregate import MetricsAggregator
from appmux.metrics.service import ChildProbe, MetricsService, PlatformSample
from appmux.metrics.sources import ROOT, read_platform_sample, read_process_tree_rss_mb
from appmux.packages.cleanup import run_cleanup_once
from appmux.packages.overlay import OverlayBuilder, SandboxStepRunner, pip_index_environment
from appmux.packages.store import ContentStore, WorkspaceHttpApi
from appmux.packages.wheels import WheelStore
from appmux.reconciler.admission import (
    OVERHEAD_PER_CHILD_MB,
    MemoryLedger,
    MemoryLimitUnreadableError,
    read_container_memory_limit_mb,
)
from appmux.reconciler.app_control import RunState
from appmux.reconciler.build import PackageStep, StoreReleaseBuilder, package_policy_from
from appmux.reconciler.child_env import EnvironmentRefused, PlatformIdentity, ReleaseEnvironment
from appmux.reconciler.children import ChildStartError
from appmux.reconciler.clock import SystemClock
from appmux.reconciler.health import UnixSocketHealthProbe
from appmux.reconciler.output import OutputRing
from appmux.reconciler.reconciler import Reconciler
from appmux.reconciler.route_table import RouteTable
from appmux.reconciler.sandbox_adapter import (
    RunDirKeeper,
    SandboxChildLauncher,
    SandboxRunningChild,
)
from appmux.reconciler.snapshot_source import SnapshotSource
from appmux.reconciler.swap import ConnectionCloser
from appmux.reconciler.update import UpdateService
from appmux.reconciler.update_packages import UpdatePackages
from appmux.registry.client import RegistryClient
from appmux.sandbox.rules import EgressRule
from appmux.sandbox.watchdog import (
    CHECK_INTERVAL_S,
    Offence,
    WatchedChild,
    process_tree_rss_bytes,
    watch,
)
from appmux.secrets.credentials import ChildCredentials, CredentialsRefused
from appmux.secrets.resolver import (
    DatabricksSecretReader,
    MissingResourceKeys,
    ResourceValueResolver,
    SecretUnreadable,
)
from appmux.secrets.shared_sp_token import (
    ClientCredentialsMinter,
    SharedSpTokenFiles,
    SharedSpTokens,
    TokenMintFailed,
)
from appmux.settings import EnvironmentSettings, PlatformSettings
from appmux.upstream.client import UpstreamClient

if TYPE_CHECKING:
    import httpx

    from appmux.registry.protocol import Registry
    from appmux.registry.snapshot import RegistrySnapshot

__all__ = [
    "RUN_DIR",
    "RUN_DIR_NAME",
    "WORK_DIR",
    "ChildCredentialsAdapter",
    "MemoryLimitUnreadableError",
    "ResourceValuesAdapter",
    "Service",
    "global_egress_rules",
    "memory_reserve_mb",
    "platform_identity",
]

logger = logging.getLogger(__name__)

# `/tmp` survives a redeploy and is where design 5.3 keeps the environment cache and the local
# copies of releases. The run directories sit under it: a child's relay socket lives in one, and a
# unix socket path may be 107 bytes at most (`test_service_assembly.py` checks the longest slug).
WORK_DIR: Final = Path("/tmp/appmux")  # noqa: S108 - the container's own scratch space, by design
RUN_DIR_NAME: Final = "run"
RUN_DIR: Final = WORK_DIR / RUN_DIR_NAME
RELEASES_DIR_NAME: Final = "releases"
OVERLAYS_DIR_NAME: Final = "overlays"
# The build sandboxes' run directories sit under the children's, so the stale-run clean-up at
# start-up also empties what a crashed build left.
BUILD_RUN_DIR_NAME: Final = "build"
# The store lives in the service principal's own folder (spike 4), next to nothing a person owns.
STORE_FOLDER_NAME: Final = "appmux-store"
MEMORY_RESERVE_KEY: Final = "memory_reserve_mb"
# The content-store clean-up (data model rule 11: only files no live release names and older than a
# day). The first run waits an hour so a start-up that is still settling is not also listing the
# store; after that it runs once a day.
CLEANUP_FIRST_DELAY_S: Final = 3600.0
CLEANUP_INTERVAL_S: Final = 24 * 3600.0


class _Resolver(Protocol):
    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]: ...


class ResourceValuesAdapter:
    """``ResourceValueResolver`` seen from the reconciler: its refusals become environment refusals.

    The reconciler stores the message of an ``EnvironmentRefused`` as the release's
    ``status_reason``. The resolver's own messages name keys and scopes and never a value.
    """

    def __init__(self, resolver: _Resolver) -> None:
        self._resolver = resolver

    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]:
        try:
            return await self._resolver.resolve(app_slug, keys)
        except (MissingResourceKeys, SecretUnreadable) as exc:
            raise EnvironmentRefused(exc.message) from exc


class _Credentials(Protocol):
    async def credentials_for(self, app_slug: str) -> Mapping[str, str]: ...


class ChildCredentialsAdapter:
    """``ChildCredentials`` seen from the reconciler: its refusals become environment refusals.

    The refusal's message says what failed and what to do next and holds no secret value, so the
    reconciler can store it as the release's ``status_reason``.
    """

    def __init__(self, credentials: _Credentials) -> None:
        self._credentials = credentials

    async def credentials_for(self, app_slug: str) -> Mapping[str, str]:
        try:
            return await self._credentials.credentials_for(app_slug)
        except CredentialsRefused as exc:
            raise EnvironmentRefused(exc.message) from exc


class _RunDirTokenFiles:
    """``SharedSpTokenFiles`` seen from the launcher: a token that cannot be had stops the start.

    The check at the reconciler already minted once, so this fails only when Databricks stops
    answering between that check and the start. The child must not start with a variable that
    points at a token file nobody wrote.
    """

    def __init__(self, files: SharedSpTokenFiles) -> None:
        self._files = files

    def needed_by(self, environment: Mapping[str, str]) -> bool:
        return self._files.needed_by(environment)

    async def attach(self, directory: Path) -> RunDirKeeper:
        try:
            return await self._files.attach(directory)
        except TokenMintFailed as exc:
            raise ChildStartError(f"{exc}; {exc.advice}") from exc


def platform_identity(settings: EnvironmentSettings) -> PlatformIdentity:
    """What a child is told about the workspace and about appmux's own address."""
    return PlatformIdentity(
        workspace_host=settings.databricks_host,
        workspace_id=settings.databricks_workspace_id,
        # The child's own URL is this plus "/<slug>"; a trailing slash would double the one between.
        parent_url=settings.databricks_app_url.rstrip("/"),
    )


def memory_reserve_mb(snapshot: RegistrySnapshot) -> int:
    """The memory appmux keeps for itself: the stored setting, or the default if it is unusable."""
    row = snapshot.setting(MEMORY_RESERVE_KEY)
    value = row.value if row is not None else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return PlatformSettings().memory_reserve_mb


def global_egress_rules(snapshot: RegistrySnapshot) -> tuple[EgressRule, ...]:
    """The global egress list: the stored one, or the closed default if the setting is not a list.

    The registry already holds each rule as an ``EgressRule``; only a value of another kind under
    this key is left to catch. It must not open anything, so the default (container address and
    link-local denied) applies instead of "no rules". An empty stored list is an admin's choice
    and stays empty.
    """
    row = snapshot.setting(EGRESS_GLOBAL_KEY)
    if row is not None and isinstance(row.value, list):
        rules = tuple(rule for rule in row.value if isinstance(rule, EgressRule))
        if len(rules) == len(row.value):
            return rules
    if row is not None:
        logger.error("the stored global egress setting is not a rule list; the default applies")
    return tuple(PlatformSettings().egress_global)


class Service:
    def __init__(
        self,
        *,
        settings: EnvironmentSettings,
        registry: Registry,
        workspace: httpx.AsyncClient,
        service_principal_token: Callable[[], str],
        service_principal_token_async: Callable[[], Awaitable[SecretStr]],
        routes: RouteTable,
        upstream: UpstreamClient,
        closer: ConnectionCloser,
        is_self_check_passing: Callable[[], bool],
        container_ip: Callable[[], IPv4Address | None],
        stop_budget_s: float,
        metrics_stop_budget_s: float,
        memory_limit_mb: Callable[[], int] | None = None,
        work_dir: Path = WORK_DIR,
        cleanup_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        metrics_root: Path = ROOT,
        memory_watch_interval_s: float = CHECK_INTERVAL_S,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._workspace = workspace
        self._service_principal_token_async = service_principal_token_async
        self._upstream = upstream
        self._is_self_check_passing = is_self_check_passing
        self._container_ip = container_ip
        self._memory_limit_mb = memory_limit_mb
        self._stop_budget_s = stop_budget_s
        self._run_dir = work_dir / RUN_DIR_NAME
        self._releases_dir = work_dir / RELEASES_DIR_NAME
        self._overlays_dir = work_dir / OVERLAYS_DIR_NAME
        self._reconciler: Reconciler | None = None
        self._cleanup_sleep = cleanup_sleep
        self._cleanup_task: asyncio.Task[None] | None = None
        self._memory_watch_interval_s = memory_watch_interval_s
        self._memory_watch_task: asyncio.Task[None] | None = None

        self.routes = routes
        self.closer = closer
        workspace_api = WorkspaceHttpApi(workspace)
        self.content_store = ContentStore(
            workspace_api,
            folder=f"/Workspace/Users/{settings.databricks_client_id}/{STORE_FOLDER_NAME}",
            token_source=service_principal_token,
        )
        self.updater = UpdateService(
            registry=registry,
            snapshots=SnapshotSource(workspace_api, self.content_store),
            store=self.content_store,
            packages=UpdatePackages(store=WheelStore(self.content_store), workspace=workspace_api),
        )
        self.attach_checker = DatabricksAttachChecker(workspace)
        # The Sharing form's suggestions (GET /principals): the person's token, then the platform's.
        self.principal_directory = DirectoryLookup(
            workspace, platform_token=service_principal_token_async
        )
        # What ``GET /apps/{app}/environment`` reads: the releases the builder writes and the
        # platform values every child is started with.
        self.child_setup = ChildSetup(
            platform=platform_identity(settings), releases_dir=self._releases_dir
        )
        # The edge counts requests into ``metrics``; the metrics service samples the container and
        # the children into it and writes the finished minutes. A figure no file states is left
        # out, never invented (``metrics/sources.py``).
        self.metrics = MetricsAggregator()
        self._metrics_root = metrics_root
        if isinstance(registry, RegistryClient):
            registry.observe_calls(self.metrics.record_registry_call)
        self._metrics_service = MetricsService(
            self.metrics,
            registry,
            platform=self.platform_sample,
            children=self.child_probes,
            stop_budget_s=metrics_stop_budget_s,
        )
        self._is_metrics_started = False

    # --- the start-up steps and the hook --------------------------------------------------

    async def ensure_store_folder(self, registry: Registry) -> None:
        """Create the content store's folder if missing; ``ContentStoreError`` if it cannot."""
        await self.content_store.ensure_folder()

    async def start_children(self, registry: Registry) -> None:
        """Start the reconciler, unless the self-check failed; once only."""
        if self._reconciler is not None:
            return
        if not self._is_self_check_passing():
            logger.error("the self-check did not pass, so no app is started (FR-013)")
            return
        container_ip = self._container_ip()
        if container_ip is None:
            raise RuntimeError(
                "the container's address is unknown, so no sandbox can be given its egress rules"
            )
        read_limit = self._memory_limit_mb or read_container_memory_limit_mb
        memory_limit_mb = read_limit()
        # Nothing runs yet, so anything left here belongs to a process that is gone.
        await asyncio.to_thread(functools.partial(shutil.rmtree, self._run_dir, ignore_errors=True))
        self._reconciler = self._reconciler_for(container_ip, memory_limit_mb)
        await self._reconciler.start()
        self._start_memory_watch()

    async def start_metrics(self, registry: Registry) -> None:
        """Start sampling and flushing the metrics; once only."""
        if not self._is_metrics_started:
            self._is_metrics_started = True
            self._metrics_service.start()

    async def start_cleanup(self, registry: Registry) -> None:
        """Start the daily content-store clean-up; once only. It never blocks the start-up."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._clean_up_daily(registry), name="content-store clean-up"
            )

    async def _clean_up_daily(self, registry: Registry) -> None:
        delay_s = CLEANUP_FIRST_DELAY_S
        while True:
            await self._cleanup_sleep(delay_s)
            delay_s = CLEANUP_INTERVAL_S
            try:
                await run_cleanup_once(self.content_store, registry)
            except Exception:
                # The last line of defence of a background loop: run_cleanup_once logs and skips
                # the failures it expects, so anything here is a bug, and one bad day must not
                # end the clean-up for the life of the process. The next day tries again.
                logger.exception("the content-store clean-up failed; trying again tomorrow")

    def wake(self, slug: str) -> None:
        """Ask the reconciler to look at ``slug``; nothing to wake before it runs."""
        if self._reconciler is not None:
            self._reconciler.wake(slug)

    # --- what the admin routes ask of the reconciler (``AppControl``, ``RenameCascade``) -------
    # Until the reconciler runs (the self-check failed) no copy exists, so there is nothing to
    # restart or move; the registry rows the routes wrote are what the next start reads.

    def run_state(self, slug: str, desired_state: str) -> RunState:
        """How ``slug`` runs; before the reconciler runs, what the registry alone supports."""
        if self._reconciler is None:
            return state_from_desired(slug, "stopped" if desired_state == "stopped" else "running")
        return self._reconciler.run_state(slug, desired_state)

    def request_restart(self, slug: str) -> None:
        if self._reconciler is not None:
            self._reconciler.request_restart(slug)

    def app_renamed(self, old_slug: str, new_slug: str) -> None:
        if self._reconciler is not None:
            self._reconciler.app_renamed(old_slug, new_slug)

    async def stop_app(self, slug: str) -> None:
        if self._reconciler is not None:
            await self._reconciler.stop_app(slug)

    async def renamed(self, old_slug: str, new_slug: str) -> None:
        """The cascade of an approved rename request: the same move as a rename by edit.

        The reconciler only schedules the restart under the new name, so this cannot fail; a copy
        that does not come up leaves the old one serving and Restart tries again.
        """
        self.app_renamed(old_slug, new_slug)

    def memory_ledger(self) -> MemoryLedger | None:
        """The slot's memory books, or ``None`` before the reconciler (and its limit) exist."""
        return None if self._reconciler is None else self._reconciler.memory_ledger()

    # --- what the metrics sampler reads ---------------------------------------------------------

    def platform_sample(self) -> PlatformSample | None:
        """The ``_platform`` figures now, or ``None`` while the container states no memory."""
        reconciler = self._reconciler
        return read_platform_sample(
            children_running=0 if reconciler is None else reconciler.running_app_count(),
            children_registered=len(self._registry.snapshot.apps),
            root=self._metrics_root,
        )

    def child_probes(self) -> list[ChildProbe]:
        """One probe per running sandbox: its process tree's memory and its pump's counters."""
        if self._reconciler is None:
            return []
        root = self._metrics_root
        return [
            ChildProbe(
                slug=slug,
                instance=child.instance,
                pump_stats=child.pump_stats,
                rss_mb=functools.partial(read_process_tree_rss_mb, child.sandbox_pid, root),
            )
            for slug, child in self._reconciler.running_children()
            if isinstance(child, SandboxRunningChild)
        ]

    # --- the memory watchdog (TD-029) -----------------------------------------------------------

    def _start_memory_watch(self) -> None:
        # The same root as the sampler, so a test points both at one fake /proc.
        measure_rss_bytes = functools.partial(
            process_tree_rss_bytes, proc_root=self._metrics_root / "proc"
        )
        self._memory_watch_task = asyncio.create_task(
            watch(
                self._watched_children,
                self._stop_over_memory,
                interval_s=self._memory_watch_interval_s,
                measure_rss_bytes=measure_rss_bytes,
            ),
            name="memory watchdog",
        )

    def _watched_children(self) -> list[WatchedChild]:
        """Every running sandbox whose process tree can be found, with what it may hold."""
        if self._reconciler is None:
            return []
        watched: list[WatchedChild] = []
        for slug, child in self._reconciler.running_children():
            if not isinstance(child, SandboxRunningChild):
                continue
            root_pid = child.sandbox_pid
            if root_pid is None:  # still coming up: the supervisor has not said its pid
                continue
            watched.append(
                WatchedChild(
                    slug=slug,
                    root_pid=root_pid,
                    budget_mb=child.memory_budget_mb,
                    # The sandbox's own processes are in the tree; admission counts the same share.
                    allowance_mb=OVERHEAD_PER_CHILD_MB,
                )
            )
        return watched

    async def _stop_over_memory(self, offence: Offence) -> None:
        """Stop the one sandbox that ``offence`` names; the reconciler sees it end and says why."""
        if self._reconciler is None:
            return
        for _slug, child in self._reconciler.running_children():
            if isinstance(child, SandboxRunningChild) and child.sandbox_pid == offence.root_pid:
                await child.stop_over_memory(offence.reason)
                return
        # It ended between the reading and now (or was replaced): nothing is left to stop.

    async def _stop_memory_watch(self) -> None:
        task, self._memory_watch_task = self._memory_watch_task, None
        if task is None:
            return
        task.cancel()
        # Waited for, so no stop of the watchdog's is still under way while the children stop.
        await asyncio.gather(task, return_exceptions=True)

    def output(self, slug: str) -> OutputRing | None:
        """What ``slug``'s child wrote; ``None`` before the reconciler (or that child) ran."""
        return None if self._reconciler is None else self._reconciler.output(slug)

    async def stop(self) -> None:
        """``SIGTERM``: stop the children within the budget, then close the clients."""
        await self._stop_cleanup()
        await self._stop_memory_watch()
        try:
            if self._reconciler is not None:
                await self._reconciler.stop_all(self._stop_budget_s)
        finally:
            try:
                # After the children, so the last minute holds what they answered while stopping;
                # before the clients close and before ``__main__`` closes the registry it writes to.
                await self._metrics_service.stop()
            finally:
                try:
                    await self._upstream.aclose()
                finally:
                    await self._workspace.aclose()

    async def _stop_cleanup(self) -> None:
        task, self._cleanup_task = self._cleanup_task, None
        if task is None:
            return
        task.cancel()
        # The loop only ever ends by this cancel; waiting for it keeps a pass from outliving the
        # clients it uses.
        await asyncio.gather(task, return_exceptions=True)

    # --- the reconciler ---------------------------------------------------------------------

    def _reconciler_for(self, container_ip: IPv4Address, memory_limit_mb: int) -> Reconciler:
        registry = self._registry
        resolver = ResourceValueResolver(
            snapshot=lambda: registry.snapshot,
            reader=DatabricksSecretReader(
                self._workspace, token_provider=self._service_principal_token_async
            ),
        )
        # One cache for both uses: the check that refuses a release and the files the child reads.
        shared_sp_tokens = SharedSpTokens(
            ClientCredentialsMinter(
                self._workspace,
                client_id=self._settings.databricks_client_id,
                client_secret=self._settings.databricks_client_secret,
            )
        )
        app_environment = Path(sys.prefix)
        appmux_port = self._settings.databricks_app_port

        def global_rules() -> tuple[EgressRule, ...]:
            return global_egress_rules(registry.snapshot)

        overlays = OverlayBuilder(
            runner=SandboxStepRunner(
                run_base=self._run_dir / BUILD_RUN_DIR_NAME,
                app_environment=app_environment,
                container_ip=container_ip,
                appmux_port=appmux_port,
                global_rules=global_rules,
            ),
            store=WheelStore(self.content_store),
            cache_dir=self._overlays_dir,
            python=f"{sys.prefix}/bin/python",
            # A default: the package step reads the stored limit again at every build.
            build_timeout_s=PlatformSettings().build_timeout_s,
            index_environment=pip_index_environment(os.environ),
        )
        packages = PackageStep(
            overlays=overlays, policy=lambda slug: package_policy_from(registry.snapshot, slug)
        )
        return Reconciler(
            registry=registry,
            routes=self.routes,
            launcher=SandboxChildLauncher(
                run_base=self._run_dir,
                app_environment=app_environment,
                container_ip=container_ip,
                appmux_port=appmux_port,
                global_rules=global_rules,
                run_dir_files=_RunDirTokenFiles(SharedSpTokenFiles(shared_sp_tokens)),
            ),
            builder=StoreReleaseBuilder(self.content_store, self._releases_dir, packages),
            environments=ReleaseEnvironment(
                platform=platform_identity(self._settings),
                resources=ResourceValuesAdapter(resolver),
                credentials=ChildCredentialsAdapter(
                    ChildCredentials(
                        snapshot=lambda: registry.snapshot,
                        resources=resolver,
                        tokens=shared_sp_tokens,
                    )
                ),
            ),
            probe=UnixSocketHealthProbe(self._upstream),
            closer=self.closer,
            clock=SystemClock(),
            memory_limit_mb=memory_limit_mb,
            memory_reserve_mb=lambda: memory_reserve_mb(registry.snapshot),
            metrics=self.metrics,
        )
