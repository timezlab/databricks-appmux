"""Shared set-up for the contract tests that drive the whole edge application (T048, T049).

Not a test module: it holds the starting data, a fake group lookup, a scripted canary and a driver
that sends one request through the ASGI interface (nothing opens a port). All emails are
placeholders.

Starting data:

* ``sales``: owner alice; bob ``CAN_USE`` and carol ``CAN_MANAGE`` by user grants;
* ``docs``: owner frank, no other grant;
* ``platform_admins``: dana as a user.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Collection, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, TypeVar

import httpx
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from stub_registry import StubRegistry

from appmux.admin.api import RouteProvider, create_admin_app
from appmux.admin.authorizer import GroupSource, RegistryAuthorizer, make_authorizer
from appmux.identity.actor import Actor
from appmux.sandbox.self_check import CHECK_IDS
from appmux.settings import EnvironmentSettings

ALICE: Final = "alice@example.com"
BOB: Final = "bob@example.com"
CAROL: Final = "carol@example.com"
DANA: Final = "dana@example.com"
FRANK: Final = "frank@example.com"
VISITOR_TOKEN: Final = "visitor-token-value-for-tests"  # noqa: S105 - a fake value for a test
NOW: Final = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
MOUNT: Final = "/_appmux/api/v1"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
App = Callable[
    [Scope, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]],
    Awaitable[None],
]


class FakeGroups:
    """A group lookup that knows no group: people here are named by grants and the admin list."""

    async def groups_for(self, actor: Actor) -> frozenset[str] | None:
        return frozenset()


def app_row(slug: str, *, owner: str) -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": slug.title(),
        "owner": owner,
        "source_path": f"/Workspace/Users/{owner}/{slug}",
        "memory_budget_mb": 256,
        "created_by": DANA,
        "created_at": NOW,
        "updated_by": DANA,
        "updated_at": NOW,
    }


def seeded_registry(*, platform_writer_tokens: Collection[str] | None = None) -> StubRegistry:
    registry = StubRegistry(clock=lambda: NOW, platform_writer_tokens=platform_writer_tokens)
    return seed_standard_rows(registry)


SeededRegistry = TypeVar("SeededRegistry", bound=StubRegistry)


def seed_standard_rows(registry: SeededRegistry) -> SeededRegistry:
    """Put the apps, grants and platform admin of ``seeded_registry`` into ``registry``."""
    for row in (app_row("sales", owner=ALICE), app_row("docs", owner=FRANK)):
        registry.seed("apps", row)
        registry.seed(
            "app_config",
            {
                "app_slug": row["slug"],
                "desired_state": "running",
                "updated_by": DANA,
                "updated_at": NOW,
            },
        )
    for person, level in ((BOB, "CAN_USE"), (CAROL, "CAN_MANAGE")):
        registry.seed(
            "app_grants",
            {
                "app_slug": "sales",
                "principal_type": "user",
                "principal": person,
                "permission": level,
                "granted_by": ALICE,
                "granted_at": NOW,
            },
        )
    registry.seed(
        "platform_settings",
        {
            "key": "platform_admins",
            "value": [{"type": "user", "name": DANA}],
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )
    return registry


def settings_for_wiring() -> EnvironmentSettings:
    """Placeholder settings for the real ``_load_wiring`` (it opens no connection)."""
    return EnvironmentSettings.from_environment(
        {
            "DATABRICKS_HOST": "workspace.example.invalid",
            "DATABRICKS_CLIENT_ID": "client-id-placeholder",
            "DATABRICKS_CLIENT_SECRET": "secret-placeholder-for-tests",
            "DATABRICKS_APP_PORT": "8123",
            "DATABRICKS_WORKSPACE_ID": "1234567890",
            "DATABRICKS_APP_URL": "https://appmux.example.invalid",
            "PGHOST": "database.example.invalid",
            "APPMUX_LAKEBASE_DATABASE": "appmux",
        }
    )


def canary_text(
    *, failing: Mapping[str, str] | None = None, passing_details: Mapping[str, str] | None = None
) -> str:
    """What a canary prints: every check passing unless ``failing`` names it, with its detail."""
    failing = failing or {}
    details = passing_details or {}
    return json.dumps(
        {
            "checks": [
                {
                    "id": check_id,
                    "passed": check_id not in failing,
                    "detail": failing.get(check_id, details.get(check_id, "")),
                }
                for check_id in CHECK_IDS
            ]
        }
    )


class ScriptedCanary:
    """A canary runner that prints the next scripted text (the last one again when it runs out)."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts) or [canary_text()]
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    headers: Mapping[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode()

    def json(self) -> Any:
        return json.loads(self.body)


def scope_of(
    method: str,
    path: str,
    headers: Sequence[tuple[str, str]] = (),
    *,
    scope_type: str = "http",
    raw_path: str | None = None,
) -> Scope:
    """An ASGI scope. ``path`` is the decoded path; ``raw_path`` is what the client sent (as the
    server would pass it), which differs from ``path`` only when the client percent-encoded it.
    """
    scope: Scope = {
        "type": scope_type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "path": path,
        "raw_path": (path if raw_path is None else raw_path).encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    if scope_type == "http":
        scope["method"] = method
    return scope


async def call(
    app: App,
    method: str,
    path: str,
    *,
    email: str | None = None,
    headers: Sequence[tuple[str, str]] = (),
    json_body: object = None,
    same_origin: bool | None = None,
    raw_path: str | None = None,
) -> Reply:
    """One request through ``app``; a change carries ``Sec-Fetch-Site: same-origin`` by default."""
    sent_headers = list(headers)
    if email is not None:
        sent_headers += [("X-Forwarded-Email", email), ("X-Forwarded-Access-Token", VISITOR_TOKEN)]
    if same_origin is None:
        same_origin = method not in ("GET", "HEAD", "OPTIONS")
    if same_origin:
        sent_headers.append(("Sec-Fetch-Site", "same-origin"))
    body = b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        sent_headers.append(("Content-Type", "application/json"))
    pending: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[Message] = []
    response_over = asyncio.Event()

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        # As a server does: after the body, the client is still there until the response is over.
        # An app that reads on (the request pipeline listens for a hang-up) would otherwise take
        # the end of the body for a client that left.
        await response_over.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            response_over.set()

    await app(scope_of(method, path, sent_headers, raw_path=raw_path), receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    return Reply(
        status=start["status"],
        headers={name.decode().lower(): value.decode() for name, value in start["headers"]},
        body=b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"),
    )


class AdminApi:
    """The admin API alone (no gate, no UI) over ``registry``, for tests of one route provider.

    ``providers`` receives the real ``RegistryAuthorizer`` (so a provider that needs the
    authorizer's access, as ``IdentityRoutes`` does, can take it) and returns the route providers
    to mount. ``call`` sends a request as ``email`` with a placeholder visitor token; a change
    carries ``Sec-Fetch-Site: same-origin`` unless ``same_origin`` says otherwise.
    """

    def __init__(
        self,
        registry: StubRegistry,
        providers: Callable[[RegistryAuthorizer], Sequence[RouteProvider]],
        *,
        groups: GroupSource | None = None,
        bootstrap_admins: Sequence[str] = (),
    ) -> None:
        self.registry = registry
        self.authorizer = make_authorizer(
            registry,
            groups if groups is not None else FakeGroups(),
            bootstrap_admins=bootstrap_admins,
        )
        admin = create_admin_app(
            registry=registry,
            authorizer=self.authorizer,
            route_providers=list(providers(self.authorizer)),
        )
        self._client = TestClient(Starlette(routes=[Mount(MOUNT, app=admin)]))

    def call(
        self,
        method: str,
        path: str,
        email: str | None,
        *,
        json: object = None,
        headers: Mapping[str, str] | None = None,
        same_origin: bool | None = None,
    ) -> httpx.Response:
        sent: dict[str, str] = {}
        if email is not None:
            sent["X-Forwarded-Email"] = email
            sent["X-Forwarded-Access-Token"] = VISITOR_TOKEN
        if same_origin is None:
            same_origin = method not in ("GET", "HEAD", "OPTIONS")
        if same_origin:
            sent["Sec-Fetch-Site"] = "same-origin"
        sent.update(headers or {})
        return self._client.request(method, f"{MOUNT}{path}", headers=sent, json=json)
