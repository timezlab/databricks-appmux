"""The ``examples/mcp-tools`` MCP server: the JSON-RPC rules it follows and the tools it offers.

The example is a standalone Databricks App, not part of the ``appmux`` package, so the module is
loaded from its folder. The official ``mcp`` client is not a dependency of this project; a run
against it is described in ``examples/mcp-tools/README.md``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from starlette.testclient import TestClient

EXAMPLE_PATH = Path(__file__).resolve().parents[2] / "examples" / "mcp-tools" / "app.py"
ACCEPT = "application/json, text/event-stream"


def _load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("example_mcp_tools", EXAMPLE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their own module up here
    spec.loader.exec_module(module)
    return module


example = _load_example()


@pytest.fixture
def client() -> TestClient:
    return TestClient(example.app)


def _rpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _post(client: TestClient, body: Any, **headers: str) -> Any:
    return client.post(
        "/mcp",
        content=body if isinstance(body, bytes) else json.dumps(body),
        headers={"Content-Type": "application/json", "Accept": ACCEPT, **headers},
    )


def _call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = _post(client, _rpc("tools/call", {"name": name, "arguments": arguments}))
    assert response.status_code == 200
    result: dict[str, Any] = response.json()["result"]
    return result


def test_initialize_echoes_a_supported_protocol_version(client: TestClient) -> None:
    response = _post(client, _rpc("initialize", {"protocolVersion": "2025-06-18"}))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "mcp-tools"
    assert result["capabilities"] == {"tools": {"listChanged": False}}


def test_initialize_offers_the_newest_version_for_one_it_does_not_speak(
    client: TestClient,
) -> None:
    response = _post(client, _rpc("initialize", {"protocolVersion": "1999-01-01"}))

    assert response.json()["result"]["protocolVersion"] == "2025-11-25"


def test_the_server_is_stateless(client: TestClient) -> None:
    response = _post(client, _rpc("initialize", {"protocolVersion": "2025-11-25"}))

    assert "mcp-session-id" not in response.headers


def test_tools_list_names_every_tool_with_a_schema(client: TestClient) -> None:
    result = _post(client, _rpc("tools/list")).json()["result"]

    assert [tool["name"] for tool in result["tools"]] == [
        "add",
        "utc_now",
        "word_count",
        "whoami",
    ]
    assert all(tool["inputSchema"]["type"] == "object" for tool in result["tools"])


def test_add_returns_the_sum_as_text(client: TestClient) -> None:
    assert _call(client, "add", {"a": 17, "b": 25}) == {
        "content": [{"type": "text", "text": "42"}],
        "isError": False,
    }
    assert _call(client, "add", {"a": 0.5, "b": 0.25})["content"][0]["text"] == "0.75"


def test_word_count_counts_words(client: TestClient) -> None:
    result = _call(client, "word_count", {"text": "one two  three\nfour"})

    assert result["content"][0]["text"] == "4"


def test_utc_now_is_an_iso_timestamp(client: TestClient) -> None:
    text = _call(client, "utc_now", {})["content"][0]["text"]

    assert text.endswith("Z")
    assert len(text) == len("2026-01-01T00:00:00Z")


@pytest.mark.parametrize(
    "arguments",
    [{"a": "1", "b": 2}, {"a": True, "b": 2}, {"a": 1}, {}],
)
def test_bad_arguments_are_a_tool_error_the_model_can_read(
    client: TestClient, arguments: dict[str, Any]
) -> None:
    result = _call(client, "add", arguments)

    assert result["isError"] is True
    assert "must be a number" in result["content"][0]["text"]


def test_an_unknown_tool_is_a_protocol_error(client: TestClient) -> None:
    response = _post(client, _rpc("tools/call", {"name": "missing", "arguments": {}}))

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32602
    assert response.json()["id"] == 1


def test_an_unknown_method_answers_method_not_found(client: TestClient) -> None:
    response = _post(client, _rpc("resources/list", request_id=7))

    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32601, "message": "method not found: resources/list"},
    }


def test_a_notification_gets_202_and_no_body(client: TestClient) -> None:
    response = _post(client, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert response.status_code == 202
    assert response.content == b""


def test_a_response_from_the_client_gets_202(client: TestClient) -> None:
    response = _post(client, {"jsonrpc": "2.0", "id": 3, "result": {}})

    assert response.status_code == 202


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"not json", -32700),
        (b"[]", -32600),
        (b'{"id": 1, "method": "ping"}', -32600),
        (b'{"jsonrpc": "2.0", "id": true, "method": "ping"}', -32600),
    ],
)
def test_a_broken_message_gets_400_with_a_json_rpc_error(
    client: TestClient, body: bytes, code: int
) -> None:
    response = _post(client, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == code
    assert response.json()["id"] is None


def test_a_body_over_the_limit_is_refused(client: TestClient) -> None:
    response = _post(client, b" " * (example.MAX_BODY_BYTES + 1))

    assert response.status_code == 413


def test_a_stream_or_a_session_end_is_not_offered(client: TestClient) -> None:
    for response in (client.get("/mcp"), client.delete("/mcp")):
        assert response.status_code == 405
        assert response.headers["allow"] == "POST"


def test_an_unsupported_protocol_version_header_is_refused(client: TestClient) -> None:
    response = _post(client, _rpc("ping"), **{"MCP-Protocol-Version": "2020-01-01"})

    assert response.status_code == 400


def test_a_known_protocol_version_header_is_accepted(client: TestClient) -> None:
    response = _post(client, _rpc("ping"), **{"MCP-Protocol-Version": "2025-11-25"})

    assert response.json()["result"] == {}


def test_a_foreign_origin_is_refused_and_the_same_origin_is_not(client: TestClient) -> None:
    foreign = _post(client, _rpc("ping"), Origin="https://evil.example")
    same = _post(client, _rpc("ping"), Origin="http://testserver")
    behind_the_proxy = _post(
        client,
        _rpc("ping"),
        Origin="https://apps.example",
        **{"X-Forwarded-Host": "apps.example"},
    )

    assert foreign.status_code == 403
    assert same.status_code == 200
    assert behind_the_proxy.status_code == 200


def test_whoami_reports_the_headers_and_never_the_token(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        content=json.dumps(_rpc("tools/call", {"name": "whoami", "arguments": {}})),
        headers={
            "Content-Type": "application/json",
            "X-Forwarded-Email": "dana@example.com",
            "X-Forwarded-Prefix": "/demo",
            "X-Forwarded-Access-Token": "secret-value",
        },
    )

    text = response.json()["result"]["content"][0]["text"]
    reported = json.loads(text)
    assert reported["x_forwarded_email"] == "dana@example.com"
    assert reported["x_forwarded_prefix"] == "/demo"
    assert reported["access_token_header_present"] is True
    assert "secret-value" not in response.text


def test_whoami_without_headers_is_anonymous(client: TestClient) -> None:
    reported = json.loads(_call(client, "whoami", {})["content"][0]["text"])

    assert reported["x_forwarded_email"] == "anonymous"
    assert reported["access_token_header_present"] is False


def test_the_page_and_the_health_check_answer(client: TestClient) -> None:
    page = client.get("/", headers={"X-Forwarded-Email": "<b>dana</b>@example.com"})

    assert page.status_code == 200
    assert "&lt;b&gt;dana&lt;/b&gt;" in page.text  # the identity is escaped, not rendered
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/whoami").json()["tools"] == [
        "add",
        "utc_now",
        "word_count",
        "whoami",
    ]
