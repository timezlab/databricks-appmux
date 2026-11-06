"""How the running service is put together (T095): the parts that share state, and their order.

``Service`` (``appmux/assembly.py``) owns what the edge and the reconciler share: the content store,
the Update flow, the route table's closer, the workspace client. These tests pin the decisions of
that wiring, not the parts themselves (each has its own tests):

* the content store is the service principal's own workspace folder, created at start-up, and a
  store that cannot be created stops the start;
* the reconciler starts only when the self-check passed (FR-013: no route opens otherwise), reads
  the container's memory limit once, and takes its memory reserve and global egress rules from the
  registry *each time they are asked*, so a saved setting reaches the next start;
* a stale run directory from an earlier process is gone before the first child starts;
* ``SIGTERM`` stops the children within the budget it is given and closes the clients whatever
  the stop did;
* the secret resolver's refusals reach the reconciler as ``EnvironmentRefused``.

No network call is made: the workspace is an ``httpx.MockTransport`` and the reconciler is a
recording stand-in.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tarfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from edge_harness import ALICE, DANA, NOW, app_row, seeded_registry
from pydantic import SecretStr
from service_harness import (
    CONTAINER_IP,
    RECORDED_LEDGER,
    RECORDED_RING,
    Closer,
    RecordingReconciler,
    Workspace,
    install_reconciler,
    sandbox_with_rules,
)
from stub_registry import StubRegistry

import appmux.assembly as assembly
from appmux.assembly import (
    Service,
    global_egress_rules,
    memory_reserve_mb,
)
from appmux.packages.modes import PackageMode
from appmux.packages.overlay import pip_index_environment
from appmux.packages.store import ContentStoreError
from appmux.packages.wheels import WheelStore
from appmux.reconciler.admission import OVERHEAD_PER_CHILD_MB
from appmux.reconciler.build import StoreReleaseBuilder
from appmux.reconciler.child_env import EnvironmentRefused
from appmux.reconciler.children import ChildStartError
from appmux.reconciler.route_table import RouteTable
from appmux.reconciler.sandbox_adapter import MEMORY_STOP_GRACE_S, SandboxRunningChild
from appmux.registry.client import RegistryClient
from appmux.registry.errors import RegistryError
from appmux.registry.models import Release
from appmux.sandbox.launcher import LAUNCH_SOCKET_NAME, SandboxExit
from appmux.sandbox.watchdog import RSS_HEADROOM_FACTOR
from appmux.secrets.resolver import MissingResourceKeys, SecretUnreadable, UnreadableSecret
from appmux.secrets.shared_sp_token import TOKEN_COMMAND_PATH
from appmux.settings import EnvironmentSettings, PlatformSettings
from appmux.upstream.client import UpstreamClient

pytestmark = pytest.mark.anyio

CLIENT_ID: Final = "client-id-placeholder"
STORE_FOLDER: Final = f"/Workspace/Users/{CLIENT_ID}/appmux-store"
SYNC_TOKEN: Final = "sync-token-placeholder"  # noqa: S105 - an obviously fake placeholder
STOP_BUDGET_S: Final = 4.0
METRICS_STOP_BUDGET_S: Final = 4.0


def environment(**overrides: str) -> EnvironmentSettings:
    return EnvironmentSettings.from_environment(
        {
            "DATABRICKS_HOST": "workspace.example.invalid",
            "DATABRICKS_CLIENT_ID": CLIENT_ID,
            "DATABRICKS_CLIENT_SECRET": "secret-placeholder-for-tests",
            "DATABRICKS_APP_PORT": "8123",
            "DATABRICKS_WORKSPACE_ID": "1234567890",
            "DATABRICKS_APP_URL": "https://appmux.example.invalid/",
            "PGHOST": "database.example.invalid",
            "APPMUX_LAKEBASE_DATABASE": "appmux",
            **overrides,
        }
    )


@pytest.fixture(autouse=True)
def recording_reconciler(monkeypatch: pytest.MonkeyPatch) -> None:
    install_reconciler(monkeypatch)


def make_service(
    tmp_path: Path,
    *,
    registry: StubRegistry | None = None,
    workspace: Workspace | None = None,
    is_self_check_passing: Callable[[], bool] = lambda: True,
    container_ip: Callable[[], IPv4Address | None] = lambda: CONTAINER_IP,
    memory_limit_mb: Callable[[], int] = lambda: 8192,
    upstream: UpstreamClient | None = None,
    cleanup_sleep: Callable[[float], Awaitable[None]] | None = None,
    metrics_root: Path | None = None,
    memory_watch_interval_s: float | None = None,
) -> Service:
    return Service(
        settings=environment(),
        registry=registry if registry is not None else seeded_registry(),
        workspace=(workspace if workspace is not None else Workspace()).client,
        service_principal_token=lambda: SYNC_TOKEN,
        service_principal_token_async=_async_token,
        routes=RouteTable(),
        upstream=upstream if upstream is not None else UpstreamClient(),
        closer=Closer(),
        is_self_check_passing=is_self_check_passing,
        container_ip=container_ip,
        memory_limit_mb=memory_limit_mb,
        stop_budget_s=STOP_BUDGET_S,
        metrics_stop_budget_s=METRICS_STOP_BUDGET_S,
        work_dir=tmp_path,
        **({} if cleanup_sleep is None else {"cleanup_sleep": cleanup_sleep}),
        **(
            {}
            if memory_watch_interval_s is None
            else {"memory_watch_interval_s": memory_watch_interval_s}
        ),
        # The tests' machine's own /sys and /proc are not the container's: an empty root by default.
        metrics_root=metrics_root if metrics_root is not None else tmp_path / "no-container",
    )


async def _async_token() -> SecretStr:
    return SecretStr(SYNC_TOKEN)


# --- what the admin routes ask of the running reconciler -----------------------------------


async def test_before_the_reconciler_runs_the_state_is_what_the_registry_wants(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path, is_self_check_passing=lambda: False)
    await service.start_children(seeded_registry())

    assert service.run_state("sales", "stopped") == "stopped"
    assert service.run_state("sales", "running") == "starting"


async def test_a_restart_a_rename_and_the_state_reach_the_reconciler_once_it_runs(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    await service.start_children(seeded_registry())
    (reconciler,) = RecordingReconciler.instances

    service.request_restart("sales")
    service.app_renamed("sales", "revenue")
    await service.renamed("docs", "manuals")
    state = service.run_state("sales", "running")

    assert reconciler.calls[1:] == [
        ("request_restart", "sales"),
        ("app_renamed", ("sales", "revenue")),
        ("app_renamed", ("docs", "manuals")),
        ("run_state", ("sales", "running")),
    ]
    assert state == "crashed"


async def test_before_the_reconciler_runs_no_restart_has_failed(tmp_path: Path) -> None:
    service = make_service(tmp_path, is_self_check_passing=lambda: False)
    await service.start_children(seeded_registry())

    assert service.last_restart_failure("sales") is None


async def test_the_reason_a_restart_failed_comes_from_the_reconciler_once_it_runs(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    await service.start_children(seeded_registry())
    (reconciler,) = RecordingReconciler.instances
    reconciler.restart_failures["sales"] = "the process exited with status 3 before the check"

    assert (
        service.last_restart_failure("sales") == "the process exited with status 3 before the check"
    )
    assert service.last_restart_failure("billing") is None
    assert ("last_restart_failure", "sales") in reconciler.calls


async def test_stopping_an_app_reaches_the_reconciler_once_it_runs(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    await service.start_children(seeded_registry())
    (reconciler,) = RecordingReconciler.instances

    await service.stop_app("sales")

    assert reconciler.calls[1:] == [("stop_app", "sales")]


async def test_stopping_an_app_with_no_reconciler_has_nothing_to_stop(tmp_path: Path) -> None:
    service = make_service(tmp_path, is_self_check_passing=lambda: False)
    await service.start_children(seeded_registry())

    await service.stop_app("sales")

    assert RecordingReconciler.instances == []


async def test_a_restart_or_a_rename_with_no_reconciler_has_nothing_to_move(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path, is_self_check_passing=lambda: False)
    await service.start_children(seeded_registry())

    service.request_restart("sales")
    service.app_renamed("sales", "revenue")
    await service.renamed("sales", "revenue")  # the rename is saved; no copy runs under either name

    assert RecordingReconciler.instances == []


async def test_the_memory_books_are_the_reconcilers_and_unknown_before_it_runs(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    assert service.memory_ledger() is None

    await service.start_children(seeded_registry())

    assert service.memory_ledger() == RECORDED_LEDGER


async def test_the_output_rings_are_the_reconcilers_and_none_before_it_runs(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)
    assert service.output("sales") is None

    await service.start_children(seeded_registry())

    assert service.output("sales") is RECORDED_RING
    (reconciler,) = RecordingReconciler.instances
    assert ("output", "sales") in reconciler.calls


def test_the_environment_route_reads_the_releases_the_builder_writes(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    assert service.child_setup.releases_dir == tmp_path / assembly.RELEASES_DIR_NAME
    assert service.child_setup.platform == assembly.platform_identity(environment())


# --- packages: the overlay builder and the Update's resolution ------------------------------

DIGEST: Final = "sha256:" + "ab" * 32
PIP_INDEX_URL: Final = "https://index.example.invalid/simple"
MISSING_PACKAGE: Final = "appmux-test-package-that-is-not-installed==1.0"


class RecordingParts:
    """Stands in for a class ``Service`` builds: keeps the arguments and the calls it got."""

    instances: list[RecordingParts]

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.ensured: list[dict[str, Any]] = []
        type(self).instances.append(self)

    async def ensure(self, **kwargs: Any) -> None:
        self.ensured.append(kwargs)


def install_parts(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[RecordingParts]]:
    """``Service`` builds recording stand-ins for the sandbox runner, the overlay builder and the
    Update's package resolution; the classes keep their instances apart."""
    parts: dict[str, type[RecordingParts]] = {}
    for name in ("SandboxStepRunner", "OverlayBuilder", "UpdatePackages", "UpdateService"):
        parts[name] = type(name, (RecordingParts,), {"instances": []})
        monkeypatch.setattr(assembly, name, parts[name])
    return parts


