"""The adapter between the reconciler's ``ChildLauncher`` and ``sandbox.launcher.start_sandbox``.

The sandbox itself is proven on a container (the canary and the integration tests); this checks
what the adapter decides: the spec it builds, the files it prepares, how it names a failure.
``start_sandbox`` is replaced by a function that records the spec and returns a fake handle.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from appmux.reconciler.children import ChildStart, ChildStartError
from appmux.reconciler.sandbox_adapter import MAX_SOCKET_PATH_BYTES, SandboxChildLauncher
from appmux.sandbox.launcher import SandboxExit, SandboxSpec, SandboxStartError
from appmux.sandbox.rules import EgressRule

pytestmark = pytest.mark.anyio

APP_RULE = EgressRule(action="allow", cidr="203.0.113.0/24", port="443", comment="an api")
GLOBAL_RULE = EgressRule(action="deny", cidr="198.51.100.0/24", port="any", comment="")


class FakeHandle:
    def __init__(self) -> None:
        self.started_with: list[float] = []
        self.stopped_with: list[float] = []
        self.start_error: SandboxStartError | None = None
        self.exit = SandboxExit(status=3, signal=None)

    async def wait_started(self, *, timeout_s: float) -> None:
        self.started_with.append(timeout_s)
        if self.start_error is not None:
            raise self.start_error

    async def wait(self) -> SandboxExit:
        return self.exit

    async def stop(self, *, grace_s: float) -> SandboxExit:
        self.stopped_with.append(grace_s)
        return self.exit


class FakeStarter:
    def __init__(self) -> None:
        self.specs: list[SandboxSpec] = []
        self.handle = FakeHandle()
        self.error: Exception | None = None

    async def __call__(self, spec: SandboxSpec) -> FakeHandle:
        self.specs.append(spec)
        if self.error is not None:
            raise self.error
        return self.handle


def child_start(release_no: int = 2, **changes: object) -> ChildStart:
    fields: dict[str, object] = {
        "slug": "sales",
        "release_no": release_no,
        "command": ("python", "app.py"),
        "environment": {"API_KEY": "sk-test-value", "PORT": "8000"},
        "release_dir": Path("/srv/releases-example/sales/2"),
        "port": 8000,
        "memory_budget_mb": 256,
        "overlays": (Path("/srv/overlays-example/a"),),
        "app_egress": (APP_RULE,),
    }
    fields.update(changes)
    return ChildStart(**fields)  # type: ignore[arg-type]


def launcher(tmp_path: Path, starter: FakeStarter) -> SandboxChildLauncher:
    return SandboxChildLauncher(
        run_base=tmp_path / "run",
        app_environment=Path("/opt/app-environment-example"),
        container_ip=IPv4Address("10.0.0.5"),
        appmux_port=8080,
        global_rules=lambda: (GLOBAL_RULE,),
        start=starter,
    )


async def test_the_spec_names_what_the_child_runs_and_how_much_it_may_use(tmp_path: Path) -> None:
    starter = FakeStarter()

    await launcher(tmp_path, starter).start(child_start())

    (spec,) = starter.specs
    assert spec.slug == "sales"
    assert spec.command == ("python", "app.py")
    assert spec.environment == {"API_KEY": "sk-test-value", "PORT": "8000"}
    assert spec.app_environment == Path("/opt/app-environment-example")
    assert spec.release_dir == Path("/srv/releases-example/sales/2")
    assert spec.overlays == (Path("/srv/overlays-example/a"),)
    assert spec.child_port == 8000
    assert spec.memory_budget_mb == 256


async def test_the_edge_reaches_the_child_through_a_socket_in_its_own_scratch_directory(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()

    running = await launcher(tmp_path, starter).start(child_start())

    (spec,) = starter.specs
    assert spec.relay_socket_path == running.socket_path
    assert spec.relay_socket_path is not None
    assert spec.relay_socket_path.parent == spec.scratch_dir
    assert spec.scratch_dir.is_relative_to(tmp_path / "run" / "sales")
    assert spec.log_path.parent == spec.scratch_dir
    assert spec.rules_path.parent == spec.scratch_dir


async def test_the_egress_rules_file_holds_the_apps_rules_then_the_global_ones(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()

    await launcher(tmp_path, starter).start(child_start())

    document = json.loads(starter.specs[0].rules_path.read_text())
    assert [rule["cidr"] for rule in document["app_rules"]] == ["203.0.113.0/24"]
    assert [rule["cidr"] for rule in document["global_rules"]] == ["198.51.100.0/24"]
    assert document["container_ip"] == "10.0.0.5"
    assert document["appmux_port"] == 8080


async def test_every_start_gets_a_scratch_directory_of_its_own(tmp_path: Path) -> None:
    starter = FakeStarter()
    adapter = launcher(tmp_path, starter)

    await adapter.start(child_start())
    await adapter.start(child_start())

    first, second = starter.specs
    assert first.scratch_dir != second.scratch_dir
    assert first.relay_socket_path != second.relay_socket_path


async def test_what_an_earlier_run_left_in_the_directory_is_gone_before_the_start(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    first = launcher(tmp_path, starter)
    await first.start(child_start())
    stale = starter.specs[0].scratch_dir / "child.log"
    stale.write_text("old failure")

    # A new launcher (a new process of appmux) starts the same release into the same names.
    await launcher(tmp_path, starter).start(child_start())

    assert starter.specs[1].scratch_dir == starter.specs[0].scratch_dir
    assert not stale.exists()


async def test_a_directory_too_long_for_a_unix_socket_is_refused_before_anything_starts(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    deep = tmp_path / ("d" * MAX_SOCKET_PATH_BYTES)
    adapter = SandboxChildLauncher(
        run_base=deep,
        app_environment=Path("/opt/app-environment-example"),
        container_ip=IPv4Address("10.0.0.5"),
        appmux_port=8080,
        start=starter,
    )

    with pytest.raises(ChildStartError, match="too long for a unix socket"):
        await adapter.start(child_start())

    assert starter.specs == []


async def test_a_sandbox_that_cannot_be_spawned_is_a_start_failure(tmp_path: Path) -> None:
    starter = FakeStarter()
    starter.error = SandboxStartError("the sandbox supervisor did not start")

    with pytest.raises(ChildStartError, match="the sandbox supervisor did not start"):
        await launcher(tmp_path, starter).start(child_start())


async def test_a_sandbox_that_fails_to_come_up_is_a_start_failure_with_its_message(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    starter.handle.start_error = SandboxStartError("stage 1 failed: mount denied")
    running = await launcher(tmp_path, starter).start(child_start())

    with pytest.raises(ChildStartError, match="stage 1 failed: mount denied"):
        await running.wait_started(timeout_s=90.0)

    assert starter.handle.started_with == [90.0]


async def test_exit_and_stop_report_how_the_child_ended(tmp_path: Path) -> None:
    starter = FakeStarter()
    running = await launcher(tmp_path, starter).start(child_start())

    assert (await running.wait()).description == "exited with status 3"
    assert (await running.stop(grace_s=10.0)).description == "exited with status 3"
    assert starter.handle.stopped_with == [10.0]


async def test_the_log_tail_is_the_end_of_the_childs_log(tmp_path: Path) -> None:
    starter = FakeStarter()
    running = await launcher(tmp_path, starter).start(child_start())
    spec = starter.specs[0]
    spec.log_path.write_text("starting\nTraceback: boom\n")

    assert running.log_tail() == "starting\nTraceback: boom"


# --- a run directory on the host (the shared service principal's token files) -------------------

CLI_SCRIPT = "/run/appmux/token-command.sh"


class FakeKeeper:
    def __init__(self) -> None:
        self.stops = 0

    async def stop(self) -> None:
        self.stops += 1


class FakeRunDirFiles:
    """Stands in for ``SharedSpTokenFiles``: records the directory it was given and when."""

    def __init__(self, starter: FakeStarter) -> None:
        self.attached: list[Path] = []
        self.keeper = FakeKeeper()
        self.error: Exception | None = None
        self.started_before_attach: bool | None = None
        self._starter = starter

    def needed_by(self, environment: Mapping[str, str]) -> bool:
        return environment.get("DATABRICKS_CLI_PATH") == CLI_SCRIPT

    async def attach(self, directory: Path) -> FakeKeeper:
        self.started_before_attach = bool(self._starter.specs)
        self.attached.append(directory)
        if self.error is not None:
            raise self.error
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        return self.keeper


def shared_sp_child() -> ChildStart:
    return child_start(environment={"DATABRICKS_CLI_PATH": CLI_SCRIPT, "PORT": "8000"})


def launcher_with_files(
    tmp_path: Path, starter: FakeStarter, files: FakeRunDirFiles, **changes: object
) -> SandboxChildLauncher:
    return SandboxChildLauncher(
        run_base=tmp_path / "run",
        app_environment=Path("/opt/app-environment-example"),
        container_ip=IPv4Address("10.0.0.5"),
        appmux_port=8080,
        start=starter,
        run_dir_files=files,
        **changes,  # type: ignore[arg-type]
    )


async def test_a_child_that_names_the_token_command_gets_a_host_run_directory_with_the_token(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    files = FakeRunDirFiles(starter)

    await launcher_with_files(tmp_path, starter, files).start(shared_sp_child())

    (spec,) = starter.specs
    assert spec.host_run_dir is not None
    assert spec.host_run_dir.parent == spec.scratch_dir
    assert files.attached == [spec.host_run_dir]
    assert files.started_before_attach is False


async def test_a_child_that_does_not_name_the_token_command_keeps_the_tmpfs_run_directory(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    files = FakeRunDirFiles(starter)

    await launcher_with_files(tmp_path, starter, files).start(child_start())

    assert starter.specs[0].host_run_dir is None
    assert files.attached == []


async def test_a_token_that_cannot_be_put_in_place_is_a_start_failure_and_nothing_starts(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    files = FakeRunDirFiles(starter)
    files.error = ChildStartError("the shared service principal's token could not be minted")

    with pytest.raises(ChildStartError, match="could not be minted"):
        await launcher_with_files(tmp_path, starter, files).start(shared_sp_child())

    assert starter.specs == []


async def test_the_token_files_stop_being_kept_when_the_child_ends_or_is_stopped(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    files = FakeRunDirFiles(starter)
    running = await launcher_with_files(tmp_path, starter, files).start(shared_sp_child())

    await running.wait()
    assert files.keeper.stops >= 1

    stops_after_wait = files.keeper.stops
    await running.stop(grace_s=1.0)
    assert files.keeper.stops >= stops_after_wait


async def test_the_token_files_stop_being_kept_when_the_sandbox_cannot_be_spawned(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    starter.error = SandboxStartError("the sandbox supervisor did not start")
    files = FakeRunDirFiles(starter)

    with pytest.raises(ChildStartError):
        await launcher_with_files(tmp_path, starter, files).start(shared_sp_child())

    assert files.keeper.stops == 1


class BlockedHandle(FakeHandle):
    """A child that keeps running until it is stopped."""

    def __init__(self) -> None:
        super().__init__()
        self.is_stopped = asyncio.Event()

    async def wait(self) -> SandboxExit:
        await self.is_stopped.wait()
        return self.exit

    async def stop(self, *, grace_s: float) -> SandboxExit:
        self.stopped_with.append(grace_s)
        self.is_stopped.set()
        return self.exit


async def test_a_host_run_directory_over_its_limit_stops_the_child_and_says_why(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    starter.handle = BlockedHandle()
    files = FakeRunDirFiles(starter)
    running = await launcher_with_files(
        tmp_path, starter, files, run_dir_limit_bytes=1024, run_dir_watch_interval_s=0.01
    ).start(shared_sp_child())
    spec = starter.specs[0]
    assert spec.host_run_dir is not None
    (spec.host_run_dir / "filler").write_bytes(b"x" * 64 * 1024)

    exit_ = await asyncio.wait_for(running.wait(), timeout=5.0)

    assert "run directory holds" in exit_.description
    assert starter.handle.stopped_with != []
    assert files.keeper.stops >= 1


async def test_a_host_run_directory_under_its_limit_leaves_the_child_running(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    starter.handle = BlockedHandle()
    files = FakeRunDirFiles(starter)
    running = await launcher_with_files(
        tmp_path, starter, files, run_dir_limit_bytes=1024 * 1024, run_dir_watch_interval_s=0.01
    ).start(shared_sp_child())

    await asyncio.sleep(0.1)

    assert starter.handle.stopped_with == []
    await running.stop(grace_s=1.0)


class SlowStopHandle(BlockedHandle):
    """The child is gone as soon as it is told to stop, but ``stop`` takes a moment to return."""

    def __init__(self) -> None:
        super().__init__()
        self.has_finished_stopping = False

    async def stop(self, *, grace_s: float) -> SandboxExit:
        self.stopped_with.append(grace_s)
        self.is_stopped.set()
        await asyncio.sleep(0.05)
        self.has_finished_stopping = True
        return self.exit


async def test_the_watchdog_that_is_stopping_the_child_is_waited_for_not_cancelled(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    starter.handle = SlowStopHandle()
    files = FakeRunDirFiles(starter)
    running = await launcher_with_files(
        tmp_path, starter, files, run_dir_limit_bytes=1024, run_dir_watch_interval_s=0.01
    ).start(shared_sp_child())
    spec = starter.specs[0]
    assert spec.host_run_dir is not None
    (spec.host_run_dir / "filler").write_bytes(b"x" * 64 * 1024)

    await asyncio.wait_for(running.wait(), timeout=5.0)

    assert starter.handle.has_finished_stopping
