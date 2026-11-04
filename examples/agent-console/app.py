"""agent-console: calls ``examples/agent-hub`` over MCP and A2A, as you or as itself, and shows who
the hub saw.

It is a standard Databricks App: nothing in it knows about appmux, and the same folder runs on a
standalone App. Started with ``python -m uvicorn app:app``; uvicorn reads its host, port and root
path from ``UVICORN_HOST``, ``UVICORN_PORT`` and ``UVICORN_ROOT_PATH``, which the platform sets
(and appmux sets, design 5.4).

The question it answers: when this App calls another App over MCP or A2A, can the other App tell
*who* is behind the call? Every call can be sent under one of two identities.

``me``   the visitor's own token. The Databricks proxy gives an App that was delegated OBO the
         visitor's token in ``X-Forwarded-Access-Token``; this App sends it on as the bearer of the
         call. The hub then sees the visitor, and its ``databricks_me`` tool can act as them.
``app``  this App's own identity, found by the Databricks SDK the way it finds it for any App (under
         appmux that needs the shared service principal delegated). The hub sees a service
         principal and has no user to act as.

HTTP paths
    GET   /                      200 HTML: the identity check and a form for one call. Relative URLs
                                 only, because appmux never rewrites a body (design 7.1 step 7).
    GET   /api/context           200 JSON: who you are, who the App is, where the hub is.
    POST  /api/call              one call to the hub: ``{identity, protocol, action, text?}``.
    POST  /api/identity-check    ``whoami`` and ``databricks_me`` over both protocols under both
                                 identities: what the hub saw for each of the eight calls.
    GET   /healthz               200 ``{"status": "ok"}``. appmux checks ``/`` by default.

The two POST routes act with the visitor's token, so they accept only a JSON body from this page
(``Sec-Fetch-Site`` absent or ``same-origin``): a form on another site cannot make the visitor's
browser call the hub through this App. No token is ever echoed, logged or returned.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]

VERSION: Final = "1.0.0"
STARTED_AT: Final = datetime.now(UTC)

MCP_PROTOCOL_VERSION: Final = "2025-11-25"
A2A_VERSION: Final = "1.0"
SCIM_ME_PATH: Final = "/api/2.0/preview/scim/v2/Me"
CARD_PATH: Final = ".well-known/agent-card.json"

IDENTITIES: Final = ("me", "app")
PROTOCOLS: Final = ("mcp", "a2a")
ACTIONS: Final = ("discover", "whoami", "databricks_me", "ask")

MAX_TEXT_CHARS: Final = 2_000
MAX_BODY_BYTES: Final = 16 * 1024
ERROR_BODY_CHARS: Final = 200
CALL_TIMEOUT_S: Final = 90.0
COMPLETED: Final = "TASK_STATE_COMPLETED"

EMAIL_HEADER: Final = "x-forwarded-email"
OBO_HEADER: Final = "x-forwarded-access-token"
ANONYMOUS: Final = "anonymous"

SENT_AS: Final = {
    "me": "your own token, forwarded by appmux (OBO)",
    "app": "this App's own service principal",
}


# --- settings and credentials ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    """What the admin can set, from the environment (``app.yaml``, or the release config)."""

    provider_url: str

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(provider_url=os.environ.get("PROVIDER_URL", "").strip())

    @property
    def configured(self) -> bool:
        return bool(self.provider_url)

    @property
    def a2a_url(self) -> str:
        return f"{self.provider_url.rstrip('/')}/" if self.configured else ""

    @property
    def mcp_url(self) -> str:
        return f"{self.provider_url.rstrip('/')}/mcp" if self.configured else ""

    @property
    def card_url(self) -> str:
        return f"{self.a2a_url}{CARD_PATH}" if self.configured else ""


class CredentialsMissing(Exception):
    """This App has no Databricks identity of its own to call anyone with."""


class Credentials(Protocol):
    async def headers(self) -> dict[str, str]: ...

    async def host(self) -> str: ...

    async def describe(self) -> JsonObject: ...


class DatabricksCredentials:
    """The App's own identity, found by the Databricks SDK the way it finds it for any App.

    Standalone: the App's service principal (``DATABRICKS_CLIENT_ID`` and ``..._SECRET``). Under
    appmux: the shared service principal's token command, only when it was delegated. On a laptop:
    ``DATABRICKS_CONFIG_PROFILE`` or ``DATABRICKS_HOST`` with ``DATABRICKS_TOKEN``. The SDK renews
    a token itself; this class only asks it for the headers at the moment of each call.
    """

    def __init__(self) -> None:
        self._config: Any = None

    async def _config_or_raise(self) -> Any:
        if self._config is None:
            # Imported here so the page and the tests work where the SDK is not installed.
            from databricks.sdk.core import Config

            try:
                # Config() resolves the credentials at once and may run the token command.
                self._config = await asyncio.to_thread(Config)
            except ValueError as problem:
                # The SDK's message lists the settings it saw, including the workspace address.
                raise CredentialsMissing(
                    "this App has no Databricks credentials: a standalone App gets its service "
                    "principal from the platform; a child under appmux needs a platform admin to "
                    "delegate the shared service principal to it"
                ) from problem
        return self._config

    async def headers(self) -> dict[str, str]:
        config = await self._config_or_raise()
        return dict(await asyncio.to_thread(config.authenticate))

    async def host(self) -> str:
        config = await self._config_or_raise()
        return str(config.host).rstrip("/")

    async def describe(self) -> JsonObject:
        try:
            config = await self._config_or_raise()
        except CredentialsMissing as problem:
            return {"configured": False, "reason": str(problem)}
        return {"configured": True, "auth_type": config.auth_type}


@dataclass(frozen=True, slots=True)
class Caller:
    """The visitor, from the headers of one request. The token stays out of ``repr``."""

    email: str
    access_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_request(cls, request: Request) -> Caller:
        return cls(
            email=request.headers.get(EMAIL_HEADER, ANONYMOUS),
            access_token=request.headers.get(OBO_HEADER) or None,
        )

    def describe(self) -> JsonObject:
        return {"email": self.email, "user_token_forwarded": self.access_token is not None}


# --- calls to the hub -----------------------------------------------------------------------


class UpstreamError(Exception):
    """A call to the hub cannot be made or failed; the message is safe to show the visitor."""


@dataclass(frozen=True, slots=True)
class CallRequest:
    identity: str
    protocol: str
    action: str
    text: str = ""


class InvalidCall(ValueError):
    """The body of ``POST /api/call`` does not fit this App."""


def parse_call(body: Any) -> CallRequest:
    if not isinstance(body, dict):
        raise InvalidCall("the body must be a JSON object")
    identity, protocol, action = body.get("identity"), body.get("protocol"), body.get("action")
    if identity not in IDENTITIES:
        raise InvalidCall(f"'identity' must be one of {', '.join(IDENTITIES)}")
    if protocol not in PROTOCOLS:
        raise InvalidCall(f"'protocol' must be one of {', '.join(PROTOCOLS)}")
    if action not in ACTIONS:
        raise InvalidCall(f"'action' must be one of {', '.join(ACTIONS)}")
    text = body.get("text", "")
    if action == "ask":
        if protocol == "mcp":
            raise InvalidCall("MCP has no agent to ask: use A2A for 'ask'")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise InvalidCall(f"'text' must be a string of 1 to {MAX_TEXT_CHARS} characters")
    return CallRequest(identity, protocol, action, text if action == "ask" else "")


@dataclass(slots=True)
class CallOutcome:
    request: CallRequest
    sent_as: str
    wire: list[JsonObject] = field(default_factory=list)
    result: str = ""
    error: str | None = None
    provider_saw: JsonObject | None = None
    details: JsonObject | None = None

    def to_json(self) -> JsonObject:
        return {
            "ok": self.error is None,
            "identity": self.request.identity,
            "protocol": self.request.protocol,
            "action": self.request.action,
            "sent_as": self.sent_as,
            "wire": self.wire,
            "result": self.result,
            "error": self.error,
            "provider_saw": self.provider_saw,
            "details": self.details,
        }


def _rpc_result(step: str, response: httpx.Response, request_id: int | str) -> JsonObject:
    """The ``result`` of the JSON-RPC answer with ``request_id``, from a JSON or SSE body."""
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        bodies = [
            line.removeprefix("data:").strip()
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
    else:
        bodies = [response.text]
    for body in bodies:
        try:
            answer = json.loads(body)
        except ValueError:
            continue
        if isinstance(answer, dict) and answer.get("id") == request_id:
            if "error" in answer:
                error = answer["error"]
                raise UpstreamError(f"{step}: {error.get('message', error)}")
            result = answer.get("result")
            if isinstance(result, dict):
                return result
    raise UpstreamError(f"{step}: the answer holds no result for request {request_id}")


def _task_text(task: JsonObject) -> str:
    """The text a task holds: its artifacts, else the message of its status."""
    parts: list[Any] = []
    for artifact in task.get("artifacts", []):
        parts.extend(artifact.get("parts", []))
    if not parts:
        parts = task.get("status", {}).get("message", {}).get("parts", [])
    return "\n".join(
        str(part["text"]) for part in parts if isinstance(part, dict) and "text" in part
    )


class Hub:
    """The hub, reached over HTTP under the identity the visitor picks."""

    def __init__(
        self, settings: Settings, http: httpx.AsyncClient, credentials: Credentials
    ) -> None:
        self._settings = settings
        self._http = http
        self._credentials = credentials

    async def call(self, request: CallRequest, caller: Caller) -> CallOutcome:
        outcome = CallOutcome(request=request, sent_as=SENT_AS[request.identity])
        try:
            if not self._settings.configured:
                raise UpstreamError("PROVIDER_URL is not set: give it the address of agent-hub")
            authorization = await self._authorization(request.identity, caller)
            if request.protocol == "mcp":
                outcome.result = await self._mcp(request, authorization, outcome)
            else:
                outcome.result, outcome.details = await self._a2a(request, authorization, outcome)
        except UpstreamError as problem:
            outcome.error = str(problem)
            return outcome
        if request.action == "whoami":
            try:
                saw = json.loads(outcome.result)
            except ValueError:
                saw = None
            outcome.provider_saw = saw if isinstance(saw, dict) else None
        return outcome

    async def _authorization(self, identity: str, caller: Caller) -> dict[str, str]:
        if identity == "me":
            if caller.access_token is None:
                raise UpstreamError(
                    "no user token came with your request, so there is nothing to send as you. "
                    "A token arrives only when you open this App in a browser as a person and a "
                    "platform admin delegated OBO to it"
                )
            return {"Authorization": f"Bearer {caller.access_token}"}
        try:
            return await self._credentials.headers()
        except CredentialsMissing as problem:
            raise UpstreamError(str(problem)) from problem

    async def _send(
        self,
        step: str,
        method: str,
        url: str,
        headers: dict[str, str],
        wire: list[JsonObject],
        payload: JsonObject | None = None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, url, json=payload, headers=headers)
        except httpx.HTTPError as problem:
            wire.append({"step": step, "status": None})
            raise UpstreamError(
                f"{step}: could not reach the hub ({type(problem).__name__})"
            ) from problem
        wire.append({"step": step, "status": response.status_code})
        # The Apps proxy answers a caller it does not accept with a 302 to its sign-in page, or
        # with 401 `{}` when the request accepts JSON: both mean the same thing.
        if response.is_redirect or response.status_code == httpx.codes.UNAUTHORIZED:
            raise UpstreamError(
                f"{step} answered {response.status_code}, the sign-in answer of the Databricks "
                "proxy: it did not accept this token as a caller, or this identity lacks CAN USE "
                "on the hub's App"
            )
        if response.status_code == httpx.codes.NOT_FOUND:
            raise UpstreamError(
                f"{step} answered 404: the address is wrong, or appmux hides the hub from this "
                "identity because it has no grant on it"
            )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise UpstreamError(
                f"{step} answered {response.status_code}: {response.text[:ERROR_BODY_CHARS]}"
            )
        return response

    # --- MCP --------------------------------------------------------------------------------

    async def _mcp(
        self, request: CallRequest, authorization: dict[str, str], outcome: CallOutcome
    ) -> str:
        url, wire = self._settings.mcp_url, outcome.wire
        headers = {**authorization, "Accept": "application/json, text/event-stream"}
        step = "initialize"
        response = await self._send(
            step,
            "POST",
            url,
            headers,
            wire,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "agent-console", "version": VERSION},
                },
            },
        )
        negotiated = _rpc_result(step, response, 1).get("protocolVersion", MCP_PROTOCOL_VERSION)
        headers["MCP-Protocol-Version"] = str(negotiated)
        await self._send(
            "notifications/initialized",
            "POST",
            url,
            headers,
            wire,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        if request.action == "discover":
            step = "tools/list"
            response = await self._send(
                step, "POST", url, headers, wire, {"jsonrpc": "2.0", "id": 2, "method": step}
            )
            tools = _rpc_result(step, response, 2).get("tools", [])
            return "\n".join(f"{tool['name']}: {tool.get('description', '')}" for tool in tools)
        step = f"tools/call {request.action}"
        response = await self._send(
            step,
            "POST",
            url,
            headers,
            wire,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": request.action, "arguments": {}},
            },
        )
        result = _rpc_result(step, response, 2)
        text = "\n".join(
            str(part["text"])
            for part in result.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        if result.get("isError") is True:
            raise UpstreamError(text)
        return text

    # --- A2A --------------------------------------------------------------------------------

    async def _a2a(
        self, request: CallRequest, authorization: dict[str, str], outcome: CallOutcome
    ) -> tuple[str, JsonObject | None]:
        headers = {**authorization, "A2A-Version": A2A_VERSION, "Accept": "application/json"}
        if request.action == "discover":
            response = await self._send(
                "GET agent card", "GET", self._settings.card_url, headers, outcome.wire
            )
            try:
                card = response.json()
                skills = ", ".join(str(skill["id"]) for skill in card["skills"])
                interface = card["supportedInterfaces"][0]["url"]
                return (
                    f"{card['name']}: {card['description']}\n"
                    f"skills: {skills}\ninterface: {interface}",
                    None,
                )
            except (ValueError, KeyError, IndexError, TypeError) as problem:
                raise UpstreamError("GET agent card: the card is not an A2A 1.0 card") from problem
        if request.action == "ask":
            parts: list[JsonObject] = [{"text": request.text}]
            step = "SendMessage (ask)"
        else:
            directive = {"skill": "call-tool", "tool": request.action, "arguments": {}}
            parts = [{"data": directive}]
            step = f"SendMessage (call-tool {request.action})"
        request_id = str(uuid.uuid4())
        response = await self._send(
            step,
            "POST",
            self._settings.a2a_url,
            headers,
            outcome.wire,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "SendMessage",
                "params": {
                    "message": {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": parts}
                },
            },
        )
        task = _rpc_result(step, response, request_id).get("task")
        if not isinstance(task, dict):
            raise UpstreamError(f"{step}: the answer is not a task")
        text = _task_text(task)
        if task.get("status", {}).get("state") != COMPLETED:
            raise UpstreamError(text or f"{step}: the task ended as {task.get('status')}")
        metadata = task.get("metadata")
        return text, metadata if isinstance(metadata, dict) else None


# --- the identity check ---------------------------------------------------------------------


async def identity_check(hub: Hub, caller: Caller) -> JsonObject:
    """What the hub saw for ``whoami`` and ``databricks_me`` under each protocol and identity."""

    async def cell(protocol: str, identity: str) -> JsonObject:
        whoami = await hub.call(CallRequest(identity, protocol, "whoami"), caller)
        me = await hub.call(CallRequest(identity, protocol, "databricks_me"), caller)
        saw = whoami.provider_saw or {}
        seen_email = saw.get("x_forwarded_email")
        user_name: Any = None
        if me.error is None:
            try:
                user_name = json.loads(me.result).get("user_name")
            except (ValueError, AttributeError):
                user_name = None
        return {
            "protocol": protocol,
            "identity": identity,
            "sent_as": SENT_AS[identity],
            "whoami": {
                "ok": whoami.error is None,
                "error": whoami.error,
                "seen_email": seen_email,
                "token_forwarded": saw.get("access_token_header_present"),
            },
            "databricks_me": {"ok": me.error is None, "user_name": user_name, "error": me.error},
            "same_as_you": (
                str(seen_email).lower() == caller.email.lower() if seen_email is not None else None
            ),
        }

    cells = await asyncio.gather(*(cell(p, i) for p in PROTOCOLS for i in IDENTITIES))
    return {"you": caller.describe(), "cells": list(cells)}


# --- the App --------------------------------------------------------------------------------


def _from_this_page(request: Request) -> bool:
    """A browser says where a request came from; a command-line client says nothing."""
    return request.headers.get("sec-fetch-site") in (None, "same-origin")


async def _json_body(request: Request) -> tuple[Any, JSONResponse | None]:
    """The JSON body of a POST from this page, or the refusal to send."""
    if not _from_this_page(request):
        return None, JSONResponse({"error": "this route accepts calls from its own page only"}, 403)
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return None, JSONResponse({"error": "the body must be application/json"}, 415)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return None, JSONResponse({"error": "the body is too large"}, 413)
    try:
        return json.loads(body), None
    except ValueError:
        return None, JSONResponse({"error": "the body is not JSON"}, 400)


def create_app(settings: Settings, credentials: Credentials, http: httpx.AsyncClient) -> Starlette:
    hub = Hub(settings, http, credentials)

    async def app_identity() -> JsonObject:
        """Who the workspace says this App is: the proof that the 'app' identity works."""
        try:
            headers = await credentials.headers()
            host = await credentials.host()
        except CredentialsMissing as problem:
            return {"configured": False, "reason": str(problem)}
        try:
            response = await http.get(f"{host}{SCIM_ME_PATH}", headers=headers)
            response.raise_for_status()
            me = response.json()
        except (httpx.HTTPError, ValueError) as problem:
            return {
                "configured": True,
                "user_name": None,
                "display_name": None,
                "reason": f"the workspace did not say who this App is ({type(problem).__name__})",
            }
        return {
            "configured": True,
            "user_name": me.get("userName"),
            "display_name": me.get("displayName"),
        }

    async def context_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "version": VERSION,
                "started_at": STARTED_AT.isoformat(),
                "you": Caller.from_request(request).describe(),
                "app_identity": await app_identity(),
                "provider": {
                    "configured": settings.configured,
                    "mcp_url": settings.mcp_url,
                    "a2a_url": settings.a2a_url,
                },
            }
        )

    async def call_endpoint(request: Request) -> JSONResponse:
        body, refusal = await _json_body(request)
        if refusal is not None:
            return refusal
        try:
            call = parse_call(body)
        except InvalidCall as problem:
            return JSONResponse({"error": str(problem)}, status_code=400)
        outcome = await hub.call(call, Caller.from_request(request))
        return JSONResponse(outcome.to_json())

    async def identity_check_endpoint(request: Request) -> JSONResponse:
        _, refusal = await _json_body(request)
        if refusal is not None:
            return refusal
        return JSONResponse(await identity_check(hub, Caller.from_request(request)))

    async def index_page(request: Request) -> HTMLResponse:
        return HTMLResponse(
            _PAGE.replace("__VERSION__", VERSION).replace(
                "__EMAIL__", html.escape(Caller.from_request(request).email)
            )
        )

    async def healthz_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        yield
        await http.aclose()

    return Starlette(
        routes=[
            Route("/", index_page, methods=["GET"]),
            Route("/api/context", context_endpoint, methods=["GET"]),
            Route("/api/call", call_endpoint, methods=["POST"]),
            Route("/api/identity-check", identity_check_endpoint, methods=["POST"]),
            Route("/healthz", healthz_endpoint, methods=["GET"]),
        ],
        lifespan=lifespan,
    )


# The page holds no data of the hub or the visitor except the e-mail the server escapes into it;
# everything else is drawn with ``textContent`` so that nothing a hub answers can become markup.
_PAGE: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>agent-console</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; --line: #8886; --soft: #8881; --good: #1a7f37; --bad: #b42318; }
  body {
    font: 16px/1.5 system-ui, sans-serif;
    max-width: 62rem;
    margin: 2rem auto;
    padding: 0 1rem;
  }
  h1 small { font-weight: 400; font-size: 0.6em; opacity: 0.7; }
  h2 { margin-top: 2rem; }
  code, pre { background: var(--soft); border-radius: 4px; }
  code { padding: 0.1rem 0.3rem; }
  pre { padding: 0.75rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }
  button, select, input { font: inherit; padding: 0.35rem 0.7rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid var(--line); padding: 0.6rem; text-align: left; vertical-align: top; }
  td p { margin: 0 0 0.35rem; }
  .verdict { font-weight: 600; }
  .verdict.good { color: var(--good); }
  .verdict.bad { color: var(--bad); }
  .muted { opacity: 0.75; }
  .row { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: end; }
  label { display: grid; gap: 0.2rem; font-size: 0.9rem; }
  #text-field { flex: 1 1 18rem; }
  #text-field input { width: 100%; box-sizing: border-box; }
</style>
</head>
<body>
<h1>agent-console <small>v__VERSION__</small></h1>
<p>You are <strong>__EMAIL__</strong>. <span id="facts" class="muted">Loading…</span></p>

<h2>Does the hub know who is calling?</h2>
<p>This App calls <code>agent-hub</code> over MCP and over A2A, once with your own token and once
with its own identity, and asks the hub two questions each time: <em>whoami</em> (the identity
headers it received) and <em>databricks_me</em> (a call to Databricks made with the token it
received, so it acts as that caller).</p>
<p><button id="check" type="button">Run the identity check</button></p>
<div id="check-out" aria-live="polite"></div>

<h2>One call</h2>
<form id="one" class="row">
  <label>Protocol
    <select id="protocol"><option value="mcp">MCP</option><option value="a2a">A2A</option></select>
  </label>
  <label>Send as
    <select id="identity">
      <option value="me">me (my token)</option><option value="app">this App</option>
    </select>
  </label>
  <label>Action
    <select id="action">
      <option value="whoami">whoami</option>
      <option value="databricks_me">databricks_me</option>
      <option value="discover">discover</option>
    </select>
  </label>
  <label id="text-field" hidden>Question for the agent
    <input id="text" maxlength="2000" value="Who am I, and who does Databricks think I am?">
  </label>
  <button type="submit">Call the hub</button>
</form>
<div id="one-out" aria-live="polite"></div>

<script>
const $ = (id) => document.getElementById(id);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function postJson(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    data = { error: "The console did not answer with JSON (HTTP " + response.status + ")." };
  }
  return data;
}

async function showContext() {
  const response = await fetch("api/context");
  const context = await response.json();
  const app = context.app_identity;
  const parts = [
    context.you.user_token_forwarded
      ? "Your token reached this App (OBO)."
      : "No user token reached this App, so “me” cannot be sent.",
    app.configured
      ? "This App is " + (app.user_name || "a service principal") + "."
      : "This App has no identity of its own yet.",
    context.provider.configured ? "Hub: " + context.provider.a2a_url : "PROVIDER_URL is not set.",
  ];
  $("facts").textContent = parts.join(" ");
}

function verdict(cell) {
  if (!cell.whoami.ok) return el("p", cell.whoami.error, "verdict bad");
  if (cell.same_as_you) return el("p", "The hub sees you.", "verdict good");
  return el("p", "The hub sees a different identity.", "verdict bad");
}

function cellNode(cell) {
  const td = el("td");
  td.append(verdict(cell));
  if (cell.whoami.ok) {
    const token = cell.whoami.token_forwarded ? ", token present" : ", no token";
    td.append(el("p", "whoami: " + cell.whoami.seen_email + token));
  }
  const me = cell.databricks_me;
  td.append(el("p", "databricks_me: " + (me.ok ? me.user_name : me.error)));
  return td;
}

function checkTable(check) {
  const table = el("table");
  const head = el("tr");
  head.append(el("th", ""), el("th", "As me (my token)"), el("th", "As this App"));
  table.append(head);
  for (const protocol of ["mcp", "a2a"]) {
    const row = el("tr");
    row.append(el("th", protocol.toUpperCase()));
    for (const identity of ["me", "app"]) {
      const cell = check.cells.find((c) => c.protocol === protocol && c.identity === identity);
      row.append(cellNode(cell));
    }
    table.append(row);
  }
  return table;
}

$("check").addEventListener("click", async () => {
  const out = $("check-out");
  out.replaceChildren(el("p", "Calling the hub eight times…", "muted"));
  const check = await postJson("api/identity-check", {});
  out.replaceChildren(check.error ? el("p", check.error, "verdict bad") : checkTable(check));
});

function syncForm() {
  const ask = $("protocol").value === "a2a";
  const action = $("action");
  const hasAsk = [...action.options].some((o) => o.value === "ask");
  if (ask && !hasAsk) action.append(new Option("ask (the model)", "ask"));
  if (!ask && hasAsk) [...action.options].find((o) => o.value === "ask").remove();
  $("text-field").hidden = action.value !== "ask";
}
$("protocol").addEventListener("change", syncForm);
$("action").addEventListener("change", syncForm);
syncForm();

$("one").addEventListener("submit", async (event) => {
  event.preventDefault();
  const out = $("one-out");
  out.replaceChildren(el("p", "Calling the hub…", "muted"));
  const body = {
    identity: $("identity").value,
    protocol: $("protocol").value,
    action: $("action").value,
  };
  if (body.action === "ask") body.text = $("text").value;
  const outcome = await postJson("api/call", body);
  const nodes = [];
  if (outcome.sent_as) nodes.push(el("p", "Sent as " + outcome.sent_as + "."));
  if (outcome.wire && outcome.wire.length) {
    const steps = outcome.wire.map((w) => w.step + " → " + (w.status ?? "no answer"));
    nodes.push(el("p", steps.join("\\n"), "muted"));
  }
  if (outcome.error) nodes.push(el("p", outcome.error, "verdict bad"));
  else nodes.push(el("pre", outcome.result));
  if (outcome.details) nodes.push(el("pre", JSON.stringify(outcome.details, null, 2)));
  out.replaceChildren(...nodes);
});

showContext();
</script>
</body>
</html>
"""


app = create_app(
    Settings.from_environment(),
    DatabricksCredentials(),
    httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=10.0)),
)