def snapshot_with_requirements(path: Path, *requirements: str) -> None:
    with tarfile.open(path, "w") as archive:
        for name, text in (
            ("app.py", "print('hi')\n"),
            ("requirements.txt", "\n".join(requirements) + "\n"),
        ):
            source = path.parent / f"{name}.part"
            source.write_text(text)
            archive.add(source, arcname=name)
            source.unlink()


async def test_a_release_with_requirements_gets_its_overlay_from_the_builder_of_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = install_parts(monkeypatch)
    monkeypatch.setenv("PIP_INDEX_URL", PIP_INDEX_URL)
    registry = seeded_registry()
    registry.seed(
        "platform_settings",
        {"key": "package_mode", "value": "index", "updated_by": ALICE, "updated_at": NOW},
    )
    registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": 1,
            "snapshot_digest": DIGEST,
            "detected": {"runtime": "python", "command": ["python", "app.py"], "env": []},
            "config": {"env": []},
            "wheels": [],
            "status": "pending",
            "status_at": NOW,
            "created_by": ALICE,
            "created_at": NOW,
        },
    )
    service = make_service(tmp_path, registry=registry)
    archive_source = tmp_path / "snapshot.tar"
    snapshot_with_requirements(archive_source, MISSING_PACKAGE)

    snapshot_bytes = archive_source.read_bytes()

    async def stored_snapshot(digest: str, destination: Path) -> None:
        assert digest == DIGEST
        await asyncio.to_thread(destination.write_bytes, snapshot_bytes)

    monkeypatch.setattr(service.content_store, "get", stored_snapshot)
    await service.start_children(registry)
    (reconciler,) = RecordingReconciler.instances
    builder = reconciler.kwargs["builder"]
    release = registry.snapshot.release("sales", 1)
    assert release is not None

    await builder.build(release)

    assert isinstance(builder, StoreReleaseBuilder)
    (overlays,) = parts["OverlayBuilder"].instances
    (runner,) = parts["SandboxStepRunner"].instances
    assert overlays.kwargs["runner"] is runner
    assert isinstance(overlays.kwargs["store"], WheelStore)
    assert overlays.kwargs["cache_dir"] == tmp_path / "overlays"
    assert overlays.kwargs["python"] == f"{sys.prefix}/bin/python"
    assert overlays.kwargs["build_timeout_s"] == PlatformSettings().build_timeout_s
    assert overlays.kwargs["index_environment"] == pip_index_environment(os.environ)
    assert overlays.kwargs["index_environment"]["PIP_INDEX_URL"] == PIP_INDEX_URL
    (ensured,) = overlays.ensured
    assert ensured["mode"] is PackageMode.INDEX
    assert list(ensured["index_requirements"]) == [MISSING_PACKAGE]
    assert ensured["wheels"] == []


async def test_the_build_sandbox_runs_beside_the_children_with_the_same_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = install_parts(monkeypatch)
    registry = seeded_registry()
    service = make_service(tmp_path, registry=registry)

    await service.start_children(registry)

    (runner,) = parts["SandboxStepRunner"].instances
    assert runner.kwargs["run_base"] == tmp_path / "run" / "build"
    assert runner.kwargs["app_environment"] == Path(sys.prefix)
    assert runner.kwargs["container_ip"] == CONTAINER_IP
    assert runner.kwargs["appmux_port"] == 8123
    assert tuple(runner.kwargs["global_rules"]()) == global_egress_rules(registry.snapshot)


async def test_the_update_resolves_packages_through_the_content_store_and_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = install_parts(monkeypatch)

    service = make_service(tmp_path)

    (packages,) = parts["UpdatePackages"].instances
    assert isinstance(packages.kwargs["store"], WheelStore)
    (updater,) = parts["UpdateService"].instances
    assert updater.kwargs["packages"] is packages
    assert service.updater is updater


