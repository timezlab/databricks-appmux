"""The entry point of a test run inside the container (``scripts/container_test_entry.py``).

A container test run replaces the App's command with this entry: it starts appmux as a child
process, waits until it answers, runs pytest against it on the container's own port and prints one
closing line with pytest's exit code. Almost all of it is process plumbing that only a container
tests. What can fail silently without one is checked here, as pure functions:

* the run configuration is read from a file the orchestrating script wrote, and a file that does
  not say what to run is refused (a run that selects no test must not read as a pass);
* the environment pytest gets names the admin, the port and the edge process, and a missing
  variable stops the run with its name: a test that falls back to a default would act as nobody;
* appmux itself never receives the test variables, because the user's token is one of them;
* the closing line that the orchestrating script looks for holds the exit code, so "tests failed"
  and "tests never ran" are different lines, and the reader of the log (``find_verdict``, run on the
  developer's machine) tells them apart, ignores the lines of another run, and says "still waiting"
  rather than "passed" when the line has not been printed yet.

The module is loaded by path because ``scripts/`` is not a package.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

MODULE = Path(__file__).resolve().parents[2] / "scripts" / "container_test_entry.py"
# Made up, and built at run time so no secret-shaped literal sits in the source.
FAKE_TOKEN = "tok" + "x" * 12
LIBS = Path("/srv/deploy/testlibs")
RUN_ID = "0123456789abcdef"


@pytest.fixture(scope="module")
def entry() -> ModuleType:
    spec = importlib.util.spec_from_file_location("container_test_entry_under_test", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["container_test_entry_under_test"] = module
    spec.loader.exec_module(module)
    return module


def container_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "DATABRICKS_APP_PORT": "8000",
        "APPMUX_BOOTSTRAP_ADMINS": "Admin@Example.test",
        "APPMUX_TEST_USER_TOKEN": FAKE_TOKEN,
        "APPMUX_TEST_FIXTURES_FOLDER": "/Workspace/Users/admin@example.test/fixtures",
        "PATH": "/usr/bin",
    }
    environment.update(overrides)
    return environment


def write_config(directory: Path, document: object) -> Path:
    path = directory / "container-test-run.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# ---- the run configuration ---------------------------------------------------------------------


def test_the_configuration_holds_the_pytest_arguments_in_order(
    entry: ModuleType, tmp_path: Path
) -> None:
    path = write_config(
        tmp_path, {"run_id": RUN_ID, "pytest_arguments": ["tests/integration/test_a.py", "-s"]}
    )

    config = entry.load_run_config(path)

    assert config.pytest_arguments == ("tests/integration/test_a.py", "-s")
    assert config.run_id == RUN_ID


@pytest.mark.parametrize(
    "document",
    [
        {"run_id": RUN_ID},
        {"run_id": RUN_ID, "pytest_arguments": []},
        {"run_id": RUN_ID, "pytest_arguments": "tests"},
        {"run_id": RUN_ID, "pytest_arguments": ["tests", 3]},
    ],
)
def test_a_configuration_that_selects_nothing_to_run_is_refused(
    entry: ModuleType, tmp_path: Path, document: object
) -> None:
    with pytest.raises(entry.RunConfigError, match="pytest_arguments"):
        entry.load_run_config(write_config(tmp_path, document))


@pytest.mark.parametrize("run_id", [None, "", "not hex!", "ab", "x" * 40])
def test_a_configuration_without_a_usable_run_id_is_refused(
    entry: ModuleType, tmp_path: Path, run_id: object
) -> None:
    document: dict[str, object] = {"pytest_arguments": ["tests"]}
    if run_id is not None:
        document["run_id"] = run_id

    with pytest.raises(entry.RunConfigError, match="run_id"):
        entry.load_run_config(write_config(tmp_path, document))


def test_a_configuration_that_is_not_an_object_is_refused(
    entry: ModuleType, tmp_path: Path
) -> None:
    with pytest.raises(entry.RunConfigError, match="run_id"):
        entry.load_run_config(write_config(tmp_path, ["tests"]))


def test_a_missing_or_unreadable_configuration_is_refused(
    entry: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(entry.RunConfigError):
        entry.load_run_config(tmp_path / "absent.json")
    with pytest.raises(entry.RunConfigError):
        entry.load_run_config(tmp_path / "broken.json")


# ---- the environment of pytest -----------------------------------------------------------------


def test_pytest_is_told_where_the_edge_is_who_the_admin_is_and_which_process_to_read(
    entry: ModuleType,
) -> None:
    environment = entry.environment_for_tests(
        container_environment(), port=8123, appmux_pid=4242, libs_dir=LIBS
    )

    assert environment["APPMUX_TEST_CONTAINER"] == "1"
    assert environment["APPMUX_TEST_BASE_URL"] == "http://127.0.0.1:8123"
    assert environment["APPMUX_TEST_ADMIN_EMAIL"] == "admin@example.test"
    assert environment["APPMUX_TEST_EDGE_PID"] == "4242"
    assert environment["APPMUX_TEST_USER_TOKEN"] == FAKE_TOKEN


def test_the_first_bootstrap_admin_is_the_admin_of_the_run(entry: ModuleType) -> None:
    environment = entry.environment_for_tests(
        container_environment(APPMUX_BOOTSTRAP_ADMINS="one@example.test, two@example.test"),
        port=8000,
        appmux_pid=1,
        libs_dir=LIBS,
    )

    assert environment["APPMUX_TEST_ADMIN_EMAIL"] == "one@example.test"


def test_a_remote_run_marker_in_the_environment_is_removed(entry: ModuleType) -> None:
    environment = entry.environment_for_tests(
        container_environment(APPMUX_TEST_REMOTE="1"), port=8000, appmux_pid=1, libs_dir=LIBS
    )

    assert "APPMUX_TEST_REMOTE" not in environment


def test_the_vendored_test_libraries_come_first_on_the_python_path(entry: ModuleType) -> None:
    environment = entry.environment_for_tests(
        container_environment(PYTHONPATH="/existing"), port=8000, appmux_pid=1, libs_dir=LIBS
    )

    assert environment["PYTHONPATH"].split(":") == [str(LIBS), "/existing"]


@pytest.mark.parametrize(
    "missing",
    [
        "APPMUX_BOOTSTRAP_ADMINS",
        "APPMUX_TEST_USER_TOKEN",
        "APPMUX_TEST_FIXTURES_FOLDER",
    ],
)
def test_a_missing_variable_stops_the_run_and_is_named(entry: ModuleType, missing: str) -> None:
    environment = container_environment()
    del environment[missing]

    with pytest.raises(entry.RunConfigError, match=missing):
        entry.environment_for_tests(environment, port=8000, appmux_pid=1, libs_dir=LIBS)


def test_an_empty_bootstrap_admin_list_is_a_missing_admin(entry: ModuleType) -> None:
    with pytest.raises(entry.RunConfigError, match="APPMUX_BOOTSTRAP_ADMINS"):
        entry.environment_for_tests(
            container_environment(APPMUX_BOOTSTRAP_ADMINS=" , "),
            port=8000,
            appmux_pid=1,
            libs_dir=LIBS,
        )


# ---- what appmux itself is given ---------------------------------------------------------------


def test_appmux_never_receives_the_test_variables_and_keeps_the_rest(entry: ModuleType) -> None:
    environment = container_environment(APPMUX_TEST_WAREHOUSE_ID="warehouse-1")

    service = entry.environment_for_service(environment)

    assert not [name for name in service if name.startswith("APPMUX_TEST_")]
    assert service["APPMUX_BOOTSTRAP_ADMINS"] == "Admin@Example.test"
    assert service["DATABRICKS_APP_PORT"] == "8000"


# ---- the closing line --------------------------------------------------------------------------


def test_the_closing_line_carries_the_exit_code(entry: ModuleType) -> None:
    assert entry.done_line(RUN_ID, 0) != entry.done_line(RUN_ID, 1)
    assert entry.done_line(RUN_ID, 5).endswith("exit=5")


def test_the_closing_line_names_the_run_so_an_earlier_log_cannot_be_mistaken_for_it(
    entry: ModuleType,
) -> None:
    assert entry.done_line(RUN_ID, 0).startswith(f"{entry.DONE_PREFIX} run={RUN_ID} ")
    assert entry.done_line("fedcba9876543210", 0) != entry.done_line(RUN_ID, 0)


def test_a_run_that_never_started_pytest_has_its_own_line(entry: ModuleType) -> None:
    line = entry.not_run_line(RUN_ID, "appmux did not answer within 300 seconds")

    assert line.startswith(f"{entry.DONE_PREFIX} run={RUN_ID} ")
    assert "exit=" not in line
    assert "appmux did not answer" in line


def test_the_pytest_command_uses_the_interpreter_of_the_run(entry: ModuleType) -> None:
    config = entry.RunConfig(run_id=RUN_ID, pytest_arguments=("tests/integration/test_a.py", "-s"))

    command = entry.pytest_command("/venv/bin/python", config)

    assert command == ["/venv/bin/python", "-m", "pytest", "tests/integration/test_a.py", "-s"]


# ---- reading the closing line from the App's log -----------------------------------------------

OTHER_RUN_ID = "fedcba9876543210"


def log_with(*lines: str) -> str:
    return "\n".join(["2026-11-04T06:00:00Z appmux started", *lines, "trailing noise"])


def test_a_log_without_the_closing_line_is_still_waiting(entry: ModuleType) -> None:
    verdict = entry.find_verdict(log_with("pytest says: 3 passed"), RUN_ID)

    assert verdict.kind == "pending"
    assert verdict.exit_code is None


def test_exit_code_zero_is_a_pass_and_any_other_is_a_failure(entry: ModuleType) -> None:
    passed = entry.find_verdict(log_with(entry.done_line(RUN_ID, 0)), RUN_ID)
    failed = entry.find_verdict(log_with(entry.done_line(RUN_ID, 1)), RUN_ID)

    assert (passed.kind, passed.exit_code) == ("passed", 0)
    assert (failed.kind, failed.exit_code) == ("failed", 1)


def test_pytest_exit_code_five_means_no_test_was_collected_and_is_not_a_pass(
    entry: ModuleType,
) -> None:
    verdict = entry.find_verdict(log_with(entry.done_line(RUN_ID, 5)), RUN_ID)

    assert verdict.kind == "failed"
    assert verdict.exit_code == 5


def test_a_run_that_never_started_pytest_is_not_a_failure_of_the_tests(entry: ModuleType) -> None:
    line = entry.not_run_line(RUN_ID, "appmux did not answer within 300 seconds")

    verdict = entry.find_verdict(log_with(line), RUN_ID)

    assert verdict.kind == "not_run"
    assert "appmux did not answer" in verdict.detail
    assert verdict.exit_code is None


def test_the_closing_line_of_another_run_is_ignored(entry: ModuleType) -> None:
    log = log_with(entry.done_line(OTHER_RUN_ID, 0))

    assert entry.find_verdict(log, RUN_ID).kind == "pending"


def test_a_line_the_platform_prefixed_is_still_found(entry: ModuleType) -> None:
    log = "[APP] 2026-11-04 06:01:02 " + entry.done_line(RUN_ID, 0)

    assert entry.find_verdict(log, RUN_ID).kind == "passed"


def test_the_last_closing_line_of_the_run_counts(entry: ModuleType) -> None:
    log = log_with(entry.not_run_line(RUN_ID, "early"), entry.done_line(RUN_ID, 0))

    assert entry.find_verdict(log, RUN_ID).kind == "passed"


def test_a_run_id_that_is_not_hex_is_refused_rather_than_used_as_a_pattern(
    entry: ModuleType,
) -> None:
    with pytest.raises(entry.RunConfigError, match="run_id"):
        entry.find_verdict("anything", ".*")
