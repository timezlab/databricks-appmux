"""The ``examples/agent-console`` App: calls the hub over MCP and A2A, as you or as itself.

The example is a standalone Databricks App, not part of the ``appmux`` package, so both modules are
loaded from their folders. The real hub sits behind a fake Databricks proxy that does what the
platform does to a call: it reads the bearer, decides who the caller is, and gives the hub the
identity headers (and the user's token only when OBO is delegated). The console's own calls cross
that fake proxy, so these tests show what the hub would see for each choice the console offers.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
APP_BEARER = "Bearer app-token-1234"
USER_OBO = "user-token-5678"
USER_BEARER = f"Bearer {USER_OBO}"
WORKSPACE = "https://workspace.example"
HUB_PREFIX = "/hub"
HUB_URL = f"https://hub.example{HUB_PREFIX}/"
DANA_EMAIL = "dana@example.com"
APP_IDENTITY = "11111111-2222-3333-4444-555555555555"
DANA = {"X-Forwarded-Email": DANA_EMAIL}
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


hub = _load("example_agent_hub_for_console", "agent-hub")
console = _load("example_agent_console", "agent-console")


class FakeCredentials:
    """Stands for an App's own identity; ``missing`` is an App nobody gave one."""

    def __init__(self, module: ModuleType, *, missing: bool = False) -> None:
        self.module = module
        self.missing = missing

    async def headers(self) -> dict[str, str]:
        if self.missing:
            raise self.module.CredentialsMissing("this App has no Databricks credentials")
        return {"Authorization": APP_BEARER}

    async def host(self) -> str:
        if self.missing:
            raise self.module.CredentialsMissing("this App has no Databricks credentials")
        return WORKSPACE

    async def describe(self) -> dict[str, Any]:
        if self.missing:
            return {"configured": False, "reason": "this App has no Databricks credentials"}
        return {"configured": True, "auth_type": "fake"}