def test_the_build_sandbox_socket_fits_in_a_unix_socket_path() -> None:
    # <run>/build/build-<8 characters>/launch.sock (``tempfile.mkdtemp`` adds eight).
    longest = assembly.RUN_DIR / "build" / ("build-" + "x" * 8) / LAUNCH_SOCKET_NAME

    assert len(str(longest).encode()) <= 107


# --- the daily content-store clean-up -------------------------------------------------------


class Clock:
    """A ``sleep`` that returns at once for ``passes`` waits and then never; keeps the delays."""

    def __init__(self, passes: int) -> None:
        self.delays: list[float] = []
        self._passes = passes

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        if len(self.delays) > self._passes:
            await asyncio.Event().wait()
        await asyncio.sleep(0)


class CleanupCalls:
    """``run_cleanup_once`` as the loop calls it; the first ``failures`` calls raise."""

    def __init__(self, *, wanted: int = 1, failures: int = 0) -> None:
        self.calls: list[tuple[object, object]] = []
        self.done = asyncio.Event()  # set once ``wanted`` calls were made
        self._wanted = wanted
        self._failures = failures

    async def __call__(self, files: object, registry: object) -> None:
        self.calls.append((files, registry))
        if len(self.calls) >= self._wanted:
            self.done.set()
        if len(self.calls) <= self._failures:
            raise RuntimeError("the clean-up broke in a way nobody expected")


async def test_the_cleanup_runs_after_the_first_delay_and_then_every_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(passes=3)
    cleanup = CleanupCalls(wanted=3)
    monkeypatch.setattr(assembly, "run_cleanup_once", cleanup)
    registry = seeded_registry()
    service = make_service(tmp_path, registry=registry, cleanup_sleep=clock.sleep)

    await service.start_cleanup(registry)
    await asyncio.wait_for(cleanup.done.wait(), timeout=5)
    await service.stop()

    assert clock.delays[:3] == [
        assembly.CLEANUP_FIRST_DELAY_S,
        assembly.CLEANUP_INTERVAL_S,
        assembly.CLEANUP_INTERVAL_S,
    ]
    assert cleanup.calls[0] == (service.content_store, registry)


async def test_no_cleanup_runs_before_the_first_delay_is_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(passes=0)
    cleanup = CleanupCalls()
    monkeypatch.setattr(assembly, "run_cleanup_once", cleanup)
    registry = seeded_registry()
    service = make_service(tmp_path, registry=registry, cleanup_sleep=clock.sleep)

    await service.start_cleanup(registry)
    await asyncio.sleep(0.01)
    await service.stop()

    assert cleanup.calls == []


async def test_a_cleanup_that_breaks_is_logged_and_the_next_day_tries_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock(passes=2)
    cleanup = CleanupCalls(wanted=2, failures=1)
    monkeypatch.setattr(assembly, "run_cleanup_once", cleanup)
    registry = seeded_registry()
    service = make_service(tmp_path, registry=registry, cleanup_sleep=clock.sleep)

    with caplog.at_level("ERROR", logger=assembly.logger.name):
        await service.start_cleanup(registry)
        await asyncio.wait_for(cleanup.done.wait(), timeout=5)
        await service.stop()

    assert len(cleanup.calls) == 2
    assert any("clean-up" in record.getMessage() for record in caplog.records)


async def test_starting_the_cleanup_twice_runs_one_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(passes=0)
    monkeypatch.setattr(assembly, "run_cleanup_once", CleanupCalls())
    registry = seeded_registry()
    service = make_service(tmp_path, registry=registry, cleanup_sleep=clock.sleep)

    await service.start_cleanup(registry)
    await service.start_cleanup(registry)
    await asyncio.sleep(0.01)
    await service.stop()

    assert clock.delays == [assembly.CLEANUP_FIRST_DELAY_S]


# --- the metrics the edge counts ------------------------------------------------------------


async def stored_minutes(registry: StubRegistry, slug: str) -> int:
    now = datetime.now(UTC)
    rows = await registry.read_metrics(
        app_slug=slug, start=now - timedelta(days=1), end=now + timedelta(days=1), limit=10
    )
    return sum(row.requests for row in rows)


async def test_the_requests_the_edge_counts_are_stored_when_the_service_stops(
    tmp_path: Path,
) -> None:
    registry = seeded_registry()
    service = make_service(tmp_path, registry=registry)
    await service.start_metrics(registry)

    service.metrics.record_request("sales", status=200, latency_ms=12.0)
    service.metrics.record_request("sales", status=500, latency_ms=30.0)
    assert await stored_minutes(registry, "sales") == 0  # a minute is written when it is over
    await service.stop()

    assert await stored_minutes(registry, "sales") == 2


async def test_the_metrics_start_once_and_stopping_one_that_never_started_is_harmless(
    tmp_path: Path,
) -> None:
    registry = seeded_registry()
    never_started = make_service(tmp_path / "never", registry=registry)
    started_twice = make_service(tmp_path / "twice", registry=registry)

    await started_twice.start_metrics(registry)
    await started_twice.start_metrics(registry)
    await started_twice.stop()
    await never_started.stop()


async def test_the_final_flush_comes_before_the_clients_close(tmp_path: Path) -> None:
    registry = seeded_registry()
    workspace = Workspace()
    service = make_service(tmp_path, registry=registry, workspace=workspace)
    await service.start_metrics(registry)
    service.metrics.record_request("sales", status=200, latency_ms=5.0)
    stored_before_close: list[int] = []
    real_aclose = workspace.client.aclose

    async def aclose_after_noting() -> None:
        stored_before_close.append(await stored_minutes(registry, "sales"))
        await real_aclose()

    workspace.client.aclose = aclose_after_noting  # type: ignore[method-assign]

    await service.stop()

    assert stored_before_close == [1]


# --- what the metrics sampler reads (T192) --------------------------------------------------------

MB: Final = 1024 * 1024
PAGE_SIZE: Final = os.sysconf("SC_PAGE_SIZE")


def fake_container(root: Path, *, used_mb: int = 2100, limit_mb: int = 8192) -> Path:
    """A cgroup v1 container (as Free Edition measured) and this process's statm, under ``root``."""
    files = {
        "sys/fs/cgroup/memory/memory.usage_in_bytes": f"{used_mb * MB}\n",
        "sys/fs/cgroup/memory/memory.limit_in_bytes": f"{limit_mb * MB}\n",
        "proc/self/statm": f"90000 {180 * MB // PAGE_SIZE} 3000 100 0 40000 0\n",
    }
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    return root


def fake_process(root: Path, pid: int, parent_pid: int, resident_mb: int) -> None:
    directory = root / "proc" / str(pid)
    directory.mkdir(parents=True)
    (directory / "stat").write_text(f"{pid} (worker) S {parent_pid} {pid} {pid} 0 -1\n")
    (directory / "statm").write_text(f"9999 {resident_mb * MB // PAGE_SIZE} 100 1 0 50 0\n")


