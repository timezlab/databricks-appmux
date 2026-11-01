"""The startup gate and the start-up sequence (T025; design 5.6 and 13.1; FR-008; research D17).

The unit under test is the ASGI application uvicorn is started with (``edge.gate.Gate``) and the
background start-up of ``appmux.__main__``. Nothing here opens a port: requests are driven through
the ASGI interface directly.

What is pinned:

* *starting*: ``GET`` and ``HEAD`` on ``/`` answer ``200`` with a static page, everything else,
  ``/_appmux/`` included, ``503``;
* *blocked*: every path, ``/`` included, ``503`` with a page that names a reason from a fixed list;
* *open*: every request reaches the top-level application;
* the pages carry no identity, registry or environment data, and no text of an exception;
* a registry that fails at start gives *blocked*, never *open*;
* once open, the admin API answers under ``/_appmux/api/v1`` and refuses a request with no identity.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import pytest
from stub_registry import StubRegistry

from appmux import __main__ as entry
from appmux.edge.app import create_edge_app
from appmux.edge.gate import BlockReason, Gate, Phase
from appmux.identity.actor import Actor
from appmux.registry.errors import (
    RegistryRoleMissing,
    RegistryUnavailable,
)
from appmux.registry.protocol import Registry
from appmux.settings import EnvironmentSettings, SettingsError

pytestmark = pytest.mark.anyio

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]

# Fake values that must never appear in a page or a log line.
FAKE_SECRET: Final = "fake-secret-value-0123"  # noqa: S105 - an obviously fake placeholder
FAKE_EMAIL: Final = "alice@example.com"
FAKE_TOKEN: Final = "fake-visitor-token-4567"  # noqa: S105 - an obviously fake placeholder
FAKE_HOST: Final = "workspace.example.invalid"
# A start-up that blocks returns at once; one that wrongly opened would poll for ever.
BLOCKED_WAIT_S: Final = 5
LEAK_MARKERS: Final = (FAKE_SECRET, FAKE_EMAIL, FAKE_TOKEN, FAKE_HOST, "Traceback")

PAGE_PATHS: Final = (
    "/_appmux/",
    "/_appmux",
    "/_appmux/api/v1/me",
    "/sales",
    "/sales/",
    "//",
    "/index.html",
    "/favicon.ico",
    "/static/../",
)


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


def _scope(
    method: str,
    path: str,
    headers: Sequence[tuple[str, str]],
    scope_type: str = "http",
) -> Scope:
    scope: Scope = {
        "type": scope_type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
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
    app: Callable[
        [Scope, Callable[[], Awaitable[Message]], Callable[[Message], Awaitable[None]]],
        Awaitable[None],
    ],
    method: str = "GET",
    path: str = "/",
    *,
    headers: Sequence[tuple[str, str]] = (),
    body: bytes = b"",
) -> Reply:
    messages: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[Message] = []

    async def receive() -> Message:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(_scope(method, path, headers), receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body_parts = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
    return Reply(
        status=start["status"],
        headers={name.decode().lower(): value.decode() for name, value in start["headers"]},
        body=b"".join(body_parts),
    )


async def lifespan(app: Gate, events: Sequence[str]) -> list[str]:
    """Drive the lifespan protocol with ``events`` and return the message types the app sent."""
    pending = [{"type": event} for event in events]
    sent: list[str] = []

    async def receive() -> Message:
        return pending.pop(0)

    async def send(message: Message) -> None:
        sent.append(message["type"])

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    return sent


class RecordingApplication:
    """Stands for the top-level application: records scopes and echoes the request body length."""

    def __init__(self) -> None:
        self.scopes: list[Scope] = []

    async def __call__(
        self,
        scope: Scope,
        receive: Callable[[], Awaitable[Message]],
        send: Callable[[Message], Awaitable[None]],
    ) -> None:
        self.scopes.append(scope)
        request = await receive()
        payload = f"inner:{len(request.get('body', b''))}".encode()
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [(b"content-length", str(len(payload)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": payload})


IDENTITY_HEADERS: Final = (
    ("X-Forwarded-Email", FAKE_EMAIL),
    ("X-Forwarded-Access-Token", FAKE_TOKEN),
    ("Cookie", "session=" + FAKE_SECRET),
)


# --- phase: starting --------------------------------------------------------------------------


async def test_a_new_gate_is_starting() -> None:
    gate = Gate()

    assert gate.phase is Phase.STARTING
    assert gate.reason is None


async def test_starting_answers_get_on_root_with_a_static_page() -> None:
    reply = await call(Gate(), "GET", "/")

    assert reply.status == 200
    assert reply.headers["content-type"].startswith("text/html")
    assert "appmux is starting" in reply.text.lower()
    assert reply.headers["content-length"] == str(len(reply.body))


async def test_starting_answers_head_on_root_with_the_headers_and_no_body() -> None:
    gate = Gate()
    get_reply = await call(gate, "GET", "/")
    head_reply = await call(gate, "HEAD", "/")

    assert head_reply.status == 200
    assert head_reply.body == b""
    assert head_reply.headers["content-length"] == get_reply.headers["content-length"]
    assert head_reply.headers["content-type"] == get_reply.headers["content-type"]


@pytest.mark.parametrize("path", PAGE_PATHS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_starting_answers_nothing_but_root_with_503(method: str, path: str) -> None:
    reply = await call(Gate(), method, path)

    assert reply.status == 503
    assert "retry-after" in reply.headers
    if method == "HEAD":
        assert reply.body == b""
    else:
        assert "starting" in reply.text.lower()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_starting_answers_root_with_503_for_a_method_other_than_get_and_head(
    method: str,
) -> None:
    reply = await call(Gate(), method, "/")

    assert reply.status == 503


async def test_starting_pages_forbid_caching_and_sniffing() -> None:
    gate = Gate()
    for path in ("/", "/sales"):
        reply = await call(gate, "GET", path)

        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in reply.headers["content-security-policy"]


async def test_starting_pages_carry_no_identity_or_environment_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", FAKE_HOST)
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", FAKE_SECRET)
    gate = Gate()

    plain = await call(gate, "GET", "/")
    with_identity = await call(gate, "GET", "/", headers=IDENTITY_HEADERS)
    refused = await call(gate, "GET", "/sales", headers=IDENTITY_HEADERS)

    assert with_identity.body == plain.body  # the same static bytes for everyone
    for reply in (plain, with_identity, refused):
        for marker in LEAK_MARKERS:
            assert marker not in reply.text
            assert marker not in json.dumps(reply.headers)


# --- phase: blocked ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(BlockReason))
async def test_blocked_names_the_reason_on_every_path_root_included(reason: BlockReason) -> None:
    gate = Gate()
    gate.block(reason)

    assert gate.phase is Phase.BLOCKED
    assert gate.reason is reason
    for path in ("/", *PAGE_PATHS):
        reply = await call(gate, "GET", path)

        assert reply.status == 503
        assert reason.title in reply.text
        assert reply.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("reason", list(BlockReason))
async def test_blocked_pages_ask_for_a_retry_and_forbid_caching_and_sniffing(
    reason: BlockReason,
) -> None:
    gate = Gate()
    gate.block(reason)

    for path in ("/", "/sales"):
        reply = await call(gate, "GET", path)

        assert int(reply.headers["retry-after"]) > 0
        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in reply.headers["content-security-policy"]


async def test_blocked_head_gets_the_headers_and_no_body() -> None:
    gate = Gate()
    gate.block(BlockReason.REGISTRY_UNREACHABLE)
    get_reply = await call(gate, "GET", "/")
    head_reply = await call(gate, "HEAD", "/")

    assert head_reply.status == 503
    assert head_reply.body == b""
    assert head_reply.headers["content-length"] == get_reply.headers["content-length"]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_blocked_refuses_every_method(method: str) -> None:
    gate = Gate()
    gate.block(BlockReason.STARTUP_FAILED)

    assert (await call(gate, method, "/_appmux/api/v1/apps")).status == 503


async def test_blocked_pages_differ_by_reason_and_carry_no_identity() -> None:
    gate = Gate()
    gate.block(BlockReason.REGISTRY_UNREACHABLE)
    unreachable = await call(gate, "GET", "/", headers=IDENTITY_HEADERS)
    gate.block(BlockReason.STARTUP_FAILED)
    failed = await call(gate, "GET", "/", headers=IDENTITY_HEADERS)

    assert unreachable.body != failed.body
    for reply in (unreachable, failed):
        for marker in LEAK_MARKERS:
            assert marker not in reply.text


async def test_block_takes_a_reason_from_the_fixed_list_only() -> None:
    gate = Gate()

    free_text: Any = f"connection to {FAKE_HOST} refused, password {FAKE_SECRET}"
    with pytest.raises(TypeError):
        gate.block(free_text)

    assert gate.phase is Phase.STARTING


# --- phase: open ------------------------------------------------------------------------------


async def test_open_hands_every_request_to_the_application() -> None:
    inner = RecordingApplication()
    gate = Gate()
    gate.open(inner)

    assert gate.phase is Phase.OPEN
    for method, path in (("GET", "/"), ("POST", "/sales/x"), ("GET", "/_appmux/")):
        reply = await call(gate, method, path, body=b"abcd")

        assert reply.status == 201
        assert reply.text == "inner:4"
    assert [scope["path"] for scope in inner.scopes] == ["/", "/sales/x", "/_appmux/"]


async def test_open_is_possible_only_from_starting() -> None:
    gate = Gate()
    gate.open(RecordingApplication())

    with pytest.raises(RuntimeError, match="already open"):
        gate.open(RecordingApplication())

    blocked = Gate()
    blocked.block(BlockReason.REGISTRY_UNREACHABLE)
    with pytest.raises(RuntimeError, match="blocked"):
        blocked.open(RecordingApplication())
    assert blocked.phase is Phase.BLOCKED
    assert (await call(blocked, "GET", "/")).status == 503


async def test_blocking_an_open_gate_closes_it_again() -> None:
    # Fail closed: a later failed check may shut the routes; nothing may reopen them.
    inner = RecordingApplication()
    gate = Gate()
    gate.open(inner)
    gate.block(BlockReason.STARTUP_FAILED)

    reply = await call(gate, "GET", "/sales")

    assert reply.status == 503
    assert inner.scopes == []


# --- websocket and lifespan -------------------------------------------------------------------


async def test_a_websocket_is_refused_before_accept_unless_open() -> None:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await Gate()(_scope("GET", "/sales/ws", (), scope_type="websocket"), receive, send)

    assert [(message["type"], message["code"]) for message in sent] == [("websocket.close", 1013)]


async def test_a_websocket_is_refused_the_same_way_when_blocked() -> None:
    gate = Gate()
    gate.block(BlockReason.STARTUP_FAILED)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await gate(_scope("GET", "/sales/ws", (), scope_type="websocket"), receive, send)

    assert [(message["type"], message["code"]) for message in sent] == [("websocket.close", 1013)]


async def test_a_websocket_reaches_the_application_once_open() -> None:
    scopes: list[Scope] = []

    async def application(
        scope: Scope,
        receive: Callable[[], Awaitable[Message]],
        send: Callable[[Message], Awaitable[None]],
    ) -> None:
        scopes.append(scope)
        await send({"type": "websocket.accept"})

    gate = Gate()
    gate.open(application)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await gate(_scope("GET", "/sales/ws", (), scope_type="websocket"), receive, send)

    assert [scope["type"] for scope in scopes] == ["websocket"]
    assert [message["type"] for message in sent] == ["websocket.accept"]


async def test_lifespan_startup_completes_at_once_and_shutdown_runs_the_hooks_in_order() -> None:
    gate = Gate()
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    async def broken() -> None:
        raise RuntimeError(f"hook failed with {FAKE_SECRET}")

    async def last() -> None:
        order.append("last")

    for hook in (first, broken, last):
        gate.add_shutdown_hook(hook)

    sent = await lifespan(gate, ["lifespan.startup", "lifespan.shutdown"])

    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert order == ["first", "last"]  # a failing hook does not stop the next one


async def test_a_hook_added_as_first_runs_before_the_hooks_registered_earlier() -> None:
    # The children must stop before the registry closes, but the hook that stops them can only be
    # added once the application is built, long after the one that closes the registry.
    gate = Gate()
    order: list[str] = []

    async def registered_early() -> None:
        order.append("early")

    async def registered_late_as_first() -> None:
        order.append("late, as first")

    async def registered_last() -> None:
        order.append("last")

    gate.add_shutdown_hook(registered_early)
    gate.add_shutdown_hook(registered_late_as_first, first=True)
    gate.add_shutdown_hook(registered_last)

    await lifespan(gate, ["lifespan.startup", "lifespan.shutdown"])

    assert order == ["late, as first", "early", "last"]


async def test_a_failing_shutdown_hook_is_logged_without_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = Gate()

    async def broken() -> None:
        raise RuntimeError(f"hook failed with {FAKE_SECRET}")

    gate.add_shutdown_hook(broken)
    with caplog.at_level(logging.INFO):
        await lifespan(gate, ["lifespan.startup", "lifespan.shutdown"])

    assert "RuntimeError" in caplog.text
    assert FAKE_SECRET not in caplog.text


# --- the start-up sequence --------------------------------------------------------------------


class ManagedStub(StubRegistry):
    """The in-memory registry with the two extras ``start_application`` needs."""

    def __init__(self, *, refresh_error: BaseException | None = None) -> None:
        super().__init__()
        self.refresh_error = refresh_error
        self.has_refreshed = False
        self.is_polling = False
        self.is_closed = False

    async def refresh(self) -> None:
        if self.refresh_error is not None:
            raise self.refresh_error
        await super().refresh()
        self.has_refreshed = True

    async def poll(self) -> None:
        self.is_polling = True
        try:
            await asyncio.Event().wait()
        finally:
            self.is_polling = False

    async def close(self) -> None:
        self.is_closed = True


class FakeGroups:
    async def groups_for(self, actor: Actor) -> frozenset[str] | None:
        return frozenset()


def real_build(registry: Registry) -> Any:
    return create_edge_app(registry=registry, group_source=FakeGroups())


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the condition did not become true")


async def test_start_up_opens_the_gate_and_keeps_polling_until_cancelled() -> None:
    gate = Gate()
    registry = ManagedStub()
    task = asyncio.create_task(entry.start_application(gate, registry=registry, build=real_build))

    await until(lambda: gate.phase is Phase.OPEN)
    await until(lambda: registry.is_polling)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert registry.is_polling is False  # the poll loop ended with the task


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RegistryUnavailable(), BlockReason.REGISTRY_UNREACHABLE),
        (
            RegistryUnavailable(f"password {FAKE_SECRET} at {FAKE_HOST}"),
            BlockReason.REGISTRY_UNREACHABLE,
        ),
        (RegistryRoleMissing(f"role {FAKE_SECRET}"), BlockReason.REGISTRY_REFUSED),
        (RuntimeError(f"surprise {FAKE_SECRET}"), BlockReason.STARTUP_FAILED),
        (ValueError(f"surprise {FAKE_SECRET}"), BlockReason.STARTUP_FAILED),
    ],
)
async def test_a_registry_that_fails_at_start_blocks_the_gate_and_never_opens_it(
    error: BaseException, reason: BlockReason
) -> None:
    gate = Gate()
    registry = ManagedStub(refresh_error=error)
    built: list[Registry] = []
    stepped: list[Registry] = []

    def build(candidate: Registry) -> Any:
        built.append(candidate)
        return real_build(candidate)

    async def step(candidate: Registry) -> None:
        stepped.append(candidate)

    await asyncio.wait_for(
        entry.start_application(gate, registry=registry, build=build, steps=(step,)),
        timeout=BLOCKED_WAIT_S,
    )

    assert gate.phase is Phase.BLOCKED
    assert gate.reason is reason
    assert built == []  # the application was never built
    assert stepped == []  # no step ran without a first snapshot
    assert registry.is_polling is False
    for path in ("/", "/_appmux/api/v1/me", "/sales"):
        reply = await call(gate, "GET", path, headers=(("X-Forwarded-Email", FAKE_EMAIL),))

        assert reply.status == 503
        assert FAKE_SECRET not in reply.text
        assert FAKE_HOST not in reply.text


async def test_the_reason_text_never_contains_an_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = Gate()
    registry = ManagedStub(refresh_error=RuntimeError(f"dsn password={FAKE_SECRET}"))

    with caplog.at_level(logging.DEBUG):
        await asyncio.wait_for(
            entry.start_application(gate, registry=registry, build=real_build),
            timeout=BLOCKED_WAIT_S,
        )

    page = (await call(gate, "GET", "/")).text
    assert FAKE_SECRET not in page
    assert "RuntimeError" not in page
    assert FAKE_SECRET not in caplog.text  # nor in a log line
    assert "RuntimeError" in caplog.text  # the class is logged so an operator can act


async def test_a_failing_step_or_build_blocks_the_gate() -> None:
    async def failing_step(registry: Registry) -> None:
        raise RuntimeError(f"step failed {FAKE_SECRET}")

    gate = Gate()
    await entry.start_application(
        gate, registry=ManagedStub(), build=real_build, steps=(failing_step,)
    )
    assert gate.phase is Phase.BLOCKED
    assert gate.reason is BlockReason.STARTUP_FAILED

    def failing_build(registry: Registry) -> Any:
        raise RuntimeError(f"build failed {FAKE_SECRET}")

    gate = Gate()
    await entry.start_application(gate, registry=ManagedStub(), build=failing_build)
    assert gate.phase is Phase.BLOCKED
    assert FAKE_SECRET not in (await call(gate, "GET", "/")).text


async def test_steps_run_after_the_first_load_and_before_the_gate_opens() -> None:
    gate = Gate()
    registry = ManagedStub()
    seen: list[tuple[str, Phase, bool, bool]] = []

    def make_step(name: str) -> Callable[[Registry], Awaitable[None]]:
        async def step(candidate: Registry) -> None:
            # ``has_refreshed``: the first snapshot was loaded before this step ran (T067's
            # self_check reads it; on a real registry it raises until the first load).
            seen.append((name, gate.phase, candidate is registry, registry.has_refreshed))

        return step

    task = asyncio.create_task(
        entry.start_application(
            gate,
            registry=registry,
            build=real_build,
            steps=(make_step("one"), make_step("two")),
        )
    )
    await until(lambda: gate.phase is Phase.OPEN)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen == [
        ("one", Phase.STARTING, True, True),
        ("two", Phase.STARTING, True, True),
    ]


async def test_cancelling_the_start_up_leaves_the_gate_starting() -> None:
    gate = Gate()
    released = asyncio.Event()

    class SlowRegistry(ManagedStub):
        async def refresh(self) -> None:
            await released.wait()

    task = asyncio.create_task(
        entry.start_application(gate, registry=SlowRegistry(), build=real_build)
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.phase is Phase.STARTING  # neither blocked nor open: the process is stopping


async def test_the_module_level_steps_are_the_default_and_are_read_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Later tasks (self_check, the reconciler) add their step to STARTUP_STEPS and must not
    # have to touch this test; a monkeypatched tuple must take effect for the same reason.
    assert isinstance(entry.STARTUP_STEPS, tuple)
    assert all(callable(step) for step in entry.STARTUP_STEPS)
    ran: list[Registry] = []

    async def step(candidate: Registry) -> None:
        ran.append(candidate)

    monkeypatch.setattr(entry, "STARTUP_STEPS", (step,))
    registry = ManagedStub()
    task = asyncio.create_task(entry.start_application(Gate(), registry=registry, build=real_build))
    await until(lambda: registry.is_polling)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ran == [registry]


# --- once open: the top-level application ------------------------------------------------------


@pytest.fixture
async def open_gate() -> AsyncIterator[Gate]:
    gate = Gate()
    registry = ManagedStub()
    task = asyncio.create_task(entry.start_application(gate, registry=registry, build=real_build))
    await until(lambda: gate.phase is Phase.OPEN)
    yield gate
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_admin_api_refuses_a_request_with_no_identity(open_gate: Gate) -> None:
    reply = await call(open_gate, "GET", "/_appmux/api/v1/me")

    assert reply.status == 401
    assert json.loads(reply.body)["error"]["code"] == "unauthorized"


async def test_the_admin_api_answers_a_request_with_an_identity(open_gate: Gate) -> None:
    reply = await call(
        open_gate, "GET", "/_appmux/api/v1/me", headers=(("X-Forwarded-Email", FAKE_EMAIL),)
    )

    assert reply.status == 200
    assert json.loads(reply.body)["email"] == FAKE_EMAIL


async def test_the_admin_api_refuses_a_cross_origin_write(open_gate: Gate) -> None:
    reply = await call(
        open_gate,
        "PUT",
        "/_appmux/api/v1/apps/sales/grants",
        headers=(("X-Forwarded-Email", FAKE_EMAIL),),
        body=b"{}",
    )

    assert reply.status == 403


@pytest.mark.parametrize(
    "path",
    ["/", "/_appmux/", "/_appmux", "/_appmux/ui/index.html", "/sales", "/sales/x", "/sales/"],
)
async def test_every_other_path_answers_404_until_the_pipeline_exists(
    open_gate: Gate, path: str
) -> None:
    reply = await call(open_gate, "GET", path, headers=(("X-Forwarded-Email", FAKE_EMAIL),))

    assert reply.status == 404
    assert reply.headers["cache-control"] == "no-store"
    assert reply.text == "not found"  # fixed text: the path is not echoed
    assert FAKE_EMAIL not in reply.text


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE"])
async def test_the_admin_mount_without_a_trailing_slash_is_a_plain_404_never_a_redirect(
    open_gate: Gate, method: str
) -> None:
    # Starlette redirects /x to /x/ by default, building the URL from the Host header and the
    # scope's scheme (plain http behind the TLS-terminating proxy). D5: never redirect.
    reply = await call(
        open_gate,
        method,
        "/_appmux/api/v1",
        headers=(("Host", "evil.example"), ("X-Forwarded-Email", FAKE_EMAIL)),
    )

    assert reply.status == 404
    assert "location" not in reply.headers
    assert reply.headers["cache-control"] == "no-store"
    assert reply.text == ("" if method == "HEAD" else "not found")


async def test_the_admin_mount_without_a_trailing_slash_is_never_a_redirect_in_other_phases() -> (
    None
):
    starting = Gate()
    blocked = Gate()
    blocked.block(BlockReason.STARTUP_FAILED)

    for gate in (starting, blocked):
        reply = await call(gate, "GET", "/_appmux/api/v1")

        assert reply.status == 503
        assert "location" not in reply.headers


async def test_head_on_root_is_404_once_open_with_no_body(open_gate: Gate) -> None:
    reply = await call(open_gate, "HEAD", "/")

    assert reply.status == 404
    assert reply.body == b""


# --- the three untested guards: wiring failure, SIGTERM, bind order ----------------------------


def settings_for_wiring() -> EnvironmentSettings:
    return EnvironmentSettings.from_environment(valid_environment())


async def test_a_failure_while_loading_the_registry_client_blocks_the_gate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The import of psycopg or the SDK failing must not leave the gate answering "starting".
    def failing_wiring(settings: EnvironmentSettings, gate: Gate) -> Any:
        raise ImportError(f"libpq not found, password {FAKE_SECRET}")

    monkeypatch.setattr(entry, "_load_wiring", failing_wiring)
    gate = Gate()

    with caplog.at_level(logging.DEBUG):
        await asyncio.wait_for(
            entry._start_with_real_wiring(gate, settings_for_wiring()), timeout=BLOCKED_WAIT_S
        )

    assert gate.phase is Phase.BLOCKED
    assert gate.reason is BlockReason.STARTUP_FAILED
    assert (await call(gate, "GET", "/")).status == 503
    assert "ImportError" in caplog.text
    assert "loading the registry client" in caplog.text


async def test_a_wiring_that_loads_runs_the_start_up_and_opens_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ManagedStub()
    monkeypatch.setattr(entry, "_load_wiring", lambda settings, gate: (registry, real_build))
    gate = Gate()

    task = asyncio.create_task(entry._start_with_real_wiring(gate, settings_for_wiring()))
    await until(lambda: gate.phase is Phase.OPEN)
    await entry.stop_background(task)

    assert registry.is_closed is True


async def test_every_failure_stage_logs_the_class_and_never_the_message(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_error = f"connection failed, password={FAKE_SECRET} host={FAKE_HOST}"

    async def failing_step(registry: Registry) -> None:
        raise KeyError(secret_error)

    def failing_build(registry: Registry) -> Any:
        raise LookupError(secret_error)

    def failing_wiring(settings: EnvironmentSettings, gate: Gate) -> Any:
        raise OSError(secret_error)

    async def run_registry_load(gate: Gate) -> None:
        registry = ManagedStub(refresh_error=RuntimeError(secret_error))
        await entry.start_application(gate, registry=registry, build=real_build)

    async def run_unavailable_load(gate: Gate) -> None:
        registry = ManagedStub(refresh_error=RegistryUnavailable(secret_error))
        await entry.start_application(gate, registry=registry, build=real_build)

    async def run_refused_load(gate: Gate) -> None:
        registry = ManagedStub(refresh_error=RegistryRoleMissing(secret_error))
        await entry.start_application(gate, registry=registry, build=real_build)

    async def run_step(gate: Gate) -> None:
        await entry.start_application(
            gate, registry=ManagedStub(), build=real_build, steps=(failing_step,)
        )

    async def run_build(gate: Gate) -> None:
        await entry.start_application(gate, registry=ManagedStub(), build=failing_build)

    async def run_wiring(gate: Gate) -> None:
        monkeypatch.setattr(entry, "_load_wiring", failing_wiring)
        await entry._start_with_real_wiring(gate, settings_for_wiring())

    stages: list[tuple[Callable[[Gate], Awaitable[None]], str]] = [
        (run_registry_load, "RuntimeError"),
        (run_unavailable_load, "RegistryUnavailable"),
        (run_refused_load, "RegistryRoleMissing"),
        (run_step, "KeyError"),
        (run_build, "LookupError"),
        (run_wiring, "OSError"),
    ]
    for run, error_class in stages:
        caplog.clear()
        gate = Gate()

        with caplog.at_level(logging.DEBUG):
            await asyncio.wait_for(run(gate), timeout=BLOCKED_WAIT_S)

        assert gate.phase is Phase.BLOCKED, error_class
        assert error_class in caplog.text, error_class
        for marker in (FAKE_SECRET, FAKE_HOST):
            assert marker not in caplog.text, error_class
            assert marker not in (await call(gate, "GET", "/")).text, error_class


class FakeUvicornServer:
    """Stands for ``uvicorn.Server``: ``serve`` does what ``script`` says, ``started`` is a flag."""

    script: Callable[[FakeUvicornServer], Awaitable[None]]

    def __init__(self, config: Any) -> None:
        self.config = config
        self.started = False

    async def serve(self) -> None:
        await type(self).script(self)


def install_fake_server(
    monkeypatch: pytest.MonkeyPatch, script: Callable[[FakeUvicornServer], Awaitable[None]]
) -> None:
    server_class = type("ScriptedServer", (FakeUvicornServer,), {"script": staticmethod(script)})
    monkeypatch.setattr(entry.uvicorn, "Server", server_class)
    monkeypatch.setattr(entry, "BIND_POLL_INTERVAL_S", 0.005)


async def test_sigterm_through_the_lifespan_cancels_the_poll_task_and_closes_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # uvicorn re-raises SIGTERM after serve() returns, so serve()'s own ``finally`` never runs
    # on a signal: the cleanup has to happen in the lifespan shutdown. The fake server checks
    # the state right after the lifespan shutdown, before serve() could clean up itself.
    registries: list[ManagedStub] = []
    observed: dict[str, bool] = {}

    async def wiring(gate: Gate, settings: EnvironmentSettings) -> None:
        registry = ManagedStub()
        registries.append(registry)
        await entry.start_application(gate, registry=registry, build=real_build)

    async def script(server: FakeUvicornServer) -> None:
        gate = server.config.app
        server.started = True
        await until(lambda: bool(registries) and registries[0].is_polling)
        await lifespan(gate, ["lifespan.startup", "lifespan.shutdown"])
        observed["is_closed"] = registries[0].is_closed
        observed["is_polling"] = registries[0].is_polling

    monkeypatch.setattr(entry, "_start_with_real_wiring", wiring)
    install_fake_server(monkeypatch, script)

    await entry.serve(settings_for_wiring())

    assert observed == {"is_closed": True, "is_polling": False}


async def test_the_shutdown_hook_stops_a_running_start_up_task() -> None:
    gate = Gate()
    registry = ManagedStub()
    task = asyncio.create_task(entry.start_application(gate, registry=registry, build=real_build))
    await until(lambda: registry.is_polling)

    entry.stop_background_on_shutdown(gate, task)
    await lifespan(gate, ["lifespan.startup", "lifespan.shutdown"])

    assert task.cancelled()
    assert registry.is_closed is True


async def test_the_registry_is_not_touched_before_the_port_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def wiring(gate: Gate, settings: EnvironmentSettings) -> None:
        events.append("wiring")

    async def script(server: FakeUvicornServer) -> None:
        await asyncio.sleep(0.05)  # many poll intervals: the start-up has had every chance
        events.append("bound")
        server.started = True
        await until(lambda: "wiring" in events)

    monkeypatch.setattr(entry, "_start_with_real_wiring", wiring)
    install_fake_server(monkeypatch, script)

    await entry.serve(settings_for_wiring())

    assert events == ["bound", "wiring"]


async def test_a_bind_failure_ends_serve_without_starting_the_registry_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def wiring(gate: Gate, settings: EnvironmentSettings) -> None:
        events.append("wiring")

    async def script(server: FakeUvicornServer) -> None:
        await asyncio.sleep(0.02)
        raise SystemExit(1)  # what uvicorn does when the port cannot be bound

    monkeypatch.setattr(entry, "_start_with_real_wiring", wiring)
    install_fake_server(monkeypatch, script)

    with pytest.raises(SystemExit):
        await entry.serve(settings_for_wiring())
    await asyncio.sleep(0.03)

    assert events == []


# --- the entry point --------------------------------------------------------------------------


def valid_environment() -> dict[str, str]:
    return {
        "DATABRICKS_HOST": FAKE_HOST,
        "DATABRICKS_CLIENT_ID": "client-id-placeholder",
        "DATABRICKS_CLIENT_SECRET": FAKE_SECRET,
        "DATABRICKS_APP_PORT": "8123",
        "DATABRICKS_WORKSPACE_ID": "1234567890",
        "DATABRICKS_APP_URL": "https://appmux.example.invalid",
        "PGHOST": "database.example.invalid",
        "APPMUX_LAKEBASE_DATABASE": "appmux",
    }


def test_the_server_config_binds_the_platform_port_with_the_gate() -> None:
    settings = EnvironmentSettings.from_environment(valid_environment())
    gate = Gate()

    config = entry.server_config(settings, gate)

    assert config.app is gate
    assert config.port == 8123
    assert config.host == "0.0.0.0"  # noqa: S104 - the proxy reaches the container on any address
    assert config.lifespan == "on"
    assert config.proxy_headers is False  # the edge reads X-Real-Ip itself
    assert config.access_log is False  # a query string may carry a token
    assert config.server_header is False  # no server banner on a page anyone can reach
    assert config.timeout_graceful_shutdown is not None
    assert config.timeout_graceful_shutdown < 15  # the platform allows 15 s on SIGTERM


def test_invalid_settings_stop_the_start_before_anything_binds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started: list[EnvironmentSettings] = []

    def fake_serve(settings: EnvironmentSettings) -> None:
        started.append(settings)

    environment = valid_environment()
    del environment["PGHOST"]
    environment["DATABRICKS_APP_PORT"] = "not-a-port"

    with caplog.at_level(logging.ERROR):
        code = entry.main(environment, run=fake_serve)

    assert code != 0
    assert started == []
    assert "PGHOST" in caplog.text
    assert "DATABRICKS_APP_PORT" in caplog.text
    assert "not-a-port" not in caplog.text


def test_valid_settings_start_the_server() -> None:
    started: list[EnvironmentSettings] = []

    def fake_serve(settings: EnvironmentSettings) -> None:
        started.append(settings)

    code = entry.main(valid_environment(), run=fake_serve)

    assert code == 0
    assert [settings.databricks_app_port for settings in started] == [8123]


def test_settings_error_is_the_error_the_entry_point_catches() -> None:
    with pytest.raises(SettingsError):
        EnvironmentSettings.from_environment({})


async def test_stopping_the_background_work_cancels_the_task_and_closes_the_registry() -> None:
    registry = ManagedStub()
    gate = Gate()
    task = asyncio.create_task(entry.start_application(gate, registry=registry, build=real_build))
    await until(lambda: registry.is_polling)

    await entry.stop_background(task)
    await entry.stop_background(task)  # idempotent: the hook and the finally both run

    assert task.cancelled()
    assert registry.is_closed is True
