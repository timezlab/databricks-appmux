"""Hostile fixture memory_hog: allocates memory until it is stopped.

Run it only inside an appmux sandbox, through the launcher; it exists to prove the memory watchdog
and address-space limit hold (design 5.7, spec US10, TD-003). On a machine without those limits it
keeps growing until the kernel kills a process, possibly not this one. It is a standalone
Databricks-style app: ``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

A background thread appends one written (non-zero, so counted in RSS) chunk every interval and
never frees anything. It stops growing only when an allocation raises ``MemoryError`` (the
``RLIMIT_AS`` limit being hit), when the optional cap is reached, or when the process is killed.
The thread starts before the server, so the health check can pass while the app is already growing.

Configuration
    HOG_CHUNK_MB          megabytes per allocation, default 32.
    HOG_INTERVAL_SECONDS  pause between allocations, default 0.1. A very small value makes a "fast
                          allocator", the known gap of the watchdog (TD-003).
    HOG_MAX_MB            stop growing at this many megabytes; default 0, which means no cap.
    HOG_START_DELAY_SECONDS  pause before the first allocation, default 0. The memory watchdog
                          (TD-029) stops a child that grows this fast within a second or two of its
                          start, which can be before the health check passes; ``app.yaml`` sets a
                          delay so the release is up first and the stop happens to a running app.

HTTP paths
    GET /          200 ``{"fixture": "memory_hog", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "memory_hog", "held_mb": <int>,
                   "stopped_by": <str or null>}``.

``held_mb`` is the total allocated so far. ``stopped_by`` is null while growing, ``"MemoryError"``
when the address-space limit refused an allocation, and ``"cap"`` when ``HOG_MAX_MB`` was reached.
A killed process answers nothing, which is the expected end.
"""

from __future__ import annotations

import os
import threading
import time

import uvicorn
from fastapi import FastAPI

DEFAULT_CHUNK_MB = 32
DEFAULT_INTERVAL_SECONDS = 0.1
UNLIMITED = 0
BYTES_PER_MB = 1024 * 1024
FILL_BYTE = b"\x01"

app = FastAPI()
held_chunks: list[bytearray] = []
stop_reason: list[str] = []


def configured_chunk_mb() -> int:
    return int(os.environ.get("HOG_CHUNK_MB", DEFAULT_CHUNK_MB))


def allocate_until_stopped(
    chunk_mb: int, interval_seconds: float, max_mb: int, start_delay_seconds: float
) -> None:
    time.sleep(start_delay_seconds)
    while True:
        if max_mb != UNLIMITED and (len(held_chunks) + 1) * chunk_mb > max_mb:
            stop_reason.append("cap")
            return
        try:
            held_chunks.append(bytearray(FILL_BYTE) * (chunk_mb * BYTES_PER_MB))
        except MemoryError:
            # The address-space limit refused the allocation. Stop growing and stay alive, so the
            # test can tell "refused by the limit" from "killed by the watchdog".
            stop_reason.append("MemoryError")
            return
        time.sleep(interval_seconds)


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "memory_hog", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    return {
        "fixture": "memory_hog",
        "held_mb": len(held_chunks) * configured_chunk_mb(),
        "stopped_by": stop_reason[0] if stop_reason else None,
    }


if __name__ == "__main__":
    threading.Thread(
        target=allocate_until_stopped,
        args=(
            configured_chunk_mb(),
            float(os.environ.get("HOG_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)),
            int(os.environ.get("HOG_MAX_MB", UNLIMITED)),
            float(os.environ.get("HOG_START_DELAY_SECONDS", 0)),
        ),
        daemon=True,
    ).start()
    uvicorn.run(
        app,
        # appmux and the platform both set UVICORN_HOST=0.0.0.0 (design 5.4). Run without it, the
        # hostile /report must not be open on every interface, so the default is loopback.
        host=os.environ.get("UVICORN_HOST", "127.0.0.1"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
