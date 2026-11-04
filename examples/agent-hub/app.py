"""agent-hub: one App that is an MCP server and an A2A agent, and knows who is calling.

It is a standard Databricks App: nothing in it knows about appmux, and the same folder runs on a
standalone App. Started with ``python -m uvicorn app:app``; uvicorn reads its host, port and root
path from ``UVICORN_HOST``, ``UVICORN_PORT`` and ``UVICORN_ROOT_PATH``, which the platform sets
(and appmux sets, design 5.4).

Both protocols are JSON-RPC 2.0 over HTTP, so this file speaks them by hand on Starlette, which the
App environment already holds: MCP as the stateless Streamable HTTP transport of the 2025-11-25
revision (one JSON body per answer), A2A as 1.0 with the JSON-RPC binding. Clients written for the
full protocols, such as the official ``mcp`` and ``a2a-sdk`` packages, work against it.

One set of tools, two doors
    ``whoami``          the identity headers of the call, as the Databricks proxy forwarded them.
    ``databricks_me``   asks Databricks who the caller is **with the caller's own token** (the
                        ``X-Forwarded-Access-Token`` header), so it is an action taken as that
                        person, not a report of a header.
    ``add``, ``utc_now`` small tools with nothing to do with identity.
    The MCP door (``POST /mcp``) lists and calls them. The A2A door (``POST /``) has a ``call-tool``
    skill that calls one directly and an ``ask`` skill that gives them to a chat model
    (``LLM_ENDPOINT``, a Databricks foundation model) which may call them in a loop. Either way a
    tool runs for the **caller of that request**.

Two identities in one request
    The model is called with the App's **own** identity (the Databricks SDK finds it; under appmux
    that needs the shared service principal delegated to this child, design 8.1). ``databricks_me``
    uses the **caller's** token, which arrives only when a platform admin delegated OBO to this
    child, and only with the scopes the appmux App declares. The token belongs to whoever called:
    a person, or the service principal of an App that called (measured in a browser, 2026-11-04).
    Under appmux that service principal is the shared one, so this App cannot tell which child
    called. Without OBO no token arrives for anyone: ``databricks_me`` then says so.

HTTP paths
    GET    /                              200 HTML: the card, the tools, who is calling.
    POST   /mcp                           one MCP message. GET and DELETE answer 405 (no stream, no
                                          session).
    GET    /.well-known/agent-card.json   200 JSON: the card. A child under appmux serves it at
                                          ``/<slug>/.well-known/agent-card.json`` only.
    POST   /                              ``SendMessage``, ``SendStreamingMessage`` or ``GetTask``.
    GET    /api/status                    200 JSON: the settings and who is calling.
    GET    /healthz                       200 ``{"status": "ok"}``. appmux checks ``/`` by default.

Tasks are kept in memory (the newest ``MAX_TASKS``), per caller, so they vanish when the process is
replaced. No token is ever echoed, logged, stored in a task, or shown to the model.
"""

from __future__ import annotations

import asyncio
import copy
import html
import json
import logging
import os
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]

VERSION: Final = "1.0.0"
STARTED_AT: Final = datetime.now(UTC)

A2A_VERSION: Final = "1.0"
A2A_VERSION_HEADER: Final = "a2a-version"
CARD_PATH: Final = "/.well-known/agent-card.json"
# Newest first. Without an ``MCP-Protocol-Version`` header the spec says to assume the oldest.
MCP_PROTOCOL_VERSIONS: Final = ("2025-11-25", "2025-06-18", "2025-03-26")
DEFAULT_LLM_ENDPOINT: Final = "databricks-gpt-oss-120b"
SCIM_ME_PATH: Final = "/api/2.0/preview/scim/v2/Me"

