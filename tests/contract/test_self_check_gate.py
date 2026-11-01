"""The self-check gate and ``POST /platform/self-check`` (T048; T065, T067; FR-013, design 6.4).

The unit under test is the edge application (``create_edge_app``) with the shared
``SelfCheckState``, and the start-up wiring of ``appmux.__main__`` that runs the self-check before
the startup gate opens. Nothing here opens a port; requests are driven through the ASGI interface.

What is pinned:

* while the latest self-check is not a pass (it failed, or has not run), every request but the
  ones for ``/_appmux/`` answers ``503`` with a status page that names the failed checks by their
  fixed ids and nothing else (no detail text, no identity); a passing result changes nothing;
* ``/_appmux/`` stays reachable, so a platform admin can read the result and run it again, but only
  by an exact path: a path that could mean something else after normalisation is gated;
* ``POST /platform/self-check`` needs ``CAN_MANAGE`` on the platform, re-runs the canary, returns
  the per-check result in the contract's shape and opens or closes the gate with it;
* the start-up runs the self-check before the gate opens, and with no canary runner configured it
  fails closed with a result that says so.
"""

from __future__ import annotations

import asyncio
import html
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
from edge_harness import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    FRANK,
    MOUNT,
    NOW,
    App,
    FakeGroups,
    Message,
    ScriptedCanary,
    call,
    canary_text,
    scope_of,
    seeded_registry,
    settings_for_wiring,
)
from service_harness import install_service_doubles
from stub_registry import StubRegistry

from appmux import __main__ as entry
from appmux.admin.schemas import SelfCheckResult
from appmux.edge.app import create_edge_app
from appmux.edge.gate import Gate, Phase
from appmux.registry.protocol import Registry
from appmux.sandbox.self_check import (
    CANARY_CHECK_NAME,
    CHECK_IDS,
    CanaryUnavailable,
    SelfCheckState,
    describe_check,
)
from appmux.settings import EnvironmentSettings

pytestmark = pytest.mark.anyio

SELF_CHECK_PATH: Final = f"{MOUNT}/platform/self-check"
ME_PATH: Final = f"{MOUNT}/me"
FAILED_CHECK: Final = "pid_isolation"
# What a canary prints about a failure; only the platform admin's response may carry it.
DETAIL_MARKER: Final = "saw-process-one-marker"
SECOND_FAILED_CHECK: Final = "metadata_address"
LEAK_MARKERS: Final = (DETAIL_MARKER, ALICE, BOB, DANA, "Traceback")
# The real wiring imports psycopg and the SDK in a worker thread; the first import is slow.
UNTIL_ATTEMPTS: Final = 4000


@pytest.fixture(autouse=True)
def service_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real wiring's workspace, token, container and reconciler touch nothing outside.

    Installed before each test body, so a test that patches ``_container_ip`` itself overrides it.
    """
    install_service_doubles(monkeypatch)


async def ready_state(*texts: str) -> tuple[SelfCheckState, ScriptedCanary]:
    """A state whose first run (the start-up's) has already finished with the first text."""
    canary = ScriptedCanary(*texts)
    state = SelfCheckState(canary)
    await state.run()
    return state, canary


def failing_text(*names: str) -> str:
    return canary_text(failing=dict.fromkeys(names or (FAILED_CHECK,), DETAIL_MARKER))


def edge(state: SelfCheckState | None, registry: StubRegistry | None = None) -> App:
    return create_edge_app(
        registry=registry or seeded_registry(),
        group_source=FakeGroups(),
        self_check=state,
    )


async def websocket_messages(app: App, path: str) -> list[Message]:
    sent: list[Message] = []
    pending: list[Message] = [{"type": "websocket.connect"}]

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "websocket.disconnect", "code": 1000}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope_of("GET", path, scope_type="websocket"), receive, send)
    return sent