class Workspace(httpx.AsyncBaseTransport):
    """What the hub reaches: a scripted chat route and the SCIM route that says who a token is."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.chat_requests: list[httpx.Request] = []
        self.me_requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/2.0/preview/scim/v2/Me":
            self.me_requests.append(request)
            # Databricks answers with whoever owns the bearer: the person, or an App's service
            # principal.
            if request.headers["authorization"] == APP_BEARER:
                return httpx.Response(
                    200, json={"userName": APP_IDENTITY, "displayName": "agent-console"}
                )
            return httpx.Response(
                200, json={"userName": DANA_EMAIL, "displayName": "Dana", "id": "42"}
            )
        assert request.url.path == "/serving-endpoints/chat/completions", request.url.path
        self.chat_requests.append(request)
        message = (
            self.replies.pop(0) if self.replies else {"role": "assistant", "content": "(done)"}
        )
        return httpx.Response(200, json={"choices": [{"message": message}]})


class Platform(httpx.AsyncBaseTransport):
    """The Databricks proxy in front of the hub, and the workspace API the console also reaches.

    A call to the hub is accepted when its bearer is the user's or the App's; the hub is then
    handed the identity headers the platform would set. Anything else gets the sign-in answer.
    """

    def __init__(self, hub_app: Any, *, obo_delegated: bool = True) -> None:
        self.hub = httpx.ASGITransport(app=hub_app)
        self.obo_delegated = obo_delegated
        self.seen: list[httpx.Request] = []
        self.hub_status: int | None = None  # force one answer, such as appmux's 404

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.host == "workspace.example":
            assert request.headers["authorization"] == APP_BEARER
            return httpx.Response(
                200, json={"userName": APP_IDENTITY, "displayName": "agent-console"}
            )
        assert request.url.host == "hub.example", request.url
        bearer = request.headers.get("authorization")
        headers = [
            (k, v) for k, v in request.headers.raw if not k.lower().startswith(b"x-forwarded-")
        ]
        if bearer == USER_BEARER:
            headers += [
                (b"x-forwarded-email", DANA_EMAIL.encode()),
                (b"x-forwarded-user", b"1234@1234"),
            ]
            if self.obo_delegated:
                headers.append((b"x-forwarded-access-token", USER_OBO.encode()))
        elif bearer == APP_BEARER:
            identity = APP_IDENTITY.encode()
            headers += [
                (b"x-forwarded-email", identity),
                (b"x-forwarded-user", identity + b"@" + identity),
            ]
            # Measured in a browser on 2026-11-04: with OBO delegated to the callee, an App's own
            # token is forwarded too, the same way a person's is.
            if self.obo_delegated:
                headers.append(
                    (b"x-forwarded-access-token", APP_BEARER.removeprefix("Bearer ").encode())
                )
        else:
            return httpx.Response(302, headers={"location": "https://login.example/"})
        if self.hub_status is not None:
            return httpx.Response(self.hub_status, text="not found")
        # appmux strips the slug before the child sees the path, and says so in a header.
        assert request.url.path.startswith(HUB_PREFIX), request.url
        headers.append((b"x-forwarded-prefix", HUB_PREFIX.encode()))
        forwarded = httpx.Request(
            request.method,
            request.url.copy_with(path=request.url.path.removeprefix(HUB_PREFIX) or "/"),
            headers=headers,
            content=request.content,
        )
        return await self.hub.handle_async_request(forwarded)

    def hub_bearers(self) -> list[str]:
        return [r.headers["authorization"] for r in self.seen if r.url.host == "hub.example"]


def _hub_app(workspace: Workspace) -> Any:
    return hub.create_app(
        hub.Settings(agent_name="hub", llm_endpoint="test-model", public_url=HUB_URL.rstrip("/")),
        FakeCredentials(hub),
        httpx.AsyncClient(transport=workspace),
    )


def _build(
    *replies: dict[str, Any],
    provider_url: str = HUB_URL,
    obo_delegated: bool = True,
    credentials_missing: bool = False,
) -> tuple[TestClient, Platform, Workspace]:
    workspace = Workspace(*replies)
    platform = Platform(_hub_app(workspace), obo_delegated=obo_delegated)
    app = console.create_app(
        console.Settings(provider_url=provider_url),
        FakeCredentials(console, missing=credentials_missing),
        httpx.AsyncClient(transport=platform),
    )
    return TestClient(app), platform, workspace


def _tool_reply(name: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": name, "arguments": "{}"}}
        ],
    }


def _call(
    client: TestClient,
    identity: str,
    protocol: str,
    action: str,
    headers: dict[str, str] | None = None,
    **extra: str,
) -> dict[str, Any]:
    response = client.post(
        "/api/call",
        json={"identity": identity, "protocol": protocol, "action": action, **extra},
        headers=headers if headers is not None else DANA_WITH_TOKEN,
    )
    assert response.status_code == 200, response.text
    outcome: dict[str, Any] = response.json()
    return outcome


def _never_shows_a_token(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


# --- the page and what it knows -------------------------------------------------------------


def test_context_says_who_you_are_who_the_app_is_and_where_the_hub_is() -> None:
    client, _, _ = _build()

    response = client.get("/api/context", headers=DANA_WITH_TOKEN)

    context = response.json()
    assert context["you"] == {"email": DANA_EMAIL, "user_token_forwarded": True}
    assert context["app_identity"] == {
        "configured": True,
        "user_name": APP_IDENTITY,
        "display_name": "agent-console",
    }
    assert context["provider"] == {
        "configured": True,
        "mcp_url": f"{HUB_URL}mcp",
        "a2a_url": HUB_URL,
    }
    _never_shows_a_token(response.text)


def test_context_without_a_forwarded_token_says_so() -> None:
    client, _, _ = _build()

    context = client.get("/api/context", headers=DANA).json()

    assert context["you"]["user_token_forwarded"] is False


def test_context_for_an_app_with_no_credentials_and_no_hub_says_what_is_missing() -> None:
    client, _, _ = _build(provider_url="", credentials_missing=True)

    context = client.get("/api/context", headers=DANA).json()

    assert context["app_identity"]["configured"] is False
    assert "no Databricks credentials" in context["app_identity"]["reason"]
    assert context["provider"] == {"configured": False, "mcp_url": "", "a2a_url": ""}


def test_the_page_escapes_the_caller_uses_only_relative_urls_and_the_health_check_answers() -> None:
    client, _, _ = _build()

    page = client.get("/", headers={"X-Forwarded-Email": "<b>dana</b>@example.com"})

    assert page.status_code == 200
    assert "&lt;b&gt;dana&lt;/b&gt;" in page.text
    # appmux never rewrites a body (design 7.1), so a URL starting with / would leave the prefix.
    for absolute in ('href="/', 'src="/', "fetch('/", 'fetch("/', 'action="/'):
        assert absolute not in page.text
    assert client.get("/healthz").json() == {"status": "ok"}


# --- one call: MCP and A2A, as you or as the app --------------------------------------------


@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
def test_as_me_the_hub_sees_you_and_the_token_that_arrived_with_you(protocol: str) -> None:
    client, platform, _ = _build()

    outcome = _call(client, "me", protocol, "whoami")

    assert outcome["ok"] is True
    assert outcome["sent_as"] == "your own token, forwarded by appmux (OBO)"
    assert outcome["provider_saw"]["x_forwarded_email"] == DANA_EMAIL
    assert outcome["provider_saw"]["access_token_header_present"] is True
    assert set(platform.hub_bearers()) == {USER_BEARER}
    _never_shows_a_token(json.dumps(outcome))


@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
def test_as_the_app_the_hub_sees_the_apps_own_identity_and_its_own_token(protocol: str) -> None:
    client, platform, _ = _build()

    outcome = _call(client, "app", protocol, "whoami")

    assert outcome["sent_as"] == "this App's own service principal"
    assert outcome["provider_saw"]["x_forwarded_email"] == APP_IDENTITY
    assert outcome["provider_saw"]["access_token_header_present"] is True
    assert set(platform.hub_bearers()) == {APP_BEARER}


def test_as_the_app_without_obo_on_the_hub_no_token_arrives_and_databricks_me_says_so() -> None:
    client, _, workspace = _build(obo_delegated=False)

    whoami = _call(client, "app", "mcp", "whoami")
    databricks_me = _call(client, "app", "mcp", "databricks_me")

    assert whoami["provider_saw"]["x_forwarded_email"] == APP_IDENTITY
    assert whoami["provider_saw"]["access_token_header_present"] is False
    assert databricks_me["ok"] is False
    assert "no user token" in databricks_me["error"]
    assert workspace.me_requests == []


@pytest.mark.parametrize("protocol", ["mcp", "a2a"])
def test_databricks_me_acts_as_whoever_the_forwarded_token_belongs_to(protocol: str) -> None:
    client, _, workspace = _build()

    as_me = _call(client, "me", protocol, "databricks_me")
    as_app = _call(client, "app", protocol, "databricks_me")

    assert as_me["ok"] is True
    assert json.loads(as_me["result"])["user_name"] == DANA_EMAIL
    assert as_app["ok"] is True
    assert json.loads(as_app["result"])["user_name"] == APP_IDENTITY
    assert [r.headers["authorization"] for r in workspace.me_requests] == [
        USER_BEARER,
        APP_BEARER,
    ]


def test_the_wire_log_lists_each_request_of_the_protocol_without_a_token() -> None:
    client, _, _ = _build()

    mcp = _call(client, "me", "mcp", "whoami")
    a2a = _call(client, "me", "a2a", "whoami")

    assert [step["step"] for step in mcp["wire"]] == [
        "initialize",
        "notifications/initialized",
        "tools/call whoami",
    ]
    assert [step["step"] for step in a2a["wire"]] == ["SendMessage (call-tool whoami)"]
    assert all(step["status"] in (200, 202) for step in mcp["wire"] + a2a["wire"])


def test_discover_lists_the_tools_over_mcp_and_the_card_over_a2a() -> None:
    client, _, _ = _build()

    tools = _call(client, "me", "mcp", "discover")
    card = _call(client, "me", "a2a", "discover")

    for name in ("whoami", "databricks_me", "add", "utc_now"):
        assert name in tools["result"]
    assert "hub" in card["result"]
    assert "ask" in card["result"]
    assert "call-tool" in card["result"]


def test_ask_over_a2a_lets_the_model_call_a_tool_as_you() -> None:
    client, platform, workspace = _build(
        _tool_reply("databricks_me"), {"role": "assistant", "content": "You are Dana."}
    )

    outcome = _call(client, "me", "a2a", "ask", text="Who am I in Databricks?")

    assert outcome["ok"] is True
    assert outcome["result"] == "You are Dana."
    assert outcome["details"]["caller"] == DANA_EMAIL
    assert [call["name"] for call in outcome["details"]["toolCalls"]] == ["databricks_me"]
    # Two identities in one task: the hub's model call is the hub's own, the lookup is yours.
    assert {r.headers["authorization"] for r in workspace.chat_requests} == {APP_BEARER}
    assert [r.headers["authorization"] for r in workspace.me_requests] == [USER_BEARER]
    assert set(platform.hub_bearers()) == {USER_BEARER}


def test_ask_as_the_app_is_a_task_of_the_app_and_its_tool_has_no_user() -> None:
    client, _, _ = _build(
        _tool_reply("databricks_me"), {"role": "assistant", "content": "I have no user token."}
    )

    outcome = _call(client, "app", "a2a", "ask", text="Who am I in Databricks?")

    assert outcome["ok"] is True
    assert outcome["details"]["caller"] == APP_IDENTITY


def test_without_a_forwarded_token_as_me_is_refused_before_anything_is_sent() -> None:
    client, platform, _ = _build()

    outcome = _call(client, "me", "mcp", "whoami", headers=DANA)

    assert outcome["ok"] is False
    assert "no user token" in outcome["error"]
    assert "OBO" in outcome["error"]
    assert platform.seen == []


def test_when_obo_is_not_delegated_to_the_hub_it_sees_you_but_holds_no_token() -> None:
    client, _, _ = _build(obo_delegated=False)

    outcome = _call(client, "me", "mcp", "whoami")

    assert outcome["provider_saw"]["x_forwarded_email"] == DANA_EMAIL
    assert outcome["provider_saw"]["access_token_header_present"] is False


def test_a_token_the_proxy_does_not_accept_is_reported_as_its_sign_in_answer() -> None:
    client, _, _ = _build()

    outcome = _call(
        client, "me", "mcp", "whoami", headers={**DANA, "X-Forwarded-Access-Token": "other"}
    )

    assert outcome["ok"] is False
    assert "302" in outcome["error"]
    assert "did not accept" in outcome["error"]
    assert outcome["wire"] == [{"step": "initialize", "status": 302}]


def test_a_404_from_appmux_is_explained_as_a_missing_grant_or_a_wrong_address() -> None:
    client, platform, _ = _build()
    platform.hub_status = 404

    outcome = _call(client, "app", "a2a", "whoami")

    assert outcome["ok"] is False
    assert "404" in outcome["error"]
    assert "grant" in outcome["error"]


def test_an_app_with_no_credentials_cannot_call_as_itself() -> None:
    client, _, _ = _build(credentials_missing=True)

    outcome = _call(client, "app", "mcp", "whoami")

    assert outcome["ok"] is False
    assert "no Databricks credentials" in outcome["error"]


def test_without_a_hub_address_a_call_says_which_setting_to_make() -> None:
    client, platform, _ = _build(provider_url="")

    outcome = _call(client, "me", "mcp", "whoami")

    assert outcome["ok"] is False
    assert "PROVIDER_URL" in outcome["error"]
    assert platform.seen == []


# --- what the console refuses ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"identity": "root", "protocol": "mcp", "action": "whoami"}, "'identity'"),
        ({"identity": "me", "protocol": "grpc", "action": "whoami"}, "'protocol'"),
        ({"identity": "me", "protocol": "mcp", "action": "delete"}, "'action'"),
        ({"identity": "me", "protocol": "mcp", "action": "ask", "text": "hi"}, "MCP has no agent"),
        ({"identity": "me", "protocol": "a2a", "action": "ask"}, "'text'"),
        ({"identity": "me", "protocol": "a2a", "action": "ask", "text": "x" * 2001}, "'text'"),
        ([], "JSON object"),
    ],
)
def test_a_call_that_does_not_fit_is_a_400_that_says_what_is_wrong(
    body: Any, expected: str
) -> None:
    client, platform, _ = _build()

    response = client.post("/api/call", json=body, headers=DANA_WITH_TOKEN)

    assert response.status_code == 400
    assert expected in response.json()["error"]
    assert platform.seen == []


def test_a_call_must_be_json_and_must_come_from_this_page() -> None:
    client, platform, _ = _build()
    body = {"identity": "me", "protocol": "mcp", "action": "whoami"}

    as_form = client.post("/api/call", data=body, headers=DANA_WITH_TOKEN)
    cross_site = client.post(
        "/api/call", json=body, headers={**DANA_WITH_TOKEN, "Sec-Fetch-Site": "cross-site"}
    )
    not_json = client.post(
        "/api/call",
        content=b"{nope",
        headers={**DANA_WITH_TOKEN, "Content-Type": "application/json"},
    )
    same_origin = client.post(
        "/api/call", json=body, headers={**DANA_WITH_TOKEN, "Sec-Fetch-Site": "same-origin"}
    )

    assert as_form.status_code == 415
    assert cross_site.status_code == 403
    assert not_json.status_code == 400
    assert same_origin.status_code == 200
    assert len(platform.hub_bearers()) == 3  # only the same-origin call went out


# --- the identity check ---------------------------------------------------------------------


def test_the_identity_check_shows_what_the_hub_sees_for_each_protocol_and_each_identity() -> None:
    client, _, _ = _build()

    response = client.post("/api/identity-check", json={}, headers=DANA_WITH_TOKEN)

    check = response.json()
    assert check["you"] == {"email": DANA_EMAIL, "user_token_forwarded": True}
    cells = {(cell["protocol"], cell["identity"]): cell for cell in check["cells"]}
    assert list(cells) == [("mcp", "me"), ("mcp", "app"), ("a2a", "me"), ("a2a", "app")]
    for protocol in ("mcp", "a2a"):
        me, app = cells[(protocol, "me")], cells[(protocol, "app")]
        assert me["same_as_you"] is True
        assert me["whoami"]["seen_email"] == DANA_EMAIL
        assert me["whoami"]["token_forwarded"] is True
        assert me["databricks_me"] == {"ok": True, "user_name": DANA_EMAIL, "error": None}
        assert app["same_as_you"] is False
        assert app["whoami"]["seen_email"] == APP_IDENTITY
        assert app["whoami"]["token_forwarded"] is True
        assert app["databricks_me"] == {"ok": True, "user_name": APP_IDENTITY, "error": None}
    _never_shows_a_token(response.text)


def test_the_identity_check_without_your_token_fills_the_me_rows_with_the_reason() -> None:
    client, _, _ = _build()

    check = client.post("/api/identity-check", json={}, headers=DANA).json()

    me = next(c for c in check["cells"] if (c["protocol"], c["identity"]) == ("mcp", "me"))
    assert me["same_as_you"] is None
    assert me["whoami"]["ok"] is False
    assert "no user token" in me["whoami"]["error"]
    app = next(c for c in check["cells"] if (c["protocol"], c["identity"]) == ("mcp", "app"))
    assert app["whoami"]["ok"] is True


def test_the_identity_check_is_refused_cross_site() -> None:
    client, platform, _ = _build()

    response = client.post(
        "/api/identity-check", json={}, headers={**DANA_WITH_TOKEN, "Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403
    assert platform.seen == []