MAX_TASKS: Final = 100
MAX_MESSAGE_CHARS: Final = 4_000
MAX_BODY_BYTES: Final = 64 * 1024
MAX_TEXT_CHARS: Final = 10_000
MAX_TOOL_ROUNDS: Final = 4
MAX_ANSWER_TOKENS: Final = 600
ERROR_BODY_CHARS: Final = 200
CALL_TIMEOUT_S: Final = 90.0
# A mitigation, not a cure: a pay-per-token endpoint answers 429 once the workspace's request rate
# is exceeded, which a task of a few model calls reaches on the Free Edition (measured 2026-11-03).
RATE_LIMIT_RETRY_DELAYS_S: Final = (2.0, 4.0, 8.0)

# JSON-RPC 2.0 and A2A error codes.
PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
TASK_NOT_FOUND: Final = -32001
UNSUPPORTED_OPERATION: Final = -32004
VERSION_NOT_SUPPORTED: Final = -32009

# Known A2A 1.0 methods this example does not implement.
UNSUPPORTED_METHODS: Final = frozenset(
    {
        "CancelTask",
        "SubscribeToTask",
        "ListTasks",
        "GetExtendedAgentCard",
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
    }
)

# Task states and roles as the protobuf JSON mapping writes them.
SUBMITTED: Final = "TASK_STATE_SUBMITTED"
WORKING: Final = "TASK_STATE_WORKING"
COMPLETED: Final = "TASK_STATE_COMPLETED"
FAILED: Final = "TASK_STATE_FAILED"
ROLE_USER: Final = "ROLE_USER"
ROLE_AGENT: Final = "ROLE_AGENT"

# Headers the proxy sets and appmux forwards (design 7.1). appmux drops any client copy of them.
EMAIL_HEADER: Final = "x-forwarded-email"
USER_HEADER: Final = "x-forwarded-user"
USERNAME_HEADER: Final = "x-forwarded-preferred-username"
PREFIX_HEADER: Final = "x-forwarded-prefix"
REQUEST_ID_HEADER: Final = "x-request-id"
OBO_HEADER: Final = "x-forwarded-access-token"
ANONYMOUS: Final = "anonymous"

SYSTEM_PROMPT: Final = (
    "You are the assistant of agent-hub, a demo of Databricks Apps. Your tools run as the person "
    "talking to you: whoami says who the Databricks proxy told this App is calling, and "
    "databricks_me asks Databricks, with that person's own token, who they are. When a tool can "
    "give the answer, call it instead of guessing. Answer in one or two sentences."
)


# --- settings and credentials ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    """What the admin can set, from the environment (``app.yaml``, or the release config)."""

    agent_name: str
    llm_endpoint: str
    public_url: str

    @classmethod
    def from_environment(cls) -> Settings:
        environment = os.environ
        return cls(
            agent_name=environment.get("AGENT_NAME") or "agent-hub",
            llm_endpoint=environment.get("LLM_ENDPOINT") or DEFAULT_LLM_ENDPOINT,
            # The platform sets DATABRICKS_APP_URL; appmux sets it to <parent>/<slug> for a child.
            public_url=(
                environment.get("A2A_PUBLIC_URL") or environment.get("DATABRICKS_APP_URL", "")
            ),
        )


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
            # Imported here so the card and the tests work where the SDK is not installed.
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


# --- who is calling -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Caller:
    """Who the proxy says is calling, from the headers of one request.

    ``access_token`` is the caller's own token (a person's, or an App's service principal's),
    present only when OBO was delegated to this App. It is kept out of ``repr`` and out of
    ``describe`` so that it cannot reach a log, a task or a model by accident.
    """

    email: str
    user: str
    username: str
    prefix: str
    request_id: str
    access_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_request(cls, request: Request) -> Caller:
        headers = request.headers
        return cls(
            email=headers.get(EMAIL_HEADER, ANONYMOUS),
            user=headers.get(USER_HEADER, ANONYMOUS),
            username=headers.get(USERNAME_HEADER, ANONYMOUS),
            prefix=headers.get(PREFIX_HEADER, ""),
            request_id=headers.get(REQUEST_ID_HEADER, ""),
            access_token=headers.get(OBO_HEADER) or None,
        )

    def describe(self) -> JsonObject:
        return {
            "x_forwarded_email": self.email,
            "x_forwarded_user": self.user,
            "x_forwarded_preferred_username": self.username,
            "x_forwarded_prefix": self.prefix,
            "x_request_id": self.request_id,
            "access_token_header_present": self.access_token is not None,
        }


