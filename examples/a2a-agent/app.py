"""a2a-agent: an Agent2Agent server whose agent is a Databricks foundation model with MCP tools.

It is a standard Databricks App: nothing in it knows about appmux, and the same folder runs on a
standalone App. Started with ``python -m uvicorn app:app``; uvicorn reads its host, port and root
path from ``UVICORN_HOST``, ``UVICORN_PORT`` and ``UVICORN_ROOT_PATH``, which the platform sets
(and appmux sets, design 5.4).

A2A is JSON-RPC 2.0 over HTTP, so this file speaks it by hand on Starlette, which the App
environment already holds. It follows A2A 1.0 with the JSON-RPC binding. A client written for that
version, such as the official ``a2a-sdk``, works against it.

What the agent does
    A message with a text part is answered by a chat model from the Databricks free foundation
    models (``LLM_ENDPOINT``). When ``MCP_SERVER_URL`` names an MCP server, the model is offered
    that server's tools and may call them, so one answer can cross three hops: a caller, this App,
    an MCP App. Two other skills skip the model so a hop can be tested alone: a ``data`` part
    ``{"skill": "call-tool", "tool": "...", "arguments": {...}}`` calls one MCP tool, and
    ``{"skill": "relay", "text": "..."}`` sends the text to another A2A agent (``A2A_PEER_URL``).

HTTP paths
    GET  /                              200 HTML: the card, the settings, who is calling.
    GET  /.well-known/agent-card.json   200 JSON: the card. A child under appmux serves it at
                                        ``/<slug>/.well-known/agent-card.json`` only: appmux
                                        routes nothing at the root of the host.
    POST /                              one JSON-RPC request: ``SendMessage`` (the answer as a
                                        task), ``SendStreamingMessage`` (Server-Sent Events: the
                                        task, then a status update per step, the answer, the end)
                                        or ``GetTask``. Other A2A methods answer "unsupported".
    GET  /api/status                    200 JSON: the settings and whether this App has credentials.
    GET  /healthz                       200 ``{"status": "ok"}``. appmux checks ``/`` by default.

Calls this App makes use its **own** identity, never the caller's: the Databricks SDK finds it
(``Config()``). A standalone App has its service principal; a child under appmux has none unless a
platform admin delegates the shared one (design 8.1), which appmux hands over as a token command
that the same SDK call uses. A redirect from a peer is reported, never followed: the Databricks
proxy answers 302 to its sign-in page (or 401 ``{}`` when the request accepts JSON) when it does not
accept the token it was given, or when that identity lacks CAN USE on the App it calls.

Tasks are kept in memory (the newest ``MAX_TASKS``), per caller, so they vanish when the process is
replaced. The access token is never echoed.
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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

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
MCP_PROTOCOL_VERSION: Final = "2025-11-25"
DEFAULT_LLM_ENDPOINT: Final = "databricks-gpt-oss-120b"

MAX_TASKS: Final = 100
MAX_MESSAGE_CHARS: Final = 4_000
MAX_BODY_BYTES: Final = 64 * 1024
MAX_TOOL_ROUNDS: Final = 4
MAX_ANSWER_TOKENS: Final = 600
PEER_ERROR_CHARS: Final = 200
CALL_TIMEOUT_S: Final = 90.0
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
PREFIX_HEADER: Final = "x-forwarded-prefix"
REQUEST_ID_HEADER: Final = "x-request-id"
OBO_HEADER: Final = "x-forwarded-access-token"
ANONYMOUS: Final = "anonymous"

SYSTEM_PROMPT: Final = (
    "You are a small assistant in a demo of Databricks Apps. When a tool can give the answer, "
    "call it instead of guessing. Answer in one or two sentences."
)


# --- settings and credentials ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    """What the admin can set, from the environment (``app.yaml``, or the release config)."""

    agent_name: str
    llm_endpoint: str
    mcp_server_url: str
    peer_url: str
    public_url: str

    @classmethod
    def from_environment(cls) -> Settings:
        environment = os.environ
        return cls(
            agent_name=environment.get("AGENT_NAME") or "a2a-agent",
            llm_endpoint=environment.get("LLM_ENDPOINT") or DEFAULT_LLM_ENDPOINT,
            mcp_server_url=environment.get("MCP_SERVER_URL", ""),
            peer_url=environment.get("A2A_PEER_URL", ""),
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


# --- calls to other Apps and to the model ---------------------------------------------------


class PeerError(Exception):
    """A call to another App or to the model failed; the message is safe to show a caller."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AgentError(Exception):
    """The task cannot be answered; the message becomes the failed task's message."""