# --- a passing result ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/sales", "/sales/x", "/_appmux/api/v1/nowhere"])
async def test_a_passing_result_leaves_the_application_as_it_was(path: str) -> None:
    state, _ = await ready_state()

    reply = await call(edge(state), "GET", path, email=BOB)

    assert reply.status == 404
    assert reply.text != "" and "self-check" not in reply.text.lower()


async def test_without_a_self_check_state_the_edge_is_not_gated() -> None:
    # The seam for code that builds the edge alone; the start-up always passes the shared state
    # (see the wiring tests below).
    reply = await call(edge(None), "GET", "/sales/x", email=BOB)

    assert reply.status == 404


# --- a failed or missing result -----------------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("path", ["/", "/sales", "/sales/x", "/index.html", "/favicon.ico"])
async def test_a_failed_result_answers_503_on_every_path_and_method(method: str, path: str) -> None:
    state, _ = await ready_state(failing_text())

    reply = await call(edge(state), method, path, email=BOB)

    assert reply.status == 503
    assert reply.headers["content-type"] == "text/html; charset=utf-8"
    assert reply.headers["cache-control"] == "no-store"
    assert reply.headers["x-content-type-options"] == "nosniff"
    assert reply.headers["retry-after"].isdigit()
    assert "default-src 'none'" in reply.headers["content-security-policy"]
    assert int(reply.headers["content-length"]) == len(reply.body)


async def test_the_page_names_the_failed_check_and_nothing_a_canary_printed() -> None:
    state, _ = await ready_state(failing_text())

    reply = await call(edge(state), "GET", "/sales/x", email=BOB)

    assert FAILED_CHECK in reply.text
    assert html.escape(describe_check(FAILED_CHECK)) in reply.text
    for marker in LEAK_MARKERS:
        assert marker not in reply.text
    passing = [name for name in CHECK_IDS if name != FAILED_CHECK]
    assert not any(name in reply.text for name in passing)


async def test_the_page_names_every_failed_check_in_run_order() -> None:
    state, _ = await ready_state(failing_text(SECOND_FAILED_CHECK, FAILED_CHECK, "cpu_pin"))

    text = (await call(edge(state), "GET", "/", email=BOB)).text

    positions = [text.index(name) for name in ("pid_isolation", "metadata_address", "cpu_pin")]
    assert positions == sorted(positions)
    assert "environ_isolation" not in text


async def test_head_gets_the_headers_of_the_page_and_no_body() -> None:
    state, _ = await ready_state(failing_text())
    app = edge(state)

    get = await call(app, "GET", "/sales")
    head = await call(app, "HEAD", "/sales")

    assert head.status == 503 and head.body == b""
    assert head.headers["content-length"] == get.headers["content-length"] == str(len(get.body))


async def test_before_the_first_run_finishes_nothing_is_served_and_the_page_names_the_canary() -> (
    None
):
    state = SelfCheckState(ScriptedCanary())

    reply = await call(edge(state), "GET", "/sales/x", email=BOB)

    assert reply.status == 503
    assert CANARY_CHECK_NAME in reply.text


async def test_a_canary_that_cannot_run_blocks_the_edge_and_the_page_keeps_its_message_back() -> (
    None
):
    async def no_runner() -> str:
        raise CanaryUnavailable("no canary runner is configured")

    state = SelfCheckState(no_runner)
    await state.run()

    reply = await call(edge(state), "GET", "/", email=BOB)

    assert reply.status == 503
    assert CANARY_CHECK_NAME in reply.text
    assert "no canary runner" not in reply.text  # a detail is for the platform page


async def test_a_websocket_is_refused_before_accept_while_the_result_is_failed() -> None:
    state, _ = await ready_state(failing_text())

    sent = await websocket_messages(edge(state), "/sales/socket")

    assert sent == [{"type": "websocket.close", "code": 1013}]


# --- /_appmux/ stays reachable ------------------------------------------------------------------


