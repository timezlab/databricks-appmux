"""The App's command during a test run inside the container (``scripts/run-container-tests.sh``).

A normal deployment runs ``python -m appmux``. A test deployment (a synced copy with the tests
laid over it; the repository, ``build/deploy`` and a release zip never hold this file) runs this
entry instead. It

1. starts appmux as a child process, exactly as the platform would, without any ``APPMUX_TEST_*``
   variable (the user's token is one of them);
2. waits until appmux answers on the container's own port;
3. runs pytest against it, from inside the container, so the tests can play the Databricks proxy
   with ``X-Forwarded-Email`` and read the edge process's CPU;
4. prints one closing line (``done_line``) that the orchestrating script looks for in the App's log;
5. keeps appmux running until the platform stops it, so the App stays healthy while the log is read.

Everything is printed to standard output, which the platform keeps as the App's log. Nothing here
prints the token, the admin or the folder: they only travel in the environment.

The pure parts (reading the configuration, building the two environments, the closing lines) are
checked by ``tests/unit/test_container_test_entry.py``; the process handling is container-only.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

DEPLOY_DIR: Final = Path(__file__).resolve().parent
RUN_CONFIG_FILE: Final = "container-test-run.json"
LIBS_DIRECTORY: Final = "testlibs"
DONE_PREFIX: Final = "APPMUX-CONTAINER-TESTS-DONE"
TEST_VARIABLE_PREFIX: Final = "APPMUX_TEST_"
READY_TIMEOUT_S: Final = 300.0
READY_POLL_S: Final = 2.0
READY_REQUEST_TIMEOUT_S: Final = 5.0
STOP_GRACE_S: Final = 30.0
API_ME_PATH: Final = "/_appmux/api/v1/me"
NOT_RUN_EXIT_CODE: Final = 70
# The orchestrating script makes a fresh id for every run and looks for it in the closing line, so
# the log of an earlier run on the same App cannot be taken for this one.
RUN_ID_PATTERN: Final = re.compile(r"[0-9a-f]{8,32}")


class RunConfigError(Exception):
    """The run cannot start as configured; the message names what is missing and never a value."""


@dataclass(frozen=True, slots=True)
class RunConfig:
    run_id: str
    pytest_arguments: tuple[str, ...]


def load_run_config(path: Path) -> RunConfig:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunConfigError(f"the run configuration {path.name} cannot be read") from exc
    fields: dict[str, object] = document if isinstance(document, dict) else {}
    run_id = fields.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise RunConfigError(f"{path.name} must hold run_id: 8 to 32 lower-case hex digits")
    arguments = fields.get("pytest_arguments")
    if (
        not isinstance(arguments, list)
        or not arguments
        or not all(isinstance(argument, str) for argument in arguments)
    ):
        raise RunConfigError(
            f"{path.name} must hold pytest_arguments: a non-empty list of strings "
            "(a run that selects no test would read as a pass)"
        )
    return RunConfig(run_id=run_id, pytest_arguments=tuple(arguments))


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise RunConfigError(f"{name} is not set in the App's environment")
    return value


def environment_for_tests(
    environment: Mapping[str, str], *, port: int, appmux_pid: int, libs_dir: Path
) -> dict[str, str]:
    """What pytest is given: the App's environment plus the facts of this container."""
    administrators = [
        address.strip().lower()
        for address in _required(environment, "APPMUX_BOOTSTRAP_ADMINS").split(",")
        if address.strip()
    ]
    if not administrators:
        raise RunConfigError("APPMUX_BOOTSTRAP_ADMINS names no administrator")
    _required(environment, "APPMUX_TEST_USER_TOKEN")
    _required(environment, "APPMUX_TEST_FIXTURES_FOLDER")
    result = {name: value for name, value in environment.items() if name != "APPMUX_TEST_REMOTE"}
    result.update(
        {
            "APPMUX_TEST_CONTAINER": "1",
            "APPMUX_TEST_BASE_URL": f"http://127.0.0.1:{port}",
            "APPMUX_TEST_ADMIN_EMAIL": administrators[0],
            "APPMUX_TEST_EDGE_PID": str(appmux_pid),
        }
    )
    existing = environment.get("PYTHONPATH", "")
    result["PYTHONPATH"] = f"{libs_dir}:{existing}" if existing else str(libs_dir)
    return result