class Peers:
    """Authenticated JSON calls to other Apps, with errors a person can act on."""

    def __init__(self, http: httpx.AsyncClient, credentials: Credentials) -> None:
        self._http = http
        self._credentials = credentials

    async def post(
        self, what: str, url: str, payload: JsonObject, headers: dict[str, str]
    ) -> httpx.Response:
        try:
            authorization = await self._credentials.headers()
        except CredentialsMissing as problem:
            raise PeerError(str(problem)) from problem
        try:
            response = await self._http.post(
                url, json=payload, headers={**authorization, **headers}
            )
        except httpx.HTTPError as problem:
            raise PeerError(f"{what}: could not reach it ({type(problem).__name__})") from problem
        # The Apps proxy answers a caller it does not accept with a 302 to its sign-in page, or
        # with 401 `{}` when the request accepts JSON: both mean the same thing.
        if response.is_redirect or response.status_code == httpx.codes.UNAUTHORIZED:
            raise PeerError(
                f"{what} answered {response.status_code}, the sign-in answer of the Databricks "
                "proxy: it did not accept this App's token as a caller. The caller needs an "
                "OAuth token the proxy accepts and CAN USE on that App",
                status_code=response.status_code,
            )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise PeerError(
                f"{what} answered {response.status_code}: {response.text[:PEER_ERROR_CHARS]}",
                status_code=response.status_code,
            )
        return response


def _rpc_result(what: str, response: httpx.Response, request_id: int | str) -> JsonObject:
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
                raise PeerError(f"{what}: {error.get('message', error)}")
            result = answer.get("result")
            if isinstance(result, dict):
                return result
    raise PeerError(f"{what}: the answer holds no result for request {request_id}")


@dataclass(frozen=True, slots=True)
class ToolResult:
    text: str
    is_error: bool


