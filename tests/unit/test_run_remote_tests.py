"""``scripts/run-remote-tests.sh``: the container tests, run from this machine (T108, T109, ...).

The integration tests need a real Databricks proxy in front of appmux, so they run from the
developer's machine against the deployed development App, as the owner of the Databricks CLI's
OAuth token (``tests/integration/ops_session.py``, ``APPMUX_TEST_REMOTE=1``). The script finds the
App's address and the token through the CLI, puts the fixtures in the workspace and starts pytest,
with the token only in pytest's environment. What can be checked without a workspace is checked
here against a stub CLI and a stub ``uv``: what pytest is given, what is uploaded first, that
nothing sensitive is printed or put on a command line, and that a missing piece stops the run
before pytest starts.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-remote-tests.sh"

WORKSPACE_FOLDER = "/Workspace/Users/user@example.com/appmux-deploy"
FIXTURES_FOLDER = f"{WORKSPACE_FOLDER}-fixtures"
PROFILE = "example-profile"
APP_URL = "https://appmux-0000.example.invalid"
# Fake secret-shaped text, built at run time so no such literal sits in the source.
FAKE_TOKEN = "Zq7" + "x" * 15 + "9"

STUB_CLI = """#!/bin/bash
# Stands in for the Databricks CLI: records every call, prints canned JSON.
printf '%s\\n' "$*" >> "$STUB_CALLS"
case "$1" in
  apps)
    if [ -n "${STUB_APP_URL:-}" ]; then
      printf '{"name": "appmux", "url": "%s"}\\n' "$STUB_APP_URL"
    else
      echo '{"name": "appmux"}'
    fi
    ;;
  auth)
    if [ -n "${STUB_TOKEN:-}" ]; then
      printf '{"access_token": "%s", "token_type": "Bearer"}\\n' "$STUB_TOKEN"
    else
      echo '{"token_type": "Bearer"}'
    fi
    ;;
  sync)
    (cd "$3" && find . -type f | sort) > "$STUB_SYNC_LISTING"
    echo "Initial Sync Complete for $4"
    ;;