async def test_the_admin_api_answers_while_the_result_is_failed() -> None:
    state, _ = await ready_state(failing_text())

    reply = await call(edge(state), "GET", ME_PATH, email=DANA)

    assert reply.status == 200
    assert reply.json()["email"] == DANA


@pytest.mark.parametrize("path", ["/_appmux", "/_appmux/", "/_appmux/ui/index.html"])
async def test_the_admin_pages_are_not_answered_by_the_status_page(path: str) -> None:
    state, _ = await ready_state(failing_text())

    reply = await call(edge(state), "GET", path, email=DANA)

    assert reply.status != 503
    assert FAILED_CHECK not in reply.text


async def test_the_admin_websocket_path_is_not_closed_with_try_again_later() -> None:
    state, _ = await ready_state(failing_text())

    sent = await websocket_messages(edge(state), f"{MOUNT}/me")

    assert {"type": "websocket.close", "code": 1013} not in sent
    assert sent  # the admin API itself refuses it


# (decoded path, raw path as the client sent it). The decoded path is what the router sees; the raw
# one shows an encoded dot, slash or backslash that a later layer might decode again.
NOT_AN_ADMIN_PATH: Final = [
    ("/_appmux/../sales", "/_appmux/../sales"),
    ("/_appmux/../sales", "/_appmux/%2e%2e/sales"),
    ("/_appmux/../sales", "/_appmux/%2E%2E/sales"),
    ("/_appmux/./x", "/_appmux/./x"),
    ("/_appmux/api/../../sales", "/_appmux/api/../../sales"),
    ("/_appmux/x/y", "/_appmux/x%2Fy"),
    ("/_appmux/x\\y", "/_appmux/x%5Cy"),
    ("/_appmux/..%2fsales", "/_appmux/..%2fsales"),
    ("/_appmux//x", "/_appmux//x"),
    ("//_appmux/x", "//_appmux/x"),
    ("/_appmux/x//", "/_appmux/x//"),
    ("/_appmuxfoo", "/_appmuxfoo"),
    ("/_appmux.json", "/_appmux.json"),
    ("/_Appmux/", "/_Appmux/"),
    ("/_APPMUX/", "/_APPMUX/"),
    ("/ _appmux/x", "/%20_appmux/x"),
    ("/_appmux\\x", "/_appmux\\x"),
    ("/_appmux/x\\..\\sales", "/_appmux/x\\..\\sales"),
    ("/_appmux/x\x00", "/_appmux/x%00"),
    ("/_appmux/x\n", "/_appmux/x%0A"),
    ("/x/_appmux/", "/x/_appmux/"),
    ("/sales/_appmux/", "/sales/_appmux/"),
]


@pytest.mark.parametrize(("path", "raw_path"), NOT_AN_ADMIN_PATH)
async def test_only_an_exact_admin_path_skips_the_status_page(path: str, raw_path: str) -> None:
    # Whatever a later layer would make of these, they are not "/_appmux/..." as written: a request
    # that reached a child by way of the exemption would bypass the self-check.
    state, _ = await ready_state(failing_text())

    reply = await call(edge(state), "GET", path, email=BOB, raw_path=raw_path)

    assert reply.status == 503
    assert FAILED_CHECK in reply.text


async def test_an_encoded_character_that_is_not_a_dot_or_a_slash_keeps_the_admin_path() -> None:
    # The page encodes an email with encodeURIComponent; that must still reach the admin API.
    state, _ = await ready_state(failing_text())

    reply = await call(
        edge(state),
        "DELETE",
        f"{MOUNT}/apps/sales/grants/user/bob@example.com",
        email=DANA,
        raw_path=f"{MOUNT}/apps/sales/grants/user/bob%40example.com",
    )

    assert reply.status != 503
    assert FAILED_CHECK not in reply.text


# --- POST /platform/self-check ------------------------------------------------------------------