class SandboxStandIn:
    """The part of ``SandboxHandle`` a running sandbox child reads for its metrics."""

    def __init__(self, sandbox_pid: int | None) -> None:
        self.sandbox_pid = sandbox_pid

    async def wait_started(self, *, timeout_s: float) -> None:
        return None

    async def wait(self) -> Any:
        raise AssertionError("not used")

    async def stop(self, *, grace_s: float) -> Any:
        raise AssertionError("not used")

    async def read_stats(self, *, timeout_s: float) -> Any:
        return None


def sandbox_child(
    root: Path,
    slug: str,
    instance: str,
    sandbox_pid: int | None,
    *,
    memory_budget_mb: int = 256,
    process: SandboxStandIn | None = None,
) -> SandboxRunningChild:
    scratch = root / "run" / slug / instance
    return SandboxRunningChild(
        process if process is not None else SandboxStandIn(sandbox_pid),
        socket_path=scratch / "relay.sock",
        log_path=scratch / "child.log",
        stderr_path=scratch / "child.err",
        memory_budget_mb=memory_budget_mb,
        rules_path=scratch / "rules.json",
    )


def metrics_service(tmp_path: Path, registry: StubRegistry, root: Path) -> Service:
    return make_service(tmp_path / "work", registry=registry, metrics_root=root)


async def let_the_first_sample_run() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_the_platform_sample_reads_the_container_and_counts_the_apps(tmp_path: Path) -> None:
    registry = seeded_registry()
    service = metrics_service(tmp_path, registry, fake_container(tmp_path / "root"))
    await service.start_children(registry)
    RecordingReconciler.instances[-1].app_count = 1

    sample = service.platform_sample()

    assert sample is not None
    assert (sample.mem_used_mb, sample.mem_limit_mb, sample.edge_rss_mb) == (2100, 8192, 180)
    assert sample.children_running == 1
    assert sample.children_registered == len(registry.snapshot.apps)


async def test_before_the_reconciler_runs_no_child_is_running(tmp_path: Path) -> None:
    registry = seeded_registry()
    service = metrics_service(tmp_path, registry, fake_container(tmp_path / "root"))

    sample = service.platform_sample()

    assert sample is not None
    assert sample.children_running == 0
    assert service.child_probes() == []


async def test_a_platform_row_is_stored_when_the_container_states_its_memory(
    tmp_path: Path,
) -> None:
    registry = seeded_registry()
    service = metrics_service(tmp_path, registry, fake_container(tmp_path / "root"))
    await service.start_metrics(registry)
    await let_the_first_sample_run()

    await service.stop()

    rows = await registry.read_metrics(
        app_slug="_platform",
        start=datetime.now(UTC) - timedelta(days=1),
        end=datetime.now(UTC) + timedelta(days=1),
        limit=10,
    )
    assert [row.mem_used_mb for row in rows] == [2100]


async def test_no_platform_row_is_stored_when_no_file_states_the_memory(tmp_path: Path) -> None:
    registry = seeded_registry()
    service = metrics_service(tmp_path, registry, tmp_path / "empty-root")
    await service.start_metrics(registry)
    service.metrics.record_request("sales", status=200, latency_ms=5.0)
    await let_the_first_sample_run()

    await service.stop()

    assert service.platform_sample() is None
    assert await stored_minutes(registry, "sales") == 1
    rows = await registry.read_metrics(
        app_slug="_platform",
        start=datetime.now(UTC) - timedelta(days=1),
        end=datetime.now(UTC) + timedelta(days=1),
        limit=10,
    )
    assert rows == ()


async def test_each_running_sandbox_is_probed_for_its_memory_and_its_pump(tmp_path: Path) -> None:
    root = fake_container(tmp_path / "root")
    fake_process(root, 500, 1, 20)  # the sandbox's root
    fake_process(root, 501, 500, 190)  # the child
    registry = seeded_registry()
    service = metrics_service(tmp_path, registry, root)
    await service.start_children(registry)
    live = sandbox_child(tmp_path, "sales", "1-1", sandbox_pid=500)
    incoming = sandbox_child(tmp_path, "sales", "2-2", sandbox_pid=None)
    RecordingReconciler.instances[-1].children = [("sales", live), ("sales", incoming)]

    probes = service.child_probes()

    assert [(probe.slug, probe.instance) for probe in probes] == [
        ("sales", "1-1"),
        ("sales", "2-2"),
    ]
    assert [probe.rss_mb() for probe in probes] == [210, None]
    assert await probes[0].pump_stats() is None


async def test_the_reconciler_counts_into_the_same_aggregator_the_edge_feeds(
    tmp_path: Path,
) -> None:
    registry = seeded_registry()
    service = make_service(tmp_path, registry=registry)

    await service.start_children(registry)

    assert RecordingReconciler.instances[-1].kwargs["metrics"] is service.metrics


async def test_the_registry_client_reports_its_calls_to_the_aggregator(tmp_path: Path) -> None:
    async def no_database(parameters: Any) -> Any:
        raise OSError("connection refused")

    async def token() -> SecretStr:
        return SecretStr(SYNC_TOKEN)

    client = RegistryClient(
        host="database.example.invalid",
        port=5432,
        database="appmux",
        user="sp-client-id",
        token_provider=token,
        connection_factory=no_database,
    )
    service = Service(
        settings=environment(),
        registry=client,
        workspace=Workspace().client,
        service_principal_token=lambda: SYNC_TOKEN,
        service_principal_token_async=_async_token,
        routes=RouteTable(),
        upstream=UpstreamClient(),
        closer=Closer(),
        is_self_check_passing=lambda: True,
        container_ip=lambda: CONTAINER_IP,
        stop_budget_s=STOP_BUDGET_S,
        metrics_stop_budget_s=METRICS_STOP_BUDGET_S,
        work_dir=tmp_path / "work",
        metrics_root=fake_container(tmp_path / "root"),
    )
    service.metrics.observe_platform(
        mem_used_mb=1, mem_limit_mb=1, children_running=0, children_registered=0, edge_rss_mb=1
    )

    with pytest.raises(RegistryError):
        await client.fetch_rows("apps")

    (platform,) = [
        row
        for row in service.metrics.drain(include_open_minute=True)
        if row.app_slug == "_platform"
    ]
    assert platform.registry_errors == 1


# --- the content store ----------------------------------------------------------------------


async def test_the_store_folder_is_created_in_the_service_principals_own_workspace_folder(
    tmp_path: Path,
) -> None:
    workspace = Workspace()
    service = make_service(tmp_path, workspace=workspace)

    await service.ensure_store_folder(seeded_registry())

    (request,) = workspace.requests
    assert request.url.path == "/api/2.0/workspace/mkdirs"
    assert json.loads(request.content) == {"path": STORE_FOLDER}
    assert request.headers["authorization"] == f"Bearer {SYNC_TOKEN}"


async def test_a_store_folder_that_cannot_be_created_stops_the_start(tmp_path: Path) -> None:
    service = make_service(tmp_path, workspace=Workspace(status=500))

    with pytest.raises(ContentStoreError):
        await service.ensure_store_folder(seeded_registry())


