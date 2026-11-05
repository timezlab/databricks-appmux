"""Fail-closed on a real container: a weakened sandbox fails ``self_check``, nothing is served.

SC-002 and T070. Each of the three protections that ``SandboxSpec.omit_layers`` can leave out (the
``/proc`` overmount, the capability drop, the pump's filter) is left out in turn, and the canary of
``self_check`` runs in a real sandbox built without it. For each one the test asserts what the
design says must follow:

* the self-check fails, and the check that the missing layer guards is among the failed ones;
* the self-check gate (``SelfCheckGate``, the wrapper of the edge) opens no route: a request for an
  app is answered ``503`` and never reaches the application behind the gate;
* the status page of that ``503`` names the failed check;
* ``/_appmux/`` still passes the gate, which is how a platform admin sees the failure.

A run with nothing left out is the control: the same code passes all 13 checks and the gate lets a
request for an app through. Without it a failure could mean the harness is broken, not that the
sandbox was weakened.

What runs is the production wiring, not a copy of it: ``__main__._canary_runner`` builds the canary
from this container's facts, ``SelfCheckState`` keeps the result and ``SelfCheckGate`` reads it. The
one thing replaced is ``launcher.start_sandbox``, which the canary calls to start its sandbox: the
replacement adds ``omit_layers`` to the spec. ``omit_layers`` is a constructor argument that nothing
reads except the launcher (``tests/unit/test_sandbox_spec.py`` pins that), so this is the only way
to weaken a layer.

It needs what only a Databricks container gives (user namespaces, ``slirp4netns``, the container's
address) and the App's own environment, which ``scripts/run-container-tests.sh`` runs it in. It does
not run in the default gate: it is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``. It needs ``APPMUX_TEST_BASE_URL`` to read the port of the running
service, which the canary tests the sandbox against.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from ops_session import load_target

# The production canary runner is module-private: using it, not a copy, is what makes this run
# the wiring the service has.
from appmux.__main__ import _canary_runner
from appmux.edge.app import SelfCheckGate
from appmux.sandbox import launcher
from appmux.sandbox.launcher import Layer, SandboxSpec
from appmux.sandbox.self_check import CHECK_IDS, CHECKS, SelfCheckState
from appmux.settings import EnvironmentSettings

pytestmark = pytest.mark.integration

# Which check each missing layer must fail. A layer guards more than one thing, so the assertion is
# that this one is among the failed checks, never that it is the only one: the test prints the whole
# list for the record (spikes README, "plan checks").
GUARDED_BY: dict[Layer, str] = {
    Layer.PROC_OVERMOUNT: "platform_files",
    Layer.CAPABILITY_DROP: "capabilities",
    Layer.PUMP_FILTER: "loopback_and_container_ip",
}
APP_PATH = "/some-app/"
ADMIN_PATH = "/_appmux/api/v1/me"
SERVICE_UNAVAILABLE = 503
OK = 200


class _Behind:
    """The application behind the gate: records every path that got through, answers 200."""

    def __init__(self) -> None:
        self.reached: list[str] = []

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.reached.append(scope["path"])
        await send({"type": "http.response.start", "status": OK, "headers": []})
        await send({"type": "http.response.body", "body": b"behind the gate"})


async def _ask(gate: SelfCheckGate, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=gate)
    async with httpx.AsyncClient(transport=transport, base_url="http://appmux.test") as client:
        return await client.get(path)


async def _self_check_without(
    omitted: frozenset[Layer], monkeypatch: pytest.MonkeyPatch, port: int
) -> tuple[SelfCheckState, _Behind, httpx.Response, httpx.Response]:
    """Run the production canary without ``omitted``, then ask the gate for an app and the API."""
    real_start = launcher.start_sandbox

    async def start_without_layers(spec: SandboxSpec, **options: Any) -> Any:
        return await real_start(spec.model_copy(update={"omit_layers": omitted}), **options)

    # run_canary looks start_sandbox up in the launcher when it runs, so this reaches it.
    monkeypatch.setattr(launcher, "start_sandbox", start_without_layers)
    settings = EnvironmentSettings.from_environment()
    assert settings.databricks_app_port == port, (
        "the App's port and the port in APPMUX_TEST_BASE_URL differ; the canary would test the "
        "wrong one"
    )
    state = SelfCheckState(_canary_runner(settings))
    await state.run()
    behind = _Behind()
    gate = SelfCheckGate(behind, state)
    app_answer = await _ask(gate, APP_PATH)
    admin_answer = await _ask(gate, ADMIN_PATH)
    return state, behind, app_answer, admin_answer


@pytest.fixture(scope="module")
def service_port() -> int:
    return load_target().port


def test_with_every_layer_in_force_the_self_check_passes_and_the_gate_opens(
    monkeypatch: pytest.MonkeyPatch, service_port: int
) -> None:
    state, behind, app_answer, _ = asyncio.run(
        _self_check_without(frozenset(), monkeypatch, service_port)
    )

    assert state.latest is not None
    print(f"control: {[check.name for check in state.latest.checks if not check.passed]} failed")
    assert state.is_passing, f"the control run failed {state.failed_names}: the harness is broken"
    assert [check.name for check in state.latest.checks] == list(CHECK_IDS)
    assert app_answer.status_code == OK
    # The harness also asks the gate for the admin path; with the self-check passing, both get in.
    # (The first container run, 2026-11-05, expected only the app path and failed on this.)
    assert behind.reached == [APP_PATH, ADMIN_PATH]


@pytest.mark.parametrize("layer", list(GUARDED_BY), ids=lambda layer: layer.value)
def test_a_missing_layer_fails_the_self_check_and_opens_no_route(
    layer: Layer, monkeypatch: pytest.MonkeyPatch, service_port: int
) -> None:
    state, behind, app_answer, admin_answer = asyncio.run(
        _self_check_without(frozenset({layer}), monkeypatch, service_port)
    )

    failed = list(state.failed_names)
    print(f"without {layer.value}: failed checks {failed}")
    guarded = GUARDED_BY[layer]
    assert not state.is_passing, f"the self-check passed without the {layer.value} layer"
    assert guarded in failed, f"without {layer.value} the check {guarded} should fail; got {failed}"
    # No route: the request for an app never reached the application behind the gate.
    assert app_answer.status_code == SERVICE_UNAVAILABLE
    assert APP_PATH not in behind.reached, "a request for an app got through a failed self-check"
    # The status page names the check, with what a sandboxed app must not be able to do.
    page = html.unescape(app_answer.text)  # the page escapes the apostrophes of the descriptions
    assert guarded in page
    assert CHECKS[guarded] in page
    # The one way in stays open, so an admin can read the failure and run the check again.
    assert admin_answer.status_code == OK
    assert behind.reached == [ADMIN_PATH]
