"""Failing fixture crash_loop: starts, serves, then exits with status 7 shortly after every start.

The health check passes (the app answers below 500 within a second), so the release turns healthy
and opens its route; then the process dies, the reconciler restarts it with backoff, and after
repeated failures the app must become ``crashed`` with only its own route answering 503
(design 5.6, spec US2). It is a standalone Databricks-style app: ``python app.py`` and the port in
``DATABRICKS_APP_PORT``.

Configuration
    CRASH_LOOP_EXIT_AFTER_S   seconds between the start and the exit; a number, default 3. It must
                              stay above the one-second health poll, or the release fails its
                              health check instead of crashing after it.

HTTP paths
    GET /          200 ``{"fixture": "crash_loop", "status": "ok"}``.
    GET /report    200 JSON ``{"fixture": "crash_loop", "exit_after_s": <seconds>,
                   "exit_status": 7}``.

The exit is a hard ``os._exit`` from a timer thread, after a line on stderr naming the status, so
the log shows the exit code and the last log lines like any real crash.
"""

from __future__ import annotations

import os
import sys
import threading

import uvicorn
from fastapi import FastAPI

DEFAULT_EXIT_AFTER_S = 3.0
EXIT_STATUS = 7

EXIT_AFTER_S = float(os.environ.get("CRASH_LOOP_EXIT_AFTER_S", DEFAULT_EXIT_AFTER_S))

app = FastAPI()


@app.get("/")
def index() -> dict[str, str]:
    return {"fixture": "crash_loop", "status": "ok"}


@app.get("/report")
def report() -> dict[str, object]:
    return {"fixture": "crash_loop", "exit_after_s": EXIT_AFTER_S, "exit_status": EXIT_STATUS}


def exit_now() -> None:
    sys.stderr.write(f"crash_loop: exiting with status {EXIT_STATUS} after {EXIT_AFTER_S} s\n")
    sys.stderr.flush()
    os._exit(EXIT_STATUS)


if __name__ == "__main__":
    threading.Timer(EXIT_AFTER_S, exit_now).start()
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