# --- starting the children ------------------------------------------------------------------


async def test_a_failed_self_check_starts_no_app(tmp_path: Path) -> None:
    asked_for_limit: list[bool] = []

    def memory_limit() -> int:
        asked_for_limit.append(True)
        return 8192

    service = make_service(
        tmp_path, is_self_check_passing=lambda: False, memory_limit_mb=memory_limit
    )

    await service.start_children(seeded_registry())
    service.wake("sales")

    assert RecordingReconciler.instances == []
    assert asked_for_limit == []


async def test_a_passed_self_check_starts_the_reconciler_once(tmp_path: Path) -> None:
    service = make_service(tmp_path, memory_limit_mb=lambda: 6144)

    await service.start_children(seeded_registry())
    await service.start_children(seeded_registry())

    (reconciler,) = RecordingReconciler.instances
    assert reconciler.calls == [("start", None)]
    assert reconciler.kwargs["memory_limit_mb"] == 6144
    assert reconciler.kwargs["routes"] is service.routes
    assert reconciler.kwargs["closer"] is service.closer


async def test_a_service_without_a_given_limit_reads_it_through_the_module_function_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The wiring passes no limit reader. The default must be looked up when the children start,
    # not bound when the module loads, or a test (and a host without a readable limit) cannot
    # replace it and the start would read the real cgroup of whatever machine it runs on.
    monkeypatch.setattr(assembly, "read_container_memory_limit_mb", lambda: 3072)
    service = Service(
        settings=environment(),
        registry=seeded_registry(),
        workspace=Workspace().client,
        service_principal_token=lambda: SYNC_TOKEN,
        service_principal_token_async=_async_token,
        routes=RouteTable(),
        upstream=UpstreamClient(),
        closer=Closer(),
        is_self_check_passing=lambda: True,
        container_ip=lambda: CONTAINER_IP,
        stop_budget_s=STOP_BUDGET_S,
        metrics_stop_budget_s=METRICS_STOP_BUDGET_S,
        work_dir=tmp_path,
    )

    await service.start_children(seeded_registry())

    (reconciler,) = RecordingReconciler.instances
    assert reconciler.kwargs["memory_limit_mb"] == 3072


async def test_the_memory_reserve_is_read_from_the_registry_each_time_it_is_asked(
    tmp_path: Path,
) -> None:
    registry = seeded_registry()
    service = make_service(tmp_path, registry=registry)
    await service.start_children(registry)
    (reconciler,) = RecordingReconciler.instances
    reserve: Callable[[], int] = reconciler.kwargs["memory_reserve_mb"]

    before = reserve()
    registry.seed(
        "platform_settings",
        {"key": "memory_reserve_mb", "value": 2048, "updated_by": ALICE, "updated_at": NOW},
    )

    assert (before, reserve()) == (PlatformSettings().memory_reserve_mb, 2048)


async def test_an_unreadable_memory_limit_stops_the_start(tmp_path: Path) -> None:
    def unreadable() -> int:
        raise assembly.MemoryLimitUnreadableError("no source states a limit")

    service = make_service(tmp_path, memory_limit_mb=unreadable)

    with pytest.raises(assembly.MemoryLimitUnreadableError):
        await service.start_children(seeded_registry())

    assert RecordingReconciler.instances == []


async def test_an_unknown_container_address_stops_the_start(tmp_path: Path) -> None:
    # A passed self-check implies the address was known; if it vanished since, nothing is
    # started against a guess.
    service = make_service(tmp_path, container_ip=lambda: None)

    with pytest.raises(RuntimeError, match="address"):
        await service.start_children(seeded_registry())

    assert RecordingReconciler.instances == []


async def test_a_run_directory_left_by_an_earlier_process_is_gone_before_the_first_child(
    tmp_path: Path,
) -> None:
    stale = tmp_path / "run" / "sales" / "3-1"
    stale.mkdir(parents=True)
    (stale / "relay.sock").write_text("left over")
    elsewhere = tmp_path / "releases" / "sales" / "3"
    elsewhere.mkdir(parents=True)
    service = make_service(tmp_path)

    await service.start_children(seeded_registry())

    assert not (tmp_path / "run").exists()
    assert elsewhere.is_dir()


