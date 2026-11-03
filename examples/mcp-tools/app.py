"""mcp-tools: a small Model Context Protocol server for trying appmux and Databricks Apps.

It is a standard Databricks App: nothing in it knows about appmux, and the same folder runs on a
standalone App. Started with ``python -m uvicorn app:app``; uvicorn reads its host, port and root
path from ``UVICORN_HOST``, ``UVICORN_PORT`` and ``UVICORN_ROOT_PATH``, which the platform sets
(and appmux sets, design 5.4).

MCP is JSON-RPC 2.0 over HTTP, so this file speaks it by hand on Starlette, which the App
environment already holds. It follows the Streamable HTTP transport of the 2025-11-25 revision in
its simplest legal form: **stateless** (no ``Mcp-Session-Id`` is issued) and every answer is one
JSON body (never a Server-Sent Events stream). A client written for the full transport, such as the
official ``mcp`` package, works against it.

HTTP paths
    GET    /            200 HTML: the endpoint, the tools, who is calling. Relative links only,
                        because appmux never rewrites a body (design 7.1 step 7).
    POST   /mcp         one JSON-RPC message. A request gets 200 with ``application/json``; a
                        notification or a response gets 202 with no body; a bad message gets 400.
    GET    /mcp         405: this server has no stream to offer (the spec allows that).
    DELETE /mcp         405: there is no session to end.
    GET    /api/whoami  200 JSON: the identity headers as they arrive.
    GET    /healthz     200 ``{"status": "ok"}``. appmux's health check asks ``/`` by default.

JSON-RPC methods: ``initialize``, ``ping``, ``tools/list`` and ``tools/call``; the notification
``notifications/initialized`` is accepted and ignored. Tools: ``add``, ``utc_now``, ``word_count``
and ``whoami``. ``whoami`` reports the identity headers of the request that called it, which is how
a caller learns who this server sees at the end of a chain of apps.

Authentication is the Databricks proxy's job (an OAuth bearer and CAN USE for the caller); this app
trusts the headers it forwards and never sees the bearer. The access-token header is reported as
present or absent and never echoed.
"""

from __future__ import annotations

import html
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

VERSION: Final = "1.0.0"
SERVER_NAME: Final = "mcp-tools"
STARTED_AT: Final = datetime.now(UTC)

# Newest first. Without an ``MCP-Protocol-Version`` header the spec says to assume the oldest.
SUPPORTED_PROTOCOL_VERSIONS: Final = ("2025-11-25", "2025-06-18", "2025-03-26")
MAX_BODY_BYTES: Final = 64 * 1024
MAX_TEXT_CHARS: Final = 10_000

# JSON-RPC 2.0 error codes.
PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602

# Headers the proxy sets and appmux forwards (design 7.1). appmux drops any client copy of them.
EMAIL_HEADER: Final = "x-forwarded-email"
USER_HEADER: Final = "x-forwarded-user"
USERNAME_HEADER: Final = "x-forwarded-preferred-username"
PREFIX_HEADER: Final = "x-forwarded-prefix"
REQUEST_ID_HEADER: Final = "x-request-id"
OBO_HEADER: Final = "x-forwarded-access-token"
ANONYMOUS: Final = "anonymous"


@dataclass(frozen=True, slots=True)
class Caller:
    """Who the proxy says is calling, from the headers of one request."""

    email: str
    user: str
    username: str
    prefix: str
    request_id: str
    has_access_token: bool

    @classmethod
    def from_headers(cls, request: Request) -> Caller:
        headers = request.headers
        return cls(
            email=headers.get(EMAIL_HEADER, ANONYMOUS),
            user=headers.get(USER_HEADER, ANONYMOUS),
            username=headers.get(USERNAME_HEADER, ANONYMOUS),
            prefix=headers.get(PREFIX_HEADER, ""),
            request_id=headers.get(REQUEST_ID_HEADER, ""),
            has_access_token=OBO_HEADER in headers,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "x_forwarded_email": self.email,
            "x_forwarded_user": self.user,
            "x_forwarded_preferred_username": self.username,
            "x_forwarded_prefix": self.prefix,
            "x_request_id": self.request_id,
            "access_token_header_present": self.has_access_token,
        }


class ToolArgumentError(ValueError):
    """The arguments do not fit the tool; the model gets the message back and can try again."""


