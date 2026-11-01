"""Building, caching and publishing a release's overlay (FR-031, FR-032, design 5.3).

The sandbox is replaced by a fake ``SandboxRunner`` that records what it was asked to run and
what the directories held at that moment, and plays the part of pip by writing a file into the
overlay directory. What these tests prove is the builder's logic: which wheels reach the build,
the order and time budget of the steps, atomic publication, the cache, and that ``pip check`` only
warns. That pip itself installs from these commands in a real sandbox is not proven here.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fake_workspace import FakeWorkspace

from appmux.packages.modes import DENY_ALL_EGRESS, PackageMode
from appmux.packages.overlay import (
    BuildStep,
    Overlay,
    OverlayBuilder,
    OverlayBuildFailed,
    StepResult,
    overlay_key,
    pip_index_environment,
)
from appmux.packages.store import ContentStore, DigestMismatch
from appmux.packages.wheels import ResolvedWheel, WheelStore
from appmux.sandbox.rules import EgressRule

pytestmark = pytest.mark.anyio

STORE_FOLDER = "/Workspace/Users/00000000-example/appmux-store"
SP_TOKEN = "sp-token-example"  # noqa: S105 - an obviously fake label
PYTHON = "/app/python/.venv/bin/python"
PYTHON_VERSION = "3.11"
PLATFORM = "cp311-cp311-manylinux2014_x86_64"
APP_RULES = (
    EgressRule(action="allow", cidr="203.0.113.0/24", port="443", comment="the company index"),
)
ALPHA_FILE = "appmux_fixture_alpha-1.0.0-py3-none-any.whl"
BETA_FILE = "appmux_fixture_beta-1.0.0-py3-none-any.whl"
BUILD_TIMEOUT_S = 600.0

OK = StepResult(exit_code=0, timed_out=False, output="")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def names_in(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def requirement_files_in(directory: Path) -> dict[str, str]:
    return {
        entry.name: entry.read_text(encoding="utf-8")
        for entry in directory.iterdir()
        if entry.suffix == ".txt"
    }


@dataclass
class Call:
    step: BuildStep
    timeout_s: float
    wheel_dir_names: list[str]
    requirement_files: dict[str, str]
    overlay_dir: Path
    wheel_dir: Path


@dataclass
class FakeRunner:
    """Plays pip: records each step, then writes ``installed`` into the overlay on success."""

    results: dict[str, StepResult] = field(default_factory=dict)
    on_run: Callable[[BuildStep], None] | None = None
    calls: list[Call] = field(default_factory=list)

    async def run(
        self, step: BuildStep, *, wheel_dir: Path, overlay_dir: Path, timeout_s: float
    ) -> StepResult:
        self.calls.append(
            Call(
                step,
                timeout_s,
                names_in(wheel_dir),
                requirement_files_in(wheel_dir),
                overlay_dir,
                wheel_dir,
            )
        )
        if self.on_run is not None:
            self.on_run(step)
        result = self.results.get(step.label, OK)
        if step.label in {"wheels", "index"} and result.exit_code == 0:
            (overlay_dir / f"installed-by-{step.label}.txt").write_text("x", encoding="utf-8")
        return result

    def labels(self) -> list[str]:
        return [call.step.label for call in self.calls]


def new_store() -> tuple[WheelStore, FakeWorkspace]:
    workspace = FakeWorkspace(required_token=SP_TOKEN)
    content = ContentStore(workspace, folder=STORE_FOLDER, token_source=lambda: SP_TOKEN)
    return WheelStore(content), workspace


async def stored_wheel(
    store: WheelStore, tmp_path: Path, filename: str, data: bytes
) -> ResolvedWheel:
    local = tmp_path / f"upload-{filename}"
    local.write_bytes(data)
    await store.add(sha256_of(data), local)
    return ResolvedWheel(sha256_of(data), filename, "src")


def builder_over(
    runner: FakeRunner,
    store: WheelStore,
    tmp_path: Path,
    *,
    clock: Callable[[], float] | None = None,
    python_version: str = PYTHON_VERSION,
    platform: str = PLATFORM,
    index_environment: dict[str, str] | None = None,
) -> OverlayBuilder:
    return OverlayBuilder(
        runner=runner,
        store=store,
        cache_dir=tmp_path / "cache",
        python=PYTHON,
        build_timeout_s=BUILD_TIMEOUT_S,
        python_version=python_version,
        platform=platform,
        clock=clock or (lambda: 0.0),
        index_environment=index_environment,
    )


async def build(
    builder: OverlayBuilder,
    wheels: Sequence[ResolvedWheel],
    *,
    mode: PackageMode = PackageMode.WHEELHOUSE,
    index_requirements: Sequence[str] = (),
) -> Overlay:
    overlay = await builder.ensure(
        mode=mode, wheels=wheels, index_requirements=index_requirements, app_rules=APP_RULES
    )
    assert overlay is not None
    return overlay


# --- the cache key ------------------------------------------------------------------------------


def wheel(name: str, data: bytes) -> ResolvedWheel:
    return ResolvedWheel(sha256_of(data), name, "src")


def key_of(
    wheels: Sequence[ResolvedWheel],
    index_requirements: Sequence[str] = (),
    *,
    python_version: str = PYTHON_VERSION,
    platform: str = PLATFORM,
) -> str:
    return overlay_key(wheels, index_requirements, python_version=python_version, platform=platform)


def test_the_key_is_the_digest_of_the_wheel_list_the_python_version_and_the_platform() -> None:
    alpha, beta = wheel(ALPHA_FILE, b"alpha"), wheel(BETA_FILE, b"beta")
    key = key_of([alpha, beta])
    assert len(key) == 64
    assert set(key) <= set("0123456789abcdef")
    assert key == key_of([alpha, beta])  # deterministic, from the arguments alone
    assert key == key_of([beta, alpha])  # the order of the list does not change the overlay
    assert key == key_of([alpha, beta, alpha])  # nor does a wheel named twice
    assert key != key_of([alpha])
    assert key != key_of([alpha, beta], python_version="3.12")
    assert key != key_of([alpha, beta], platform="cp311-cp311-manylinux_2_28_aarch64")
    assert key != key_of([alpha, wheel(BETA_FILE, b"another beta")])


def test_the_file_name_and_source_of_a_wheel_do_not_change_the_key() -> None:
    # the bytes are what is installed; where they came from does not matter
    one = ResolvedWheel(sha256_of(b"alpha"), ALPHA_FILE, "wheels/")
    other = ResolvedWheel(sha256_of(b"alpha"), ALPHA_FILE, "content store")
    assert key_of([one]) == key_of([other])


def test_index_requirements_are_part_of_the_key() -> None:
    alpha = wheel(ALPHA_FILE, b"alpha")
    assert key_of([alpha], ["requests>=2.31"]) != key_of([alpha])
    assert key_of([alpha], ["a>=1", "b>=1"]) == key_of([alpha], ["b>=1", "a>=1"])
    assert key_of([], ["requests>=2.31"]) != key_of([], ["requests>=2.32"])


# --- a release that needs nothing ---------------------------------------------------------------


async def test_a_release_that_needs_no_wheel_and_no_index_package_has_no_overlay(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    store, _ = new_store()
    builder = builder_over(runner, store, tmp_path)

    overlay = await builder.ensure(
        mode=PackageMode.WHEELHOUSE, wheels=[], index_requirements=[], app_rules=APP_RULES
    )

    assert overlay is None
    assert runner.calls == []
    assert not (tmp_path / "cache").exists()


# --- building -----------------------------------------------------------------------------------


async def test_the_build_gets_exactly_the_wheels_of_this_release_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    await stored_wheel(store, tmp_path, BETA_FILE, b"beta")  # in the store, not in this release
    runner = FakeRunner()
    builder = builder_over(runner, store, tmp_path)

    overlay = await build(builder, [alpha])

    assert overlay.built is True
    assert overlay.warnings == ()
    assert overlay.key == key_of([alpha])
    assert overlay.path == tmp_path / "cache" / overlay.key
    assert names_in(overlay.path) == ["installed-by-wheels.txt"]

    wheels_call = runner.calls[0]
    assert wheels_call.wheel_dir_names == [ALPHA_FILE, "wheels-needed.txt"]  # no beta
    assert wheels_call.requirement_files == {
        "wheels-needed.txt": f"appmux-fixture-alpha==1.0.0 --hash=sha256:{alpha.sha256}\n"
    }
    # the scratch directories are inside the cache so the final rename never crosses a filesystem
    assert wheels_call.overlay_dir.parent.parent == tmp_path / "cache"
    # and none is left behind: only the finished overlay and its pip-check note
    assert names_in(tmp_path / "cache") == [overlay.key, f"{overlay.key}.pip-check.txt"]


async def test_steps_run_in_order_each_with_its_own_egress_list(tmp_path: Path) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner()
    builder = builder_over(runner, store, tmp_path)

    await build(builder, [alpha], mode=PackageMode.WHEELHOUSE_INDEX, index_requirements=["foo==2"])

    assert runner.labels() == ["wheels", "index", "check"]
    assert [call.step.egress for call in runner.calls] == [
        DENY_ALL_EGRESS,
        APP_RULES,
        DENY_ALL_EGRESS,
    ]
    index_call = runner.calls[1]
    assert index_call.requirement_files["index-needed.txt"] == "foo==2\n"


async def test_the_build_time_limit_is_one_budget_shared_by_the_steps(tmp_path: Path) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    now = [1000.0]
    runner = FakeRunner(on_run=lambda step: now.__setitem__(0, now[0] + 100.0))
    builder = builder_over(runner, store, tmp_path, clock=lambda: now[0])

    await build(builder, [alpha])

    assert [call.timeout_s for call in runner.calls] == [BUILD_TIMEOUT_S, BUILD_TIMEOUT_S - 100.0]


async def test_a_step_that_overruns_the_limit_fails_the_build_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner(
        results={"wheels": StepResult(exit_code=None, timed_out=True, output="Collecting x")}
    )
    builder = builder_over(runner, store, tmp_path)

    with pytest.raises(OverlayBuildFailed) as raised:
        await build(builder, [alpha])

    assert "did not finish within 600 s" in raised.value.reason
    assert "build_timeout_s" in raised.value.reason  # says what to do next
    assert runner.labels() == ["wheels"]  # no check after a failure
    assert names_in(tmp_path / "cache") == []


async def test_a_failed_install_reports_the_tail_of_the_log_and_leaves_no_overlay(
    tmp_path: Path,
) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    log = "\n".join(f"line {number}" for number in range(100))
    runner = FakeRunner(results={"wheels": StepResult(exit_code=1, timed_out=False, output=log)})
    builder = builder_over(runner, store, tmp_path)

    with pytest.raises(OverlayBuildFailed) as raised:
        await build(builder, [alpha])

    assert "exit code 1" in raised.value.reason
    assert raised.value.log_tail.splitlines()[-1] == "line 99"
    assert len(raised.value.log_tail.splitlines()) <= 20
    assert "line 0" not in raised.value.log_tail.splitlines()
    assert names_in(tmp_path / "cache") == []


async def test_a_failed_build_can_be_tried_again_and_then_publishes(tmp_path: Path) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner(results={"wheels": StepResult(exit_code=1, timed_out=False, output="no")})
    builder = builder_over(runner, store, tmp_path)
    with pytest.raises(OverlayBuildFailed):
        await build(builder, [alpha])

    runner.results.clear()
    overlay = await build(builder, [alpha])

    assert overlay.built is True
    assert names_in(overlay.path) == ["installed-by-wheels.txt"]


async def test_a_wheel_the_store_holds_with_other_bytes_stops_the_build_before_pip_runs(
    tmp_path: Path,
) -> None:
    store, workspace = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    workspace.add_file(f"{STORE_FOLDER}/{alpha.sha256}", b"someone replaced this file")
    runner = FakeRunner()
    builder = builder_over(runner, store, tmp_path)

    with pytest.raises(DigestMismatch):
        await build(builder, [alpha])

    assert runner.calls == []
    assert names_in(tmp_path / "cache") == []


async def test_a_runner_that_raises_leaves_no_scratch_directory(tmp_path: Path) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")

    def explode(step: BuildStep) -> None:
        raise RuntimeError("the sandbox could not start")

    builder = builder_over(FakeRunner(on_run=explode), store, tmp_path)
    with pytest.raises(RuntimeError, match="could not start"):
        await build(builder, [alpha])
    assert names_in(tmp_path / "cache") == []


# --- pip check ----------------------------------------------------------------------------------


async def test_pip_check_output_is_a_warning_on_the_release_never_a_failure(
    tmp_path: Path,
) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    conflict = (
        "opentelemetry-sdk 1.27.0 has requirement opentelemetry-api==1.27.0, "
        "but you have opentelemetry-api 1.45.1.\n"
        "\n"
        "opentelemetry-semantic-conventions 0.48b0 has requirement opentelemetry-api==1.27.0, "
        "but you have opentelemetry-api 1.45.1.\n"
    )
    runner = FakeRunner(
        results={"check": StepResult(exit_code=1, timed_out=False, output=conflict)}
    )
    builder = builder_over(runner, store, tmp_path)

    overlay = await build(builder, [alpha])

    assert overlay.built is True  # the overlay is published all the same
    assert overlay.warnings == tuple(line for line in conflict.splitlines() if line.strip())
    assert overlay.path.is_dir()


async def test_a_pip_check_that_cannot_finish_is_a_warning_too(tmp_path: Path) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner(results={"check": StepResult(exit_code=None, timed_out=True, output="")})
    overlay = await build(builder_over(runner, store, tmp_path), [alpha])
    assert overlay.warnings == ("pip check did not finish in time; conflicts, if any, are unknown",)


async def test_pip_check_is_skipped_with_a_warning_when_the_install_used_the_whole_budget(
    tmp_path: Path,
) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    now = [0.0]
    runner = FakeRunner(on_run=lambda step: now.__setitem__(0, now[0] + BUILD_TIMEOUT_S))
    builder = builder_over(runner, store, tmp_path, clock=lambda: now[0])

    overlay = await build(builder, [alpha])

    assert runner.labels() == ["wheels"]
    assert overlay.warnings == ("pip check was skipped: the install used the whole build time",)


async def test_a_long_pip_check_output_is_cut_to_a_bounded_warning(tmp_path: Path) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    output = "\n".join(
        f"pkg{number} has requirement x, but you have y." + "z" * 500 for number in range(200)
    )
    runner = FakeRunner(results={"check": StepResult(exit_code=1, timed_out=False, output=output)})

    overlay = await build(builder_over(runner, store, tmp_path), [alpha])

    assert len(overlay.warnings) <= 21  # a bounded number of lines, plus a note that more exist
    assert all(len(line) <= 320 for line in overlay.warnings)
    assert "more" in overlay.warnings[-1]


# --- the cache ----------------------------------------------------------------------------------


async def test_the_second_start_reuses_the_overlay_and_runs_nothing(tmp_path: Path) -> None:
    store, workspace = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner(
        results={"check": StepResult(exit_code=1, timed_out=False, output="conflict")}
    )
    first = await build(builder_over(runner, store, tmp_path), [alpha])
    workspace.calls.clear()
    calls_after_first = len(runner.calls)

    # a new builder: the cache is the directory, not the object (a restart of appmux)
    second = await build(builder_over(runner, store, tmp_path), [alpha])

    assert second.built is False
    assert second.path == first.path
    assert second.warnings == ("conflict",)  # the warning is not lost on reuse
    assert len(runner.calls) == calls_after_first
    assert workspace.calls == []  # not even the store is read


async def test_a_different_python_or_platform_builds_another_overlay(tmp_path: Path) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner()

    first = await build(builder_over(runner, store, tmp_path), [alpha])
    other = await build(builder_over(runner, store, tmp_path, python_version="3.12"), [alpha])

    assert other.key != first.key
    assert other.built is True
    assert first.path.is_dir()
    assert other.path.is_dir()


async def test_two_releases_with_one_wheel_list_share_one_overlay_and_one_build(
    tmp_path: Path,
) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner()
    builder = builder_over(runner, store, tmp_path)

    first, second = await asyncio.gather(build(builder, [alpha]), build(builder, [alpha]))

    assert first.path == second.path
    assert sorted([first.built, second.built]) == [False, True]
    assert runner.labels() == ["wheels", "check"]  # built once


async def test_an_index_overlay_is_cached_by_its_requirements(tmp_path: Path) -> None:
    store, _ = new_store()
    runner = FakeRunner()
    builder = builder_over(runner, store, tmp_path)

    first = await build(builder, [], mode=PackageMode.INDEX, index_requirements=["requests>=2.31"])
    again = await build(builder, [], mode=PackageMode.INDEX, index_requirements=["requests>=2.31"])
    newer = await build(builder, [], mode=PackageMode.INDEX, index_requirements=["requests>=2.32"])

    assert (first.built, again.built, newer.built) == (True, False, True)
    assert runner.labels() == ["index", "check", "index", "check"]
    assert first.path != newer.path


# --- the time budget is read at every build -----------------------------------------------------


async def test_a_budget_given_to_ensure_replaces_the_one_the_builder_was_made_with(
    tmp_path: Path,
) -> None:
    # build_timeout_s is a platform setting an admin can change while appmux runs.
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner(
        results={"wheels": StepResult(exit_code=None, timed_out=True, output="Collecting x")}
    )
    builder = builder_over(runner, store, tmp_path)

    with pytest.raises(OverlayBuildFailed) as raised:
        await builder.ensure(
            mode=PackageMode.WHEELHOUSE,
            wheels=[alpha],
            index_requirements=[],
            app_rules=APP_RULES,
            build_timeout_s=30.0,
        )

    assert runner.calls[0].timeout_s == 30.0
    assert "did not finish within 30 s" in raised.value.reason


# --- index credentials stay out of what the release shows ---------------------------------------

INDEX_TOKEN = "tok-test-index-secret"  # noqa: S105 - an obviously fake value
INDEX_URL = f"https://dana:{INDEX_TOKEN}@index.example/simple"


async def test_the_index_credential_reaches_only_the_index_step(tmp_path: Path) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner()
    builder = builder_over(runner, store, tmp_path, index_environment={"PIP_INDEX_URL": INDEX_URL})

    await build(builder, [alpha], mode=PackageMode.WHEELHOUSE_INDEX, index_requirements=["foo==2"])

    by_label = {call.step.label: call.step.environment for call in runner.calls}
    assert by_label["index"]["PIP_INDEX_URL"] == INDEX_URL
    assert "PIP_INDEX_URL" not in by_label["wheels"]
    assert "PIP_INDEX_URL" not in by_label["check"]


async def test_the_credential_is_hidden_in_the_log_tail_of_a_failed_index_install(
    tmp_path: Path,
) -> None:
    store, _ = new_store()
    log = (
        f"Looking in indexes: {INDEX_URL}\n"
        f"WARNING: 401 for https://dana:{INDEX_TOKEN}@index.example/simple/foo/\n"
        f"ERROR: the token {INDEX_TOKEN} was refused"
    )
    runner = FakeRunner(results={"index": StepResult(exit_code=1, timed_out=False, output=log)})
    builder = builder_over(runner, store, tmp_path, index_environment={"PIP_INDEX_URL": INDEX_URL})

    with pytest.raises(OverlayBuildFailed) as raised:
        await build(builder, [], mode=PackageMode.INDEX, index_requirements=["foo==2"])

    assert INDEX_TOKEN not in raised.value.log_tail
    assert INDEX_TOKEN not in raised.value.reason
    assert "index.example" in raised.value.log_tail  # the rest of the line still helps


async def test_the_credential_is_hidden_in_the_pip_check_warnings(tmp_path: Path) -> None:
    store, _ = new_store()
    runner = FakeRunner(
        results={
            "check": StepResult(
                exit_code=1, timed_out=False, output=f"foo has requirement bar, see {INDEX_URL}"
            )
        }
    )
    builder = builder_over(runner, store, tmp_path, index_environment={"PIP_INDEX_URL": INDEX_URL})

    overlay = await build(builder, [], mode=PackageMode.INDEX, index_requirements=["foo==2"])

    assert overlay.warnings
    assert all(INDEX_TOKEN not in line for line in overlay.warnings)


async def test_the_credential_is_hidden_when_the_runner_itself_fails(tmp_path: Path) -> None:
    store, _ = new_store()

    class FailingRunner:
        async def run(
            self, step: BuildStep, *, wheel_dir: Path, overlay_dir: Path, timeout_s: float
        ) -> StepResult:
            raise OverlayBuildFailed(
                f"the build sandbox could not start: {INDEX_TOKEN}",
                log_tail=f"supervisor said {INDEX_URL}",
            )

    builder = OverlayBuilder(
        runner=FailingRunner(),
        store=store,
        cache_dir=tmp_path / "cache",
        python=PYTHON,
        build_timeout_s=BUILD_TIMEOUT_S,
        python_version=PYTHON_VERSION,
        platform=PLATFORM,
        index_environment={"PIP_INDEX_URL": INDEX_URL},
    )

    with pytest.raises(OverlayBuildFailed) as raised:
        await build(builder, [], mode=PackageMode.INDEX, index_requirements=["foo==2"])

    assert INDEX_TOKEN not in raised.value.reason
    assert INDEX_TOKEN not in raised.value.log_tail
    assert "could not start" in raised.value.reason


async def test_a_build_without_index_credentials_leaves_its_log_alone(tmp_path: Path) -> None:
    store, _ = new_store()
    alpha = await stored_wheel(store, tmp_path, ALPHA_FILE, b"alpha")
    runner = FakeRunner(
        results={
            "wheels": StepResult(exit_code=1, timed_out=False, output="ERROR: no matching hash")
        }
    )

    with pytest.raises(OverlayBuildFailed) as raised:
        await build(builder_over(runner, store, tmp_path), [alpha])

    assert raised.value.log_tail == "ERROR: no matching hash"


def test_only_pips_index_variables_are_taken_from_the_process_environment() -> None:
    environ = {
        "PIP_INDEX_URL": INDEX_URL,
        "PIP_EXTRA_INDEX_URL": "https://extra.example/simple",
        "PIP_TRUSTED_HOST": "index.example",
        "PIP_REQUIRE_VIRTUALENV": "1",  # pip's, but not about the index
        "DATABRICKS_TOKEN": "tok-test-not-for-pip",
        "HOME": "/root",
    }

    assert pip_index_environment(environ) == {
        "PIP_INDEX_URL": INDEX_URL,
        "PIP_EXTRA_INDEX_URL": "https://extra.example/simple",
        "PIP_TRUSTED_HOST": "index.example",
    }


def test_an_empty_index_variable_is_not_passed_on() -> None:
    assert pip_index_environment({"PIP_INDEX_URL": "", "HOME": "/root"}) == {}
