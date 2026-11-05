"""Hostile fixture memory_hog_workers: spreads its memory over several processes.

Run it only inside an appmux sandbox, through the launcher. It is the case that ``RLIMIT_AS`` does
not cover (design 5.7, TD-003, TD-029): the address-space limit is set per process, so a child that
starts several workers, each well under the limit, can hold several times its budget. Only the RSS
watchdog, which sums the whole process tree of the sandbox, can stop it. It is a standalone
Databricks-style app: ``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

The server starts ``HOG_WORKERS`` worker processes. After ``HOG_START_DELAY_SECONDS`` (so the
health check passes first) each worker writes ``HOG_WORKER_MB`` megabytes in small chunks, so the
memory is resident, and then sleeps. Nothing here grows without end: the total is at most
``MAX_WORKERS * MAX_WORKER_MB`` megabytes whatever the environment says, and a worker that nobody
stops ends itself after ``WORKER_LIFETIME_SECONDS``. Killing the sandbox kills the workers with it.

Configuration (the defaults hold 600 MB in total, about twice a 256 MB budget)
    HOG_WORKERS              worker processes, default 4, at most 8.
    HOG_WORKER_MB            megabytes each worker holds, default 150, at most 300. A worker plus
                             Python's own size stays far below the address-space limit of a 256 MB
                             budget (512 MB), which is the point.
    HOG_START_DELAY_SECONDS  pause before the workers allocate, default 4.

HTTP paths
    GET /          200 ``{"fixture": "memory_hog_workers", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "memory_hog_workers", "workers": <int>,
                   "worker_mb": <int>}``: what it was told to hold, not what it holds.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import uvicorn
from fastapi import FastAPI

DEFAULT_WORKERS = 4
MAX_WORKERS = 8
DEFAULT_WORKER_MB = 150
MAX_WORKER_MB = 300
DEFAULT_START_DELAY_SECONDS = 4.0
CHUNK_MB = 25
CHUNK_INTERVAL_SECONDS = 0.1
WORKER_LIFETIME_SECONDS = 600
WORKER_FLAG = "--worker"
BYTES_PER_MB = 1024 * 1024
FILL_BYTE = b"\x01"


def configured_workers() -> int:
    return max(0, min(int(os.environ.get("HOG_WORKERS", DEFAULT_WORKERS)), MAX_WORKERS))


def configured_worker_mb() -> int:
    return max(1, min(int(os.environ.get("HOG_WORKER_MB", DEFAULT_WORKER_MB)), MAX_WORKER_MB))


def configured_start_delay_seconds() -> float:
    return float(os.environ.get("HOG_START_DELAY_SECONDS", DEFAULT_START_DELAY_SECONDS))


def hold_memory(megabytes: int, start_delay_seconds: float) -> None:
    """The worker: wait, write ``megabytes`` in chunks, keep them until the lifetime is over."""
    time.sleep(start_delay_seconds)
    held: list[bytearray] = []
    remaining_mb = megabytes
    while remaining_mb > 0:
        chunk_mb = min(CHUNK_MB, remaining_mb)
        held.append(bytearray(FILL_BYTE) * (chunk_mb * BYTES_PER_MB))
        remaining_mb -= chunk_mb
        time.sleep(CHUNK_INTERVAL_SECONDS)
    time.sleep(WORKER_LIFETIME_SECONDS)


app = FastAPI()


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "memory_hog_workers", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    return {
        "fixture": "memory_hog_workers",
        "workers": configured_workers(),
        "worker_mb": configured_worker_mb(),
    }


if __name__ == "__main__":
    if sys.argv[1:2] == [WORKER_FLAG]:
        hold_memory(int(sys.argv[2]), float(sys.argv[3]))
    else:
        for _ in range(configured_workers()):
            # Re-runs this file with this interpreter and nothing else.
            subprocess.Popen(
                [
                    sys.executable,
                    os.path.abspath(__file__),
                    WORKER_FLAG,
                    str(configured_worker_mb()),
                    str(configured_start_delay_seconds()),
                ]
            )
        uvicorn.run(
            app,
            # Same as memory_hog: loopback unless appmux and the platform say otherwise.
            host=os.environ.get("UVICORN_HOST", "127.0.0.1"),
            port=int(os.environ["DATABRICKS_APP_PORT"]),
        )