ToolFunction = Callable[[dict[str, Any], Caller], str]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: ToolFunction

    def listing(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _number(arguments: dict[str, Any], key: str) -> float:
    value = arguments.get(key)
    # bool is an int in Python; "true" is not a number to a caller.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ToolArgumentError(f"'{key}' must be a number")
    return float(value)


def _text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str):
        raise ToolArgumentError(f"'{key}' must be a string")
    if len(value) > MAX_TEXT_CHARS:
        raise ToolArgumentError(f"'{key}' is longer than {MAX_TEXT_CHARS} characters")
    return value


def _add(arguments: dict[str, Any], caller: Caller) -> str:
    total = _number(arguments, "a") + _number(arguments, "b")
    return str(int(total)) if total.is_integer() else str(total)


def _utc_now(arguments: dict[str, Any], caller: Caller) -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _word_count(arguments: dict[str, Any], caller: Caller) -> str:
    return str(len(_text(arguments, "text").split()))


def _whoami(arguments: dict[str, Any], caller: Caller) -> str:
    return json.dumps(caller.describe(), indent=2)


TOOLS: Final[dict[str, Tool]] = {
    tool.name: tool
    for tool in (
        Tool(
            name="add",
            description="Add two numbers and return the sum.",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "the first number"},
                    "b": {"type": "number", "description": "the second number"},
                },
                "required": ["a", "b"],
            },
            run=_add,
        ),
        Tool(
            name="utc_now",
            description="Return the current UTC time as an ISO 8601 string.",
            input_schema={"type": "object", "properties": {}},
            run=_utc_now,
        ),
        Tool(
            name="word_count",
            description="Count the words in a text.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "the text to count"}},
                "required": ["text"],
            },
            run=_word_count,
        ),
        Tool(
            name="whoami",
            description=(
                "Return the identity headers this server received with the call: who the "
                "Databricks proxy says is calling. The access token is never shown."
            ),
            input_schema={"type": "object", "properties": {}},
            run=_whoami,
        ),
    )
}


