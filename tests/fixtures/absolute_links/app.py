"""Routing fixture absolute_links: a page that hard-codes an absolute link.

Its page names ``/static/x.css`` from the origin root. Behind appmux the page is at
``/<slug>/``, so the browser asks the *origin* for ``/static/x.css``: ``static`` is no slug, and the
edge must answer ``404`` (design 7.2: an app with hard-coded absolute links is not hostable in v1;
the page loads, the stray request gets a ``404``). The request must not be routed to any app, this
one included. It is a standalone Databricks-style app: ``python app.py`` and the port in
``DATABRICKS_APP_PORT``.

HTTP paths
    GET /                200 HTML with ``<link rel="stylesheet" href="/static/x.css">``.
    GET /static/x.css    200 ``text/css``, so the page works when the app is hosted at the root.
    GET /report          200 JSON ``{"fixture": "absolute_links", "hard_coded_links":
                         ["/static/x.css"], "hits": {"<path>": <count>}}``; ``hits`` counts the
                         requests this process has received per path (not ``/report`` itself). A
                         test loads ``/<slug>/``, asks the origin for ``/static/x.css``, expects
                         the edge's ``404``, and then reads ``/<slug>/report`` to see that
                         ``/static/x.css`` has zero hits: the stray request was refused, not
                         misrouted here.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

HARD_CODED_STYLESHEET = "/static/x.css"
REPORT_PATH = "/report"

app = FastAPI()
hits_by_path: Counter[str] = Counter()


@app.middleware("http")
async def count_hits(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.url.path != REPORT_PATH:
        hits_by_path[request.url.path] += 1
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (
        "<!doctype html><html><head>"
        f'<link rel="stylesheet" href="{HARD_CODED_STYLESHEET}">'
        "</head><body><h1>absolute_links</h1></body></html>"
    )


@app.get(HARD_CODED_STYLESHEET)
def stylesheet() -> Response:
    return Response("body { font-family: sans-serif; }\n", media_type="text/css")


@app.get(REPORT_PATH)
def report() -> dict[str, object]:
    return {
        "fixture": "absolute_links",
        "hard_coded_links": [HARD_CODED_STYLESHEET],
        "hits": dict(hits_by_path),
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("UVICORN_HOST", "0.0.0.0"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
