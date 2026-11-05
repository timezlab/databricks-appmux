"""``scripts/run-container-tests.sh``: the container tests, run inside the development App.

``run-remote-tests.sh`` plays the tests from the developer's machine through the platform's proxy.
Some tests cannot be played that way (500 requests a second need the edge's own port, the edge's CPU
needs its process id, the sandbox checks need the sandbox), so this script puts them in the App: it
deploys the code with the tests laid over it, waits for the closing line in the App's log, prints
what pytest printed and deploys the normal code again. What can go wrong here is not in the tests
but around them: an App left running the test entry with the user's token in its ``app.yaml``,
a verdict taken from another run's log, a hung run that never ends. Those are checked against a stub
CLI and stub deploy and log scripts; the real run is the proof of the rest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_SCRIPTS = REPO_ROOT / "scripts"

WORKSPACE_FOLDER = "/Workspace/Users/user@example.com/appmux-deploy"
PROFILE = "example-profile"
ADMIN = "admin@example.com"
# Fake secret-shaped text, built at run time so no such literal sits in the source.
FAKE_TOKEN = "Zq7" + "x" * 15 + "9"

STUB_CLI = """#!/bin/bash
# Stands in for the Databricks CLI: records every call, prints canned JSON.
printf 'cli %s\\n' "$*" >> "$STUB_CALLS"
case "$1" in
  auth)
    printf '{"access_token": "%s", "token_type": "Bearer"}\\n' "$STUB_TOKEN"
    ;;
  apps)
    # apps get NAME: the two states a run needs to explain a log nobody can read
    if [ -n "${STUB_APP_STOPPED:-}" ]; then
      echo '{"app_status": {"state": "UNAVAILABLE", "message": "App status is unavailable."},'
      echo ' "compute_status": {"state": "STOPPED", "message": "App compute was stopped."}}'
    else
      echo '{"app_status": {"state": "RUNNING", "message": "App is running."},'
      echo ' "compute_status": {"state": "ACTIVE", "message": "App compute is running."}}'
    fi
    ;;
  workspace)
    case "$2" in
      delete)
        # workspace delete --recursive PATH: the name leaves the folder, unless it was never there
        name=$(basename "$4")
        if ! grep -qx -- "$name" "$STUB_WORKSPACE_FILES"; then
          echo "RESOURCE_DOES_NOT_EXIST" >&2
          exit 1
        fi
        grep -vx -- "$name" "$STUB_WORKSPACE_FILES" > "$STUB_WORKSPACE_FILES.new"
        mv "$STUB_WORKSPACE_FILES.new" "$STUB_WORKSPACE_FILES"
        ;;
      *)
        # workspace list: the folder as a JSON array; STUB_LEFTOVER makes the entry file stay
        # even after a delete (a delete that did not take effect)
        {
          cat "$STUB_WORKSPACE_FILES"
          [ -n "${STUB_LEFTOVER:-}" ] && echo container_test_entry.py
        } | sort -u | python3 -c '
import json, sys
print(json.dumps([{"path": "/x/" + line.strip()} for line in sys.stdin if line.strip()]))'
        ;;
    esac
    ;;
  sync)
    echo "Initial Sync Complete"
    ;;
esac
"""

# Stands in for uv: `uv run python <script> ...` runs the interpreter of the test run, which has
# pytest (the overlay builder looks for it).
STUB_UV = """#!/bin/bash
printf 'uv %s\\n' "$1 $2 ..." >> "$STUB_CALLS"
shift 2
exec "$STUB_PYTHON" "$@"
"""

STUB_SYNC_DEPLOY = """#!/bin/bash
# Records whether it was given an overlay and what the overlay held; fails when told to.
if [ -n "${APPMUX_DEPLOY_OVERLAY:-}" ]; then
  printf 'sync-deploy with overlay\\n' >> "$STUB_CALLS"
  cp "$APPMUX_DEPLOY_OVERLAY/container-test-run.json" "$STUB_RUN_CONFIG"
  if grep -q "$STUB_TOKEN" "$APPMUX_DEPLOY_OVERLAY/app.yaml"; then
    echo yes > "$STUB_TOKEN_IN_APP_YAML"
  fi
  echo "$APPMUX_DEPLOY_OVERLAY" > "$STUB_OVERLAY_PATH"
  [ "${STUB_FAIL_ON:-}" = overlay-early ] && { echo "sync FAILED" >&2; exit 1; }
  ls -A "$APPMUX_DEPLOY_OVERLAY" | tee "$STUB_OVERLAY_NAMES" >> "$STUB_WORKSPACE_FILES"
  [ "${STUB_FAIL_ON:-}" = overlay ] && { echo "deployment FAILED" >&2; exit 1; }