# --- the tools ------------------------------------------------------------------------------


class ToolError(Exception):
    """A tool cannot do what it was asked; the message is for the caller and the model to read."""


ToolFunction = Callable[[JsonObject, Caller], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: JsonObject
    run: ToolFunction

    def listing(self) -> JsonObject:
        """The tool as MCP lists it."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

    def function(self) -> JsonObject:
        """The tool in the shape the chat route takes for function calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


def _number(arguments: JsonObject, key: str) -> float:
    value = arguments.get(key)
    # bool is an int in Python; "true" is not a number to a caller.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ToolError(f"'{key}' must be a number")
    return float(value)


class Toolbox:
    """The tools, and what they need to act: an HTTP client and the App's own credentials."""

    def __init__(self, http: httpx.AsyncClient, credentials: Credentials) -> None:
        self._http = http
        self._credentials = credentials
        self._tools: dict[str, Tool] = {
            tool.name: tool
            for tool in (
                Tool(
                    name="whoami",
                    description=(
                        "Return the identity headers this App received with the call: who the "
                        "Databricks proxy says is calling. A token is never shown."
                    ),
                    input_schema={"type": "object", "properties": {}},
                    run=self._whoami,
                ),
                Tool(
                    name="databricks_me",
                    description=(
                        "Ask Databricks who the caller is, using the token that came with the "
                        "call. It acts as that caller (a person, or the service principal of an "
                        "App), so it fails when the call carries no user token."
                    ),
                    input_schema={"type": "object", "properties": {}},
                    run=self._databricks_me,
                ),
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
                    run=self._add,
                ),
                Tool(
                    name="utc_now",
                    description="Return the current UTC time as an ISO 8601 string.",
                    input_schema={"type": "object", "properties": {}},
                    run=self._utc_now,
                ),
            )
        }

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def listings(self) -> list[JsonObject]:
        return [tool.listing() for tool in self._tools.values()]

    def functions(self) -> list[JsonObject]:
        return [tool.function() for tool in self._tools.values()]

    async def call(self, name: str, arguments: JsonObject, caller: Caller) -> str:
        if name not in self._tools:
            raise ToolError(f"there is no tool named {name!r}")
        return await self._tools[name].run(arguments, caller)

    async def _whoami(self, arguments: JsonObject, caller: Caller) -> str:
        return json.dumps(caller.describe(), indent=2)

    async def _databricks_me(self, arguments: JsonObject, caller: Caller) -> str:
        if caller.access_token is None:
            raise ToolError(
                "no user token came with this call, so there is no user to act as. A token "
                "arrives only when a platform admin delegated OBO to this App; without it the "
                "proxy forwards none, whoever the caller is"
            )
        try:
            host = await self._credentials.host()
        except CredentialsMissing as problem:
            raise ToolError(str(problem)) from problem
        try:
            response = await self._http.get(
                f"{host}{SCIM_ME_PATH}", headers={"Authorization": f"Bearer {caller.access_token}"}
            )
        except httpx.HTTPError as problem:
            raise ToolError(f"could not reach Databricks ({type(problem).__name__})") from problem
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ToolError(
                f"Databricks answered {response.status_code} to the caller's token (it may lack "
                f"the scope this call needs): {response.text[:ERROR_BODY_CHARS]}"
            )
        try:
            me = response.json()
        except ValueError as problem:
            raise ToolError("Databricks answered something that is not JSON") from problem
        return json.dumps(
            {
                "user_name": me.get("userName"),
                "display_name": me.get("displayName"),
                "id": me.get("id"),
            }
        )

    async def _add(self, arguments: JsonObject, caller: Caller) -> str:
        total = _number(arguments, "a") + _number(arguments, "b")
        return str(int(total)) if total.is_integer() else str(total)

    async def _utc_now(self, arguments: JsonObject, caller: Caller) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- MCP ------------------------------------------------------------------------------------


