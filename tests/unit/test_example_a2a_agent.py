"""The ``examples/a2a-agent`` A2A server: the protocol it speaks and how its agent behaves.

The example is a standalone Databricks App, not part of the ``appmux`` package, so the module is
loaded from its folder. Its calls to other Apps run in process: the real ``examples/mcp-tools``
app and a second copy of the agent sit behind a transport that picks the app by host name, and the
chat model is a script. The official ``a2a-sdk`` client is not a dependency of this project; a run
against it is described in ``examples/a2a-agent/README.md``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
BEARER = "Bearer test-token"
WORKSPACE = "https://workspace.example"
MCP_URL = "https://mcp.example/mcp"
PEER_URL = "https://peer.example/"
PUBLIC_URL = "https://agent.example"


def _load(name: str, folder: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / folder / "app.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their own module up here
    spec.loader.exec_module(module)
    return module


agent_module = _load("example_a2a_agent", "a2a-agent")
mcp_module = _load("example_mcp_tools_for_agent", "mcp-tools")


class FakeCredentials:
    """Stands for the App's own identity; ``missing`` is an App nobody gave one."""

    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing

    async def headers(self) -> dict[str, str]:
        if self.missing:
            raise agent_module.CredentialsMissing("this App has no Databricks credentials")
        return {"Authorization": BEARER}

    async def host(self) -> str:
        if self.missing:
            raise agent_module.CredentialsMissing("this App has no Databricks credentials")
        return WORKSPACE

    async def describe(self) -> dict[str, Any]:
        if self.missing:
            return {"configured": False, "reason": "this App has no Databricks credentials"}
        return {"configured": True, "auth_type": "fake"}


@dataclass
class Network(httpx.AsyncBaseTransport):
    """Routes a call to the app behind its host name and keeps what was sent."""

    hosts: dict[str, httpx.AsyncBaseTransport]
    seen: list[httpx.Request] = field(default_factory=list)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return await self.hosts[request.url.host].handle_async_request(request)

    def calls_to(self, host: str) -> list[httpx.Request]:
        return [request for request in self.seen if request.url.host == host]


class ScriptedModel(httpx.AsyncBaseTransport):
    """The chat route of a serving endpoint: answers from a script, remembers the requests."""

    def __init__(
        self, *replies: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]
    ) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/serving-endpoints/chat/completions"
        body = json.loads(request.content)
        self.requests.append(body)
        reply = self.replies.pop(0) if self.replies else _text_reply("(script ran out)")
        message = reply(body) if callable(reply) else reply
        return httpx.Response(200, json={"choices": [{"message": message}]})


