"""Failing fixture always_500: listens, but answers 500 on every path, so health never passes.

Health is a GET that answers below 500 (design 5.5); this app answers 500 to every method on every
path, ``/report`` and ``/docs`` included, except the one counter request below. A release of it must
end ``failed`` with the last error a 500 and its route must never open (spec US1 scenario 4).

HTTP paths
    GET /__always_500_report__
                   200 JSON ``{"fixture": "always_500", "requests_answered_500": <count>}``, the
                   number of requests it has refused with a 500 so far, for a test that reaches the
                   process directly. This is the only request that does not get a 500. It is not
                   ``/report`` (the other fixtures' report path): a ``health_path`` could name
                   ``/report`` and the fixture would turn healthy. No ``health_path`` should name
                   this path.
    anything else  500 ``{"fixture": "always_500", "error": "always_500"}``.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
COUNTER_PATH = "/__always_500_report__"

# No generated /docs, /redoc or /openapi.json: those would answer 200 and break the fixture's point.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
answered_500 = 0  # only the event loop thread touches it, so it needs no lock


@app.get(COUNTER_PATH)
async def counter() -> dict[str, object]:
    return {"fixture": "always_500", "requests_answered_500": answered_500}


@app.api_route("/{path:path}", methods=ALL_METHODS)
async def always_fail(path: str) -> JSONResponse:
    global answered_500
    answered_500 += 1
    return JSONResponse({"fixture": "always_500", "error": "always_500"}, status_code=500)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
