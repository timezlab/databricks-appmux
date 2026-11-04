"""The ``examples/agent-hub`` App: one MCP server and one A2A agent that know who is calling.

The example is a standalone Databricks App, not part of the ``appmux`` package, so the module is
loaded from its folder. The workspace (the chat route of a serving endpoint and the SCIM "who am I"
route) is a script behind an in-process transport; nothing leaves the process. What these tests
pin down is the identity story: the App calls the model with **its own** token, calls Databricks
for the user with **the caller's** token, and never shows a token to anyone.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
APP_BEARER = "Bearer app-token-1234"
USER_OBO = "user-token-5678"
WORKSPACE = "https://workspace.example"
PUBLIC_URL = "https://hub.example"
DANA = {"X-Forwarded-Email": "dana@example.com", "X-Forwarded-User": "1234@1234"}
DANA_WITH_TOKEN = {**DANA, "X-Forwarded-Access-Token": USER_OBO}
SECRETS = ("app-token-1234", USER_OBO)


def _load(name: str, folder: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / folder / "app.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their own module up here
    spec.loader.exec_module(module)
    return module


hub = _load("example_agent_hub", "agent-hub")


class FakeCredentials:
    """Stands for the App's own identity; ``missing`` is an App nobody gave one."""

    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing

    async def headers(self) -> dict[str, str]:
        if self.missing:
            raise hub.CredentialsMissing("this App has no Databricks credentials")
        return {"Authorization": APP_BEARER}

    async def host(self) -> str:
        if self.missing:
            raise hub.CredentialsMissing("this App has no Databricks credentials")
        return WORKSPACE

    async def describe(self) -> dict[str, Any]:
        if self.missing:
            return {"configured": False, "reason": "this App has no Databricks credentials"}
        return {"configured": True, "auth_type": "fake"}


Reply = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


class Workspace(httpx.AsyncBaseTransport):
    """The workspace: a scripted chat route, and the SCIM route that answers "who am I"."""

    def __init__(
        self,
        *replies: Reply,
        me: dict[str, Any] | None = None,
        me_status: int = 200,
        chat_statuses: tuple[int, ...] = (),
    ) -> None:
        self.replies = list(replies)
        self.me = me or {"userName": "dana@example.com", "displayName": "Dana", "id": "42"}
        self.me_status = me_status
        self.chat_statuses = list(chat_statuses)
        self.chat_requests: list[httpx.Request] = []
        self.me_requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/2.0/preview/scim/v2/Me":
            self.me_requests.append(request)
            return httpx.Response(self.me_status, json=self.me)
        assert request.url.path == "/serving-endpoints/chat/completions", request.url.path
        self.chat_requests.append(request)
        if self.chat_statuses:
            status = self.chat_statuses.pop(0)
            if status != 200:
                return httpx.Response(status, json={"error": "limit"})
        reply = self.replies.pop(0) if self.replies else _text_reply("(script ran out)")
        message = reply(self.chat_body(-1)) if callable(reply) else reply
        return httpx.Response(200, json={"choices": [{"message": message}]})

    def chat_body(self, index: int) -> dict[str, Any]:
        body: dict[str, Any] = json.loads(self.chat_requests[index].content)
        return body


def _text_reply(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _tool_reply(name: str, arguments: str = "{}", call_id: str = "call-1") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }


def _build(
    workspace: Workspace | None = None,
    *,
    credentials: FakeCredentials | None = None,
    **settings: str,
) -> tuple[TestClient, Workspace]:
    workspace = workspace or Workspace()
    values = {"agent_name": "test-hub", "llm_endpoint": "test-model", "public_url": PUBLIC_URL}
    app = hub.create_app(
        hub.Settings(**{**values, **settings}),
        credentials or FakeCredentials(),
        httpx.AsyncClient(transport=workspace),
    )
    return TestClient(app), workspace