else
  printf 'sync-deploy without overlay\\n' >> "$STUB_CALLS"
  [ "${STUB_FAIL_ON:-}" = restore ] && { echo "deployment FAILED" >&2; exit 1; }
fi
echo "deployment succeeded"
"""

STUB_FETCH_LOGS = """#!/bin/bash
# The App's log: some noise, the pytest output and (unless told otherwise) the run's closing line.
printf 'fetch-logs %s\\n' "$*" >> "$STUB_CALLS"
# STUB_LOG_HANGS: the read never returns (`databricks apps logs --tail-lines 0` did that for two
# hours on 2026-11-05, with the closing line already in the log).
if [ -n "${STUB_LOG_HANGS:-}" ]; then
  exec sleep 1000
fi
# STUB_LOG_FAILS=all: no read works (the App is down); STUB_LOG_FAILS=N: the first N reads fail.
if [ "${STUB_LOG_FAILS:-}" = all ]; then
  echo "fetch-logs.sh: databricks apps logs failed" >&2
  exit 1
fi
reads=$(grep -c '^fetch-logs' "$STUB_CALLS")
if [ -n "${STUB_LOG_FAILS:-}" ] && [ "$reads" -le "$STUB_LOG_FAILS" ]; then
  echo "fetch-logs.sh: databricks apps logs failed" >&2
  exit 1
fi
echo "2026-11-04 appmux started"
echo "another run: APPMUX-CONTAINER-TESTS-DONE run=ffffffffffffffff exit=0"
if [ -z "${STUB_NO_DONE:-}" ]; then
  run_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' \\
    "$STUB_RUN_CONFIG")
  echo "appmux answers; running pytest inside the container"
  echo "tests/integration/test_a.py::test_one PASSED  Authorization: Bearer $STUB_TOKEN"
  echo "APPMUX-CONTAINER-TESTS-DONE run=$run_id ${STUB_DONE_TAIL:-exit=0}"
