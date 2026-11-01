"""Routing fixture base_path_aware: a page whose links carry the prefix appmux serves it under.

appmux strips the app's prefix from the request (``/sales/x`` reaches the app as ``/x``), adds the
header ``X-Forwarded-Prefix: /sales`` to every request and sets ``APPMUX_BASE_PATH=/sales`` in the
child's environment (design 5.4 and 7.2). This app builds every link it writes from that prefix, so
the browser's follow-up requests go to ``/sales/static/x.css`` and reach the app again. It is a
standalone Databricks-style app: ``python app.py`` and the port in ``DATABRICKS_APP_PORT``.

The prefix is taken from the request header first and from ``APPMUX_BASE_PATH`` second; with neither
(a standalone App) it is empty and the links are root-relative. A value that is not a plain path
such as ``/sales`` is ignored, because the page writes it into HTML.

HTTP paths
    GET /                200 HTML with ``<link href="<prefix>/static/x.css">`` and
                         ``<a href="<prefix>/api/ping">``.
    GET /static/x.css    200 ``text/css``.
    GET /api/ping        200 ``{"ok": true}``.
    GET /report          200 JSON for the request that asked: ``x_forwarded_prefix`` and
                         ``appmux_base_path`` (the raw values, or null), ``base_path`` (the prefix
                         used, "" for none) and ``source`` (``"x-forwarded-prefix"``,
                         ``"APPMUX_BASE_PATH"`` or ``"none"``).
"""

from __future__ import annotations

import os
import re

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

BASE_PATH_VARIABLE = "APPMUX_BASE_PATH"
PREFIX_HEADER = "x-forwarded-prefix"
PLAIN_PATH_PATTERN = re.compile(r"(/[A-Za-z0-9._~-]+)+")

app = FastAPI()


def normalise_prefix(raw: str | None) -> str | None:
    """``"/sales/"`` becomes ``"/sales"``; None for a missing, empty or non-path value."""
    if raw is None:
        return None
    trimmed = raw.strip().rstrip("/")
    return trimmed if PLAIN_PATH_PATTERN.fullmatch(trimmed) else None


def resolve_base_path(request: Request) -> tuple[str, str]:
    """The prefix to put in links and where it came from."""
    from_header = normalise_prefix(request.headers.get(PREFIX_HEADER))
    if from_header is not None:
        return from_header, PREFIX_HEADER
    from_environment = normalise_prefix(os.environ.get(BASE_PATH_VARIABLE))
    if from_environment is not None:
        return from_environment, BASE_PATH_VARIABLE
    return "", "none"


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    base_path, _source = resolve_base_path(request)
    # base_path matched PLAIN_PATH_PATTERN, so it holds no character that needs HTML escaping.
    return (
        "<!doctype html><html><head>"
        f'<link rel="stylesheet" href="{base_path}/static/x.css">'
        "</head><body><h1>base_path_aware</h1>"
        f'<a href="{base_path}/api/ping">ping</a>'
        "</body></html>"
    )


@app.get("/static/x.css")
def stylesheet() -> Response:
    return Response("body { font-family: sans-serif; }\n", media_type="text/css")


@app.get("/api/ping")
def ping() -> dict[str, bool]:
    return {"ok": True}


@app.get("/report")
def report(request: Request) -> dict[str, object]:
    base_path, source = resolve_base_path(request)
    return {
        "fixture": "base_path_aware",
        "x_forwarded_prefix": request.headers.get(PREFIX_HEADER),
        "appmux_base_path": os.environ.get(BASE_PATH_VARIABLE),
        "base_path": base_path,
        "source": source,
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