class RpcError(Exception):
    """A JSON-RPC error answer."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id: Any, code: int, message: str) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class McpServer:
    """The MCP door: stateless, JSON answers, the tools of the toolbox."""

    def __init__(self, settings: Settings, toolbox: Toolbox) -> None:
        self._settings = settings
        self._toolbox = toolbox

    async def handle(self, message: Any, caller: Caller) -> JsonObject | None:
        """Answer one decoded message; ``None`` means a notification or a response (202)."""
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
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "'params' must be an object")
        try:
            result = await self._dispatch(method, params, caller)
        except RpcError as problem:
            return _error(request_id, problem.code, problem.message)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    async def _dispatch(self, method: str, params: JsonObject, caller: Caller) -> JsonObject:
        if method == "initialize":
            requested = params.get("protocolVersion")
            # Spec: the requested version if supported, else the newest this server speaks.
            chosen = requested if requested in MCP_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSIONS[0]
            return {
                "protocolVersion": chosen,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self._settings.agent_name, "version": VERSION},
                "instructions": "Tools that run as the caller: " + ", ".join(self._toolbox.names()),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self._toolbox.listings()}
        if method == "tools/call":
            return await self._call_tool(params, caller)
        raise RpcError(METHOD_NOT_FOUND, f"method not found: {method}")

    async def _call_tool(self, params: JsonObject, caller: Caller) -> JsonObject:
        name = params.get("name")
        # Spec: an unknown tool is a protocol error, not a tool result.
        if not isinstance(name, str) or name not in self._toolbox:
            raise RpcError(INVALID_PARAMS, f"unknown tool: {name!r}")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "'arguments' must be an object")
        try:
            text = await self._toolbox.call(name, arguments, caller)
        except ToolError as problem:
            # Spec (2025-11-25): a failed tool is a tool execution error, so the model sees it.
            return {"content": [{"type": "text", "text": str(problem)}], "isError": True}
        return {"content": [{"type": "text", "text": text}], "isError": False}


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


# --- the model ------------------------------------------------------------------------------


class ModelError(Exception):
    """A call to the model failed; the message is safe to show a caller."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgentError(Exception):
    """The task cannot be answered; the message becomes the failed task's message."""