fi
"""


@dataclass(frozen=True)
class Stubs:
    environment: dict[str, str]
    calls: Path
    run_config: Path
    token_in_app_yaml: Path
    overlay_path: Path

    def call_lines(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


def write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)


@pytest.fixture
def stubs(tmp_path: Path) -> Stubs:
    scripts = tmp_path / "scripts"
    shutil.copytree(REAL_SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    write_executable(scripts / "sync-deploy.sh", STUB_SYNC_DEPLOY)
    write_executable(scripts / "fetch-logs.sh", STUB_FETCH_LOGS)
    stub_dir = tmp_path / "stub-bin"
    write_executable(stub_dir / "databricks", STUB_CLI)
    write_executable(stub_dir / "uv", STUB_UV)
    calls = tmp_path / "calls.txt"
    run_config = tmp_path / "run-config.json"
    token_in_app_yaml = tmp_path / "token-in-app-yaml.txt"
    overlay_path = tmp_path / "overlay-path.txt"
    workspace_files = tmp_path / "workspace-files.txt"
    workspace_files.write_text("app.yaml\n", encoding="utf-8")  # what the normal sync leaves there
    environment = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "STUB_CALLS": str(calls),
        "STUB_TOKEN": FAKE_TOKEN,
        "STUB_PYTHON": sys.executable,
        "STUB_RUN_CONFIG": str(run_config),
        "STUB_TOKEN_IN_APP_YAML": str(token_in_app_yaml),
        "STUB_OVERLAY_PATH": str(overlay_path),
        "STUB_OVERLAY_NAMES": str(tmp_path / "overlay-names.txt"),
        "STUB_WORKSPACE_FILES": str(workspace_files),
        "APPMUX_REPO_ROOT": str(REPO_ROOT),
        "APPMUX_DATABRICKS_PROFILE": PROFILE,
        "APPMUX_WORKSPACE_FOLDER": WORKSPACE_FOLDER,
        "APPMUX_DEPLOY_BOOTSTRAP_ADMIN": ADMIN,
        "APPMUX_CONTAINER_TESTS_POLL_S": "1",
        "APPMUX_CONTAINER_TESTS_TIMEOUT_S": "1",
    }
    return Stubs(environment, calls, run_config, token_in_app_yaml, overlay_path)


def run_script(stubs: Stubs, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    script = Path(stubs.calls).parent / "scripts" / "run-container-tests.sh"
    return subprocess.run(
        ["bash", str(script), *args],
        env={**stubs.environment, **overrides},
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def deploys(stubs: Stubs) -> list[str]:
    return [line for line in stubs.call_lines() if line.startswith("sync-deploy")]


# --- what must be set, and what stops the run --------------------------------------------------


@pytest.mark.parametrize(
    "unset",
    ["APPMUX_DATABRICKS_PROFILE", "APPMUX_WORKSPACE_FOLDER", "APPMUX_DEPLOY_BOOTSTRAP_ADMIN"],
)
def test_an_unset_variable_stops_the_run_before_anything_is_deployed(
    stubs: Stubs, unset: str
) -> None:
    stubs.environment.pop(unset)

    result = run_script(stubs)

    assert result.returncode != 0
    assert unset in result.stderr
    assert not stubs.call_lines(), "no CLI call, no deployment"


# --- the outcomes ------------------------------------------------------------------------------


def test_a_passing_run_exits_zero_and_deploys_the_code_again_without_the_overlay(
    stubs: Stubs,
) -> None:
    result = run_script(stubs, "tests/integration/test_a.py", "-s")

    assert result.returncode == 0, result.stderr
    assert deploys(stubs) == ["sync-deploy with overlay", "sync-deploy without overlay"]


def test_a_failing_run_exits_one_and_still_restores(stubs: Stubs) -> None:
    result = run_script(stubs, "tests/integration/test_a.py", STUB_DONE_TAIL="exit=1")

    assert result.returncode == 1
    assert deploys(stubs)[-1] == "sync-deploy without overlay"


def test_a_run_whose_pytest_never_started_exits_seventy_and_says_why(stubs: Stubs) -> None:
    result = run_script(
        stubs,
        "tests/integration/test_a.py",
        STUB_DONE_TAIL="pytest did not run: appmux did not answer",
    )

    assert result.returncode == 70
    assert "appmux did not answer" in result.stdout
    assert deploys(stubs)[-1] == "sync-deploy without overlay"


def test_a_run_whose_closing_line_never_comes_gives_up_and_restores(stubs: Stubs) -> None:
    result = run_script(stubs, "tests/integration/test_a.py", STUB_NO_DONE="1")

    assert result.returncode == 3
    assert "gave up" in result.stderr
    assert deploys(stubs)[-1] == "sync-deploy without overlay"


def test_a_log_nobody_can_read_ends_the_wait_early_and_says_what_state_the_app_is_in(
    stubs: Stubs,
) -> None:
    # The second run on 2026-11-04 waited 2400 s for a log that could not be read because the
    # platform had stopped the App's compute; a failed read was taken for "not there yet".
    result = run_script(
        stubs,
        "tests/integration/test_a.py",
        STUB_LOG_FAILS="all",
        STUB_APP_STOPPED="1",
        APPMUX_CONTAINER_TESTS_TIMEOUT_S="1000",
        APPMUX_CONTAINER_TESTS_MAX_LOG_FAILURES="3",
    )

    assert result.returncode == 3
    assert "could not be read 3 times in a row" in result.stderr
    assert "App compute was stopped" in result.stdout + result.stderr
    assert deploys(stubs)[-1] == "sync-deploy without overlay"


def test_a_read_of_the_log_that_hangs_is_cut_off_and_counts_as_a_failed_read(
    stubs: Stubs,
) -> None:
    result = run_script(
        stubs,
        "tests/integration/test_a.py",
        STUB_LOG_HANGS="1",
        APPMUX_CONTAINER_TESTS_TIMEOUT_S="1000",
        APPMUX_CONTAINER_TESTS_READ_TIMEOUT_S="1",
        APPMUX_CONTAINER_TESTS_MAX_LOG_FAILURES="2",
    )

    assert result.returncode == 3
    assert "could not be read 2 times in a row" in result.stderr
    assert deploys(stubs)[-1] == "sync-deploy without overlay"


def test_the_log_is_read_with_a_limit_on_the_lines_never_all_of_it(stubs: Stubs) -> None:
    # `--tail-lines 0` asks for everything and is the call that hung.
    result = run_script(stubs, "tests/integration/test_a.py")

    assert result.returncode == 0, result.stderr
    reads = [line for line in stubs.call_lines() if line.startswith("fetch-logs")]
    assert reads
    for read in reads:
        limit = read.split("--tail-lines ")[1].split()[0]
        assert int(limit) > 0, read


def test_a_few_failed_reads_before_the_log_comes_do_not_end_the_wait(stubs: Stubs) -> None:
    # The log is not there while the App starts: failures short of the limit are patience.
    result = run_script(
        stubs,
        "tests/integration/test_a.py",
        STUB_LOG_FAILS="2",
        APPMUX_CONTAINER_TESTS_TIMEOUT_S="1000",
        APPMUX_CONTAINER_TESTS_MAX_LOG_FAILURES="3",
    )

    assert result.returncode == 0, result.stderr


def test_giving_up_on_a_closing_line_that_never_comes_says_what_state_the_app_is_in(
    stubs: Stubs,
) -> None:
    result = run_script(
        stubs, "tests/integration/test_a.py", STUB_NO_DONE="1", STUB_APP_STOPPED="1"
    )

    assert result.returncode == 3
    assert "App compute was stopped" in result.stdout + result.stderr


def test_a_failed_restore_says_what_state_the_app_is_in(stubs: Stubs) -> None:
    result = run_script(
        stubs, "tests/integration/test_a.py", STUB_FAIL_ON="restore", STUB_APP_STOPPED="1"
    )

    assert result.returncode == 4
    assert "App compute was stopped" in result.stderr


def test_the_closing_line_of_another_run_is_not_taken_for_this_one(stubs: Stubs) -> None:
    # The stub's log always holds a passing line of run ffffffffffffffff; without its own line
    # this run must still be waiting, never "passed".
    result = run_script(stubs, "tests/integration/test_a.py", STUB_NO_DONE="1")

    assert "the tests passed" not in result.stdout
    assert result.returncode != 0


def test_a_deployment_that_fails_is_restored_because_part_of_it_may_have_reached_the_app(
    stubs: Stubs,
) -> None:
    result = run_script(stubs, "tests/integration/test_a.py", STUB_FAIL_ON="overlay")

    assert result.returncode != 0
    assert deploys(stubs) == ["sync-deploy with overlay", "sync-deploy without overlay"]


def test_a_restore_that_fails_is_loud_and_exits_four_even_when_the_tests_passed(
    stubs: Stubs,
) -> None:
    result = run_script(stubs, "tests/integration/test_a.py", STUB_FAIL_ON="restore")

    assert result.returncode == 4
    assert "token" in result.stderr
    assert "scripts/sync-deploy.sh" in result.stderr


def test_a_test_file_left_in_the_workspace_after_the_restore_is_loud(stubs: Stubs) -> None:
    result = run_script(stubs, "tests/integration/test_a.py", STUB_LEFTOVER="1")

    assert result.returncode == 4
    assert "container_test_entry.py" in result.stderr


def test_the_files_of_the_overlay_are_taken_out_of_the_workspace_before_the_normal_code_is_synced(
    stubs: Stubs,
) -> None:
    """``databricks sync`` from a fresh folder only uploads: nothing it no longer has is deleted,
    so the first container run left the entry, the tests and the libraries in the folder and the
    restore deployment carried them back into the App."""
    result = run_script(stubs, "tests/integration/test_a.py")

    assert result.returncode == 0, result.stderr
    calls = stubs.call_lines()
    deleted = [
        next(word for word in line.split() if word.startswith(WORKSPACE_FOLDER))
        for line in calls
        if line.startswith("cli workspace delete")
    ]
    overlay_names = set((stubs.overlay_path.parent / "overlay-names.txt").read_text().split())
    assert overlay_names > {"container_test_entry.py", "tests", "pytest.ini"}, overlay_names
    # Everything the overlay put there goes, except the app.yaml the normal sync replaces.
    assert {path.removeprefix(f"{WORKSPACE_FOLDER}/") for path in deleted} == overlay_names - {
        "app.yaml"
    }
    overlay_sync = calls.index("sync-deploy with overlay")
    normal_sync = calls.index("sync-deploy without overlay")
    first_delete = next(
        i for i, line in enumerate(calls) if line.startswith("cli workspace delete")
    )
    assert overlay_sync < first_delete < normal_sync


def test_the_restore_deletes_only_what_the_overlay_put_in_the_folder(stubs: Stubs) -> None:
    workspace_files = Path(stubs.environment["STUB_WORKSPACE_FILES"])
    workspace_files.write_text("app.yaml\nfixtures\nREADME.md\n", encoding="utf-8")

    run_script(stubs, "tests/integration/test_a.py")

    deleted = [line for line in stubs.call_lines() if line.startswith("cli workspace delete")]
    assert deleted
    assert not any(
        "fixtures" in line or "README.md" in line or "app.yaml" in line for line in deleted
    )


def test_a_run_whose_deployment_failed_before_the_overlay_arrived_still_restores(
    stubs: Stubs,
) -> None:
    """The overlay's files may not be in the folder at all: nothing is deleted, nothing fails."""
    result = run_script(stubs, "tests/integration/test_a.py", STUB_FAIL_ON="overlay-early")

    assert "sync-deploy without overlay" in stubs.call_lines()
    assert not any(line.startswith("cli workspace delete") for line in stubs.call_lines())
    assert result.returncode == 1, result.stderr


