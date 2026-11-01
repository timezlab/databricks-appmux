"""Fixture memory_fastapi: a FastAPI app that holds a configurable amount of memory.

For the memory budget and admission control tests (design 5.7, spec US10): the app holds real,
touched memory, so its resident set size (RSS) is about what it was asked to hold, and a test can
set a budget below, at and above that number. It is a standalone Databricks-style app:
``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

Configuration
    MEMORY_HOLD_MB   megabytes to hold from start, default 64 (an integer, 0 to 4096). Set it as a
                     release ``env`` entry to size a child before it starts. A value that is not an
                     integer in that range stops the app at start with a ``ValueError`` that names
                     the variable, the same range ``POST /hold`` enforces.

HTTP paths
    GET /           200 ``{"fixture": "memory_fastapi", "status": "ok"}``.
    GET /report     200 JSON ``{"fixture": "memory_fastapi", "held_mb": <int>, "rss_mb": <float>}``.
    POST /hold      query ``mb=<int>``: replaces the held memory with ``mb`` megabytes (0 frees it)
                    and answers with the same JSON as ``/report``. It grows or shrinks a running
                    child, which is how a test pushes an app over its budget or back under.

``held_mb`` is what the app was told to hold; ``rss_mb`` is read from ``/proc/self/statm`` on every
call, so it also counts the interpreter and the framework (roughly 40 to 60 MB with nothing held).
The pages are written, not just reserved, so the kernel counts them in RSS.
"""

from __future__ import annotations

import os
import threading
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Query

DEFAULT_HOLD_MB = 64
MIN_HOLD_MB = 0
MAX_HOLD_MB = 4096
BYTES_PER_MB = 1024 * 1024
STATM_RESIDENT_PAGES_FIELD = 1
FILL_BYTE = b"\x01"

app = FastAPI()
held_memory: list[bytearray] = []
held_memory_lock = threading.Lock()


def hold_memory(megabytes: int) -> None:
    """Replace what is held with ``megabytes`` of written (non-zero) bytes."""
    with held_memory_lock:
        # Freed first, so the peak is the new amount and not old plus new; a failed allocation
        # (MemoryError) therefore leaves nothing held.
        held_memory.clear()
        held_memory.append(bytearray(FILL_BYTE) * (megabytes * BYTES_PER_MB))


def configured_hold_mb() -> int:
    """``MEMORY_HOLD_MB`` as an integer in range; anything else stops the app at start."""
    raw_value = os.environ.get("MEMORY_HOLD_MB", str(DEFAULT_HOLD_MB))
    requirement = f"MEMORY_HOLD_MB must be an integer from {MIN_HOLD_MB} to {MAX_HOLD_MB}"
    try:
        megabytes = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{requirement}, got: {raw_value!r}") from exc
    if not MIN_HOLD_MB <= megabytes <= MAX_HOLD_MB:
        raise ValueError(f"{requirement}, got: {megabytes}")
    return megabytes


def resident_set_mb() -> float:
    with open("/proc/self/statm", encoding="ascii") as statm:
        resident_pages = int(statm.read().split()[STATM_RESIDENT_PAGES_FIELD])
    return round(resident_pages * os.sysconf("SC_PAGE_SIZE") / BYTES_PER_MB, 1)


def describe_memory() -> dict[str, object]:
    return {
        "fixture": "memory_fastapi",
        "held_mb": sum(len(buffer) for buffer in held_memory) // BYTES_PER_MB,
        "rss_mb": resident_set_mb(),
    }


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "memory_fastapi", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    return describe_memory()


@app.post("/hold")
def hold(mb: Annotated[int, Query(ge=MIN_HOLD_MB, le=MAX_HOLD_MB)]) -> dict[str, object]:
    hold_memory(mb)
    return describe_memory()


if __name__ == "__main__":
    hold_memory(configured_hold_mb())
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