def environment_for_service(environment: Mapping[str, str]) -> dict[str, str]:
    """What appmux is given: the App's environment without any test variable."""
    return {
        name: value
        for name, value in environment.items()
        if not name.startswith(TEST_VARIABLE_PREFIX)
    }


def pytest_command(python: str, config: RunConfig) -> list[str]:
    return [python, "-m", "pytest", *config.pytest_arguments]


def done_line(run_id: str, exit_code: int) -> str:
    return f"{DONE_PREFIX} run={run_id} exit={exit_code}"


def not_run_line(run_id: str, reason: str) -> str:
    return f"{DONE_PREFIX} run={run_id} pytest did not run: {reason}"


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the App's log says about one run (read on the developer's machine)."""

    kind: Literal["pending", "passed", "failed", "not_run"]
    exit_code: int | None = None
    detail: str = ""


def find_verdict(log_text: str, run_id: str) -> Verdict:
    """The outcome of run ``run_id`` from the App's log, or ``pending`` while its line is absent.

    Only the closing line of this run counts (an earlier run on the same App leaves its own in the
    log), the last one wins, and any exit code but 0 is a failure: pytest's 5 ("no test collected")
    must not read as a pass.
    """
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise RunConfigError("run_id must be 8 to 32 lower-case hex digits")
    closing = re.compile(
        rf"{re.escape(DONE_PREFIX)} run={run_id} "
        r"(?:exit=(?P<code>\d+)|pytest did not run: (?P<why>.*))"
    )
    verdict = Verdict(kind="pending")
    for match in closing.finditer(log_text):
        if match["code"] is not None:
            code = int(match["code"])
            verdict = Verdict(kind="passed" if code == 0 else "failed", exit_code=code)
        else:
            verdict = Verdict(kind="not_run", detail=match["why"].strip())
    return verdict


# ---- container only: processes and waiting ----------------------------------------------------


def _appmux_answers(port: int, admin: str) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{API_ME_PATH}", headers={"X-Forwarded-Email": admin}
    )
    try:
        with urllib.request.urlopen(request, timeout=READY_REQUEST_TIMEOUT_S) as response:  # noqa: S310
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def _wait_until_ready(service: subprocess.Popen[bytes], port: int, admin: str) -> str | None:
    """``None`` when appmux answers; else the reason it never did."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if service.poll() is not None:
            return f"appmux exited with status {service.returncode} before it answered"
        if _appmux_answers(port, admin):
            return None
        time.sleep(READY_POLL_S)
    return f"appmux did not answer within {READY_TIMEOUT_S:g} seconds"


def _stop(service: subprocess.Popen[bytes]) -> None:
    if service.poll() is None:
        service.terminate()
        try:
            service.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            service.kill()


def main() -> int:
    environment = dict(os.environ)
    try:
        config = load_run_config(DEPLOY_DIR / RUN_CONFIG_FILE)
        port = int(_required(environment, "DATABRICKS_APP_PORT"))
    except (RunConfigError, ValueError) as exc:
        # Without a readable run id the orchestrating script cannot match a closing line, so this
        # one is only for a person reading the log; the script's own timeout ends the wait.
        print(f"{DONE_PREFIX} run=unknown pytest did not run: {exc}", flush=True)
        return NOT_RUN_EXIT_CODE
    service = subprocess.Popen(
        [sys.executable, "-m", "appmux"],
        cwd=DEPLOY_DIR,
        env=environment_for_service(environment),
    )
    signal.signal(signal.SIGTERM, lambda *_: _stop(service))
    try:
        test_environment = environment_for_tests(
            environment, port=port, appmux_pid=service.pid, libs_dir=DEPLOY_DIR / LIBS_DIRECTORY
        )
        reason = _wait_until_ready(service, port, test_environment["APPMUX_TEST_ADMIN_EMAIL"])
    except RunConfigError as exc:
        reason = str(exc)
    if reason is not None:
        print(not_run_line(config.run_id, reason), flush=True)
    else:
        print("appmux answers; running pytest inside the container", flush=True)
        finished = subprocess.run(
            pytest_command(sys.executable, config),
            cwd=DEPLOY_DIR,
            env=test_environment,
            check=False,
        )
        print(done_line(config.run_id, finished.returncode), flush=True)
    # The App stays up with appmux until the platform stops it, so its log can be read.
    return service.wait()


if __name__ == "__main__":
    raise SystemExit(main())