# --- what goes to the App ----------------------------------------------------------------------


def test_the_overlay_carries_the_token_in_app_yaml_and_is_gone_after_the_run(stubs: Stubs) -> None:
    run_script(stubs, "tests/integration/test_a.py")

    assert stubs.token_in_app_yaml.read_text().strip() == "yes"
    assert not Path(stubs.overlay_path.read_text().strip()).exists()


def test_the_pytest_arguments_of_the_caller_are_the_run_configuration(stubs: Stubs) -> None:
    run_script(stubs, "tests/integration/test_a.py", "-s", "-k", "one")

    config = json.loads(stubs.run_config.read_text())
    assert config["pytest_arguments"] == ["tests/integration/test_a.py", "-s", "-k", "one"]


def test_without_arguments_the_tests_that_need_no_preparation_run(stubs: Stubs) -> None:
    run_script(stubs)

    config = json.loads(stubs.run_config.read_text())
    assert "tests/integration/test_edge_capacity.py" in config["pytest_arguments"]


def test_the_fixtures_are_uploaded_before_the_overlay_is_deployed(stubs: Stubs) -> None:
    run_script(stubs, "tests/integration/test_a.py")

    calls = stubs.call_lines()
    sync_index = next(i for i, line in enumerate(calls) if line.startswith("cli sync "))
    deploy_index = next(i for i, line in enumerate(calls) if line == "sync-deploy with overlay")
    assert sync_index < deploy_index


# --- the log -----------------------------------------------------------------------------------


def test_what_pytest_printed_is_shown_after_the_run(stubs: Stubs) -> None:
    result = run_script(stubs, "tests/integration/test_a.py")

    assert "test_one PASSED" in result.stdout


# --- nothing sensitive is shown ----------------------------------------------------------------


def test_the_token_the_admin_and_the_folder_are_never_printed(stubs: Stubs) -> None:
    result = run_script(stubs, "tests/integration/test_a.py")

    shown = result.stdout + result.stderr
    assert FAKE_TOKEN not in shown
    assert ADMIN not in shown
    assert WORKSPACE_FOLDER not in shown
    assert "user@example.com" not in shown
    assert "<token>" in shown, "the App's log must have passed through the masking"


def test_the_token_is_on_no_command_line(stubs: Stubs) -> None:
    run_script(stubs, "tests/integration/test_a.py")

    assert FAKE_TOKEN not in stubs.calls.read_text()
