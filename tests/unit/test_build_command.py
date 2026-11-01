"""The commands that build an overlay, and how a child starts on it (FR-032, design 5.3).

These are the *commands as data*: what pip is asked to do, with which environment and which egress
list, and how the launcher then puts the overlay in front of the environment. That pip really
installs from them, inside a real sandbox, is container-only and not proven here
(``tests/integration/test_offline_install.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from appmux.packages.modes import DENY_ALL_EGRESS, PackageMode
from appmux.packages.overlay import (
    INDEX_REQUIREMENTS_FILE,
    OVERLAY_DIR_IN_SANDBOX,
    WHEEL_DIR_IN_SANDBOX,
    WHEEL_REQUIREMENTS_FILE,
    BuildStep,
    Overlay,
    OverlayError,
    index_requirements_text,
    launch_overlays,
    plan_build,
    wheel_requirements_text,
)
from appmux.packages.wheels import ResolvedWheel, UnsafeWheelName
from appmux.sandbox.launcher import SandboxSpec, child_environment
from appmux.sandbox.rootfs import BUILD_OUTPUT_PATH, RELEASE_PATH, overlay_path
from appmux.sandbox.rules import EgressRule

PYTHON = "/app/python/.venv/bin/python"  # the environment's interpreter, as the sandbox sees it
APP_RULES = (
    EgressRule(action="allow", cidr="203.0.113.0/24", port="443", comment="the company index"),
)
ALPHA = "a" * 64
BETA = "b" * 64


def steps_for(
    mode: PackageMode,
    *,
    has_wheels: bool = True,
    has_index_requirements: bool = False,
    **overrides: Any,
) -> dict[str, BuildStep]:
    steps = plan_build(
        mode,
        python=PYTHON,
        has_wheels=has_wheels,
        has_index_requirements=has_index_requirements,
        app_rules=APP_RULES,
        **overrides,
    )
    return {step.label: step for step in steps}


# --- the wheel install --------------------------------------------------------------------------


def test_the_overlay_is_built_with_the_offline_hashed_pip_install_of_the_design() -> None:
    step = steps_for(PackageMode.WHEELHOUSE)["wheels"]
    assert step.argv == (
        PYTHON,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        "--require-hashes",
        "--find-links",
        WHEEL_DIR_IN_SANDBOX,
        "--target",
        OVERLAY_DIR_IN_SANDBOX,
        "-r",
        f"{WHEEL_DIR_IN_SANDBOX}/{WHEEL_REQUIREMENTS_FILE}",
    )


def test_the_directories_are_where_the_sandbox_puts_a_release_and_the_build_output() -> None:
    # The wheel directory is mounted read-only where a release is, the overlay under construction
    # read-write at the build-output path (rootfs.plan_rootfs); the commands name those places.
    assert WHEEL_DIR_IN_SANDBOX == RELEASE_PATH
    assert OVERLAY_DIR_IN_SANDBOX == BUILD_OUTPUT_PATH


def test_a_command_is_a_list_of_plain_arguments_never_a_shell_string() -> None:
    every_mode = [
        steps_for(mode, has_wheels=mode.uses_wheels, has_index_requirements=mode.uses_index)
        for mode in PackageMode
    ]
    for steps in every_mode:
        for step in steps.values():
            assert isinstance(step.argv, tuple)
            assert all(isinstance(argument, str) for argument in step.argv)
            assert step.argv[0] == PYTHON  # not `sh`, not `env`: the interpreter itself
            assert "-c" not in step.argv
            assert all(not any(c in argument for c in ";|&$`\n") for argument in step.argv)


def test_the_wheel_step_reaches_nothing_in_every_mode() -> None:
    for mode in (PackageMode.WHEELHOUSE, PackageMode.WHEELHOUSE_INDEX):
        step = steps_for(mode, has_index_requirements=mode.uses_index)["wheels"]
        assert step.egress == DENY_ALL_EGRESS, mode


def test_wheelhouse_gives_every_step_an_all_deny_egress_list() -> None:
    steps = steps_for(PackageMode.WHEELHOUSE)
    assert set(steps) == {"wheels", "check"}
    assert all(step.egress == DENY_ALL_EGRESS for step in steps.values())


def test_the_wheel_pass_is_offline_so_it_names_no_index_and_no_url() -> None:
    argv = steps_for(PackageMode.WHEELHOUSE)["wheels"].argv
    assert "--no-index" in argv
    assert not any("://" in argument for argument in argv)
    assert not any(argument.startswith(("--index-url", "--extra-index-url")) for argument in argv)


# --- the index step -----------------------------------------------------------------------------


def test_index_mode_installs_from_the_index_under_the_apps_egress_rules() -> None:
    steps = steps_for(PackageMode.INDEX, has_wheels=False, has_index_requirements=True)
    assert list(steps) == ["index", "check"]
    index = steps["index"]
    assert index.argv == (
        PYTHON,
        "-m",
        "pip",
        "install",
        "--target",
        OVERLAY_DIR_IN_SANDBOX,
        "-r",
        f"{WHEEL_DIR_IN_SANDBOX}/{INDEX_REQUIREMENTS_FILE}",
    )
    assert index.egress == APP_RULES  # and only the app's rules: it may reach what the app may
    assert steps["check"].egress == DENY_ALL_EGRESS


def test_source_distributions_are_built_only_in_index_mode() -> None:
    index = steps_for(PackageMode.INDEX, has_wheels=False, has_index_requirements=True)["index"]
    mixed = steps_for(PackageMode.WHEELHOUSE_INDEX, has_index_requirements=True)["index"]
    assert "--only-binary=:all:" not in index.argv
    assert "--only-binary=:all:" in mixed.argv


def test_wheelhouse_plus_index_runs_the_wheel_pass_first_and_offline() -> None:
    steps = plan_build(
        PackageMode.WHEELHOUSE_INDEX,
        python=PYTHON,
        has_wheels=True,
        has_index_requirements=True,
        app_rules=APP_RULES,
    )
    assert [step.label for step in steps] == ["wheels", "index", "check"]
    assert [step.egress for step in steps] == [DENY_ALL_EGRESS, APP_RULES, DENY_ALL_EGRESS]


def test_the_index_environment_reaches_only_the_index_step() -> None:
    steps = steps_for(
        PackageMode.WHEELHOUSE_INDEX,
        has_index_requirements=True,
        index_environment={"PIP_INDEX_URL": "https://index.example.invalid/simple"},
    )
    assert steps["index"].environment["PIP_INDEX_URL"] == "https://index.example.invalid/simple"
    assert "PIP_INDEX_URL" not in steps["wheels"].environment
    assert "PIP_INDEX_URL" not in steps["check"].environment


@pytest.mark.parametrize(
    ("mode", "has_wheels", "has_index"),
    [
        (PackageMode.WHEELHOUSE, True, True),  # wheelhouse has no index to give a package
        (PackageMode.INDEX, True, False),  # index mode does not use wheels
    ],
)
def test_a_plan_the_mode_does_not_allow_is_refused(
    mode: PackageMode, has_wheels: bool, has_index: bool
) -> None:
    with pytest.raises(OverlayError, match="package mode"):
        plan_build(
            mode,
            python=PYTHON,
            has_wheels=has_wheels,
            has_index_requirements=has_index,
            app_rules=APP_RULES,
        )


@pytest.mark.parametrize("python", ["python", "./python", "", "bin/python"])
def test_the_interpreter_must_be_an_absolute_path(python: str) -> None:
    with pytest.raises(OverlayError, match="absolute"):
        plan_build(
            PackageMode.WHEELHOUSE,
            python=python,
            has_wheels=True,
            has_index_requirements=False,
            app_rules=APP_RULES,
        )


# --- the environment of a build step ------------------------------------------------------------


def test_a_build_step_gets_a_small_fixed_environment() -> None:
    environment = steps_for(PackageMode.WHEELHOUSE)["wheels"].environment
    assert environment == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/app",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def test_pip_check_sees_the_overlay_the_way_the_child_will() -> None:
    check = steps_for(PackageMode.WHEELHOUSE)["check"]
    assert check.argv == (PYTHON, "-m", "pip", "check")
    assert check.environment["PYTHONPATH"] == OVERLAY_DIR_IN_SANDBOX


# --- the requirement files ----------------------------------------------------------------------


def test_the_wheel_requirements_pin_every_wheel_to_its_hash() -> None:
    wheels = [
        ResolvedWheel(ALPHA, "appmux_fixture_alpha-1.0.0-py3-none-any.whl", "src"),
        ResolvedWheel(BETA, "My_Package-2.0.0+acme1-cp311-cp311-manylinux2014_x86_64.whl", "src"),
    ]
    assert wheel_requirements_text(wheels) == (
        f"appmux-fixture-alpha==1.0.0 --hash=sha256:{ALPHA}\n"
        f"my-package==2.0.0+acme1 --hash=sha256:{BETA}\n"
    )


def test_a_wheel_listed_twice_appears_once_and_an_unsafe_name_is_refused() -> None:
    alpha = ResolvedWheel(ALPHA, "appmux_fixture_alpha-1.0.0-py3-none-any.whl", "src")
    assert wheel_requirements_text([alpha, alpha]).count("\n") == 1
    with pytest.raises(UnsafeWheelName):
        wheel_requirements_text([ResolvedWheel(ALPHA, "../x-1.0-py3-none-any.whl", "src")])


def test_index_requirements_are_written_one_per_line_without_hashes() -> None:
    assert index_requirements_text(["requests[socks]>=2.31", "foo==2.0"]) == (
        "requests[socks]>=2.31\nfoo==2.0\n"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "foo --index-url https://x.example.invalid",
        "foo\nbar",
        "-r x",
        "foo @ https://x.example.invalid/foo-1.0-py3-none-any.whl",  # a URL is not an index pin
    ],
)
def test_an_index_requirement_cannot_smuggle_an_option_or_a_second_line(bad: str) -> None:
    with pytest.raises(OverlayError, match="requirement"):
        index_requirements_text([bad])


# --- how a child starts on the overlay ----------------------------------------------------------


def child_spec(overlays: tuple[Path, ...], **environment: str) -> SandboxSpec:
    return SandboxSpec(
        slug="shop",
        command=("python", "-m", "uvicorn", "app:app"),
        environment=environment,
        app_environment=Path("/app/python/.venv"),
        release_dir=Path("/srv/releases/shop-1"),
        scratch_dir=Path("/srv/run/shop"),
        log_path=Path("/srv/run/shop/child.log"),
        rules_path=Path("/srv/run/shop/rules.json"),
        overlays=overlays,
    )


def overlay_at(path: Path) -> Overlay:
    return Overlay(key="k" * 64, path=path, warnings=(), built=False)


def test_the_child_starts_with_the_overlay_on_pythonpath_and_its_bin_first_on_path() -> None:
    overlay = Path("/tmp/appmux-overlays/aaaa")  # noqa: S108 - a host path in a spec, not opened
    started = child_environment(child_spec(launch_overlays(overlay_at(overlay))))

    assert started["PYTHONPATH"] == overlay_path(0)
    # an entry-point script in O/bin starts with this environment, so it imports the overlay first
    assert started["PATH"].split(":")[0] == f"{overlay_path(0)}/bin"
    assert started["PATH"].split(":")[1] == "/app/python/.venv/bin"


def test_an_app_pythonpath_stays_behind_the_overlay() -> None:
    overlay = Path("/tmp/appmux-overlays/aaaa")  # noqa: S108 - a host path in a spec, not opened
    started = child_environment(
        child_spec(launch_overlays(overlay_at(overlay)), PYTHONPATH="/srv/app/lib")
    )
    assert started["PYTHONPATH"] == f"{overlay_path(0)}:/srv/app/lib"


def test_a_release_without_an_overlay_starts_on_the_environment_alone() -> None:
    assert launch_overlays(None) == ()
    started = child_environment(child_spec(launch_overlays(None)))
    assert "PYTHONPATH" not in started
    assert not started["PATH"].startswith(overlay_path(0))