class McpClient:
    """The few MCP calls the agent needs, over Streamable HTTP.

    It does the handshake a real client does and keeps a ``Mcp-Session-Id`` if the server issues
    one, so it works against a stateless server (``examples/mcp-tools``) and a stateful one. Each
    task makes its own client; nothing is cached between tasks.
    """

    def __init__(self, url: str, peers: Peers) -> None:
        self._url = url
        self._peers = peers
        self._next_id = 0
        self._headers: dict[str, str] = {"Accept": "application/json, text/event-stream"}

    async def _send(self, message: JsonObject) -> httpx.Response:
        return await self._peers.post("the MCP server", self._url, message, self._headers)

    async def initialize(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "a2a-agent", "version": VERSION},
            },
        )
        self._headers["MCP-Protocol-Version"] = str(result.get("protocolVersion", ""))
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _request(self, method: str, params: JsonObject) -> JsonObject:
        self._next_id += 1
        request_id = self._next_id
        response = await self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        session = response.headers.get("mcp-session-id")
        if session:
            self._headers["Mcp-Session-Id"] = session
        return _rpc_result(f"the MCP server ({method})", response, request_id)

    async def list_tools(self) -> list[JsonObject]:
        result = await self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise PeerError("the MCP server (tools/list): the answer has no tool list")
        return [
            tool for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        ]

    async def call_tool(self, name: str, arguments: JsonObject) -> ToolResult:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content")
        parts = content if isinstance(content, list) else []
        text = "\n".join(
            str(part["text"])
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text" and "text" in part
        )
        return ToolResult(text=text, is_error=result.get("isError") is True)


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
    """A Databricks serving endpoint through its OpenAI-compatible chat route."""

    def __init__(self, endpoint: str, peers: Peers, credentials: Credentials) -> None:
        self._endpoint = endpoint
        self._peers = peers
        self._credentials = credentials

    async def complete(self, messages: list[JsonObject], tools: list[JsonObject]) -> JsonObject:
        try:
            url = f"{await self._credentials.host()}/serving-endpoints/chat/completions"
        except CredentialsMissing as problem:
            raise PeerError(str(problem)) from problem
        payload: JsonObject = {
            "model": self._endpoint,
            "messages": messages,
            "max_tokens": MAX_ANSWER_TOKENS,
            "temperature": 0,
        }
        if tools:
            payload["tools"] = tools
        what = f"the model '{self._endpoint}'"
        # A mitigation, not a cure: a pay-per-token endpoint answers 429 once the workspace's
        # request rate is exceeded, which a task of a few model calls reaches on the Free Edition
        # (measured 2026-11-03: 4 of 8 relayed tasks failed so). Other errors are not retried.
        delays = iter(RATE_LIMIT_RETRY_DELAYS_S)
        while True:
            try:
                response = await self._peers.post(what, url, payload, {})
                break
            except PeerError as problem:
                delay = next(delays, None)
                if problem.status_code != httpx.codes.TOO_MANY_REQUESTS or delay is None:
                    raise
                logger.warning("%s answered 429; trying again in %.0f s", what, delay)
                await asyncio.sleep(delay)
        try:
            message = response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as problem:
            raise PeerError(
                f"the model '{self._endpoint}' answered something unexpected"
            ) from problem
        if not isinstance(message, dict):
            raise PeerError(f"the model '{self._endpoint}' answered something unexpected")
        return message


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
    def __init__(
        self,
        settings: Settings,
        peers: Peers,
        credentials: Credentials,
        new_mcp: Callable[[str, Peers], McpClient] = McpClient,
    ) -> None:
        self._settings = settings
        self._peers = peers
        self._chat = ChatModel(settings.llm_endpoint, peers, credentials)
        self._new_mcp = new_mcp

    async def respond(self, request: AgentRequest) -> AsyncIterator[Step | Answer]:
        directive = request.directive or {}
        skill = directive.get("skill")
        try:
            if skill == "call-tool":
                steps = self._call_tool(directive)
            elif skill == "relay":
                steps = self._relay(directive)
            else:
                steps = self._ask(request.text)
            async for item in steps:
                yield item
        except PeerError as problem:
            raise AgentError(str(problem)) from problem

    def _mcp(self) -> McpClient:
        if not self._settings.mcp_server_url:
            raise AgentError("no MCP server is set: set MCP_SERVER_URL to its /mcp address")
        return self._new_mcp(self._settings.mcp_server_url, self._peers)

    async def _call_tool(self, directive: JsonObject) -> AsyncIterator[Step | Answer]:
        name = directive.get("tool")
        arguments = directive.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise AgentError("'call-tool' needs a string 'tool' and an object 'arguments'")
        mcp = self._mcp()
        yield Step(f"calling tool {name}")
        await mcp.initialize()
        result = await mcp.call_tool(name, arguments)
        if result.is_error:
            raise AgentError(f"the tool '{name}' failed: {result.text}")
        yield Answer(result.text, {"toolCalls": [{"name": name, "arguments": arguments}]})

    async def _ask(self, text: str) -> AsyncIterator[Step | Answer]:
        tools: list[JsonObject] = []
        mcp: McpClient | None = None
        if self._settings.mcp_server_url:
            mcp = self._mcp()
            yield Step("listing the tools of the MCP server")
            await mcp.initialize()
            tools = [_function_for(tool) for tool in await mcp.list_tools()]
        known = {tool["function"]["name"] for tool in tools}
        messages: list[JsonObject] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        calls_made: list[JsonObject] = []
        for _ in range(MAX_TOOL_ROUNDS):
            yield Step(f"asking the model '{self._settings.llm_endpoint}'")
            assistant = await self._chat.complete(messages, tools)
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
                name, arguments, problem = _parse_tool_call(call, known)
                if problem is None and mcp is not None:
                    yield Step(f"calling tool {name} with {json.dumps(arguments)}")
                    result = await mcp.call_tool(name, arguments)
                    outcome = result.text
                    calls_made.append({"name": name, "arguments": arguments, "result": result.text})
                else:
                    outcome = problem or "no tools are available"
                messages.append(
                    {"role": "tool", "tool_call_id": str(call.get("id", "")), "content": outcome}
                )
        raise AgentError(f"the model was still calling tools after {MAX_TOOL_ROUNDS} rounds")

    async def _relay(self, directive: JsonObject) -> AsyncIterator[Step | Answer]:
        text = directive.get("text")
        if not isinstance(text, str) or not text:
            raise AgentError("'relay' needs a non-empty string 'text'")
        if not self._settings.peer_url:
            raise AgentError("no peer agent is set: set A2A_PEER_URL to its interface address")
        yield Step("sending the text to the peer agent")
        request_id = str(uuid.uuid4())
        response = await self._peers.post(
            "the peer agent",
            self._settings.peer_url,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "SendMessage",
                "params": {
                    "message": {
                        "messageId": uuid.uuid4().hex,
                        "role": ROLE_USER,
                        "parts": [{"text": text}],
                    }
                },
            },
            {"A2A-Version": A2A_VERSION, "Accept": "application/json"},
        )
        result = _rpc_result("the peer agent (SendMessage)", response, request_id)
        task = result.get("task")
        if not isinstance(task, dict):
            raise AgentError("the peer agent answered with something that is not a task")
        state = task.get("status", {}).get("state")
        answer = _task_text(task)
        if state != COMPLETED:
            raise AgentError(f"the peer agent's task ended as {state}: {answer}")
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        yield Answer(answer, {"peer": {"taskId": task.get("id"), "caller": metadata.get("caller")}})


