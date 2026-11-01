"""Hostile fixture fork_bomb: forks until the system refuses, then reports how far it got.

NEVER run this outside an appmux sandbox, and never on a workstation. It exists to prove that the
launcher's ``RLIMIT_NPROC`` stops a child (design 6.4: "processes are bounded per sandbox", default
``max_processes_per_child`` 256). It is a standalone Databricks-style app: ``python app.py`` and the
port in ``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "fork_bomb"}``, so the health check passes; it forks nothing.
    GET /report    200 JSON. Each call runs the fork loop once (calls are serialised, so two
                   callers cannot add their forks together) and then kills and reaps every child
                   it made before it answers.

Safety stops, so a sandbox that failed to set a limit costs a bounded burst and not the machine:
the loop stops at ``FORK_CEILING`` children, every child sleeps at most ``CHILD_LIFETIME_S`` seconds
and exits on its own, and the parent sends them all ``SIGKILL`` as soon as the loop ends.

``/report`` keys
    forks_succeeded      how many children were created before the first refusal.
    refused_with         the errno name of the refusal (``"EAGAIN"`` when ``RLIMIT_NPROC`` stopped
                         it), or null when the loop reached the ceiling without a refusal.
    stopped_by_ceiling   true when the loop stopped at ``FORK_CEILING`` (no limit was enforced).
    ceiling              ``FORK_CEILING``.
    rlimit_nproc_soft    the soft ``RLIMIT_NPROC`` of the process, or null when unlimited. The
                         count of forks that succeeded must stay below it.
    elapsed_s            seconds the loop and the clean-up took.
"""

from __future__ import annotations

import errno
import os
import resource
import signal
import threading
import time

import uvicorn
from fastapi import FastAPI

FORK_CEILING = 1024
CHILD_LIFETIME_S = 20

app = FastAPI()
fork_loop_lock = threading.Lock()


# Fixtures are standalone apps and cannot share code, so the hostile ones repeat this helper.
def errno_name(exc: OSError) -> str:
    """The errno name of a failed system call, or the exception type when it carries no errno."""
    if exc.errno is None:
        return type(exc).__name__
    return errno.errorcode.get(exc.errno, type(exc).__name__)


def hold_then_exit() -> None:
    """Runs in a forked child: sleep, then leave without returning into the server."""
    try:
        time.sleep(CHILD_LIFETIME_S)
    finally:
        os._exit(0)


def kill_and_reap(child_pids: list[int]) -> None:
    for pid in child_pids:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            # Already gone and reaped (the child timed out first): nothing is left to clean up.
            continue


def fork_until_refused() -> dict[str, object]:
    soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NPROC)
    child_pids: list[int] = []
    refused_with: str | None = None
    started = time.monotonic()
    try:
        while len(child_pids) < FORK_CEILING:
            try:
                pid = os.fork()
            except OSError as exc:
                refused_with = errno_name(exc)
                break
            if pid == 0:
                hold_then_exit()
            child_pids.append(pid)
    finally:
        kill_and_reap(child_pids)
    return {
        "fixture": "fork_bomb",
        "forks_succeeded": len(child_pids),
        "refused_with": refused_with,
        "stopped_by_ceiling": refused_with is None,
        "ceiling": FORK_CEILING,
        "rlimit_nproc_soft": None if soft_limit == resource.RLIM_INFINITY else soft_limit,
        "elapsed_s": round(time.monotonic() - started, 3),
    }


@app.get("/")
def health() -> dict[str, str]:
    return {"fixture": "fork_bomb"}


@app.get("/report")
def report() -> dict[str, object]:
    with fork_loop_lock:
        return fork_until_refused()


if __name__ == "__main__":
    uvicorn.run(
        app,
        # appmux and the platform both set UVICORN_HOST=0.0.0.0 (design 5.4). Run without it, the
        # hostile /report must not be open on every interface, so the default is loopback.
        host=os.environ.get("UVICORN_HOST", "127.0.0.1"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