esac
"""

STUB_UV = """#!/bin/bash
# Stands in for uv: records its arguments and the pytest environment, prints what a test run
# might print (a URL, a header, the folder), and exits with the status it was told to.
printf 'uv %s\\n' "$*" >> "$STUB_CALLS"
{
  for name in APPMUX_TEST_CONTAINER APPMUX_TEST_REMOTE APPMUX_TEST_BASE_URL \\
      APPMUX_TEST_FIXTURES_FOLDER APPMUX_TEST_USER_TOKEN; do
    printf '%s=%s\\n' "$name" "${!name-<unset>}"
  done
} > "$STUB_UV_ENV"
printf 'GET %s/_appmux/api/v1/me\\n' "$APPMUX_TEST_BASE_URL"
printf 'Authorization: Bearer %s\\n' "$APPMUX_TEST_USER_TOKEN"
printf 'fixtures in %s\\n' "$APPMUX_TEST_FIXTURES_FOLDER"
exit "${STUB_UV_STATUS:-0}"
"""


@dataclass(frozen=True)
class Stubs:
    environment: dict[str, str]
    calls: Path
    sync_listing: Path
    uv_environment: Path

    def call_lines(self) -> list[str]:
        return self.calls.read_text().splitlines()

    def pytest_environment(self) -> dict[str, str]:
        pairs = (line.split("=", 1) for line in self.uv_environment.read_text().splitlines())
        return dict(pairs)


def write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)


@pytest.fixture
def stubs(tmp_path: Path) -> Stubs:
    stub_dir = tmp_path / "stub-bin"
    write_executable(stub_dir / "databricks", STUB_CLI)
    write_executable(stub_dir / "uv", STUB_UV)
    calls = tmp_path / "calls.txt"
    listing = tmp_path / "sync-listing.txt"
    uv_environment = tmp_path / "uv-environment.txt"
    environment = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "STUB_CALLS": str(calls),
        "STUB_SYNC_LISTING": str(listing),
        "STUB_UV_ENV": str(uv_environment),
        "STUB_APP_URL": APP_URL,
        "STUB_TOKEN": FAKE_TOKEN,
        "APPMUX_DATABRICKS_PROFILE": PROFILE,
        "APPMUX_WORKSPACE_FOLDER": WORKSPACE_FOLDER,
    }
    return Stubs(
        environment=environment, calls=calls, sync_listing=listing, uv_environment=uv_environment
    )


def run_script(stubs: Stubs, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={**stubs.environment, **overrides},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def without(stubs: Stubs, name: str) -> dict[str, str]:
    return {key: value for key, value in stubs.environment.items() if key != name}


# --- what must be set, and what stops the run --------------------------------------------------


@pytest.mark.parametrize("unset", ["APPMUX_DATABRICKS_PROFILE", "APPMUX_WORKSPACE_FOLDER"])
def test_an_unset_variable_stops_the_run_before_the_cli_is_called(stubs: Stubs, unset: str) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=without(stubs, unset),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode != 0
    assert unset in result.stderr
    assert not stubs.calls.exists(), "the CLI must not be called while a variable is unset"


def test_a_fixtures_folder_outside_the_workspace_is_refused(stubs: Stubs) -> None:
    result = run_script(stubs, APPMUX_FIXTURES_FOLDER="/home/someone/fixtures")

    assert result.returncode != 0
    assert "APPMUX_FIXTURES_FOLDER" in result.stderr
    assert not stubs.calls.exists()


@pytest.mark.parametrize(
    ("missing", "named"),
    [("STUB_APP_URL", "address"), ("STUB_TOKEN", "token")],
)
def test_a_piece_the_cli_does_not_give_stops_the_run_before_pytest_starts(
    stubs: Stubs, missing: str, named: str
) -> None:
    result = run_script(stubs, **{missing: ""})

    assert result.returncode != 0
    assert named in result.stderr
    assert not any(line.startswith("uv ") for line in stubs.call_lines())
    assert FAKE_TOKEN not in result.stdout + result.stderr


# --- what pytest is given ----------------------------------------------------------------------


def test_pytest_gets_the_remote_environment_the_cli_gave(stubs: Stubs) -> None:
    result = run_script(stubs)

    assert result.returncode == 0, result.stderr
    assert stubs.pytest_environment() == {
        "APPMUX_TEST_CONTAINER": "1",
        "APPMUX_TEST_REMOTE": "1",
        "APPMUX_TEST_BASE_URL": APP_URL,
        "APPMUX_TEST_FIXTURES_FOLDER": FIXTURES_FOLDER,
        "APPMUX_TEST_USER_TOKEN": FAKE_TOKEN,
    }


def test_the_fixtures_folder_can_be_chosen(stubs: Stubs) -> None:
    other = "/Workspace/Users/user@example.com/other-fixtures"

    result = run_script(stubs, APPMUX_FIXTURES_FOLDER=other)

    assert result.returncode == 0, result.stderr
    assert stubs.pytest_environment()["APPMUX_TEST_FIXTURES_FOLDER"] == other


def test_the_whole_integration_folder_runs_by_default_with_output_shown(stubs: Stubs) -> None:
    run_script(stubs)

    assert stubs.call_lines()[-1] == "uv run pytest tests/integration -s -ra"


def test_the_callers_pytest_arguments_and_exit_status_are_passed_through(stubs: Stubs) -> None:
    result = run_script(
        stubs, "tests/integration/test_zero_downtime.py", "-k", "update", STUB_UV_STATUS="3"
    )

    assert result.returncode == 3
    assert stubs.call_lines()[-1] == (
        "uv run pytest tests/integration/test_zero_downtime.py -k update"
    )


# --- the fixtures ------------------------------------------------------------------------------


def test_the_fixtures_are_uploaded_with_their_wheels_built_and_before_pytest_starts(
    stubs: Stubs,
) -> None:
    result = run_script(stubs)

    assert result.returncode == 0, result.stderr
    calls = stubs.call_lines()
    sync_index = next(index for index, line in enumerate(calls) if line.startswith("sync "))
    uv_index = next(index for index, line in enumerate(calls) if line.startswith("uv "))
    assert sync_index < uv_index
    assert calls[sync_index].split()[-3:] == [FIXTURES_FOLDER, "--profile", PROFILE]
    uploaded = {line.removeprefix("./") for line in stubs.sync_listing.read_text().splitlines()}
    assert "light_fastapi/app.py" in uploaded
    for fixture in ("wheels_only", "wheel_reuse", "env_covered", "newer_wheel"):
        assert any(
            name.startswith(f"{fixture}/wheels/") and name.endswith(".whl") for name in uploaded
        ), f"{fixture} must be uploaded with its wheels built"
    assert not any("__pycache__" in name for name in uploaded)


def test_the_repository_gets_no_wheel_from_a_run(stubs: Stubs) -> None:
    fixtures = SCRIPT.parents[1] / "tests" / "fixtures"

    run_script(stubs)

    assert not list(fixtures.rglob("*.whl")), "wheels are built in a copy, never in the repository"


# --- nothing sensitive is shown ----------------------------------------------------------------


def test_the_token_the_address_and_the_folder_are_never_printed(stubs: Stubs) -> None:
    result = run_script(stubs)

    shown = result.stdout + result.stderr
    assert FAKE_TOKEN not in shown
    assert APP_URL not in shown
    assert "appmux-0000" not in shown
    assert WORKSPACE_FOLDER not in shown
    assert FIXTURES_FOLDER not in shown
    assert "user@example.com" not in shown
    assert "<token>" in shown, "pytest's own output must have passed through the masking"


def test_the_token_is_on_no_command_line(stubs: Stubs) -> None:
    run_script(stubs)

    assert FAKE_TOKEN not in stubs.calls.read_text()
