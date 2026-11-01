"""The real ``SandboxRunner``: one build step in one sandbox started through the launcher (T135).

``start_sandbox`` is replaced by a function that records the ``SandboxSpec`` and hands back a fake
process, as ``test_reconciler_sandbox_adapter.py`` does for the child launcher. What these tests
prove is what the runner decides: the spec it builds (which directory is mounted where, which rules
apply, which time limit), the files it prepares and removes, and how each way a sandbox can end
becomes a ``StepResult`` or an ``OverlayBuildFailed``. That pip runs in a real sandbox, and that the
mounts and limits behave there, is not proven here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from appmux.packages.modes import DENY_ALL_EGRESS
from appmux.packages.overlay import (
    BUILD_LOG_READ_BYTES,
    BUILD_SLUG,
    BUILD_TMPDIR_NAME,
    OVERLAY_DIR_IN_SANDBOX,
    WHEEL_DIR_IN_SANDBOX,
    BuildStep,
    OverlayBuildFailed,
    SandboxStepRunner,
    StepResult,
)
from appmux.sandbox.launcher import (
    SUPERVISOR_LOG_NAME,
    SandboxExit,
    SandboxSpec,
    SandboxStartError,
)
from appmux.sandbox.rules import EgressRule

pytestmark = pytest.mark.anyio

APP_ENVIRONMENT = Path("/opt/app-environment-example")
PYTHON = f"{APP_ENVIRONMENT}/bin/python"
CONTAINER_IP = IPv4Address("10.0.0.5")
APPMUX_PORT = 8000
GLOBAL_RULE = EgressRule(action="deny", cidr="198.51.100.0/24", port="any", comment="")
INDEX_RULE = EgressRule(action="allow", cidr="203.0.113.0/24", port="443", comment="the index")
TIMEOUT_S = 120.0


def step(label: str = "wheels", *, egress: tuple[EgressRule, ...] = DENY_ALL_EGRESS) -> BuildStep:
    return BuildStep(
        label=label,  # type: ignore[arg-type]
        argv=(PYTHON, "-m", "pip", label),
        environment={"HOME": "/home/app", "PIP_INDEX_URL": "https://index.example/simple"},
        egress=egress,
    )


class FakeProcess:
    def __init__(
        self,
        *,
        exit_: SandboxExit | None = None,
        hang: bool = False,
        start_error: SandboxStartError | None = None,
    ) -> None:
        self.exit = exit_ or SandboxExit(status=0, signal=None)
        self.hang = hang
        self.start_error = start_error
        self.started_with: list[float] = []
        self.stop_graces: list[float] = []
        self.waiting = asyncio.Event()  # set once the runner waits for the command to end
        self._stopped = asyncio.Event()

    async def wait_started(self, *, timeout_s: float) -> None:
        self.started_with.append(timeout_s)
        if self.start_error is not None:
            raise self.start_error

    async def wait(self) -> SandboxExit:
        self.waiting.set()
        if self.hang:
            await self._stopped.wait()
            return SandboxExit.from_returncode(-9)
        return self.exit

    async def stop(self, *, grace_s: float) -> SandboxExit:
        self.stop_graces.append(grace_s)
        self._stopped.set()
        return SandboxExit.from_returncode(-15)


class FakeStarter:
    """Stands in for ``start_sandbox``: records the spec and what the directories held."""

    def __init__(self, process: FakeProcess | None = None, *, output: str | bytes = "") -> None:
        self.process = process or FakeProcess()
        self.output = output.encode() if isinstance(output, str) else output
        self.error: Exception | None = None
        self.supervisor_log = ""
        self.specs: list[SandboxSpec] = []
        self.rules_seen: list[dict[str, object]] = []
        self.temp_dir_existed: list[bool] = []

    async def __call__(self, spec: SandboxSpec) -> FakeProcess:
        self.specs.append(spec)
        if self.supervisor_log:
            (spec.scratch_dir / SUPERVISOR_LOG_NAME).write_text(
                self.supervisor_log, encoding="utf-8"
            )
        if self.error is not None:
            raise self.error
        self.rules_seen.append(json.loads(spec.rules_path.read_text(encoding="utf-8")))
        assert spec.build_output is not None
        self.temp_dir_existed.append((spec.build_output / BUILD_TMPDIR_NAME).is_dir())
        spec.log_path.write_bytes(self.output)
        return self.process


def make_runner(
    tmp_path: Path, starter: FakeStarter, *, run_base: Path | None = None
) -> SandboxStepRunner:
    return SandboxStepRunner(
        run_base=run_base or tmp_path / "run",
        app_environment=APP_ENVIRONMENT,
        container_ip=CONTAINER_IP,
        appmux_port=APPMUX_PORT,
        global_rules=lambda: (GLOBAL_RULE,),
        start=starter,
    )


def directories(tmp_path: Path) -> tuple[Path, Path]:
    wheel_dir, overlay_dir = tmp_path / "wheels", tmp_path / "overlay"
    wheel_dir.mkdir(exist_ok=True)
    overlay_dir.mkdir(exist_ok=True)
    return wheel_dir, overlay_dir


async def run_step(
    runner: SandboxStepRunner,
    tmp_path: Path,
    build_step: BuildStep | None = None,
    *,
    timeout_s: float = TIMEOUT_S,
) -> StepResult:
    wheel_dir, overlay_dir = directories(tmp_path)
    return await runner.run(
        build_step or step(), wheel_dir=wheel_dir, overlay_dir=overlay_dir, timeout_s=timeout_s
    )


# --- the spec -----------------------------------------------------------------------------------


async def test_the_wheels_are_the_release_and_the_overlay_is_the_writable_build_output(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    wheel_dir, overlay_dir = directories(tmp_path)

    await run_step(make_runner(tmp_path, starter), tmp_path)

    (spec,) = starter.specs
    assert spec.release_dir == wheel_dir  # read-only at /srv/app, where the commands look
    assert spec.build_output == overlay_dir  # writable at /srv/build, where pip installs
    assert spec.overlays == ()  # a build runs on the environment alone
    assert spec.app_environment == APP_ENVIRONMENT
    assert spec.command == (PYTHON, "-m", "pip", "wheels")
    assert spec.slug == BUILD_SLUG
    assert spec.relay_socket_path is None and spec.child_port is None
    assert WHEEL_DIR_IN_SANDBOX == "/srv/app" and OVERLAY_DIR_IN_SANDBOX == "/srv/build"


async def test_the_steps_environment_reaches_the_sandbox_unchanged_in_its_own_keys(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    await run_step(make_runner(tmp_path, starter), tmp_path, step("index"))

    (spec,) = starter.specs
    assert spec.environment["PIP_INDEX_URL"] == "https://index.example/simple"
    assert spec.environment["HOME"] == "/home/app"


async def test_an_install_step_gets_a_temp_directory_in_the_build_output_and_it_is_removed_after(
    tmp_path: Path,
) -> None:
    # /tmp inside the sandbox is a 64 MB tmpfs, and pip unpacks a wheel into a temp directory first:
    # a large wheel needs its temp space on the build output's disk instead.
    starter = FakeStarter()
    wheel_dir, overlay_dir = directories(tmp_path)

    for label in ("wheels", "index"):
        await make_runner(tmp_path, starter).run(
            step(label), wheel_dir=wheel_dir, overlay_dir=overlay_dir, timeout_s=TIMEOUT_S
        )

    assert starter.temp_dir_existed == [True, True]
    for spec in starter.specs:
        assert spec.environment["TMPDIR"] == f"{OVERLAY_DIR_IN_SANDBOX}/{BUILD_TMPDIR_NAME}"
    assert not (overlay_dir / BUILD_TMPDIR_NAME).exists(), "nothing of it ships in the overlay"


async def test_the_check_step_needs_no_temp_directory(tmp_path: Path) -> None:
    starter = FakeStarter()
    await run_step(make_runner(tmp_path, starter), tmp_path, step("check"))

    (spec,) = starter.specs
    assert "TMPDIR" not in spec.environment
    assert starter.temp_dir_existed == [False]


async def test_the_wall_clock_limit_is_a_backstop_after_the_runners_own_timeout(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    await run_step(make_runner(tmp_path, starter), tmp_path, timeout_s=90.0)

    (spec,) = starter.specs
    assert spec.wall_clock_limit_s is not None
    assert spec.wall_clock_limit_s > 90.0


async def test_the_sandbox_start_gets_a_bounded_wait(tmp_path: Path) -> None:
    starter = FakeStarter()
    await run_step(make_runner(tmp_path, starter), tmp_path, timeout_s=5.0)

    (waited,) = starter.process.started_with
    assert 0 < waited <= 5.0


# --- the rules ----------------------------------------------------------------------------------


async def test_the_steps_egress_is_the_rule_list_and_the_global_rules_still_apply(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    await run_step(make_runner(tmp_path, starter), tmp_path, step("index", egress=(INDEX_RULE,)))

    (rules,) = starter.rules_seen
    assert [rule["cidr"] for rule in rules["app_rules"]] == ["203.0.113.0/24"]  # type: ignore[attr-defined]
    assert [rule["cidr"] for rule in rules["global_rules"]] == ["198.51.100.0/24"]  # type: ignore[attr-defined]
    assert rules["container_ip"] == "10.0.0.5"
    assert rules["appmux_port"] == APPMUX_PORT


async def test_an_offline_step_is_started_with_the_deny_all_list(tmp_path: Path) -> None:
    starter = FakeStarter()
    await run_step(make_runner(tmp_path, starter), tmp_path, step("wheels"))

    (rules,) = starter.rules_seen
    assert [(rule["action"], rule["cidr"]) for rule in rules["app_rules"]] == [  # type: ignore[attr-defined]
        ("deny", "0.0.0.0/0")
    ]


# --- how a step ends ----------------------------------------------------------------------------


async def test_a_clean_exit_returns_code_zero_and_what_the_command_printed(tmp_path: Path) -> None:
    starter = FakeStarter(output="Successfully installed appmux-fixture-alpha-1.0.0\n")

    result = await run_step(make_runner(tmp_path, starter), tmp_path)

    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.output == "Successfully installed appmux-fixture-alpha-1.0.0\n"


async def test_a_failing_command_returns_its_exit_code_and_output(tmp_path: Path) -> None:
    starter = FakeStarter(
        FakeProcess(exit_=SandboxExit(status=1, signal=None)), output="ERROR: no matching hash\n"
    )

    result = await run_step(make_runner(tmp_path, starter), tmp_path)

    assert (result.exit_code, result.timed_out) == (1, False)
    assert "no matching hash" in result.output


async def test_a_command_killed_by_a_signal_has_no_exit_code(tmp_path: Path) -> None:
    starter = FakeStarter(FakeProcess(exit_=SandboxExit.from_returncode(-9)), output="Killed\n")

    result = await run_step(make_runner(tmp_path, starter), tmp_path)

    assert (result.exit_code, result.timed_out) == (None, False)


async def test_the_launchers_wall_clock_kill_is_a_timeout(tmp_path: Path) -> None:
    killed = SandboxExit.from_returncode(-9, reason="wall-clock limit of 125 s exceeded")
    starter = FakeStarter(FakeProcess(exit_=killed))

    result = await run_step(make_runner(tmp_path, starter), tmp_path)

    assert (result.exit_code, result.timed_out) == (None, True)


async def test_a_command_that_outlives_the_budget_is_stopped_and_reported_as_timed_out(
    tmp_path: Path,
) -> None:
    starter = FakeStarter(FakeProcess(hang=True), output="Collecting something-huge\n")

    result = await run_step(make_runner(tmp_path, starter), tmp_path, timeout_s=0.05)

    assert starter.process.stop_graces, "the sandbox was asked to stop"
    assert (result.exit_code, result.timed_out) == (None, True)
    assert result.output == "Collecting something-huge\n"


async def test_only_the_end_of_a_very_long_log_is_returned(tmp_path: Path) -> None:
    starter = FakeStarter(
        output="first line\n" + "x" * (BUILD_LOG_READ_BYTES * 2) + "\nlast line\n"
    )

    result = await run_step(make_runner(tmp_path, starter), tmp_path)

    assert "first line" not in result.output
    assert result.output.rstrip().endswith("last line")
    assert len(result.output.encode()) <= BUILD_LOG_READ_BYTES


async def test_a_log_that_is_not_utf8_is_returned_with_replacement_characters(
    tmp_path: Path,
) -> None:
    starter = FakeStarter(output=b"bad \xff byte\n")

    result = await run_step(make_runner(tmp_path, starter), tmp_path)

    assert result.output == "bad \ufffd byte\n"


# --- when the sandbox cannot run the step -------------------------------------------------------


async def test_a_sandbox_that_will_not_start_fails_the_build_with_the_launchers_reason(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    starter.error = SandboxStartError(
        "slirp4netns is not installed; run scripts/fetch-slirp4netns.sh"
    )

    with pytest.raises(OverlayBuildFailed, match="slirp4netns is not installed") as failure:
        await run_step(make_runner(tmp_path, starter), tmp_path)

    assert "sandbox" in failure.value.reason
    assert isinstance(failure.value.__cause__, SandboxStartError)


async def test_a_start_failure_carries_the_end_of_the_supervisors_log(tmp_path: Path) -> None:
    # the launcher's messages say "see <scratch>/supervisor.log", and the scratch directory goes
    starter = FakeStarter()
    starter.error = SandboxStartError("the supervisor ended with status 2 (see supervisor.log)")
    starter.supervisor_log = "could not attach slirp4netns\npermission denied\n"

    with pytest.raises(OverlayBuildFailed) as failure:
        await run_step(make_runner(tmp_path, starter), tmp_path)

    assert failure.value.log_tail == "could not attach slirp4netns\npermission denied"


async def test_a_sandbox_that_fails_while_starting_fails_the_build(tmp_path: Path) -> None:
    starter = FakeStarter(FakeProcess(start_error=SandboxStartError("stage 1 ended early")))

    with pytest.raises(OverlayBuildFailed, match="stage 1 ended early"):
        await run_step(make_runner(tmp_path, starter), tmp_path)


async def test_a_scratch_directory_that_cannot_be_made_fails_the_build_with_what_to_check(
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    starter = FakeStarter()

    with pytest.raises(OverlayBuildFailed, match="run directory") as failure:
        await run_step(make_runner(tmp_path, starter, run_base=blocker / "run"), tmp_path)

    assert starter.specs == []
    assert isinstance(failure.value.__cause__, OSError)


async def test_a_run_directory_too_long_for_a_unix_socket_is_refused_before_anything_starts(
    tmp_path: Path,
) -> None:
    starter = FakeStarter()
    long_base = tmp_path / ("d" * 120)

    with pytest.raises(OverlayBuildFailed, match="too long for a unix socket"):
        await run_step(make_runner(tmp_path, starter, run_base=long_base), tmp_path)

    assert starter.specs == []


# --- clean-up -----------------------------------------------------------------------------------


async def test_the_scratch_directory_is_removed_after_every_outcome(tmp_path: Path) -> None:
    run_base = tmp_path / "run"
    for starter in (
        FakeStarter(output="ok\n"),
        FakeStarter(FakeProcess(exit_=SandboxExit(2, None)), output="boom\n"),
        FakeStarter(FakeProcess(start_error=SandboxStartError("no"))),
    ):
        wheel_dir, overlay_dir = tmp_path / f"w{id(starter)}", tmp_path / f"o{id(starter)}"
        wheel_dir.mkdir()
        overlay_dir.mkdir()
        runner = make_runner(tmp_path, starter, run_base=run_base)
        with contextlib.suppress(OverlayBuildFailed):  # the failure itself is covered elsewhere
            await runner.run(step(), wheel_dir=wheel_dir, overlay_dir=overlay_dir, timeout_s=5.0)
        assert list(run_base.iterdir()) == [], "no scratch directory, log or rules file is kept"
        assert not (overlay_dir / BUILD_TMPDIR_NAME).exists()


async def test_a_cancelled_step_stops_its_sandbox_and_cleans_up(tmp_path: Path) -> None:
    starter = FakeStarter(FakeProcess(hang=True))
    wheel_dir, overlay_dir = directories(tmp_path)
    runner = make_runner(tmp_path, starter)

    task = asyncio.create_task(
        runner.run(step(), wheel_dir=wheel_dir, overlay_dir=overlay_dir, timeout_s=TIMEOUT_S)
    )
    await asyncio.wait_for(starter.process.waiting.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert starter.process.stop_graces, "a cancelled build must not leave its sandbox running"
    assert list((tmp_path / "run").iterdir()) == []
    assert not (overlay_dir / BUILD_TMPDIR_NAME).exists()
