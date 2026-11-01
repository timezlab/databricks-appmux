"""fastapi-notes: a small FastAPI app for trying appmux as an app admin and as a visitor.

It is a standard Databricks App: nothing in it knows about appmux, and the same folder runs on a
standalone App. Started with ``python -m uvicorn app:app``; uvicorn reads its host, port and root
path from ``UVICORN_HOST``, ``UVICORN_PORT`` and ``UVICORN_ROOT_PATH``, which the platform sets
(and appmux sets, design 5.4).

What it shows
    The page at ``/`` and ``GET /api/whoami`` report who is calling (the identity headers the proxy
    or appmux forwards), which release is running (``VERSION`` below and ``APP_GREETING`` from the
    environment), when this process started, and which prefix the app is served under. The notes
    are kept in memory, so they vanish when the process is replaced: an Update, a rollback, a
    config change or a restart each start a new process.

HTTP paths
    GET  /               200 HTML. Every link is relative, because appmux never rewrites a body
                         (design 7.1 step 7): open the app as ``/<slug>/`` with the trailing slash.
    GET  /api/whoami     200 JSON, see ``describe``.
    GET  /api/notes      200 JSON list of the notes.
    POST /api/notes      201 with the new note; body ``{"text": "..."}``; 409 once the list is full.
    GET  /healthz        200 ``{"status": "ok"}``. appmux's health check asks ``/`` by default.
    GET  /openapi.json   FastAPI's schema; its ``servers`` entry carries the prefix when
                         ``UVICORN_ROOT_PATH`` is set.

The access token is reported as present or absent and never echoed.
"""

from __future__ import annotations

import html
import os
from datetime import UTC, datetime
from typing import Final

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

VERSION: Final = "1.0.0"
STARTED_AT: Final = datetime.now(UTC)
MAX_NOTES: Final = 50
MAX_NOTE_LENGTH: Final = 200

# Headers the proxy sets and appmux forwards (design 7.1). appmux drops any client copy of them.
EMAIL_HEADER: Final = "x-forwarded-email"
USER_HEADER: Final = "x-forwarded-user"
USERNAME_HEADER: Final = "x-forwarded-preferred-username"
PREFIX_HEADER: Final = "x-forwarded-prefix"
REQUEST_ID_HEADER: Final = "x-request-id"
OBO_HEADER: Final = "x-forwarded-access-token"
ANONYMOUS: Final = "anonymous"

app = FastAPI(title="fastapi-notes", version=VERSION)
notes: list[dict[str, str]] = []


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_NOTE_LENGTH)


def greeting() -> str:
    return os.environ.get("APP_GREETING", "(APP_GREETING is not set)")


def describe(request: Request) -> dict[str, object]:
    headers = request.headers
    return {
        "app": "fastapi-notes",
        "version": VERSION,
        "greeting": greeting(),
        "started_at": STARTED_AT.isoformat(timespec="seconds"),
        "caller": {
            "email": headers.get(EMAIL_HEADER),
            "user": headers.get(USER_HEADER),
            "preferred_username": headers.get(USERNAME_HEADER),
        },
        # Present only for an OBO-delegated app and a visitor allowed to pass it (design 7.1).
        "access_token_header_present": OBO_HEADER in headers,
        "prefix": {
            "x_forwarded_prefix": headers.get(PREFIX_HEADER),
            "appmux_base_path": os.environ.get("APPMUX_BASE_PATH"),
        },
        "request_id": headers.get(REQUEST_ID_HEADER),
        "note_count": len(notes),
    }


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/whoami")
async def whoami(request: Request) -> dict[str, object]:
    return describe(request)


@app.get("/api/notes")
async def list_notes() -> list[dict[str, str]]:
    return notes


@app.post("/api/notes", status_code=201)
async def add_note(note: NoteIn, request: Request) -> dict[str, str]:
    if len(notes) >= MAX_NOTES:
        raise HTTPException(status_code=409, detail=f"the list holds {MAX_NOTES} notes; restart")
    entry = {
        "author": request.headers.get(EMAIL_HEADER, ANONYMOUS),
        "text": note.text,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    notes.append(entry)
    return entry


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    headers = request.headers
    return _page(
        who=headers.get(EMAIL_HEADER, ANONYMOUS),
        token_present=OBO_HEADER in headers,
        prefix=headers.get(PREFIX_HEADER) or "none",
    )


def _page(*, who: str, token_present: bool, prefix: str) -> str:
    rows = "".join(
        f"<li><b>{html.escape(n['author'])}</b> {n['at']}: {html.escape(n['text'])}</li>"
        for n in notes
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>fastapi-notes</title>
<style>body{{font:16px/1.5 system-ui;max-width:42rem;margin:2rem auto;padding:0 1rem}}
code{{background:#eee;padding:0 .25em}}</style></head>
<body>
<h1>fastapi-notes <small>v{VERSION}</small></h1>
<p>{html.escape(greeting())}</p>
<p>You are <b>{html.escape(who)}</b>. The access-token header is
{"present" if token_present else "absent"}.</p>
<p>Process started {STARTED_AT.isoformat(timespec="seconds")}. Served under prefix
<code>{html.escape(prefix)}</code>.</p>
<ul>
<li><a href="api/whoami">api/whoami</a></li>
<li><a href="api/notes">api/notes</a> (POST <code>{{"text": "..."}}</code> to add one)</li>
<li><a href="healthz">healthz</a></li>
<li><a href="docs">docs</a> (loads its scripts from a CDN)</li>
</ul>
<h2>Notes ({len(notes)})</h2>
<ul>{rows or "<li>none yet</li>"}</ul>
</body></html>"""