async def test_a_platform_admin_runs_the_canary_again_and_gets_the_per_check_result() -> None:
    state, canary = await ready_state(failing_text(), canary_text())

    reply = await call(edge(state), "POST", SELF_CHECK_PATH, email=DANA)

    assert reply.status == 200
    assert canary.calls == 2
    result = SelfCheckResult.model_validate_json(reply.body)
    assert result.passed is True
    assert [check.name for check in result.checks] == list(CHECK_IDS)
    assert all(check.passed and check.detail is None for check in result.checks)
    assert result.duration_ms >= 0


async def test_a_failed_run_is_a_200_with_the_failed_check_and_its_detail_for_the_admin() -> None:
    state, _ = await ready_state(canary_text(), failing_text(SECOND_FAILED_CHECK))

    reply = await call(edge(state), "POST", SELF_CHECK_PATH, email=DANA)

    assert reply.status == 200
    result = SelfCheckResult.model_validate_json(reply.body)
    assert result.passed is False
    failed = {check.name: check.detail for check in result.checks if not check.passed}
    assert failed == {SECOND_FAILED_CHECK: DETAIL_MARKER}


async def test_a_canary_that_cannot_run_is_a_failed_result_not_a_server_error() -> None:
    async def broken() -> str:
        raise CanaryUnavailable("no canary runner is configured")

    state = SelfCheckState(broken)

    reply = await call(edge(state), "POST", SELF_CHECK_PATH, email=DANA)

    assert reply.status == 200
    result = SelfCheckResult.model_validate_json(reply.body)
    assert result.passed is False
    assert [(c.name, c.detail) for c in result.checks] == [
        (CANARY_CHECK_NAME, "no canary runner is configured")
    ]


async def test_the_result_of_a_run_opens_and_closes_the_gate_both_ways() -> None:
    state, _ = await ready_state(failing_text(), canary_text(), failing_text(SECOND_FAILED_CHECK))
    app = edge(state)
    assert (await call(app, "GET", "/sales/x", email=BOB)).status == 503

    passed = await call(app, "POST", SELF_CHECK_PATH, email=DANA)
    assert passed.json()["passed"] is True
    assert (await call(app, "GET", "/sales/x", email=BOB)).status == 404

    failed = await call(app, "POST", SELF_CHECK_PATH, email=DANA)
    assert failed.json()["passed"] is False
    page = await call(app, "GET", "/sales/x", email=BOB)
    assert page.status == 503
    assert SECOND_FAILED_CHECK in page.text and FAILED_CHECK not in page.text


@pytest.mark.parametrize("person", [BOB, CAROL, ALICE, FRANK])
async def test_only_a_platform_admin_may_run_it(person: str) -> None:
    # bob can use `sales`, carol manages it, alice owns it: none of that is the platform.
    state, canary = await ready_state()

    reply = await call(edge(state), "POST", SELF_CHECK_PATH, email=person)

    assert reply.status == 403
    assert reply.json()["error"]["code"] == "forbidden"
    assert canary.calls == 1  # the start-up's run only


async def test_a_request_with_no_identity_is_401_and_runs_nothing() -> None:
    state, canary = await ready_state()

    reply = await call(edge(state), "POST", SELF_CHECK_PATH)

    assert reply.status == 401
    assert canary.calls == 1


async def test_a_request_that_is_not_same_origin_is_403_and_runs_nothing() -> None:
    state, canary = await ready_state()

    reply = await call(edge(state), "POST", SELF_CHECK_PATH, email=DANA, same_origin=False)

    assert reply.status == 403
    assert reply.json()["error"]["code"] == "cross_origin"
    assert canary.calls == 1


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_the_route_answers_post_only(method: str) -> None:
    state, canary = await ready_state()

    reply = await call(edge(state), method, SELF_CHECK_PATH, email=DANA)

    assert reply.status == 405
    assert canary.calls == 1