def _rpc(method: str, params: dict[str, Any], request_id: int | str = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _mcp(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/mcp", json=_rpc(method, params or {}), headers={**MCP_HEADERS, **(headers or {})}
    )
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()
    return answer


def _tool(
    client: TestClient, name: str, arguments: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    answer = _mcp(client, "tools/call", {"name": name, "arguments": arguments}, headers)
    result: dict[str, Any] = answer["result"]
    return result


def _tool_text(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


def _user_message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"messageId": "m-1", "role": "ROLE_USER", "parts": list(parts)}


def _send(
    client: TestClient, parts: list[dict[str, Any]], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    response = client.post(
        "/",
        json=_rpc("SendMessage", {"message": _user_message(*parts)}),
        headers={"A2A-Version": "1.0", **(headers or {})},
    )
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()
    assert "error" not in answer, answer
    task: dict[str, Any] = answer["result"]["task"]
    return task


def _ask(client: TestClient, text: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return _send(client, [{"text": text}], headers)


def _call_tool(
    client: TestClient, tool: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    return _send(client, [{"data": {"skill": "call-tool", "tool": tool, "arguments": {}}}], headers)


def _answer_text(task: dict[str, Any]) -> str:
    return str(task["artifacts"][0]["parts"][0]["text"])


def _failure_text(task: dict[str, Any]) -> str:
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    return str(task["status"]["message"]["parts"][0]["text"])


def _never_shows_a_token(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


# --- the plain endpoints --------------------------------------------------------------------


def test_the_card_describes_one_json_rpc_interface_and_two_skills() -> None:
    client, _ = _build()

    card = client.get("/.well-known/agent-card.json").json()

    assert card["name"] == "test-hub"
    assert card["supportedInterfaces"] == [
        {"url": f"{PUBLIC_URL}/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ]
    assert card["capabilities"]["streaming"] is True
    assert [skill["id"] for skill in card["skills"]] == ["ask", "call-tool"]


def test_without_a_public_address_the_card_uses_the_one_the_request_came_in_on() -> None:
    client, _ = _build(public_url="")

    card = client.get("/.well-known/agent-card.json").json()

    assert card["supportedInterfaces"][0]["url"] == "http://testserver/"


def test_status_reports_the_caller_and_never_a_token() -> None:
    client, _ = _build()

    response = client.get("/api/status", headers=DANA_WITH_TOKEN)

    status = response.json()
    assert status["x_forwarded_email"] == "dana@example.com"
    assert status["access_token_header_present"] is True
    assert status["credentials"] == {"configured": True, "auth_type": "fake"}
    assert status["tools"] == ["whoami", "databricks_me", "add", "utc_now"]
    _never_shows_a_token(response.text)


def test_the_page_escapes_the_caller_and_the_health_check_answers() -> None:
    client, _ = _build()

    page = client.get("/", headers={"X-Forwarded-Email": "<b>dana</b>@example.com"})

    assert page.status_code == 200
    assert "&lt;b&gt;dana&lt;/b&gt;" in page.text
    assert client.get("/healthz").json() == {"status": "ok"}


# --- MCP ------------------------------------------------------------------------------------


def test_mcp_initialize_agrees_a_version_and_lists_the_four_tools() -> None:
    client, _ = _build()

    initialized = _mcp(client, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    listed = _mcp(client, "tools/list")["result"]["tools"]

    assert initialized["protocolVersion"] == "2025-06-18"
    assert initialized["serverInfo"]["name"] == "test-hub"
    assert [tool["name"] for tool in listed] == ["whoami", "databricks_me", "add", "utc_now"]
    assert all(tool["inputSchema"]["type"] == "object" for tool in listed)


def test_a_notification_gets_202_and_a_broken_message_gets_400() -> None:
    client, _ = _build()

    notification = client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    not_json = client.post("/mcp", content=b"{nope")
    batch = client.post("/mcp", json=[_rpc("ping", {})])

    assert notification.status_code == 202
    assert not_json.status_code == 400
    assert batch.status_code == 400


def test_mcp_has_no_stream_and_refuses_a_foreign_origin() -> None:
    client, _ = _build()

    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp").status_code == 405
    foreign = client.post("/mcp", json=_rpc("ping", {}), headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403
    own = client.post("/mcp", json=_rpc("ping", {}), headers={"Origin": "http://testserver"})
    assert own.status_code == 200


def test_an_unknown_tool_is_a_protocol_error_and_bad_arguments_are_a_tool_error() -> None:
    client, _ = _build()

    unknown = _mcp(client, "tools/call", {"name": "nope", "arguments": {}})
    bad = _tool(client, "add", {"a": "x", "b": 1}, {})

    assert unknown["error"]["code"] == -32602
    assert bad["isError"] is True
    assert "'a' must be a number" in _tool_text(bad)


def test_mcp_add_and_utc_now_work() -> None:
    client, _ = _build()

    assert _tool_text(_tool(client, "add", {"a": 2, "b": 3}, {})) == "5"
    assert _tool_text(_tool(client, "utc_now", {}, {})).endswith("Z")


def test_mcp_whoami_reports_the_identity_the_proxy_forwarded_and_never_the_token() -> None:
    client, _ = _build()

    result = _tool(client, "whoami", {}, DANA_WITH_TOKEN)

    seen = json.loads(_tool_text(result))
    assert seen["x_forwarded_email"] == "dana@example.com"
    assert seen["x_forwarded_user"] == "1234@1234"
    assert seen["access_token_header_present"] is True
    _never_shows_a_token(json.dumps(result))


def test_mcp_whoami_without_identity_headers_says_anonymous() -> None:
    client, _ = _build()

    seen = json.loads(_tool_text(_tool(client, "whoami", {}, {})))

    assert seen["x_forwarded_email"] == "anonymous"
    assert seen["access_token_header_present"] is False


def test_databricks_me_asks_the_workspace_with_the_callers_token_not_the_apps() -> None:
    client, workspace = _build()

    result = _tool(client, "databricks_me", {}, DANA_WITH_TOKEN)

    assert result["isError"] is False
    assert json.loads(_tool_text(result)) == {
        "user_name": "dana@example.com",
        "display_name": "Dana",
        "id": "42",
    }
    assert [r.headers["authorization"] for r in workspace.me_requests] == [f"Bearer {USER_OBO}"]
    assert workspace.me_requests[0].url.host == "workspace.example"


def test_databricks_me_without_a_forwarded_token_is_a_tool_error_that_says_why() -> None:
    client, workspace = _build()

    result = _tool(client, "databricks_me", {}, DANA)

    assert result["isError"] is True
    assert "no user token" in _tool_text(result)
    assert "OBO" in _tool_text(result)
    assert workspace.me_requests == []


def test_databricks_me_when_databricks_refuses_the_token_is_a_tool_error() -> None:
    client, _ = _build(Workspace(me_status=403))

    result = _tool(client, "databricks_me", {}, DANA_WITH_TOKEN)

    assert result["isError"] is True
    assert "403" in _tool_text(result)
    assert "scope" in _tool_text(result)
    _never_shows_a_token(json.dumps(result))


def test_databricks_me_with_an_app_that_has_no_credentials_names_what_is_missing() -> None:
    client, _ = _build(credentials=FakeCredentials(missing=True))

    result = _tool(client, "databricks_me", {}, DANA_WITH_TOKEN)

    assert result["isError"] is True
    assert "no Databricks credentials" in _tool_text(result)


# --- A2A: direct tool calls -----------------------------------------------------------------


def test_a_direct_whoami_over_a2a_sees_the_same_caller_as_over_mcp() -> None:
    client, workspace = _build()

    task = _call_tool(client, "whoami", DANA_WITH_TOKEN)

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    seen = json.loads(_answer_text(task))
    assert seen["x_forwarded_email"] == "dana@example.com"
    assert seen["access_token_header_present"] is True
    assert task["metadata"]["caller"] == "dana@example.com"
    assert workspace.chat_requests == []
    _never_shows_a_token(json.dumps(task))


def test_a_direct_databricks_me_over_a2a_uses_the_callers_token() -> None:
    client, workspace = _build()

    task = _call_tool(client, "databricks_me", DANA_WITH_TOKEN)

    assert json.loads(_answer_text(task))["user_name"] == "dana@example.com"
    assert [r.headers["authorization"] for r in workspace.me_requests] == [f"Bearer {USER_OBO}"]


def test_a_direct_call_of_a_failing_tool_ends_as_a_failed_task_with_the_reason() -> None:
    client, _ = _build()

    task = _call_tool(client, "databricks_me", DANA)

    assert "no user token" in _failure_text(task)


def test_a_direct_call_needs_a_known_tool_and_an_object_of_arguments() -> None:
    client, _ = _build()

    unknown = _call_tool(client, "nope")
    bad = _send(client, [{"data": {"skill": "call-tool", "tool": "add", "arguments": []}}])

    assert "no tool named 'nope'" in _failure_text(unknown)
    assert "'arguments'" in _failure_text(bad)


# --- A2A: the agent -------------------------------------------------------------------------


def test_the_agent_runs_its_tools_with_the_identity_of_the_a2a_caller() -> None:
    workspace = Workspace(_tool_reply("whoami"), _text_reply("You are dana@example.com."))
    client, _ = _build(workspace)

    task = _ask(client, "Who am I?", DANA)

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _answer_text(task) == "You are dana@example.com."
    # The model was offered the tools and was handed what whoami saw: the A2A caller.
    assert [t["function"]["name"] for t in workspace.chat_body(0)["tools"]] == [
        "whoami",
        "databricks_me",
        "add",
        "utc_now",
    ]
    tool_message = workspace.chat_body(1)["messages"][-1]
    assert tool_message["role"] == "tool"
    assert json.loads(tool_message["content"])["x_forwarded_email"] == "dana@example.com"
    assert task["metadata"]["caller"] == "dana@example.com"
    assert [call["name"] for call in task["metadata"]["toolCalls"]] == ["whoami"]


def test_one_task_uses_two_identities_the_app_for_the_model_and_the_caller_for_databricks() -> None:
    workspace = Workspace(_tool_reply("databricks_me"), _text_reply("Databricks says Dana."))
    client, _ = _build(workspace)

    task = _ask(client, "Who does Databricks think I am?", DANA_WITH_TOKEN)

    assert _answer_text(task) == "Databricks says Dana."
    assert {r.headers["authorization"] for r in workspace.chat_requests} == {APP_BEARER}
    assert [r.headers["authorization"] for r in workspace.me_requests] == [f"Bearer {USER_OBO}"]
    _never_shows_a_token(json.dumps(task), json.dumps(workspace.chat_body(1)))


def test_a_tool_error_goes_back_to_the_model_instead_of_ending_the_task() -> None:
    workspace = Workspace(_tool_reply("databricks_me"), _text_reply("I cannot see your token."))
    client, _ = _build(workspace)

    task = _ask(client, "Who am I in Databricks?", DANA)

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "no user token" in workspace.chat_body(1)["messages"][-1]["content"]


def test_a_tool_the_model_invents_is_never_run() -> None:
    workspace = Workspace(_tool_reply("delete_everything"), _text_reply("I cannot."))
    client, _ = _build(workspace)

    task = _ask(client, "do it", DANA_WITH_TOKEN)

    assert _answer_text(task) == "I cannot."
    assert "no tool named 'delete_everything'" in workspace.chat_body(1)["messages"][-1]["content"]
    assert workspace.me_requests == []


def test_arguments_that_are_not_json_go_back_to_the_model() -> None:
    workspace = Workspace(_tool_reply("add", "{not json"), _text_reply("Sorry."))
    client, _ = _build(workspace)

    _ask(client, "add", DANA)

    assert "not valid JSON" in workspace.chat_body(1)["messages"][-1]["content"]


def test_a_model_that_keeps_calling_tools_ends_as_a_failed_task() -> None:
    workspace = Workspace(*[_tool_reply("utc_now") for _ in range(10)])
    client, _ = _build(workspace)

    task = _ask(client, "loop", DANA)

    assert "still calling tools" in _failure_text(task)


def test_a_model_that_answers_in_blocks_is_read_as_text() -> None:
    blocks = [{"type": "reasoning", "text": "hmm"}, {"type": "text", "text": "Fine."}]
    client, _ = _build(Workspace({"role": "assistant", "content": blocks}))

    assert _answer_text(_ask(client, "hello")) == "Fine."


def test_a_rate_limited_model_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub, "RATE_LIMIT_RETRY_DELAYS_S", (0.0, 0.0))
    workspace = Workspace(_text_reply("Finally."), chat_statuses=(429, 429, 200))
    client, _ = _build(workspace)

    task = _ask(client, "hello")

    assert _answer_text(task) == "Finally."
    assert len(workspace.chat_requests) == 3


def test_a_model_that_stays_rate_limited_ends_as_a_failed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hub, "RATE_LIMIT_RETRY_DELAYS_S", (0.0,))
    client, _ = _build(Workspace(chat_statuses=(429, 429)))

    task = _ask(client, "hello")

    assert "429" in _failure_text(task)


def test_an_app_without_credentials_cannot_ask_a_model_but_can_still_say_who_called() -> None:
    client, _ = _build(credentials=FakeCredentials(missing=True))

    asked = _ask(client, "hello", DANA)
    direct = _call_tool(client, "whoami", DANA)

    assert "no Databricks credentials" in _failure_text(asked)
    assert direct["status"]["state"] == "TASK_STATE_COMPLETED"


# --- A2A: tasks and the protocol ------------------------------------------------------------


def test_a_task_can_be_read_back_only_by_the_caller_that_made_it() -> None:
    client, _ = _build()
    task = _call_tool(client, "utc_now", DANA)

    def get(headers: dict[str, str]) -> dict[str, Any]:
        response = client.post("/", json=_rpc("GetTask", {"id": task["id"]}), headers=headers)
        answer: dict[str, Any] = response.json()
        return answer

    mine = get(DANA)
    other = get({"X-Forwarded-Email": "erin@example.com"})

    assert mine["result"]["id"] == task["id"]
    assert other["error"]["code"] == -32001


def test_streaming_sends_the_task_each_step_the_answer_and_the_end() -> None:
    workspace = Workspace(_tool_reply("add", '{"a": 1, "b": 2}'), _text_reply("Three."))
    client, _ = _build(workspace)

    with client.stream(
        "POST",
        "/",
        json=_rpc("SendStreamingMessage", {"message": _user_message({"text": "1+2?"})}),
        headers={"A2A-Version": "1.0", **DANA},
    ) as response:
        events = [
            json.loads(line.removeprefix("data:"))["result"]
            for line in response.iter_lines()
            if line.startswith("data:")
        ]

    assert "task" in events[0]
    assert events[0]["task"]["metadata"]["caller"] == "dana@example.com"
    updates = [e["statusUpdate"]["status"]["state"] for e in events if "statusUpdate" in e]
    assert updates[0] == "TASK_STATE_WORKING"
    assert updates[-1] == "TASK_STATE_COMPLETED"
    artifact = next(e for e in events if "artifactUpdate" in e)["artifactUpdate"]["artifact"]
    assert artifact["parts"] == [{"text": "Three."}]


def test_streaming_runs_its_tools_as_the_caller_too() -> None:
    workspace = Workspace(_tool_reply("databricks_me"), _text_reply("Dana."))
    client, _ = _build(workspace)

    with client.stream(
        "POST",
        "/",
        json=_rpc("SendStreamingMessage", {"message": _user_message({"text": "me?"})}),
        headers=DANA_WITH_TOKEN,
    ) as response:
        response.read()

    assert [r.headers["authorization"] for r in workspace.me_requests] == [f"Bearer {USER_OBO}"]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("not an object", "'message' must be an object"),
        ({"messageId": "m", "role": "ROLE_AGENT", "parts": [{"text": "x"}]}, "ROLE_USER"),
        ({"messageId": "", "role": "ROLE_USER", "parts": [{"text": "x"}]}, "messageId"),
        ({"messageId": "m", "role": "ROLE_USER", "parts": []}, "parts"),
        ({"messageId": "m", "role": "ROLE_USER", "parts": [{"url": "x"}]}, "text part or a data"),
        ({"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "x" * 4001}]}, "longer than"),
    ],
)
def test_a_message_that_does_not_fit_is_an_invalid_params_error(
    message: Any, expected: str
) -> None:
    client, _ = _build()

    answer = client.post("/", json=_rpc("SendMessage", {"message": message})).json()

    assert answer["error"]["code"] == -32602
    assert expected in answer["error"]["message"]


def test_the_protocol_errors_are_the_ones_the_spec_names() -> None:
    client, _ = _build()

    def code(body: dict[str, Any], headers: dict[str, str] | None = None) -> int:
        return int(client.post("/", json=body, headers=headers or {}).json()["error"]["code"])

    assert code(_rpc("Nope", {})) == -32601
    assert code(_rpc("ListTasks", {})) == -32004
    assert code(_rpc("GetTask", {"id": "missing"})) == -32001
    assert code(_rpc("SendMessage", {}), {"A2A-Version": "0.3"}) == -32009
    assert code({"jsonrpc": "1.0", "id": 1, "method": "GetTask"}) == -32600
    assert code({"jsonrpc": "2.0", "id": True, "method": "GetTask"}) == -32600
    assert client.post("/", content=b"{nope").status_code == 400
    assert client.post("/", content=b"x" * (64 * 1024 + 1)).status_code == 413
