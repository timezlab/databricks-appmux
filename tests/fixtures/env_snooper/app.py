"""Hostile fixture env_snooper: tries to reach other processes' environments and platform secrets.

Run it only inside an appmux sandbox, through the launcher; it exists to prove the sandbox holds
(design 6.1 and 6.4, spec FR-011). It is a standalone Databricks-style app: ``python app.py`` and
the port in ``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "env_snooper"}``, so the health check passes.
    GET /report    200 JSON, computed on every call (see below).

The report never contains a value that was read. The fixture only *opens* each target (access to
``/proc/<pid>/environ`` is decided at open time) and closes it again, so no secret byte is touched,
logged or returned. Every result is ``"opened"`` (the isolation failed for that target) or the
errno name of the refusal (``"ENOENT"``, ``"EACCES"``, ...).

``/report`` keys
    own_pid                 the fixture's own pid inside its pid namespace.
    visible_pids            the pids listed under ``/proc`` (a sandbox lists none).
    proc_listing            ``"ok"`` or the errno name when ``/proc`` cannot be listed.
    environ_open            ``{"<pid>": result}`` for every visible pid, PID 1 and the parent pid.
    pid1_environ_open       the result for ``/proc/1/environ``.
    foreign_environ_opened  pids other than ``own_pid`` whose environ could be opened (must be []).
    runtime_cfg_open        the result for the platform's ``runtime.cfg`` (must not be "opened").
    own_environ_open        the result for ``/proc/self/environ`` (informational).
    own_environment_names   sorted names, never values, of the fixture's own environment.
    isolated                true when no foreign environ and not ``runtime.cfg`` could be opened.
"""

from __future__ import annotations

import errno
import os

import uvicorn
from fastapi import FastAPI

PROC_ROOT = "/proc"
PLATFORM_INIT_PID = 1
RUNTIME_CFG_PATH = "/databricks/runtime-secrets/runtime.cfg"
OPENED = "opened"

app = FastAPI()


# Fixtures are standalone apps and cannot share code, so the hostile ones repeat this helper.
def errno_name(exc: OSError) -> str:
    """The errno name of a failed system call, or the exception type when it carries no errno."""
    if exc.errno is None:
        return type(exc).__name__
    return errno.errorcode.get(exc.errno, type(exc).__name__)


def try_open_for_reading(path: str) -> str:
    """``"opened"`` when the path can be opened for reading, otherwise the errno name."""
    try:
        file_descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        return errno_name(exc)
    os.close(file_descriptor)
    return OPENED


def list_visible_pids() -> tuple[list[int], str]:
    """The numeric entries of ``/proc`` and ``"ok"``, or no pids and the errno name."""
    try:
        names = os.listdir(PROC_ROOT)
    except OSError as exc:
        return [], errno_name(exc)
    return sorted(int(name) for name in names if name.isdigit()), "ok"


def build_report() -> dict[str, object]:
    own_pid = os.getpid()
    visible_pids, proc_listing = list_visible_pids()
    target_pids = sorted({*visible_pids, PLATFORM_INIT_PID, os.getppid()} - {0})
    environ_open = {
        str(pid): try_open_for_reading(f"{PROC_ROOT}/{pid}/environ") for pid in target_pids
    }
    foreign_environ_opened = [
        pid for pid in target_pids if pid != own_pid and environ_open[str(pid)] == OPENED
    ]
    runtime_cfg_open = try_open_for_reading(RUNTIME_CFG_PATH)
    return {
        "fixture": "env_snooper",
        "own_pid": own_pid,
        "visible_pids": visible_pids,
        "proc_listing": proc_listing,
        "environ_open": environ_open,
        "pid1_environ_open": environ_open[str(PLATFORM_INIT_PID)],
        "foreign_environ_opened": foreign_environ_opened,
        "runtime_cfg_open": runtime_cfg_open,
        "own_environ_open": try_open_for_reading(f"{PROC_ROOT}/self/environ"),
        "own_environment_names": sorted(os.environ),
        "isolated": not foreign_environ_opened and runtime_cfg_open != OPENED,
    }


@app.get("/")
def health() -> dict[str, str]:
    return {"fixture": "env_snooper"}


@app.get("/report")
def report() -> dict[str, object]:
    return build_report()


if __name__ == "__main__":
    uvicorn.run(
        app,
        # appmux and the platform both set UVICORN_HOST=0.0.0.0 (design 5.4). Run without it, the
        # hostile /report must not be open on every interface, so the default is loopback.
        host=os.environ.get("UVICORN_HOST", "127.0.0.1"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