async def test_a_run_changes_no_row_so_it_writes_no_audit_entry() -> None:
    registry = seeded_registry()
    state, _ = await ready_state()

    reply = await call(edge(state, registry), "POST", SELF_CHECK_PATH, email=DANA)

    assert reply.status == 200
    assert registry.audit_rows == ()


async def test_two_runs_at_once_never_overlap() -> None:
    running = 0
    most_at_once = 0
    release = asyncio.Event()

    async def slow_canary() -> str:
        nonlocal running, most_at_once
        running += 1
        most_at_once = max(most_at_once, running)
        await release.wait()
        running -= 1
        return canary_text()

    state = SelfCheckState(slow_canary)
    app = edge(state)
    first = asyncio.create_task(call(app, "POST", SELF_CHECK_PATH, email=DANA))
    second = asyncio.create_task(call(app, "POST", SELF_CHECK_PATH, email=DANA))
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()
    replies = await asyncio.gather(first, second)

    assert [reply.status for reply in replies] == [200, 200]
    assert most_at_once == 1


async def test_without_a_state_the_route_says_the_check_is_not_set_up() -> None:
    reply = await call(edge(None), "POST", SELF_CHECK_PATH, email=DANA)

    assert reply.status == 503
    assert reply.json()["error"]["code"] == "self_check_unavailable"


# --- the start-up -------------------------------------------------------------------------------