def _text_reply(text: str | list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _tool_reply(name: str, arguments: str, call_id: str = "call-1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }


def _settings(**changes: str) -> Any:
    values = {
        "agent_name": "test-agent",
        "llm_endpoint": "test-model",
        "mcp_server_url": MCP_URL,
        "peer_url": "",
        "public_url": PUBLIC_URL,
    }
    return agent_module.Settings(**{**values, **changes})


def _build(
    model: httpx.AsyncBaseTransport,
    *,
    credentials: FakeCredentials | None = None,
    peer: TestClient | None = None,
    **settings: str,
) -> tuple[TestClient, Network]:
    hosts: dict[str, httpx.AsyncBaseTransport] = {
        "workspace.example": model,
        "mcp.example": httpx.ASGITransport(app=mcp_module.app),
    }
    if peer is not None:
        hosts["peer.example"] = httpx.ASGITransport(app=peer.app)
    network = Network(hosts)
    app = agent_module.create_app(
        _settings(**settings),
        credentials or FakeCredentials(),
        httpx.AsyncClient(transport=network),
    )
    return TestClient(app), network


def _user_message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"messageId": "m-1", "role": "ROLE_USER", "parts": list(parts)}


def _rpc(method: str, params: dict[str, Any], request_id: int | str = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _send(client: TestClient, text: str, **headers: str) -> dict[str, Any]:
    response = client.post(
        "/",
        json=_rpc("SendMessage", {"message": _user_message({"text": text})}),
        headers={"A2A-Version": "1.0", **headers},
    )
    assert response.status_code == 200
    answer: dict[str, Any] = response.json()
    assert "error" not in answer, answer
    task: dict[str, Any] = answer["result"]["task"]
    return task


def _send_directive(client: TestClient, directive: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/", json=_rpc("SendMessage", {"message": _user_message({"data": directive})})
    )
    task: dict[str, Any] = response.json()["result"]["task"]
    return task


def _answer_text(task: dict[str, Any]) -> str:
    return str(task["artifacts"][0]["parts"][0]["text"])


def _failure_text(task: dict[str, Any]) -> str:
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    return str(task["status"]["message"]["parts"][0]["text"])


# --- the card and the plain endpoints -------------------------------------------------------


def test_the_card_describes_one_json_rpc_interface_at_the_public_address() -> None:
    client, _ = _build(ScriptedModel())

    card = client.get("/.well-known/agent-card.json").json()

    assert card["name"] == "test-agent"
    assert card["supportedInterfaces"] == [
        {"url": f"{PUBLIC_URL}/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    assert card["capabilities"]["streaming"] is True
    assert [skill["id"] for skill in card["skills"]] == ["ask", "call-tool", "relay"]


def test_without_a_public_address_the_card_uses_the_one_the_request_came_in_on() -> None:
    client, _ = _build(ScriptedModel(), public_url="")

    card = client.get("/.well-known/agent-card.json").json()

    assert card["supportedInterfaces"][0]["url"] == "http://testserver/"


def test_status_reports_the_settings_and_never_the_token() -> None:
    client, _ = _build(ScriptedModel())

    response = client.get(
        "/api/status",
        headers={"X-Forwarded-Email": "dana@example.com", "X-Forwarded-Access-Token": "secret"},
    )

    status = response.json()
    assert status["llm_endpoint"] == "test-model"
    assert status["mcp_server_configured"] is True
    assert status["peer_configured"] is False
    assert status["credentials"] == {"configured": True, "auth_type": "fake"}
    assert status["x_forwarded_email"] == "dana@example.com"
    assert status["access_token_header_present"] is True
    assert "secret" not in response.text


def test_status_says_so_when_the_app_has_no_credentials() -> None:
    client, _ = _build(ScriptedModel(), credentials=FakeCredentials(missing=True))

    assert client.get("/api/status").json()["credentials"]["configured"] is False


def test_the_page_and_the_health_check_answer() -> None:
    client, _ = _build(ScriptedModel())

    page = client.get("/", headers={"X-Forwarded-Email": "<b>dana</b>@example.com"})

    assert page.status_code == 200
    assert "&lt;b&gt;dana&lt;/b&gt;" in page.text
    assert client.get("/healthz").json() == {"status": "ok"}


# --- SendMessage with the model and the MCP tools -------------------------------------------


def test_a_question_is_answered_by_the_model_after_it_calls_an_mcp_tool() -> None:
    model = ScriptedModel(_tool_reply("add", '{"a": 17, "b": 25}'), _text_reply("The sum is 42."))
    client, network = _build(model)

    task = _send(client, "What is 17 plus 25?", **{"X-Forwarded-Email": "dana@example.com"})

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _answer_text(task) == "The sum is 42."
    assert task["metadata"]["caller"] == "dana@example.com"
    assert task["metadata"]["toolCalls"] == [
        {"name": "add", "arguments": {"a": 17, "b": 25}, "result": "42"}
    ]
    assert task["history"][0]["parts"] == [{"text": "What is 17 plus 25?"}]
    # The model was offered the server's tools, and got the tool's result back.
    assert [tool["function"]["name"] for tool in model.requests[0]["tools"]] == [
        "add",
        "utc_now",
        "word_count",
        "whoami",
    ]
    assert model.requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "42",
    }
    assert model.requests[0]["model"] == "test-model"
    # Every call the agent made carried its own token, never the caller's.
    assert {r.headers["authorization"] for r in network.seen} == {BEARER}


def test_the_agent_does_the_mcp_handshake_before_it_lists_tools() -> None:
    model = ScriptedModel(_text_reply("hi"))
    client, network = _build(model)

    _send(client, "hello")

    methods = [json.loads(r.content)["method"] for r in network.calls_to("mcp.example")]
    assert methods == ["initialize", "notifications/initialized", "tools/list"]
    assert network.calls_to("mcp.example")[2].headers["mcp-protocol-version"] == "2025-11-25"


def test_a_model_that_answers_in_blocks_is_read_as_text() -> None:
    blocks = [{"type": "reasoning", "text": "hmm"}, {"type": "text", "text": "Fine."}]
    client, _ = _build(ScriptedModel(_text_reply(blocks)), mcp_server_url="")

    task = _send(client, "hello")

    assert _answer_text(task) == "Fine."


def test_without_an_mcp_server_the_model_gets_no_tools() -> None:
    model = ScriptedModel(_text_reply("hello"))
    client, network = _build(model, mcp_server_url="")

    task = _send(client, "hi")

    assert _answer_text(task) == "hello"
    assert "tools" not in model.requests[0]
    assert network.calls_to("mcp.example") == []


def test_a_tool_the_model_invents_is_never_sent_to_the_server() -> None:
    model = ScriptedModel(_tool_reply("delete_everything", "{}"), _text_reply("I cannot."))
    client, network = _build(model)

    task = _send(client, "do it")

    assert _answer_text(task) == "I cannot."
    assert "tools/call" not in [
        json.loads(r.content).get("method") for r in network.calls_to("mcp.example")
    ]
    assert "no tool named 'delete_everything'" in model.requests[1]["messages"][-1]["content"]


def test_arguments_that_are_not_json_go_back_to_the_model() -> None:
    model = ScriptedModel(_tool_reply("add", "{not json"), _text_reply("Sorry."))
    client, _ = _build(model)

    task = _send(client, "add")

    assert _answer_text(task) == "Sorry."
    assert "not valid JSON" in model.requests[1]["messages"][-1]["content"]


def test_a_model_that_never_stops_calling_tools_ends_the_task_as_failed() -> None:
    forever = [_tool_reply("utc_now", "{}") for _ in range(10)]
    client, _ = _build(ScriptedModel(*forever))

    task = _send(client, "what time is it")

    assert "still calling tools after 4 rounds" in _failure_text(task)


def test_a_failed_tool_is_a_result_the_model_can_read() -> None:
    model = ScriptedModel(_tool_reply("add", '{"a": "x", "b": 1}'), _text_reply("Bad input."))
    client, _ = _build(model)

    task = _send(client, "add x and 1")

    assert _answer_text(task) == "Bad input."
    assert model.requests[1]["messages"][-1]["content"] == "'a' must be a number"


# --- the skills that skip the model ---------------------------------------------------------


def test_call_tool_reaches_the_mcp_server_without_the_model() -> None:
    model = ScriptedModel()
    client, _ = _build(model)

    task = _send_directive(
        client, {"skill": "call-tool", "tool": "add", "arguments": {"a": 2, "b": 3}}
    )

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _answer_text(task) == "5"
    assert model.requests == []


def test_call_tool_reports_a_tool_error_as_a_failed_task() -> None:
    client, _ = _build(ScriptedModel())

    task = _send_directive(client, {"skill": "call-tool", "tool": "add", "arguments": {"a": "x"}})

    assert "the tool 'add' failed: 'a' must be a number" in _failure_text(task)


@pytest.mark.parametrize(
    "directive",
    [
        {"skill": "call-tool"},
        {"skill": "call-tool", "tool": 7},
        {"skill": "call-tool", "tool": "add", "arguments": []},
        {"skill": "relay"},
        {"skill": "relay", "text": ""},
    ],
)
def test_a_directive_that_does_not_fit_fails_the_task(directive: dict[str, Any]) -> None:
    client, _ = _build(ScriptedModel(), peer_url=PEER_URL)

    task = _send_directive(client, directive)

    assert task["status"]["state"] == "TASK_STATE_FAILED"


def test_call_tool_without_an_mcp_server_says_what_to_set() -> None:
    client, _ = _build(ScriptedModel(), mcp_server_url="")

    task = _send_directive(client, {"skill": "call-tool", "tool": "add", "arguments": {}})

    assert "MCP_SERVER_URL" in _failure_text(task)


def test_relay_sends_the_text_to_the_peer_agent_and_returns_its_answer() -> None:
    peer, _ = _build(ScriptedModel(_text_reply("Hello from the peer.")), mcp_server_url="")
    client, network = _build(ScriptedModel(), peer=peer, peer_url=PEER_URL)

    task = _send_directive(client, {"skill": "relay", "text": "say hello"})

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _answer_text(task) == "Hello from the peer."
    assert task["metadata"]["peer"]["caller"] == "anonymous"
    sent = network.calls_to("peer.example")[0]
    assert sent.headers["authorization"] == BEARER
    assert sent.headers["a2a-version"] == "1.0"
    assert json.loads(sent.content)["params"]["message"]["parts"] == [{"text": "say hello"}]


def test_relay_reports_a_peer_whose_task_failed() -> None:
    # The peer has no credentials of its own, so the task it accepts ends as failed.
    peer, _ = _build(ScriptedModel(), credentials=FakeCredentials(missing=True))
    client, _ = _build(ScriptedModel(), peer=peer, peer_url=PEER_URL)

    task = _send_directive(client, {"skill": "relay", "text": "hello"})

    failure = _failure_text(task)
    assert "the peer agent's task ended as TASK_STATE_FAILED" in failure
    assert "no Databricks credentials" in failure


def test_relay_without_a_peer_says_what_to_set() -> None:
    client, _ = _build(ScriptedModel())

    task = _send_directive(client, {"skill": "relay", "text": "hi"})

    assert "A2A_PEER_URL" in _failure_text(task)


# --- a model endpoint that pushes back ---------------------------------------------------------


class FlakyModel(ScriptedModel):
    """A chat route that answers ``status`` to its first ``failures`` requests."""

    def __init__(self, status: int, failures: int, *replies: dict[str, Any]) -> None:
        super().__init__(*replies)
        self.status = status
        self.failures = failures
        self.attempts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if self.attempts <= self.failures:
            return httpx.Response(self.status, json={"error_code": "REQUEST_LIMIT_EXCEEDED"})
        return await super().handle_async_request(request)


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_module, "RATE_LIMIT_RETRY_DELAYS_S", (0.0, 0.0, 0.0))


def test_a_rate_limited_model_call_is_tried_again(no_backoff: None) -> None:
    # The free endpoints answer 429 when the workspace's request rate is exceeded.
    model = FlakyModel(429, 2, _text_reply("Got through."))
    client, _ = _build(model, mcp_server_url="")

    task = _send(client, "hello")

    assert _answer_text(task) == "Got through."
    assert model.attempts == 3


def test_a_model_that_stays_rate_limited_fails_the_task_after_the_last_try(
    no_backoff: None,
) -> None:
    model = FlakyModel(429, 99)
    client, _ = _build(model, mcp_server_url="")

    task = _send(client, "hello")

    assert "answered 429" in _failure_text(task)
    assert model.attempts == 4  # the first try and one per delay


def test_only_a_rate_limit_is_tried_again(no_backoff: None) -> None:
    model = FlakyModel(500, 1, _text_reply("never reached"))
    client, _ = _build(model, mcp_server_url="")

    task = _send(client, "hello")

    assert "answered 500" in _failure_text(task)
    assert model.attempts == 1


def test_a_rate_limited_mcp_server_is_not_tried_again(no_backoff: None) -> None:
    class Limited(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            Limited.calls += 1
            return httpx.Response(429)

    task = _call_add_through(Limited())

    assert "answered 429" in _failure_text(task)
    assert Limited.calls == 1


# --- an MCP server that is stateful and answers in SSE ---------------------------------------


class StreamingMcp(httpx.AsyncBaseTransport):
    """A stateful MCP server that answers each request as SSE: a notification, then the result.

    ``id_shift`` makes the result carry another request's id, as a confused server would.
    """

    SESSION = "session-1"

    def __init__(self, id_shift: int = 0) -> None:
        self.id_shift = id_shift
        self.sessions_seen: list[str | None] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        self.sessions_seen.append(request.headers.get("mcp-session-id"))
        if "id" not in message:
            return httpx.Response(202)
        results = {
            "initialize": {"protocolVersion": "2025-11-25", "capabilities": {}},
            "tools/call": {"content": [{"type": "text", "text": "7"}], "isError": False},
        }
        events = [
            {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "working"}},
            {
                "jsonrpc": "2.0",
                "id": message["id"] + self.id_shift,
                "result": results[message["method"]],
            },
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "mcp-session-id": self.SESSION},
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )


def _call_add_through(mcp: httpx.AsyncBaseTransport) -> dict[str, Any]:
    network = Network({"workspace.example": ScriptedModel(), "mcp.example": mcp})
    app = agent_module.create_app(
        _settings(), FakeCredentials(), httpx.AsyncClient(transport=network)
    )
    return _send_directive(
        TestClient(app), {"skill": "call-tool", "tool": "add", "arguments": {"a": 3, "b": 4}}
    )


def test_the_result_is_picked_out_of_an_sse_answer_and_the_session_is_kept() -> None:
    mcp = StreamingMcp()

    task = _call_add_through(mcp)

    assert _answer_text(task) == "7"
    # The first request has no session; every later one carries the one the server issued.
    assert mcp.sessions_seen == [None, "session-1", "session-1"]


def test_an_answer_for_another_request_is_not_taken_for_the_result() -> None:
    task = _call_add_through(StreamingMcp(id_shift=1))

    assert "holds no result for request 1" in _failure_text(task)


# --- failures of the hops -------------------------------------------------------------------


class Redirecting(httpx.AsyncBaseTransport):
    """What the Databricks proxy does with a token it does not accept."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(302, headers={"Location": "https://login.example/sign-in"})


def test_a_redirect_is_reported_and_not_followed() -> None:
    redirecting = Redirecting()
    network = Network({"workspace.example": ScriptedModel(), "mcp.example": redirecting})
    app = agent_module.create_app(
        _settings(), FakeCredentials(), httpx.AsyncClient(transport=network)
    )

    task = _send(TestClient(app), "hello")

    assert "answered 302" in _failure_text(task)
    assert "did not accept this App's token" in _failure_text(task)
    assert redirecting.calls == 1


def test_a_401_with_an_empty_body_gets_the_same_hint_as_a_redirect() -> None:
    # The proxy answers 401 `{}` instead of a redirect when the request accepts JSON.
    class Unauthorized(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={})

    network = Network({"workspace.example": ScriptedModel(), "mcp.example": Unauthorized()})
    app = agent_module.create_app(
        _settings(), FakeCredentials(), httpx.AsyncClient(transport=network)
    )

    task = _send(TestClient(app), "hello")

    assert "answered 401" in _failure_text(task)
    assert "did not accept this App's token" in _failure_text(task)


def test_an_app_without_credentials_fails_the_task_and_says_what_to_do() -> None:
    client, network = _build(ScriptedModel(), credentials=FakeCredentials(missing=True))

    task = _send(client, "hello")

    assert "no Databricks credentials" in _failure_text(task)
    assert network.seen == []  # nothing is sent without a token


def test_a_peer_error_carries_the_status_and_a_short_body() -> None:
    class Refusing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="x" * 1000)

    network = Network({"workspace.example": ScriptedModel(), "mcp.example": Refusing()})
    app = agent_module.create_app(
        _settings(), FakeCredentials(), httpx.AsyncClient(transport=network)
    )

    message = _failure_text(_send(TestClient(app), "hello"))

    assert "answered 403" in message
    assert len(message) < 400


def test_an_unexpected_exception_ends_the_task_without_leaking_it() -> None:
    class Exploding(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise RuntimeError("internal detail")

    client, _ = _build(Exploding(), mcp_server_url="")

    task = _send(client, "hello")

    assert "internal detail" not in json.dumps(task)
    assert "failed unexpectedly" in _failure_text(task)


# --- streaming ------------------------------------------------------------------------------


def _events(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [frame for frame in response.text.split("\n\n") if frame]
    assert all(frame.startswith("data: ") for frame in frames)
    return [json.loads(frame.removeprefix("data: ")) for frame in frames]


def test_streaming_sends_the_task_each_step_the_answer_and_the_end() -> None:
    model = ScriptedModel(_tool_reply("add", '{"a": 1, "b": 2}'), _text_reply("It is 3."))
    client, _ = _build(model)

    response = client.post(
        "/",
        json=_rpc("SendStreamingMessage", {"message": _user_message({"text": "1+2?"})}, "req-9"),
    )

    events = _events(response)
    assert {event["id"] for event in events} == {"req-9"}
    results = [event["result"] for event in events]
    assert results[0]["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    states = [r["statusUpdate"]["status"]["state"] for r in results if "statusUpdate" in r]
    assert states[0] == "TASK_STATE_WORKING"
    assert states[-1] == "TASK_STATE_COMPLETED"
    notes = [
        r["statusUpdate"]["status"]["message"]["parts"][0]["text"]
        for r in results
        if "statusUpdate" in r and "message" in r["statusUpdate"]["status"]
    ]
    assert "listing the tools of the MCP server" in notes
    assert 'calling tool add with {"a": 1, "b": 2}' in notes
    artifact = next(r["artifactUpdate"] for r in results if "artifactUpdate" in r)
    assert artifact["artifact"]["parts"] == [{"text": "It is 3."}]
    assert artifact["lastChunk"] is True
    assert "statusUpdate" in results[-1]


def test_streaming_ends_with_a_failed_status_when_a_hop_fails() -> None:
    client, _ = _build(ScriptedModel(), credentials=FakeCredentials(missing=True))

    response = client.post(
        "/", json=_rpc("SendStreamingMessage", {"message": _user_message({"text": "hi"})})
    )

    last = _events(response)[-1]["result"]["statusUpdate"]["status"]
    assert last["state"] == "TASK_STATE_FAILED"


# --- GetTask and the rules of the protocol ---------------------------------------------------


def test_get_task_returns_the_task_to_the_caller_who_made_it() -> None:
    client, _ = _build(ScriptedModel(_text_reply("hi")), mcp_server_url="")
    task = _send(client, "hello", **{"X-Forwarded-Email": "dana@example.com"})

    mine = client.post(
        "/",
        json=_rpc("GetTask", {"id": task["id"]}),
        headers={"X-Forwarded-Email": "dana@example.com"},
    )
    theirs = client.post(
        "/",
        json=_rpc("GetTask", {"id": task["id"]}),
        headers={"X-Forwarded-Email": "eli@example.com"},
    )

    assert mine.json()["result"]["id"] == task["id"]
    assert theirs.json()["error"]["code"] == -32001
    assert client.post("/", json=_rpc("GetTask", {"id": "nope"})).json()["error"]["code"] == -32001
    assert client.post("/", json=_rpc("GetTask", {})).json()["error"]["code"] == -32001


def test_the_newest_tasks_are_kept_and_the_oldest_are_dropped() -> None:
    class Echo:
        async def respond(self, request: Any) -> Any:
            yield agent_module.Answer(request.text, {})

    executor = agent_module.TaskExecutor(Echo(), max_tasks=2)

    async def run(text: str) -> str:
        message = _user_message({"text": text})
        request = agent_module.parse_message(message)
        events = [event async for event in executor.execute(message, request, "dana")]
        return str(events[0]["task"]["id"])

    ids = [asyncio.run(run(text)) for text in ("a", "b", "c")]

    assert executor.get(ids[0], "dana") is None
    assert executor.get(ids[1], "dana") is not None
    assert executor.get(ids[2], "dana") is not None


def test_an_a2a_version_other_than_1_0_is_refused() -> None:
    client, _ = _build(ScriptedModel())

    response = client.post(
        "/",
        json=_rpc("SendMessage", {"message": _user_message({"text": "hi"})}),
        headers={"A2A-Version": "0.3"},
    )

    assert response.json()["error"]["code"] == -32009


@pytest.mark.parametrize(
    "method",
    ["CancelTask", "SubscribeToTask", "ListTasks", "GetExtendedAgentCard"],
)
def test_known_methods_the_example_does_not_implement_say_unsupported(method: str) -> None:
    client, _ = _build(ScriptedModel())

    assert client.post("/", json=_rpc(method, {})).json()["error"]["code"] == -32004


def test_an_unknown_method_is_method_not_found() -> None:
    client, _ = _build(ScriptedModel())

    assert client.post("/", json=_rpc("message/send", {})).json()["error"]["code"] == -32601


@pytest.mark.parametrize(
    "message",
    [
        None,
        "text",
        {"messageId": "m", "parts": [{"text": "hi"}]},
        {"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": "hi"}]},
        {"role": "ROLE_USER", "parts": [{"text": "hi"}]},
        {"messageId": "m", "role": "ROLE_USER", "parts": []},
        {"messageId": "m", "role": "ROLE_USER", "parts": ["hi"]},
        {"messageId": "m", "role": "ROLE_USER", "parts": [{"url": "https://x"}]},
        {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "x" * 4001}]},
    ],
)
def test_a_message_that_does_not_fit_is_invalid_params(message: Any) -> None:
    client, _ = _build(ScriptedModel())

    for method in ("SendMessage", "SendStreamingMessage"):
        response = client.post("/", json=_rpc(method, {"message": message}))
        assert response.json()["error"]["code"] == -32602


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (b"not json", 400, -32700),
        (b"[]", 200, -32600),
        (b'{"id": 1, "method": "GetTask"}', 200, -32600),
        (b'{"jsonrpc": "2.0", "method": "GetTask"}', 200, -32600),
        (b'{"jsonrpc": "2.0", "id": 1, "method": 5}', 200, -32600),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": []}', 200, -32602),
        (b" " * 70_000, 413, -32600),
    ],
)
def test_a_broken_request_gets_a_json_rpc_error(body: bytes, status: int, code: int) -> None:
    client, _ = _build(ScriptedModel())

    response = client.post("/", content=body)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_the_rpc_id_comes_back_as_it_was_sent() -> None:
    client, _ = _build(ScriptedModel())

    assert client.post("/", json=_rpc("GetTask", {}, "abc")).json()["id"] == "abc"
    assert client.post("/", json=_rpc("GetTask", {}, 42)).json()["id"] == 42
