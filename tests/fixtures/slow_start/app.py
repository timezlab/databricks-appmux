"""Failing-then-fine fixture slow_start: binds its port only after a configurable delay.

For the whole delay the port refuses connections, which is what the health clock sees as
"connection refused" (design 5.5). With a delay inside the app's ``health_deadline_s`` the release
turns healthy; with a delay past it the release fails with "health check did not pass within N s"
(spec US1 scenario 5). It is a standalone Databricks-style app: ``python app.py`` and the port in
``DATABRICKS_APP_PORT``.

Configuration
    SLOW_START_DELAY_S   seconds to wait before binding the port; a number, default 10. A test sets
                         it in the release's ``env`` entries, which win over this default.

HTTP paths (served only after the delay)
    GET /          200 ``{"fixture": "slow_start", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "slow_start", "start_delay_s": <delay>,
                   "seconds_since_process_start": <seconds>}``.
"""

from __future__ import annotations

import os
import sys
import time

import uvicorn
from fastapi import FastAPI

DEFAULT_START_DELAY_S = 10.0

PROCESS_STARTED = time.monotonic()
START_DELAY_S = float(os.environ.get("SLOW_START_DELAY_S", DEFAULT_START_DELAY_S))

app = FastAPI()


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "slow_start", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    return {
        "fixture": "slow_start",
        "start_delay_s": START_DELAY_S,
        "seconds_since_process_start": round(time.monotonic() - PROCESS_STARTED, 3),
    }


if __name__ == "__main__":
    sys.stderr.write(f"slow_start: waiting {START_DELAY_S} s before binding the port\n")
    sys.stderr.flush()
    time.sleep(START_DELAY_S)
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