def _content_text(content: Any) -> str:
    """The text of a chat message: a string, or a list of blocks some models answer with."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


class ChatModel:
    """A Databricks serving endpoint through its OpenAI-compatible chat route, as the App itself."""

    def __init__(self, endpoint: str, http: httpx.AsyncClient, credentials: Credentials) -> None:
        self._endpoint = endpoint
        self._http = http
        self._credentials = credentials

    async def complete(self, messages: list[JsonObject], tools: list[JsonObject]) -> JsonObject:
        payload: JsonObject = {
            "model": self._endpoint,
            "messages": messages,
            "max_tokens": MAX_ANSWER_TOKENS,
            "temperature": 0,
        }
        if tools:
            payload["tools"] = tools
        delays = iter(RATE_LIMIT_RETRY_DELAYS_S)
        while True:
            try:
                response = await self._post(payload)
                break
            except ModelError as problem:
                delay = next(delays, None)
                if problem.status_code != httpx.codes.TOO_MANY_REQUESTS or delay is None:
                    raise
                logger.warning("the model answered 429; trying again in %.0f s", delay)
                await asyncio.sleep(delay)
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as problem:
            raise ModelError(
                f"the model '{self._endpoint}' answered something unexpected"
            ) from problem
        if not isinstance(message, dict):
            raise ModelError(f"the model '{self._endpoint}' answered something unexpected")
        return message

    async def _post(self, payload: JsonObject) -> httpx.Response:
        try:
            headers = await self._credentials.headers()
            url = f"{await self._credentials.host()}/serving-endpoints/chat/completions"
        except CredentialsMissing as problem:
            raise ModelError(str(problem)) from problem
        try:
            response = await self._http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as problem:
            raise ModelError(
                f"the model '{self._endpoint}': could not reach it ({type(problem).__name__})"
            ) from problem
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise ModelError(
                f"the model '{self._endpoint}' answered {response.status_code}: "
                f"{response.text[:ERROR_BODY_CHARS]}",
                status_code=response.status_code,
            )
        return response


# --- the agent ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentRequest:
    text: str
    directive: JsonObject | None


@dataclass(frozen=True, slots=True)
class Step:
    """A note on progress, shown to a streaming caller as a status update."""

    text: str


@dataclass(frozen=True, slots=True)
class Answer:
    text: str
    details: JsonObject


class Agent:
    def __init__(self, settings: Settings, toolbox: Toolbox, chat: ChatModel) -> None:
        self._settings = settings
        self._toolbox = toolbox
        self._chat = chat

    async def respond(self, request: AgentRequest, caller: Caller) -> AsyncIterator[Step | Answer]:
        directive = request.directive or {}
        steps = (
            self._call_tool(directive, caller)
            if directive.get("skill") == "call-tool"
            else self._ask(request.text, caller)
        )
        try:
            async for item in steps:
                yield item
        except ModelError as problem:
            raise AgentError(str(problem)) from problem

    async def _call_tool(
        self, directive: JsonObject, caller: Caller
    ) -> AsyncIterator[Step | Answer]:
        name = directive.get("tool")
        arguments = directive.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise AgentError("'call-tool' needs a string 'tool' and an object 'arguments'")
        if name not in self._toolbox:
            raise AgentError(f"there is no tool named {name!r}")
        yield Step(f"calling tool {name}")
        try:
            text = await self._toolbox.call(name, arguments, caller)
        except ToolError as problem:
            raise AgentError(f"the tool '{name}' failed: {problem}") from problem
        yield Answer(text, {"toolCalls": [{"name": name, "arguments": arguments}]})

    async def _ask(self, text: str, caller: Caller) -> AsyncIterator[Step | Answer]:
        messages: list[JsonObject] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        calls_made: list[JsonObject] = []
        for _ in range(MAX_TOOL_ROUNDS):
            yield Step(f"asking the model '{self._settings.llm_endpoint}'")
            assistant = await self._chat.complete(messages, self._toolbox.functions())
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                yield Answer(
                    _content_text(assistant.get("content")).strip(),
                    {"model": self._settings.llm_endpoint, "toolCalls": calls_made},
                )
                return
            messages.append(
                {
                    "role": "assistant",
                    "content": _content_text(assistant.get("content")),
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                name, arguments, problem = _parse_tool_call(call, self._toolbox)
                if problem is None:
                    yield Step(f"calling tool {name} with {json.dumps(arguments)}")
                    outcome = await self._run_tool(name, arguments, caller)
                    calls_made.append({"name": name, "arguments": arguments, "result": outcome})
                else:
                    outcome = problem
                messages.append(
                    {"role": "tool", "tool_call_id": str(call.get("id", "")), "content": outcome}
                )
        raise AgentError(f"the model was still calling tools after {MAX_TOOL_ROUNDS} rounds")

    async def _run_tool(self, name: str, arguments: JsonObject, caller: Caller) -> str:
        """The result of a tool for the model: a failure is text it can read and answer from."""
        try:
            return await self._toolbox.call(name, arguments, caller)
        except ToolError as problem:
            return f"Error: {problem}"


def _parse_tool_call(call: Any, toolbox: Toolbox) -> tuple[str, JsonObject, str | None]:
    """Name and arguments of a model's tool call, or the message to give the model back.

    The model's output is untrusted input: a tool it invents is never run.
    """
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        return "", {}, "the tool call is malformed"
    name = function.get("name")
    if not isinstance(name, str) or name not in toolbox:
        return "", {}, f"there is no tool named {name!r}"
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except ValueError:
        return name, {}, f"the arguments of '{name}' are not valid JSON"
    if not isinstance(arguments, dict):
        return name, {}, f"the arguments of '{name}' must be a JSON object"
    return name, arguments, None


# --- A2A tasks ------------------------------------------------------------------------------


class InvalidMessage(ValueError):
    """The ``message`` of a request does not fit A2A or this agent."""


def parse_message(message: Any) -> AgentRequest:
    if not isinstance(message, dict):
        raise InvalidMessage("'message' must be an object")
    if message.get("role") != ROLE_USER:
        raise InvalidMessage(f"'message.role' must be {ROLE_USER}")
    if not isinstance(message.get("messageId"), str) or not message["messageId"]:
        raise InvalidMessage("'message.messageId' must be a non-empty string")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) for p in parts):
        raise InvalidMessage("'message.parts' must be a non-empty list of parts")
    text = "\n".join(str(part["text"]) for part in parts if isinstance(part.get("text"), str))
    directive = next((part["data"] for part in parts if isinstance(part.get("data"), dict)), None)
    if not text and directive is None:
        raise InvalidMessage("the message needs a text part or a data part")
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidMessage(f"the text is longer than {MAX_MESSAGE_CHARS} characters")
    return AgentRequest(text=text, directive=directive)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _agent_message(text: str, task_id: str, context_id: str) -> JsonObject:
    return {
        "messageId": uuid.uuid4().hex,
        "contextId": context_id,
        "taskId": task_id,
        "role": ROLE_AGENT,
        "parts": [{"text": text}],
    }


def _status(state: str, message: JsonObject | None = None) -> JsonObject:
    status: JsonObject = {"state": state, "timestamp": _now()}
    if message is not None:
        status["message"] = message
    return status


@dataclass(slots=True)
class StoredTask:
    owner: str
    json: JsonObject


class TaskExecutor:
    """Runs messages as A2A tasks and keeps the newest ones for ``GetTask``."""

    def __init__(self, agent: Agent, max_tasks: int = MAX_TASKS) -> None:
        self._agent = agent
        self._max_tasks = max_tasks
        self._tasks: OrderedDict[str, StoredTask] = OrderedDict()

    def get(self, task_id: str, owner: str) -> JsonObject | None:
        stored = self._tasks.get(task_id)
        # A task of another caller is "not found", not "forbidden": its id is not confirmed.
        if stored is None or stored.owner != owner:
            return None
        return copy.deepcopy(stored.json)

    def _create(self, message: JsonObject, owner: str) -> StoredTask:
        task_id = uuid.uuid4().hex
        context_id = message.get("contextId")
        task: JsonObject = {
            "id": task_id,
            "contextId": context_id
            if isinstance(context_id, str) and context_id
            else uuid.uuid4().hex,
            "status": _status(SUBMITTED),
            "history": [message],
            "metadata": {"caller": owner},
        }
        stored = StoredTask(owner=owner, json=task)
        self._tasks[task_id] = stored
        while len(self._tasks) > self._max_tasks:
            self._tasks.popitem(last=False)
        return stored

    async def execute(
        self, message: JsonObject, request: AgentRequest, caller: Caller
    ) -> AsyncIterator[JsonObject]:
        """Run one message as ``caller``. Yields the ``StreamResponse`` objects of the stream.

        A blocking ``SendMessage`` reads the stream to its end and returns the stored task, so
        there is one code path for both. Whatever goes wrong ends in a ``FAILED`` task: the
        JSON-RPC call itself succeeded. The caller's token lives only in ``caller``, which is never
        written into the task.
        """
        stored = self._create(message, caller.email)
        task = stored.json
        task_id, context_id = task["id"], task["contextId"]
        yield {"task": copy.deepcopy(task)}

        def update(state: str, text: str | None = None) -> JsonObject:
            note = _agent_message(text, task_id, context_id) if text else None
            task["status"] = _status(state, note)
            return {
                "statusUpdate": {
                    "taskId": task_id,
                    "contextId": context_id,
                    "status": copy.deepcopy(task["status"]),
                }
            }

        yield update(WORKING)
        try:
            async for item in self._agent.respond(request, caller):
                if isinstance(item, Step):
                    yield update(WORKING, item.text)
                    continue
                artifact = {
                    "artifactId": uuid.uuid4().hex,
                    "name": "answer",
                    "parts": [{"text": item.text}],
                }
                task["artifacts"] = [artifact]
                task["metadata"].update(item.details)
                yield {
                    "artifactUpdate": {
                        "taskId": task_id,
                        "contextId": context_id,
                        "artifact": copy.deepcopy(artifact),
                        "lastChunk": True,
                    }
                }
        except AgentError as problem:
            yield update(FAILED, str(problem))
            return
        except Exception:
            # The last resort of a task: whatever broke, it must end as FAILED and not hang.
            logger.exception("task %s failed unexpectedly", task_id)
            yield update(FAILED, "the agent failed unexpectedly; see the App's log")
            return
        yield update(COMPLETED)


# --- the App --------------------------------------------------------------------------------


def _sse(request_id: Any, result: JsonObject) -> str:
    return f"data: {json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result})}\n\n"


async def _stream(request_id: Any, events: AsyncIterator[JsonObject]) -> AsyncIterator[str]:
    async for event in events:
        yield _sse(request_id, event)


def _base_url(settings: Settings, request: Request) -> str:
    return (settings.public_url or str(request.base_url)).rstrip("/")


def agent_card(settings: Settings, base_url: str) -> JsonObject:
    """The Agent Card (A2A 1.0): one JSON-RPC interface at the App's own address."""
    return {
        "name": settings.agent_name,
        "description": (
            "A demo agent: a Databricks foundation model whose tools run as the person calling "
            "it. The same tools are an MCP server at mcp under this address."
        ),
        "version": VERSION,
        "supportedInterfaces": [
            {"url": f"{base_url}/", "protocolBinding": "JSONRPC", "protocolVersion": A2A_VERSION}
        ],
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "ask",
                "name": "Ask",
                "description": "Answer a question; the model may call the tools as the caller.",
                "tags": ["llm", "identity"],
                "examples": ["Who am I?", "Who does Databricks think I am?"],
            },
            {
                "id": "call-tool",
                "name": "Call one tool",
                "description": (
                    "A data part {skill: call-tool, tool, arguments} calls one tool, no model."
                ),
                "tags": ["identity"],
            },
        ],
    }


