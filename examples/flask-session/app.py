"""flask-session: a small Flask app that shows cookies and redirects through appmux.

It is a standard Databricks App: nothing in it knows about appmux, and the same folder runs on a
standalone App. Started with ``python -m gunicorn --workers 1 --threads 4 app:app``; gunicorn binds
``0.0.0.0:$PORT``, and the platform (and appmux, design 5.4) sets ``PORT``. One worker, because the
signing key below is per process.

What it shows
    The page at ``/`` reports who is calling (the identity headers the proxy or appmux forwards),
    which release is running (``VERSION`` below and ``APP_GREETING`` from the environment), when
    this process started and how often this browser has loaded the page. A visitor can set a
    nickname. Both the visit count and the nickname live in Flask's signed session cookie, so they
    exercise the two things appmux rewrites on a response (design 7.1 step 7):

    * the cookie: Flask writes ``Path=/``; appmux forces ``Path=/<slug>/`` and drops ``Domain``;
    * the redirect: ``POST /nickname`` answers ``Location: /``; appmux makes it ``/<slug>/``.

    The signing key is random per process, so every Update, rollback, config change or restart
    ends all sessions: the visit count and the nickname reset.

HTTP paths
    GET  /               200 HTML. The form action and the links are relative on purpose: appmux
                         never rewrites a body, so open the app as ``/<slug>/`` with the slash.
    POST /nickname       302 to ``/``; form field ``nickname`` (up to 40 characters).
    POST /forget         302 to ``/``; clears the session.
    GET  /api/whoami     200 JSON, see ``describe``.
    GET  /healthz        200 ``{"status": "ok"}``. appmux's health check asks ``/`` by default.

The access token is reported as present or absent and never echoed.

Cookies are ``Secure`` unless ``ALLOW_INSECURE_COOKIES=1``, which is only for a local run over
plain http. Behind the platform the browser always speaks https, so leave it unset there.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from typing import Final

from flask import Flask, redirect, render_template_string, request, session

VERSION: Final = "1.0.0"
STARTED_AT: Final = datetime.now(UTC)
MAX_NICKNAME_LENGTH: Final = 40

# Headers the proxy sets and appmux forwards (design 7.1). appmux drops any client copy of them.
EMAIL_HEADER: Final = "X-Forwarded-Email"
USER_HEADER: Final = "X-Forwarded-User"
USERNAME_HEADER: Final = "X-Forwarded-Preferred-Username"
PREFIX_HEADER: Final = "X-Forwarded-Prefix"
REQUEST_ID_HEADER: Final = "X-Request-Id"
OBO_HEADER: Final = "X-Forwarded-Access-Token"

app = Flask(__name__)
app.config.update(
    # Per process on purpose: no key is stored anywhere, and a replaced process ends the sessions.
    SECRET_KEY=secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    # Lax keeps the cookie off cross-site POSTs, which is what protects the two forms below.
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("ALLOW_INSECURE_COOKIES") != "1",
)

PAGE: Final = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>flask-session</title>
<style>body{font:16px/1.5 system-ui;max-width:42rem;margin:2rem auto;padding:0 1rem}
code{background:#eee;padding:0 .25em}</style></head>
<body>
<h1>flask-session <small>v{{ version }}</small></h1>
<p>{{ greeting }}</p>
<p>You are <b>{{ who }}</b>{% if nickname %}, nicknamed <b>{{ nickname }}</b>{% endif %}.
The access-token header is {{ "present" if token_present else "absent" }}.</p>
<p>This browser has loaded the page <b>{{ visits }}</b> time(s) since the process started
{{ started_at }}. Served under prefix <code>{{ prefix }}</code>.</p>
<form method="post" action="nickname">
  <label>Nickname <input name="nickname" maxlength="{{ max_nickname }}" required></label>
  <button>Save</button>
</form>
<form method="post" action="forget"><button>Forget me</button></form>
<ul>
<li><a href="api/whoami">api/whoami</a></li>
<li><a href="healthz">healthz</a></li>
</ul>
</body></html>"""


def greeting() -> str:
    return os.environ.get("APP_GREETING", "(APP_GREETING is not set)")


def describe() -> dict[str, object]:
    headers = request.headers
    return {
        "app": "flask-session",
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
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/whoami")
def whoami() -> dict[str, object]:
    return describe()


@app.get("/")
def index() -> str:
    session["visits"] = session.get("visits", 0) + 1
    return render_template_string(
        PAGE,
        version=VERSION,
        greeting=greeting(),
        who=request.headers.get(EMAIL_HEADER, "anonymous"),
        nickname=session.get("nickname"),
        token_present=OBO_HEADER in request.headers,
        visits=session["visits"],
        started_at=STARTED_AT.isoformat(timespec="seconds"),
        prefix=request.headers.get(PREFIX_HEADER) or "none",
        max_nickname=MAX_NICKNAME_LENGTH,
    )


@app.post("/nickname")
def set_nickname() -> object:
    nickname = request.form.get("nickname", "").strip()[:MAX_NICKNAME_LENGTH]
    if nickname:
        session["nickname"] = nickname
    # An absolute path on purpose: the child believes it lives at "/", and appmux makes it
    # "/<slug>/" in the Location header (design 7.1 step 7).
    return redirect("/")


@app.post("/forget")
def forget() -> object:
    session.clear()
    return redirect("/")
