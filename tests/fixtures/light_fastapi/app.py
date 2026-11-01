"""Fixture light_fastapi: a FastAPI app that answers fast and holds almost no memory.

It is the load fixture of SC-001 (design 14), the base of the ten well-behaved apps, and the
neighbour whose p95 the hostile fixtures must not disturb. It is a standalone Databricks-style app:
``python app.py`` and the port in ``DATABRICKS_APP_PORT``. Its two requirements are the versions the
App environment already holds, so a release of it needs no wheel.

HTTP paths
    GET /          200 ``{"fixture": "light_fastapi", "status": "ok"}``.
    GET /healthz   200 ``{"status": "ok"}``.
    GET /headers   200 JSON describing the request headers the child received:
                   ``names`` (all header names, lower case, sorted, never values) and ``values``
                   (the value of each identity header below, or null when it is absent).
                   The access token appears by name only, so a test can check that
                   ``x-forwarded-access-token`` is stripped or passed (spec US9) without the
                   fixture ever echoing a credential.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI, Request

# The headers the edge sets for a child (design 7.1) that carry no credential.
IDENTITY_HEADER_NAMES = (
    "x-forwarded-email",
    "x-forwarded-user",
    "x-forwarded-preferred-username",
    "x-forwarded-prefix",
    "x-real-ip",
    "x-request-id",
)

app = FastAPI()


@app.get("/")
async def index() -> dict[str, str]:
    return {"fixture": "light_fastapi", "status": "ok"}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/headers")
async def headers(request: Request) -> dict[str, object]:
    return {
        "names": sorted(request.headers.keys()),
        "values": {name: request.headers.get(name) for name in IDENTITY_HEADER_NAMES},
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