class ManagedStub(StubRegistry):
    """The in-memory registry with the two extras ``start_application`` needs."""

    def __init__(self) -> None:
        super().__init__()
        self.is_closed = False

    async def poll(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.is_closed = True


def stub_with_admin() -> ManagedStub:
    stub = ManagedStub()
    stub.seed(
        "platform_settings",
        {
            "key": "platform_admins",
            "value": [{"type": "user", "name": DANA}],
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )
    return stub


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(UNTIL_ATTEMPTS):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the condition did not become true")


async def start_with_wiring(
    monkeypatch: pytest.MonkeyPatch, registry: ManagedStub, gate: Gate
) -> asyncio.Task[None]:
    """Run the real start-up of ``__main__`` on ``registry``; ``_load_wiring`` stays real."""
    real_load_wiring = entry._load_wiring

    def load_wiring(settings: EnvironmentSettings, candidate: Gate) -> Any:
        _, built = real_load_wiring(settings, candidate)  # makes no network call
        return registry, built

    monkeypatch.setattr(entry, "_load_wiring", load_wiring)
    task = asyncio.create_task(entry._start_with_real_wiring(gate, settings_for_wiring()))
    await until(lambda: gate.phase is not Phase.STARTING)
    return task


async def test_the_start_up_runs_the_self_check_before_the_gate_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = ScriptedCanary()
    seen_at_run: list[Phase] = []
    gate = Gate()

    async def runner() -> str:
        seen_at_run.append(gate.phase)
        return await canary()

    monkeypatch.setattr(entry, "_canary_runner", lambda settings: runner)
    task = await start_with_wiring(monkeypatch, stub_with_admin(), gate)

    assert gate.phase is Phase.OPEN
    assert seen_at_run == [Phase.STARTING]
    assert (await call(gate, "GET", "/sales/x", email=BOB)).status == 404
    await entry.stop_background(task)


async def test_with_an_unknown_container_address_the_start_up_fails_closed_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real runner needs the container's address; without it nothing can be tested.
    monkeypatch.setattr(entry, "_container_ip", lambda: None)
    gate = Gate()
    registry = stub_with_admin()

    task = await start_with_wiring(monkeypatch, registry, gate)

    # The gate opens (the admin pages must stay reachable) but nothing else is served.
    assert gate.phase is Phase.OPEN
    page = await call(gate, "GET", "/", email=BOB)
    assert page.status == 503
    assert CANARY_CHECK_NAME in page.text
    result = await call(gate, "POST", SELF_CHECK_PATH, email=DANA)
    assert result.status == 200
    body = result.json()
    assert body["passed"] is False
    assert [c["name"] for c in body["checks"]] == [CANARY_CHECK_NAME]
    assert "container's address is unknown" in body["checks"][0]["detail"]
    assert (await call(gate, "GET", ME_PATH, email=DANA)).status == 200
    await entry.stop_background(task)


async def test_a_failed_canary_does_not_stop_the_start_up_and_a_later_pass_opens_the_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = ScriptedCanary(failing_text(), canary_text())
    monkeypatch.setattr(entry, "_canary_runner", lambda settings: canary)
    gate = Gate()

    task = await start_with_wiring(monkeypatch, stub_with_admin(), gate)

    assert gate.phase is Phase.OPEN
    assert (await call(gate, "GET", "/sales/x", email=BOB)).status == 503
    again = await call(gate, "POST", SELF_CHECK_PATH, email=DANA)
    assert again.json()["passed"] is True
    assert (await call(gate, "GET", "/sales/x", email=BOB)).status == 404
    await entry.stop_background(task)


async def test_the_default_runner_without_a_container_address_raises_canary_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entry, "_container_ip", lambda: None)
    runner = entry._canary_runner(settings_for_wiring())

    with pytest.raises(CanaryUnavailable, match="container's address is unknown"):
        await runner()


async def test_the_default_runner_hands_the_facts_of_this_container_to_the_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ipaddress import IPv4Address

    from appmux.sandbox import canary as canary_module
    from appmux.settings import PlatformSettings

    configs: list[canary_module.CanaryConfig] = []

    async def fake_run_canary(config: canary_module.CanaryConfig) -> str:
        configs.append(config)
        return canary_text()

    container_ip = IPv4Address("10.0.2.15")
    monkeypatch.setattr(entry, "_container_ip", lambda: container_ip)
    monkeypatch.setattr(canary_module, "run_canary", fake_run_canary)
    settings = settings_for_wiring()

    text = await entry._canary_runner(settings)()

    assert text == canary_text()
    (config,) = configs
    defaults = PlatformSettings()
    assert config.container_ip == container_ip
    assert config.appmux_port == settings.databricks_app_port
    assert config.app_environment == Path(sys.prefix)
    # Design 6.4: self_check runs with the default rules and the default pin.
    assert config.global_rules == tuple(defaults.egress_global)
    assert config.pin_cpu is defaults.child_cpu_pin
    assert not config.work_dir.exists()  # run_canary makes it; the runner must not leave one


async def test_the_self_check_step_runs_the_state_and_does_not_raise_on_a_failed_result() -> None:
    canary = ScriptedCanary(failing_text())
    state = SelfCheckState(canary)
    step = entry._self_check_step(state)

    await step(ManagedStub())

    assert canary.calls == 1
    assert state.is_passing is False


async def test_steps_given_to_the_start_up_run_before_the_self_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def earlier_step(registry: Registry) -> None:
        order.append("earlier")

    async def runner() -> str:
        order.append("self_check")
        return canary_text()

    monkeypatch.setattr(entry, "STARTUP_STEPS", (earlier_step,))
    monkeypatch.setattr(entry, "_canary_runner", lambda settings: runner)
    gate = Gate()

    task = await start_with_wiring(monkeypatch, stub_with_admin(), gate)

    assert order == ["earlier", "self_check"]
    await entry.stop_background(task)


async def test_a_failing_earlier_step_blocks_the_gate_before_the_canary_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = ScriptedCanary()

    async def failing_step(registry: Registry) -> None:
        raise RuntimeError("an earlier step failed")

    monkeypatch.setattr(entry, "STARTUP_STEPS", (failing_step,))
    monkeypatch.setattr(entry, "_canary_runner", lambda settings: canary)
    gate = Gate()

    task = await start_with_wiring(monkeypatch, stub_with_admin(), gate)
    await entry.stop_background(task)

    assert gate.phase is Phase.BLOCKED
    assert canary.calls == 0