def _function_for(tool: JsonObject) -> JsonObject:
    """An MCP tool in the shape the chat route takes for function calling."""
    schema = tool.get("inputSchema")
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": str(tool.get("description", "")),
            "parameters": schema if isinstance(schema, dict) else {"type": "object"},
        },
    }


def _parse_tool_call(call: Any, known: set[str]) -> tuple[str, JsonObject, str | None]:
    """Name and arguments of a model's tool call, or the message to give the model back.

    The model's output is untrusted input: a tool it invents is never sent to the server.
    """
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        return "", {}, "the tool call is malformed"
    name = function.get("name")
    if not isinstance(name, str) or name not in known:
        return "", {}, f"there is no tool named {name!r}"
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except ValueError:
        return name, {}, f"the arguments of '{name}' are not valid JSON"
    if not isinstance(arguments, dict):
        return name, {}, f"the arguments of '{name}' must be a JSON object"
    return name, arguments, None


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


# --- tasks ----------------------------------------------------------------------------------


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
        self, message: JsonObject, request: AgentRequest, owner: str
    ) -> AsyncIterator[JsonObject]:
        """Run one message. Yields the ``StreamResponse`` objects of the streaming method.

        A blocking ``SendMessage`` reads the stream to its end and returns the stored task, so
        there is one code path for both. Whatever goes wrong ends in a ``FAILED`` task: the
        JSON-RPC call itself succeeded.
        """
        stored = self._create(message, owner)
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
            async for item in self._agent.respond(request):
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


# --- the A2A layer --------------------------------------------------------------------------


def _error(request_id: Any, code: int, message: str) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _sse(request_id: Any, result: JsonObject) -> str:
    return f"data: {json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result})}\n\n"


def _owner(request: Request) -> str:
    return request.headers.get(EMAIL_HEADER, ANONYMOUS)


def _base_url(settings: Settings, request: Request) -> str:
    return (settings.public_url or str(request.base_url)).rstrip("/")


