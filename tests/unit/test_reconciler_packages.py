"""The package step of the build (T136): the environment is checked at every start; overlay built.

``StoreReleaseBuilder.build`` runs in the ``building`` state, at every start of a release (a restart
and a restore included), so what it does for packages is done at every start: read the release's
``requirements.txt``, judge it again against the environment the container has *now*, and build the
overlay from the release's own wheel list. The overlay builder is replaced by a recording fake in
most tests; one test runs the real ``OverlayBuilder`` over a fake runner and an in-memory content
store to show that a release's recorded wheels reach the build as files. That pip installs them in a
real sandbox is not proven here.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

import pytest
from fake_workspace import FakeWorkspace
from reconciler_fakes import FakeArchiveStore, archive_bytes, digest_of, stub_registry_class

from appmux.packages.environment import Environment, read_environment
from appmux.packages.modes import PackageMode
from appmux.packages.overlay import (
    BuildStep,
    Overlay,
    OverlayBuilder,
    OverlayBuildFailed,
    OverlayError,
    StepResult,
)
from appmux.packages.store import ContentStore, MissingFromStore
from appmux.packages.wheels import ResolvedWheel, WheelStore
from appmux.reconciler import build as build_module
from appmux.reconciler.build import (
    BuildFailed,
    PackagePolicy,
    PackageStep,
    StoreReleaseBuilder,
    package_policy_from,
)
from appmux.registry.models import Release
from appmux.settings import PlatformSettings

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
STORE_FOLDER = "/Workspace/Users/00000000-example/appmux-store"
SP_TOKEN = "sp-token-example"  # noqa: S105 - an obviously fake label
INDEX_RULE = {"action": "allow", "cidr": "203.0.113.0/24", "port": "443", "comment": "the index"}
ALPHA_FILE = "appmux_fixture_alpha-1.0.0-py3-none-any.whl"
ALPHA_BYTES = b"alpha wheel stand-in"
ALPHA_SHA = hashlib.sha256(ALPHA_BYTES).hexdigest()
ALPHA_LINE = f"appmux-fixture-alpha==1.0.0 --hash=sha256:{ALPHA_SHA}"
ENVIRONMENT = {"fastapi": "0.115.0", "uvicorn": "0.30.6", "appdirs": "1.4.4"}
POLICY = PackagePolicy(mode=PackageMode.WHEELHOUSE, build_timeout_s=600.0, app_rules=())


def make_environment(root: Path, packages: Mapping[str, str]) -> Environment:
    root.mkdir(parents=True, exist_ok=True)
    for name, version in packages.items():
        folder = root / f"{name.replace('-', '_')}-{version}.dist-info"
        folder.mkdir()
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        (folder / "METADATA").write_text(metadata, encoding="utf-8")
    return read_environment(distributions(path=[str(root)]))


def release(
    number: int = 3, *, wheels: Sequence[tuple[str, str]] = (), slug: str = "sales"
) -> Release:
    return Release.model_validate(
        {
            "app_slug": slug,
            "release_no": number,
            "snapshot_digest": digest_of(number),
            "detected": {"runtime": "python", "command": ["python", "app.py"]},
            "wheels": [
                {"sha256": sha256, "filename": filename, "source": "wheels/"}
                for sha256, filename in wheels
            ],
            "status": "building",
            "status_at": NOW,
            "created_by": "alice@example.com",
            "created_at": NOW,
        }
    )


@dataclass
class Ensured:
    mode: PackageMode
    wheels: tuple[ResolvedWheel, ...]
    index_requirements: tuple[str, ...]
    app_rules: tuple[object, ...]
    build_timeout_s: float | None


@dataclass
class FakeOverlays:
    """The ``OverlayBuilder.ensure`` a build calls: records the call, answers a prepared overlay."""

    overlay: Overlay | None = None
    error: Exception | None = None
    calls: list[Ensured] = field(default_factory=list)

    async def ensure(
        self,
        *,
        mode: PackageMode,
        wheels: Sequence[ResolvedWheel],
        index_requirements: Sequence[str],
        app_rules: Sequence[object],
        build_timeout_s: float | None = None,
    ) -> Overlay | None:
        self.calls.append(
            Ensured(
                mode, tuple(wheels), tuple(index_requirements), tuple(app_rules), build_timeout_s
            )
        )
        if self.error is not None:
            raise self.error
        return self.overlay


class Fixture:
    """A release builder over a fake archive store with a package step wired to fakes."""

    def __init__(self, tmp_path: Path, *, environment: Environment | None = None) -> None:
        self.tmp_path = tmp_path
        self.store = FakeArchiveStore()
        self.overlays = FakeOverlays(
            overlay=Overlay("k" * 64, tmp_path / "overlays" / "k", ("a conflict",), built=True)
        )
        self.policy = POLICY
        self.policy_asked_for: list[str] = []
        self.environment = environment or make_environment(tmp_path / "site", ENVIRONMENT)
        self.environment_reads = 0
        self.builder = StoreReleaseBuilder(
            self.store,
            tmp_path / "releases",
            PackageStep(
                overlays=self.overlays, policy=self.policy_for, environment=self.read_environment
            ),
        )

    def policy_for(self, slug: str) -> PackagePolicy:
        self.policy_asked_for.append(slug)
        return self.policy

    def read_environment(self) -> Environment:
        self.environment_reads += 1
        return self.environment

    def archive(self, requirements: str | None, number: int = 3) -> None:
        files: dict[str, bytes | Any] = {"app.py": b"print(1)\n"}
        if requirements is not None:
            files["requirements.txt"] = requirements.encode()
        self.store.archives[digest_of(number)] = archive_bytes(files)


# --- a release with nothing to install ----------------------------------------------------------


async def test_a_release_without_a_requirements_file_has_no_overlay_and_reads_nothing(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.archive(None)

    built = await fixture.builder.build(release())

    assert tuple(built.overlays) == ()
    assert built.warnings == ()
    assert fixture.overlays.calls == []
    assert fixture.environment_reads == 0, "no requirements, so no reason to read the environment"
    assert fixture.policy_asked_for == []


async def test_requirements_the_environment_meets_need_no_overlay(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.archive("fastapi==0.115.0\nuvicorn==0.30.6\n")

    built = await fixture.builder.build(release())

    assert tuple(built.overlays) == ()
    assert fixture.overlays.calls == [], "nothing to build, so the overlay builder is not asked"


async def test_a_builder_made_without_a_package_step_still_unpacks_the_snapshot(
    tmp_path: Path,
) -> None:
    store = FakeArchiveStore()
    store.archives[digest_of(3)] = archive_bytes({"app.py": b"x", "requirements.txt": b"polars\n"})
    builder = StoreReleaseBuilder(store, tmp_path / "releases")

    built = await builder.build(release())

    assert tuple(built.overlays) == ()
    assert (built.release_dir / "app.py").exists()


# --- wheels -------------------------------------------------------------------------------------


async def test_the_overlay_is_built_from_the_wheels_this_release_recorded(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.archive(f"fastapi==0.115.0\n{ALPHA_LINE}\n")

    built = await fixture.builder.build(release(wheels=[(ALPHA_SHA, ALPHA_FILE)]))

    (call,) = fixture.overlays.calls
    assert call.mode is PackageMode.WHEELHOUSE
    assert call.wheels == (ResolvedWheel(ALPHA_SHA, ALPHA_FILE, "wheels/"),)
    assert call.index_requirements == ()
    assert tuple(built.overlays) == (tmp_path / "overlays" / "k",)
    assert built.warnings == ("a conflict",), "pip check output travels with the build"


async def test_the_platform_policy_and_the_apps_own_rules_decide_how_it_is_built(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.policy = PackagePolicy(
        mode=PackageMode.WHEELHOUSE_INDEX,
        build_timeout_s=45.0,
        app_rules=("a rule",),  # type: ignore[arg-type]
    )
    fixture.archive(f"{ALPHA_LINE}\n")

    await fixture.builder.build(release(wheels=[(ALPHA_SHA, ALPHA_FILE)], slug="sales"))

    (call,) = fixture.overlays.calls
    assert fixture.policy_asked_for == ["sales"]
    assert call.mode is PackageMode.WHEELHOUSE_INDEX
    assert call.build_timeout_s == 45.0
    assert call.app_rules == ("a rule",)


async def test_the_environment_and_the_policy_are_read_again_at_every_build(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.archive(f"{ALPHA_LINE}\n")
    wheels = [(ALPHA_SHA, ALPHA_FILE)]

    await fixture.builder.build(release(wheels=wheels))
    fixture.policy = PackagePolicy(mode=PackageMode.WHEELHOUSE, build_timeout_s=99.0, app_rules=())
    await fixture.builder.build(release(wheels=wheels))

    assert fixture.environment_reads == 2
    assert [call.build_timeout_s for call in fixture.overlays.calls] == [600.0, 99.0]


# --- the re-check -------------------------------------------------------------------------------


async def test_a_requirement_the_environment_no_longer_meets_fails_the_release_before_a_build(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.archive("fastapi==0.115.0\nuvicorn==0.30.6\n")
    await fixture.builder.build(release())  # it started when the environment held 0.30.6

    fixture.environment = make_environment(
        tmp_path / "redeployed", {"fastapi": "0.115.0", "uvicorn": "0.31.0"}
    )
    with pytest.raises(BuildFailed) as failure:
        await fixture.builder.build(release())

    assert failure.value.reason == (
        "the environment no longer provides 'uvicorn==0.30.6' (it has 0.31.0); "
        "add a wheel and Update"
    )
    assert fixture.overlays.calls == []


async def test_the_failure_lists_every_requirement_that_no_longer_holds(tmp_path: Path) -> None:
    fixture = Fixture(
        tmp_path, environment=make_environment(tmp_path / "bare", {"fastapi": "0.115.0"})
    )
    fixture.archive("fastapi==0.115.0\nuvicorn==0.30.6\npolars>=1.0\n")

    with pytest.raises(BuildFailed) as failure:
        await fixture.builder.build(release())

    assert "'uvicorn==0.30.6' (it has no uvicorn)" in failure.value.reason
    assert "'polars>=1.0' (it has no polars)" in failure.value.reason


async def test_a_requirement_a_recorded_wheel_backs_is_not_judged_against_the_environment(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)  # the environment has appdirs 1.4.4
    newer = b"appdirs 99.0.0 stand-in"
    sha = hashlib.sha256(newer).hexdigest()
    filename = "appdirs-99.0.0-py3-none-any.whl"
    fixture.archive(f"appdirs==99.0.0 --hash=sha256:{sha}\n")

    await fixture.builder.build(release(wheels=[(sha, filename)]))

    (call,) = fixture.overlays.calls
    assert [wheel.filename for wheel in call.wheels] == [filename]


async def test_without_the_recorded_wheel_the_same_requirement_fails_the_release(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    sha = hashlib.sha256(b"appdirs 99.0.0 stand-in").hexdigest()
    fixture.archive(f"appdirs==99.0.0 --hash=sha256:{sha}\n")

    with pytest.raises(BuildFailed, match=re.escape("no longer provides 'appdirs==99.0.0'")):
        await fixture.builder.build(release())


# --- the index modes ----------------------------------------------------------------------------


async def test_in_index_mode_what_the_environment_lacks_comes_from_the_index_and_wheels_are_ignored(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.policy = PackagePolicy(mode=PackageMode.INDEX, build_timeout_s=600.0, app_rules=())
    fixture.archive(f"fastapi==0.115.0\n{ALPHA_LINE}\n")

    await fixture.builder.build(release(wheels=[(ALPHA_SHA, ALPHA_FILE)]))

    (call,) = fixture.overlays.calls
    assert call.wheels == ()
    assert call.index_requirements == ("appmux-fixture-alpha==1.0.0",), "the hash is dropped"


async def test_in_wheelhouse_and_index_mode_the_wheels_come_first_and_the_index_fills_the_rest(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.policy = PackagePolicy(
        mode=PackageMode.WHEELHOUSE_INDEX, build_timeout_s=600.0, app_rules=()
    )
    fixture.archive(f"{ALPHA_LINE}\nrequests>=2.31\n")

    await fixture.builder.build(release(wheels=[(ALPHA_SHA, ALPHA_FILE)]))

    (call,) = fixture.overlays.calls
    assert [wheel.sha256 for wheel in call.wheels] == [ALPHA_SHA]
    assert call.index_requirements == ("requests>=2.31",)


# --- when the overlay cannot be built -----------------------------------------------------------


async def test_a_failed_overlay_build_fails_the_release_with_its_reason_and_log(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.overlays.error = OverlayBuildFailed(
        "installing the wheels failed (exit code 1); the end of the build log is attached",
        log_tail="ERROR: THESE PACKAGES DO NOT MATCH THE HASHES",
    )
    fixture.archive(f"{ALPHA_LINE}\n")

    with pytest.raises(BuildFailed) as failure:
        await fixture.builder.build(release(wheels=[(ALPHA_SHA, ALPHA_FILE)]))

    assert failure.value.reason.startswith("installing the wheels failed")
    assert failure.value.log_tail == "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES"
    assert isinstance(failure.value.__cause__, OverlayBuildFailed)


async def test_an_overlay_that_cannot_be_planned_fails_the_release(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.overlays.error = OverlayError("the package mode 'wheelhouse' has no package index")
    fixture.archive(f"{ALPHA_LINE}\n")

    with pytest.raises(BuildFailed, match="has no package index"):
        await fixture.builder.build(release(wheels=[(ALPHA_SHA, ALPHA_FILE)]))


async def test_a_wheel_the_content_store_lost_fails_the_release_and_says_what_to_do(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.overlays.error = MissingFromStore(f"{ALPHA_SHA} is not in the content store")
    fixture.archive(f"{ALPHA_LINE}\n")

    with pytest.raises(BuildFailed) as failure:
        await fixture.builder.build(release(wheels=[(ALPHA_SHA, ALPHA_FILE)]))

    assert "not in the content store" in failure.value.reason
    assert "Update" in failure.value.reason


async def test_a_disk_error_while_building_fails_the_release_with_what_to_check(
    tmp_path: Path,
) -> None:
    fixture = Fixture(tmp_path)
    fixture.overlays.error = OSError(28, "No space left on device")
    fixture.archive(f"{ALPHA_LINE}\n")

    with pytest.raises(BuildFailed, match="No space left on device") as failure:
        await fixture.builder.build(release(wheels=[(ALPHA_SHA, ALPHA_FILE)]))

    assert "free space" in failure.value.reason


async def test_a_requirements_file_that_is_not_text_fails_the_release(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    fixture.store.archives[digest_of(3)] = archive_bytes({"requirements.txt": b"\xff\xfe\x00bad"})

    with pytest.raises(BuildFailed, match=re.escape("requirements.txt is not UTF-8 text")):
        await fixture.builder.build(release())


async def test_a_requirements_file_over_the_size_limit_fails_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_module, "MAX_REQUIREMENTS_BYTES", 10)
    fixture = Fixture(tmp_path)
    fixture.archive("fastapi==0.115.0\nuvicorn==0.30.6\n")

    with pytest.raises(BuildFailed, match=re.escape("requirements.txt is larger than 10 bytes")):
        await fixture.builder.build(release())


# --- the real overlay builder -------------------------------------------------------------------


@dataclass
class RecordingRunner:
    steps: list[tuple[str, list[str]]] = field(default_factory=list)

    async def run(
        self, step: BuildStep, *, wheel_dir: Path, overlay_dir: Path, timeout_s: float
    ) -> StepResult:
        names = await asyncio.to_thread(lambda: sorted(entry.name for entry in wheel_dir.iterdir()))
        self.steps.append((step.label, names))
        if step.label == "wheels":
            (overlay_dir / "appmux_fixture_alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
        return StepResult(exit_code=0, timed_out=False, output="")


async def test_the_recorded_wheels_reach_the_real_overlay_builder_as_files_and_are_cached(
    tmp_path: Path,
) -> None:
    workspace = FakeWorkspace(required_token=SP_TOKEN)
    content = ContentStore(workspace, folder=STORE_FOLDER, token_source=lambda: SP_TOKEN)
    wheels = WheelStore(content)
    upload = tmp_path / "upload.whl"
    upload.write_bytes(ALPHA_BYTES)
    await wheels.add(ALPHA_SHA, upload)
    runner = RecordingRunner()
    overlays = OverlayBuilder(
        runner=runner,
        store=wheels,
        cache_dir=tmp_path / "overlay-cache",
        python="/opt/app-environment-example/bin/python",
        build_timeout_s=600.0,
        python_version="3.11",
        platform="cp311-cp311-manylinux2014_x86_64",
    )
    fixture = Fixture(tmp_path)
    builder = StoreReleaseBuilder(
        fixture.store,
        tmp_path / "releases",
        PackageStep(
            overlays=overlays, policy=fixture.policy_for, environment=fixture.read_environment
        ),
    )
    fixture.archive(f"fastapi==0.115.0\n{ALPHA_LINE}\n")
    row = release(wheels=[(ALPHA_SHA, ALPHA_FILE)])

    first = await builder.build(row)
    second = await builder.build(row)  # a restart: same release, same wheels

    (overlay_path,) = first.overlays
    assert (overlay_path / "appmux_fixture_alpha.py").is_file()
    assert tuple(second.overlays) == (overlay_path,)
    assert runner.steps == [
        ("wheels", [ALPHA_FILE, "wheels-needed.txt"]),
        ("check", [ALPHA_FILE, "wheels-needed.txt"]),
    ], "built once: the restart found the overlay in the cache"


# --- the platform policy ------------------------------------------------------------------------


def registry_with(**settings: object) -> Any:
    registry = stub_registry_class()()
    registry.seed(
        "apps",
        {
            "slug": "sales",
            "display_name": "Sales",
            "owner": "alice@example.com",
            "source_path": "/Workspace/Shared/apps/sales",
            "memory_budget_mb": 256,
            "egress": [INDEX_RULE],
            "created_by": "dana@example.com",
            "created_at": NOW,
            "updated_by": "dana@example.com",
            "updated_at": NOW,
        },
    )
    for key, value in settings.items():
        registry.seed(
            "platform_settings",
            {"key": key, "value": value, "updated_by": "dana@example.com", "updated_at": NOW},
        )
    return registry


def test_the_policy_is_the_platform_default_when_nothing_is_stored() -> None:
    defaults = PlatformSettings()

    policy = package_policy_from(registry_with().snapshot, "sales")

    assert policy.mode is PackageMode(defaults.package_mode)
    assert policy.build_timeout_s == defaults.build_timeout_s
    assert [rule.cidr for rule in policy.app_rules] == ["203.0.113.0/24"]


def test_the_policy_is_the_stored_mode_and_timeout() -> None:
    registry = registry_with(package_mode="wheelhouse+index", build_timeout_s=90)

    policy = package_policy_from(registry.snapshot, "sales")

    assert policy.mode is PackageMode.WHEELHOUSE_INDEX
    assert policy.build_timeout_s == 90


@pytest.mark.parametrize("unusable", ["pypi", "", True, 3, "Index"])
def test_a_stored_mode_that_is_not_a_package_mode_falls_back_to_the_default(
    unusable: object,
) -> None:
    default = PackageMode(PlatformSettings().package_mode)

    assert (
        package_policy_from(registry_with(package_mode=unusable).snapshot, "sales").mode is default
    )


@pytest.mark.parametrize("unusable", [0, -5, True, "600"])
def test_a_stored_timeout_that_is_not_a_positive_number_falls_back_to_the_default(
    unusable: object,
) -> None:
    default = PlatformSettings().build_timeout_s

    policy = package_policy_from(registry_with(build_timeout_s=unusable).snapshot, "sales")

    assert policy.build_timeout_s == default


def test_an_app_the_registry_does_not_hold_gets_no_rules_of_its_own() -> None:
    policy = package_policy_from(registry_with().snapshot, "unknown")

    assert policy.app_rules == ()


def test_the_policy_never_names_a_setting_value_when_it_logs_a_bad_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("ERROR"):
        package_policy_from(registry_with(package_mode="tok-test-odd-value").snapshot, "sales")

    assert "tok-test-odd-value" not in caplog.text
    assert "package_mode" in caplog.text