class RpcError(Exception):
    """A JSON-RPC error answer."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _initialize(params: dict[str, Any], caller: Caller) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    # Spec: answer with the requested version if supported, else with the newest this server speaks.
    chosen = (
        requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
    )
    return {
        "protocolVersion": chosen,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": VERSION},
        "instructions": "Small demo tools: add, utc_now, word_count and whoami.",
    }


def _ping(params: dict[str, Any], caller: Caller) -> dict[str, Any]:
    return {}


def _tools_list(params: dict[str, Any], caller: Caller) -> dict[str, Any]:
    return {"tools": [tool.listing() for tool in TOOLS.values()]}


def _tools_call(params: dict[str, Any], caller: Caller) -> dict[str, Any]:
    name = params.get("name")
    # Spec: an unknown tool is a protocol error, not a tool result.
    if not isinstance(name, str) or name not in TOOLS:
        raise RpcError(INVALID_PARAMS, f"unknown tool: {name!r}")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "'arguments' must be an object")
    try:
        text = TOOLS[name].run(arguments, caller)
    except ToolArgumentError as problem:
        # Spec (2025-11-25): bad arguments are a tool execution error, so the model sees it.
        return {"content": [{"type": "text", "text": str(problem)}], "isError": True}
    return {"content": [{"type": "text", "text": text}], "isError": False}


METHODS: Final[dict[str, Callable[[dict[str, Any], Caller], dict[str, Any]]]] = {
    "initialize": _initialize,
    "ping": _ping,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
}


def handle_message(message: Any, caller: Caller) -> dict[str, Any] | None:
    """Answer one decoded JSON-RPC message; ``None`` means a notification or a response (202)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
    method = message.get("method")
    if method is None:
        # A response to a request this server never sent: accept it and say nothing.
        if "result" in message or "error" in message:
            return None
        return _error(message.get("id"), INVALID_REQUEST, "no 'method' in the message")
    if not isinstance(method, str):
        return _error(message.get("id"), INVALID_REQUEST, "'method' must be a string")
    if "id" not in message:
        return None  # a notification, such as notifications/initialized
    request_id = message["id"]
    if isinstance(request_id, bool) or not isinstance(request_id, str | int):
        return _error(None, INVALID_REQUEST, "'id' must be a string or an integer")
    handler = METHODS.get(method)
    if handler is None:
        return _error(request_id, METHOD_NOT_FOUND, f"method not found: {method}")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "'params' must be an object")
    try:
        return {"jsonrpc": "2.0", "id": request_id, "result": handler(params, caller)}
    except RpcError as problem:
        return _error(request_id, problem.code, problem.message)


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _origin_allowed(request: Request) -> bool:
    """Spec: a request with an ``Origin`` is refused unless it is this server's own origin.

    A browser sends one, a command-line client or another App does not. The comparison is on the
    host, against the one the request came in on, so it holds under any appmux prefix.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return True
    own_host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlsplit(origin).netloc == own_host


async def mcp_endpoint(request: Request) -> Response:
    if not _origin_allowed(request):
        return JSONResponse(_error(None, INVALID_REQUEST, "origin not allowed"), status_code=403)
    version = request.headers.get("mcp-protocol-version")
    if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse(
            _error(None, INVALID_REQUEST, f"unsupported MCP-Protocol-Version: {version}"),
            status_code=400,
        )
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return JSONResponse(_error(None, INVALID_REQUEST, "body too large"), status_code=413)
    try:
        message = json.loads(body)
    except ValueError:
        return JSONResponse(_error(None, PARSE_ERROR, "body is not JSON"), status_code=400)
    if isinstance(message, list):
        # 2025-06-18 removed batches; supporting them would be code for nobody.
        return JSONResponse(
            _error(None, INVALID_REQUEST, "batches are not supported"), status_code=400
        )
    answer = handle_message(message, Caller.from_headers(request))
    if answer is None:
        return Response(status_code=202)
    # A broken message gets 400, a well-formed request gets 200 even when the answer is an error.
    status = 400 if answer["id"] is None and "error" in answer else 200
    return JSONResponse(answer, status_code=status)


async def mcp_no_stream(request: Request) -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})


async def whoami_endpoint(request: Request) -> JSONResponse:
    caller = Caller.from_headers(request)
    return JSONResponse(
        {
            **caller.describe(),
            "version": VERSION,
            "started_at": STARTED_AT.isoformat(),
            "appmux_base_path": os.environ.get("APPMUX_BASE_PATH", ""),
            "supported_protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "tools": list(TOOLS),
        }
    )


async def healthz_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


_PAGE: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>mcp-tools</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{
    font: 16px/1.5 system-ui, sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem;
  }}
  code, pre {{ background: #f2f2f2; border-radius: 4px; }}
  code {{ padding: 0.1rem 0.3rem; }}
  pre {{ padding: 0.75rem; overflow-x: auto; }}
</style>
</head>
<body>
<h1>mcp-tools <small>v{version}</small></h1>
<p>An MCP server (Streamable HTTP, stateless, JSON answers). You are <strong>{email}</strong>.</p>
<p>The endpoint is <a href="mcp"><code>mcp</code></a> under this page's address: send it
<code>POST</code> requests of JSON-RPC 2.0. A <code>GET</code> in a browser answers 405 on
purpose.</p>
<h2>Tools</h2>
<ul>
{tools}
</ul>
<h2>Try it</h2>
<pre>curl -s -X POST -H 'Content-Type: application/json' \\
  -H 'Accept: application/json, text/event-stream' \\
  -H "Authorization: Bearer $TOKEN" \\
  -d '{curl_body}' \\
  &lt;this page's address&gt;mcp</pre>
<p><a href="api/whoami">api/whoami</a> · <a href="healthz">healthz</a></p>
</body>
</html>
"""

_CURL_BODY: Final = html.escape(
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
        },
        separators=(",", ":"),
    ),
    quote=False,
)


async def index_page(request: Request) -> HTMLResponse:
    caller = Caller.from_headers(request)
    tools = "\n".join(
        f"<li><code>{html.escape(tool.name)}</code>: {html.escape(tool.description)}</li>"
        for tool in TOOLS.values()
    )
    return HTMLResponse(
        _PAGE.format(
            version=VERSION,
            email=html.escape(caller.email),
            tools=tools,
            curl_body=_CURL_BODY,
        )
    )


app = Starlette(
    routes=[
        Route("/", index_page),
        Route("/mcp", mcp_endpoint, methods=["POST"]),
        Route("/mcp", mcp_no_stream, methods=["GET", "DELETE"]),
        Route("/api/whoami", whoami_endpoint),
        Route("/healthz", healthz_endpoint),
    ]
)