def agent_card(settings: Settings, base_url: str) -> JsonObject:
    """The Agent Card (A2A 1.0): one JSON-RPC interface at the App's own address."""
    return {
        "name": settings.agent_name,
        "description": (
            "A demo agent: a Databricks foundation model that may call the tools of an MCP "
            "server. It also calls one tool directly or relays a message to another agent."
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
                "description": "Answer a question; the model may call the MCP server's tools.",
                "tags": ["llm", "mcp"],
                "examples": ["What is 17 plus 25?"],
            },
            {
                "id": "call-tool",
                "name": "Call one MCP tool",
                "description": (
                    "A data part {skill: call-tool, tool, arguments} calls one tool, no model."
                ),
                "tags": ["mcp"],
            },
            {
                "id": "relay",
                "name": "Relay to another agent",
                "description": "A data part {skill: relay, text} is sent to the peer agent.",
                "tags": ["a2a"],
            },
        ],
    }


def create_app(settings: Settings, credentials: Credentials, http: httpx.AsyncClient) -> Starlette:
    peers = Peers(http, credentials)
    executor = TaskExecutor(Agent(settings, peers, credentials))

    async def card_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(agent_card(settings, _base_url(settings, request)))

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
        owner = _owner(request)

        if method in ("SendMessage", "SendStreamingMessage"):
            try:
                agent_request = parse_message(params.get("message"))
            except InvalidMessage as problem:
                return JSONResponse(_error(request_id, INVALID_PARAMS, str(problem)))
            events = executor.execute(params["message"], agent_request, owner)
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
            task = executor.get(task_id, owner)
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"task": task}})
        if method == "GetTask":
            task_id = params.get("id")
            task = executor.get(task_id, owner) if isinstance(task_id, str) else None
            if task is None:
                return JSONResponse(_error(request_id, TASK_NOT_FOUND, "no such task"))
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": task})
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
                "mcp_server_configured": bool(settings.mcp_server_url),
                "peer_configured": bool(settings.peer_url),
                "credentials": await credentials.describe(),
                "x_forwarded_email": _owner(request),
                "access_token_header_present": OBO_HEADER in request.headers,
                "x_forwarded_prefix": request.headers.get(PREFIX_HEADER, ""),
                "x_request_id": request.headers.get(REQUEST_ID_HEADER, ""),
                "appmux_base_path": os.environ.get("APPMUX_BASE_PATH", ""),
            }
        )

    async def index_page(request: Request) -> HTMLResponse:
        return HTMLResponse(
            _PAGE.format(
                version=VERSION,
                name=html.escape(settings.agent_name),
                email=html.escape(_owner(request)),
                llm=html.escape(settings.llm_endpoint),
                mcp=html.escape(settings.mcp_server_url or "(not set)"),
                peer=html.escape(settings.peer_url or "(not set)"),
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
            Route(CARD_PATH, card_endpoint),
            Route("/api/status", status_endpoint),
            Route("/healthz", healthz_endpoint),
        ],
        lifespan=lifespan,
    )


async def _stream(request_id: Any, events: AsyncIterator[JsonObject]) -> AsyncIterator[str]:
    async for event in events:
        yield _sse(request_id, event)


_PAGE: Final = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>a2a-agent</title>
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
<p>An A2A agent (JSON-RPC, protocol 1.0). You are <strong>{email}</strong>.</p>
<ul>
<li>Agent card:
<a href=".well-known/agent-card.json"><code>.well-known/agent-card.json</code></a></li>
<li>Model: <code>{llm}</code></li>
<li>MCP server: <code>{mcp}</code></li>
<li>Peer agent: <code>{peer}</code></li>
</ul>
<p>Send <code>SendMessage</code> or <code>SendStreamingMessage</code> with a <code>POST</code>
to this page's address. <a href="api/status">api/status</a> ·
<a href="healthz">healthz</a></p>
</body>
</html>
"""


app = create_app(
    Settings.from_environment(),
    DatabricksCredentials(),
    httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=10.0)),
)