async def test_a_new_release_wakes_the_reconciler_once_it_runs(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.wake("sales")  # before the start: nothing to wake, and no error
    await service.start_children(seeded_registry())

    service.wake("sales")

    (reconciler,) = RecordingReconciler.instances
    assert reconciler.calls == [("start", None), ("wake", "sales")]


# --- the memory watchdog (TD-029) --------------------------------------------------------------

MEMORY_WATCH_INTERVAL_S: Final = 0.01
WATCH_TIMEOUT_S: Final = 5.0


class StoppableSandboxStandIn(SandboxStandIn):
    """A sandbox that runs until it is told to stop, and notes how it was told."""

    def __init__(self, sandbox_pid: int | None) -> None:
        super().__init__(sandbox_pid)
        self.stopped_with: list[float] = []
        self.is_stopped = asyncio.Event()

    async def wait(self) -> SandboxExit:
        await self.is_stopped.wait()
        return SandboxExit(status=137, signal=9)

    async def stop(self, *, grace_s: float) -> SandboxExit:
        self.stopped_with.append(grace_s)
        self.is_stopped.set()
        return SandboxExit(status=137, signal=9)


def watched_sandbox(
    root: Path, slug: str, *, sandbox_pid: int, own_mb: int, app_mb: int, budget_mb: int
) -> tuple[SandboxRunningChild, StoppableSandboxStandIn]:
    """A sandbox whose root holds ``own_mb`` and whose app (a child of it) holds ``app_mb``."""
    fake_process(root, sandbox_pid, 1, own_mb)
    fake_process(root, sandbox_pid + 1, sandbox_pid, app_mb)
    process = StoppableSandboxStandIn(sandbox_pid)
    child = sandbox_child(
        root, slug, f"1-{sandbox_pid}", sandbox_pid, memory_budget_mb=budget_mb, process=process
    )
    return child, process


async def watched_service(
    tmp_path: Path, *children: tuple[str, SandboxRunningChild]
) -> tuple[Service, StubRegistry]:
    registry = seeded_registry()
    service = make_service(
        tmp_path / "work",
        registry=registry,
        metrics_root=tmp_path / "root",
        memory_watch_interval_s=MEMORY_WATCH_INTERVAL_S,
    )
    await service.start_children(registry)
    RecordingReconciler.instances[-1].children = list(children)
    return service, registry


async def a_few_rounds_of_the_watch() -> None:
    await asyncio.sleep(MEMORY_WATCH_INTERVAL_S * 20)


async def test_a_sandbox_over_its_limit_is_stopped_with_the_reason_and_its_neighbour_is_not(
    tmp_path: Path,
) -> None:
    root = fake_container(tmp_path / "root")
    hog, hog_process = watched_sandbox(
        root, "sales", sandbox_pid=500, own_mb=20, app_mb=600, budget_mb=256
    )
    tidy, tidy_process = watched_sandbox(
        root, "docs", sandbox_pid=600, own_mb=20, app_mb=100, budget_mb=256
    )
    service, _ = await watched_service(tmp_path, ("sales", hog), ("docs", tidy))

    await asyncio.wait_for(hog_process.is_stopped.wait(), timeout=WATCH_TIMEOUT_S)
    exit_ = await asyncio.wait_for(hog.wait(), timeout=WATCH_TIMEOUT_S)
    await a_few_rounds_of_the_watch()

    limit_mb = int(256 * RSS_HEADROOM_FACTOR + OVERHEAD_PER_CHILD_MB)
    assert exit_.description.startswith(
        f"uses 620 MB, over its limit of {limit_mb} MB "
        f"(budget 256 MB, plus {OVERHEAD_PER_CHILD_MB} MB for the sandbox itself); "
    )
    assert hog_process.stopped_with == [MEMORY_STOP_GRACE_S]
    assert tidy_process.stopped_with == []
    await service.stop()


@pytest.mark.parametrize("budget_mb", [64, 256, 1024])
async def test_an_app_inside_its_budget_is_not_stopped_for_the_sandboxs_own_memory(
    tmp_path: Path, budget_mb: int
) -> None:
    # The tree holds the sandbox's processes beside the app: 61 MB measured on a container
    # (TD-032). Comparing the tree with the budget alone would stop every small app.
    root = fake_container(tmp_path / "root")
    child, process = watched_sandbox(
        root, "sales", sandbox_pid=500, own_mb=61, app_mb=budget_mb, budget_mb=budget_mb
    )
    service, _ = await watched_service(tmp_path, ("sales", child))

    await a_few_rounds_of_the_watch()

    assert process.stopped_with == []
    await service.stop()


async def test_a_sandbox_that_has_not_reported_its_pid_is_not_measured_or_stopped(
    tmp_path: Path,
) -> None:
    root = fake_container(tmp_path / "root")
    coming_up = sandbox_child(root, "sales", "1-1", sandbox_pid=None, memory_budget_mb=64)
    service, _ = await watched_service(tmp_path, ("sales", coming_up))

    await a_few_rounds_of_the_watch()

    await service.stop()  # nothing to stop, and nothing raised on the way


async def test_starting_the_children_twice_stops_an_offender_once(tmp_path: Path) -> None:
    root = fake_container(tmp_path / "root")
    hog, process = watched_sandbox(
        root, "sales", sandbox_pid=500, own_mb=20, app_mb=600, budget_mb=256
    )
    service, registry = await watched_service(tmp_path, ("sales", hog))

    await service.start_children(registry)
    await asyncio.wait_for(process.is_stopped.wait(), timeout=WATCH_TIMEOUT_S)
    await a_few_rounds_of_the_watch()

    assert len(process.stopped_with) == 1
    await service.stop()


async def test_stopping_the_service_ends_the_watch(tmp_path: Path) -> None:
    root = fake_container(tmp_path / "root")
    service, _ = await watched_service(tmp_path)
    await service.stop()
    hog, process = watched_sandbox(
        root, "sales", sandbox_pid=500, own_mb=20, app_mb=600, budget_mb=256
    )
    RecordingReconciler.instances[-1].children = [("sales", hog)]

    await a_few_rounds_of_the_watch()

    assert process.stopped_with == []


async def test_a_failed_self_check_starts_no_watch(tmp_path: Path) -> None:
    service = make_service(
        tmp_path, is_self_check_passing=lambda: False, memory_watch_interval_s=0.01
    )

    await service.start_children(seeded_registry())

    assert [task for task in asyncio.all_tasks() if task.get_name() == "memory watchdog"] == []
    await service.stop()


# --- stopping -------------------------------------------------------------------------------


async def test_sigterm_stops_the_children_within_the_budget_and_closes_the_clients(
    tmp_path: Path,
) -> None:
    workspace = Workspace()
    upstream = UpstreamClient()
    service = make_service(tmp_path, workspace=workspace, upstream=upstream)
    await service.start_children(seeded_registry())

    await service.stop()

    (reconciler,) = RecordingReconciler.instances
    assert reconciler.calls[-1] == ("stop_all", STOP_BUDGET_S)
    assert workspace.client.is_closed


async def test_the_clients_are_closed_even_when_the_children_cannot_all_be_stopped(
    tmp_path: Path,
) -> None:
    workspace = Workspace()
    service = make_service(tmp_path, workspace=workspace)
    await service.start_children(seeded_registry())
    (reconciler,) = RecordingReconciler.instances
    reconciler.stop_error = OSError("a child would not die")

    with pytest.raises(OSError, match="would not die"):
        await service.stop()

    assert workspace.client.is_closed


async def test_stopping_a_service_whose_children_never_started_only_closes_the_clients(
    tmp_path: Path,
) -> None:
    workspace = Workspace()
    service = make_service(tmp_path, workspace=workspace, is_self_check_passing=lambda: False)
    await service.start_children(seeded_registry())

    await service.stop()

    assert workspace.client.is_closed
    assert RecordingReconciler.instances == []


# --- what the registry decides --------------------------------------------------------------


def test_the_memory_reserve_falls_back_to_the_default_for_a_value_that_is_not_usable() -> None:
    registry = seeded_registry()
    default = PlatformSettings().memory_reserve_mb
    assert memory_reserve_mb(registry.snapshot) == default

    # Every value a stored setting can hold that is not a positive integer. `None` and floats are
    # left out because the row model (`SettingValue`) refuses them before the registry stores one.
    for unusable in (True, 0, -5, "2048"):
        registry = seeded_registry()
        registry.seed(
            "platform_settings",
            {"key": "memory_reserve_mb", "value": unusable, "updated_by": ALICE, "updated_at": NOW},
        )
        assert memory_reserve_mb(registry.snapshot) == default, unusable


def test_the_global_egress_rules_are_the_stored_list_or_else_the_closed_default() -> None:
    default = tuple(PlatformSettings().egress_global)
    stored = [{"action": "deny", "cidr": "203.0.113.0/24", "port": "any", "comment": "test net"}]
    registry = seeded_registry()
    assert global_egress_rules(registry.snapshot) == default

    registry.seed(
        "platform_settings",
        {"key": "egress_global", "value": stored, "updated_by": ALICE, "updated_at": NOW},
    )
    assert [rule.cidr for rule in global_egress_rules(registry.snapshot)] == ["203.0.113.0/24"]

    # The row model checks the shape of a rule, so a malformed rule never reaches the registry; what
    # it cannot catch is a value of another kind under this key. That must not open anything either:
    # the closed default applies.
    for not_a_rule_list in (True, 7, "allow all", ["allow all"]):
        broken = seeded_registry()
        broken.seed(
            "platform_settings",
            {
                "key": "egress_global",
                "value": not_a_rule_list,
                "updated_by": ALICE,
                "updated_at": NOW,
            },
        )
        assert global_egress_rules(broken.snapshot) == default, not_a_rule_list


# --- the resources of a child ---------------------------------------------------------------


class RefusingResolver:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def resolve(self, app_slug: str, keys: Any) -> dict[str, str]:
        raise self.error


async def test_a_detached_resource_refuses_the_environment_with_the_resolvers_words() -> None:
    adapter = assembly.ResourceValuesAdapter(RefusingResolver(MissingResourceKeys(["warehouse"])))

    with pytest.raises(EnvironmentRefused) as refused:
        await adapter.resolve("sales", ["warehouse"])

    assert "warehouse" in str(refused.value)
    assert isinstance(refused.value.__cause__, MissingResourceKeys)


async def test_an_unreadable_secret_refuses_the_environment_without_a_value() -> None:
    failure = UnreadableSecret("secret scope 'ops'", "ops", "denied")
    adapter = assembly.ResourceValuesAdapter(RefusingResolver(SecretUnreadable([failure])))

    with pytest.raises(EnvironmentRefused) as refused:
        await adapter.resolve("sales", ["token"])

    assert "ops" in str(refused.value)


# --- the identity the children are told -----------------------------------------------------


def test_the_children_are_told_the_workspace_and_the_parent_url_without_a_trailing_slash() -> None:
    identity = assembly.platform_identity(environment())

    assert identity.workspace_host == "workspace.example.invalid"
    assert identity.workspace_id == "1234567890"
    assert identity.parent_url == "https://appmux.example.invalid"


async def test_the_work_directories_are_short_enough_for_a_unix_socket() -> None:
    # relay.sock sits at <run>/<slug>/<release>-<incarnation>/; a 31-character slug is the longest.
    longest = assembly.RUN_DIR / ("a" * 31) / "99999-999" / "relay.sock"

    assert len(str(longest).encode()) <= 107


# --- the Databricks credential of a child, and the files the shared one needs -------------------

MINTED_TOKEN: Final = "tok-assembly-test-value"  # noqa: S105 - an obviously fake placeholder
OWN_SP_ID: Final = "own-sp-client-id-placeholder"
OWN_SP_SECRET: Final = "own-sp-client-secret-placeholder"  # noqa: S105 - a fake placeholder


class MintingWorkspace(Workspace):
    """A workspace that mints a token (or refuses) and holds an own service principal's secrets."""

    def __init__(self, *, mint_status: int = 200) -> None:
        self.mint_status = mint_status
        super().__init__()

    def _answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oidc/v1/token":
            if self.mint_status != 200:
                return httpx.Response(self.mint_status, json={"error": "invalid_client"})
            return httpx.Response(
                200, json={"access_token": MINTED_TOKEN, "token_type": "Bearer", "expires_in": 3600}
            )
        if request.url.path == "/api/2.0/secrets/get":
            value = {"id-key": OWN_SP_ID, "secret-key": OWN_SP_SECRET}[request.url.params["key"]]
            return httpx.Response(200, json={"value": base64.b64encode(value.encode()).decode()})
        return httpx.Response(200, json={})


def registry_with_credentials(*, shared_sp: bool = False, own_sp: bool = False) -> StubRegistry:
    registry = StubRegistry(clock=lambda: NOW)
    app = {**app_row("sales", owner=ALICE), "shared_sp_delegated": shared_sp}
    registry.seed("apps", app)
    keys = {"own_sp_client_id_key": "sp-id", "own_sp_client_secret_key": "sp-secret"}
    registry.seed(
        "app_config",
        {
            "app_slug": "sales",
            "desired_state": "running",
            "updated_by": DANA,
            "updated_at": NOW,
            **(keys if own_sp else {}),
        },
    )
    if own_sp:
        for key, target in (("sp-id", "scope/id-key"), ("sp-secret", "scope/secret-key")):
            registry.seed(
                "app_resources",
                {
                    "app_slug": "sales",
                    "key": key,
                    "type": "secret",
                    "target": target,
                    "permission": "READ",
                    "access_as": None,
                    "check_result": "ok",
                    "attached_by": ALICE,
                    "attached_at": NOW,
                },
            )
    registry.seed(
        "platform_settings",
        {
            "key": "shared_sp_delegation_allowed",
            "value": True,
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )
    return registry


def sales_release() -> Release:
    return Release.model_validate(
        {
            "app_slug": "sales",
            "release_no": 1,
            "snapshot_digest": DIGEST,
            "detected": {"runtime": "python", "command": ["python", "app.py"], "env": []},
            "status": "pending",
            "status_at": NOW,
            "created_by": ALICE,
            "created_at": NOW,
        }
    )


async def test_a_child_delegated_the_shared_service_principal_is_told_to_ask_the_token_command(
    tmp_path: Path,
) -> None:
    registry = registry_with_credentials(shared_sp=True)
    workspace = MintingWorkspace()
    service = make_service(tmp_path, registry=registry, workspace=workspace)
    await service.start_children(registry)
    (reconciler,) = RecordingReconciler.instances

    environment_of_child = await reconciler.kwargs["environments"].environment_for(sales_release())

    assert environment_of_child["DATABRICKS_AUTH_TYPE"] == "databricks-cli"
    assert environment_of_child["DATABRICKS_CLI_PATH"] == TOKEN_COMMAND_PATH
    assert "DATABRICKS_TOKEN" not in environment_of_child
    assert MINTED_TOKEN not in environment_of_child.values()


async def test_a_token_that_databricks_refuses_to_mint_refuses_the_release_with_what_to_do(
    tmp_path: Path,
) -> None:
    registry = registry_with_credentials(shared_sp=True)
    service = make_service(tmp_path, registry=registry, workspace=MintingWorkspace(mint_status=401))
    await service.start_children(registry)
    (reconciler,) = RecordingReconciler.instances

    with pytest.raises(EnvironmentRefused, match="refused") as refusal:
        await reconciler.kwargs["environments"].environment_for(sales_release())

    assert MINTED_TOKEN not in str(refusal.value)


async def test_a_child_with_its_own_service_principal_gets_the_three_standard_variables(
    tmp_path: Path,
) -> None:
    registry = registry_with_credentials(own_sp=True)
    service = make_service(tmp_path, registry=registry, workspace=MintingWorkspace())
    await service.start_children(registry)
    (reconciler,) = RecordingReconciler.instances

    environment_of_child = await reconciler.kwargs["environments"].environment_for(sales_release())

    assert environment_of_child["DATABRICKS_CLIENT_ID"] == OWN_SP_ID
    assert environment_of_child["DATABRICKS_CLIENT_SECRET"] == OWN_SP_SECRET
    assert "DATABRICKS_CLI_PATH" not in environment_of_child


async def test_a_child_with_nothing_delegated_gets_no_databricks_credential(
    tmp_path: Path,
) -> None:
    registry = registry_with_credentials()
    service = make_service(tmp_path, registry=registry, workspace=MintingWorkspace())
    await service.start_children(registry)
    (reconciler,) = RecordingReconciler.instances

    environment_of_child = await reconciler.kwargs["environments"].environment_for(sales_release())

    assert not [name for name in environment_of_child if name.startswith("DATABRICKS_CLI")]
    assert "DATABRICKS_CLIENT_SECRET" not in environment_of_child


async def test_a_child_that_asks_the_token_command_finds_a_token_in_its_run_directory(
    tmp_path: Path,
) -> None:
    registry = registry_with_credentials(shared_sp=True)
    service = make_service(tmp_path, registry=registry, workspace=MintingWorkspace())
    await service.start_children(registry)
    (reconciler,) = RecordingReconciler.instances
    environment_of_child = await reconciler.kwargs["environments"].environment_for(sales_release())
    files = reconciler.kwargs["launcher"]._run_dir_files
    run_dir = tmp_path / "child-run"
    run_dir.mkdir()

    assert files.needed_by(environment_of_child)
    keeper = await files.attach(run_dir)
    try:
        token_file = json.loads((run_dir / "shared-sp-token.json").read_text())
    finally:
        await keeper.stop()

    assert token_file["access_token"] == MINTED_TOKEN


async def test_a_child_whose_token_cannot_be_minted_at_start_does_not_start(
    tmp_path: Path,
) -> None:
    registry = registry_with_credentials(shared_sp=True)
    workspace = MintingWorkspace()
    service = make_service(tmp_path, registry=registry, workspace=workspace)
    await service.start_children(registry)
    (reconciler,) = RecordingReconciler.instances
    files = reconciler.kwargs["launcher"]._run_dir_files
    run_dir = tmp_path / "child-run"
    run_dir.mkdir()
    workspace.mint_status = 401

    with pytest.raises(ChildStartError, match="refused") as failure:
        await files.attach(run_dir)

    assert MINTED_TOKEN not in str(failure.value)
    assert not (run_dir / "shared-sp-token.json").exists()


async def test_an_app_without_the_shared_service_principal_needs_no_token_files(
    tmp_path: Path,
) -> None:
    registry = registry_with_credentials()
    service = make_service(tmp_path, registry=registry, workspace=MintingWorkspace())
    await service.start_children(registry)
    (reconciler,) = RecordingReconciler.instances
    environment_of_child = await reconciler.kwargs["environments"].environment_for(sales_release())
    files = reconciler.kwargs["launcher"]._run_dir_files

    assert not files.needed_by(environment_of_child)


# --- the egress lists reach the running sandboxes (T066, TD-016) ---------------------------------

APP_EGRESS_PORT: Final = 8123  # DATABRICKS_APP_PORT of ``environment()``


def set_app_egress(registry: StubRegistry, slug: str, cidrs: list[str]) -> None:
    """The app's own egress list as an admin saved it: allow each ``cidr`` on port 443."""
    app = registry.snapshot.app(slug)
    assert app is not None
    rules = [{"action": "allow", "cidr": cidr, "port": "443", "comment": ""} for cidr in cidrs]
    registry.seed("apps", {**app.model_dump(), "egress": rules})


def cidrs_of(rules: list[dict[str, Any]]) -> list[str]:
    return [rule["cidr"] for rule in rules]


async def service_with_sandboxes(
    tmp_path: Path, registry: StubRegistry, *slugs: str
) -> tuple[Service, dict[str, Any]]:
    """A started service whose reconciler runs one sandbox child per slug; the sandboxes by slug."""
    service = make_service(tmp_path / "work", registry=registry)
    await service.start_children(registry)
    sandboxes = {slug: sandbox_with_rules(tmp_path / "scratch", slug, "1-1") for slug in slugs}
    RecordingReconciler.instances[-1].children = [
        (slug, child) for slug, (child, _) in sandboxes.items()
    ]
    return service, {slug: process for slug, (_, process) in sandboxes.items()}


async def test_an_app_egress_save_rewrites_that_sandboxs_rules_and_signals_only_it(
    tmp_path: Path,
) -> None:
    registry = seeded_registry()
    service, processes = await service_with_sandboxes(tmp_path, registry, "sales", "docs")
    set_app_egress(registry, "sales", ["203.0.113.0/24"])  # saved after the start

    await service.egress_reloader("sales")

    (seen,) = processes["sales"].rules_seen
    assert cidrs_of(seen["app_rules"]) == ["203.0.113.0/24"]
    assert cidrs_of(seen["global_rules"]) == [
        rule.cidr for rule in global_egress_rules(registry.snapshot)
    ]
    assert (seen["container_ip"], seen["appmux_port"]) == (str(CONTAINER_IP), APP_EGRESS_PORT)
    assert processes["docs"].rules_seen == []


async def test_a_global_egress_save_reaches_every_running_sandbox_with_its_own_list(
    tmp_path: Path,
) -> None:
    registry = seeded_registry()
    set_app_egress(registry, "sales", ["203.0.113.0/24"])
    service, processes = await service_with_sandboxes(tmp_path, registry, "sales", "docs")
    registry.seed(
        "platform_settings",
        {
            "key": "egress_global",
            "value": [{"action": "deny", "cidr": "192.0.2.0/24", "port": "any", "comment": ""}],
            "updated_by": ALICE,
            "updated_at": NOW,
        },
    )

    await service.egress_reloader(None)

    (sales_seen,) = processes["sales"].rules_seen
    (docs_seen,) = processes["docs"].rules_seen
    assert cidrs_of(sales_seen["app_rules"]) == ["203.0.113.0/24"]
    assert cidrs_of(docs_seen["app_rules"]) == []
    assert (
        cidrs_of(sales_seen["global_rules"])
        == cidrs_of(docs_seen["global_rules"])
        == ["192.0.2.0/24"]
    )


async def test_the_old_copy_still_draining_and_the_new_one_both_read_the_saved_list(
    tmp_path: Path,
) -> None:
    registry = seeded_registry()
    service = make_service(tmp_path / "work", registry=registry)
    await service.start_children(registry)
    old, old_process = sandbox_with_rules(tmp_path / "scratch", "sales", "1-1")
    new, new_process = sandbox_with_rules(tmp_path / "scratch", "sales", "2-2")
    RecordingReconciler.instances[-1].children = [("sales", old), ("sales", new)]
    set_app_egress(registry, "sales", ["203.0.113.0/24"])

    await service.egress_reloader("sales")

    assert [cidrs_of(seen["app_rules"]) for seen in old_process.rules_seen] == [["203.0.113.0/24"]]
    assert [cidrs_of(seen["app_rules"]) for seen in new_process.rules_seen] == [["203.0.113.0/24"]]


async def test_a_child_that_is_not_a_sandbox_has_no_pump_to_signal(tmp_path: Path) -> None:
    registry = seeded_registry()
    service, processes = await service_with_sandboxes(tmp_path, registry, "sales")
    RecordingReconciler.instances[-1].children.append(("docs", object()))

    await service.egress_reloader(None)

    assert len(processes["sales"].rules_seen) == 1


async def test_before_the_reconciler_runs_an_egress_save_has_nothing_to_signal(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path, is_self_check_passing=lambda: False)
    await service.start_children(seeded_registry())

    await service.egress_reloader("sales")
    await service.egress_reloader(None)

    assert RecordingReconciler.instances == []