def create_app(settings: Settings, credentials: Credentials, http: httpx.AsyncClient) -> Starlette:
    toolbox = Toolbox(http, credentials)
    mcp = McpServer(settings, toolbox)
    executor = TaskExecutor(
        Agent(settings, toolbox, ChatModel(settings.llm_endpoint, http, credentials))
    )

    async def card_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(agent_card(settings, _base_url(settings, request)))

    async def mcp_endpoint(request: Request) -> Response:
        if not _origin_allowed(request):
            return JSONResponse(
                _error(None, INVALID_REQUEST, "origin not allowed"), status_code=403
            )
        version = request.headers.get("mcp-protocol-version")
        if version is not None and version not in MCP_PROTOCOL_VERSIONS:
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
        answer = await mcp.handle(message, Caller.from_request(request))
        if answer is None:
            return Response(status_code=202)
        # A broken message gets 400; a well-formed request gets 200 even if the answer is an error.
        status = 400 if answer["id"] is None and "error" in answer else 200
        return JSONResponse(answer, status_code=status)

    async def mcp_no_stream(request: Request) -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    async def rpc_endpoint(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(_error(None, INVALID_REQUEST, "body too large"), status_code=413)
        try:
            message = json.loads(body)
        except ValueError:
            return JSONResponse(_error(None, PARSE_ERROR, "body is not JSON"), status_code=400)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return JSONResponse(_error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 request"))
        request_id = message.get("id")
        method = message.get("method")
        if isinstance(request_id, bool) or not isinstance(request_id, str | int):
            return JSONResponse(_error(None, INVALID_REQUEST, "'id' must be a string or integer"))
        if not isinstance(method, str):
            return JSONResponse(_error(request_id, INVALID_REQUEST, "'method' must be a string"))
        version = request.headers.get(A2A_VERSION_HEADER)
        # Spec: an empty version means 0.3. This example speaks 1.0 only and says so.
        if version is not None and version != A2A_VERSION:
            return JSONResponse(
                _error(request_id, VERSION_NOT_SUPPORTED, f"this agent speaks A2A {A2A_VERSION}")
            )
        params = message.get("params", {})
        if not isinstance(params, dict):
            return JSONResponse(_error(request_id, INVALID_PARAMS, "'params' must be an object"))
        caller = Caller.from_request(request)

        if method in ("SendMessage", "SendStreamingMessage"):
            try:
                agent_request = parse_message(params.get("message"))
            except InvalidMessage as problem:
                return JSONResponse(_error(request_id, INVALID_PARAMS, str(problem)))
            events = executor.execute(params["message"], agent_request, caller)
            if method == "SendStreamingMessage":
                return StreamingResponse(
                    _stream(request_id, events),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            task_id = ""
            async for event in events:
                if "task" in event:
                    task_id = event["task"]["id"]
            task = executor.get(task_id, caller.email)
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"task": task}})
        if method == "GetTask":
            task_id = params.get("id")
            found = executor.get(task_id, caller.email) if isinstance(task_id, str) else None
            if found is None:
                return JSONResponse(_error(request_id, TASK_NOT_FOUND, "no such task"))
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": found})
        if method in UNSUPPORTED_METHODS:
            return JSONResponse(
                _error(
                    request_id, UNSUPPORTED_OPERATION, f"{method} is not supported by this agent"
                )
            )
        return JSONResponse(_error(request_id, METHOD_NOT_FOUND, f"method not found: {method}"))

    async def status_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "version": VERSION,
                "started_at": STARTED_AT.isoformat(),
                "agent_name": settings.agent_name,
                "llm_endpoint": settings.llm_endpoint,
                "tools": toolbox.names(),
                "credentials": await credentials.describe(),
                **Caller.from_request(request).describe(),
                "appmux_base_path": os.environ.get("APPMUX_BASE_PATH", ""),
            }
        )

    async def index_page(request: Request) -> HTMLResponse:
        caller = Caller.from_request(request)
        tools = "\n".join(
            f"<li><code>{html.escape(tool['name'])}</code>: {html.escape(tool['description'])}</li>"
            for tool in toolbox.listings()
        )
        return HTMLResponse(
            _PAGE.format(
                version=VERSION,
                name=html.escape(settings.agent_name),
                email=html.escape(caller.email),
                token="yes" if caller.access_token else "no",
                llm=html.escape(settings.llm_endpoint),
                tools=tools,
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
            Route("/", rpc_endpoint, methods=["POST"]),
            Route("/mcp", mcp_endpoint, methods=["POST"]),
            Route("/mcp", mcp_no_stream, methods=["GET", "DELETE"]),
            Route(CARD_PATH, card_endpoint),
            Route("/api/status", status_endpoint),
            Route("/healthz", healthz_endpoint),
        ],
        lifespan=lifespan,
    )


_PAGE: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>agent-hub</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{
    font: 16px/1.5 system-ui, sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem;
  }}
  code {{ background: #f2f2f2; border-radius: 4px; padding: 0.1rem 0.3rem; }}
</style>
</head>
<body>
<h1>{name} <small>v{version}</small></h1>
<p>An MCP server and an A2A agent in one App. You are <strong>{email}</strong>; a user token
came with this request: <strong>{token}</strong>.</p>
<ul>
<li>MCP: send <code>POST</code> requests of JSON-RPC 2.0 to <a href="mcp"><code>mcp</code></a>
under this page's address. A <code>GET</code> in a browser answers 405 on purpose.</li>
<li>A2A: <a href=".well-known/agent-card.json"><code>.well-known/agent-card.json</code></a>, then
<code>POST</code> to this page's address.</li>
<li>Model: <code>{llm}</code></li>
</ul>
<h2>Tools</h2>
<ul>
{tools}
</ul>
<p>Every tool runs for whoever sent the request. <a href="api/status">api/status</a> ·
<a href="healthz">healthz</a></p>
</body>
</html>
"""


app = create_app(
    Settings.from_environment(),
    DatabricksCredentials(),
    httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=10.0)),
)
