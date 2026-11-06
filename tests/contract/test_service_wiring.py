"""What the real start-up of ``__main__`` does with the running service (T095, part of T093).

``test_service_assembly.py`` pins ``Service`` on its own. These tests run the real
``_start_with_real_wiring`` (the registry stands in; the workspace, the container and the reconciler
come from ``service_harness``) and pin what only the wiring decides:

* the order of the start-up steps: the check of the service principal (ADR 0022), the content
  store's folder, then the self-check, then the children, each only after the one before it
  succeeded (a store folder that cannot be created keeps the gate shut; a failed self-check opens
  the gate but starts nothing, FR-013);
* the edge and the reconciler share one route table and one set of open connections, and a release
  that is created wakes the reconciler;
* the edge is given the control of the reconciler (restart, rename, the state of an app), its
  memory books and the metrics the pipeline counts into, and those reach the reconciler and the
  registry through the real start-up; the daily clean-up of the content store is started;
* ``SIGTERM`` stops the children within the budget the platform's 15 seconds leave, *before* the
  registry closes, and closes the workspace client.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from edge_harness import (
    ALICE,
    DANA,
    MOUNT,
    NOW,
    app_row,
    call,
    canary_text,
    seed_standard_rows,
    settings_for_wiring,
)
from service_harness import (
    RECORDED_LEDGER,
    ManagedRegistry,
    ServiceDoubles,
    install_service_doubles,
    managed_registry,
    sandbox_with_rules,
)
from starlette.types import Message

import appmux.assembly as assembly
import appmux.edge.app as edge_app
import appmux.registry.client as registry_client
from appmux import __main__ as entry
from appmux.edge.gate import BlockReason, Gate, Phase
from appmux.metrics.aggregate import MetricsAggregator
from appmux.metrics.service import DEFAULT_STOP_BUDGET_S
from appmux.registry.audit import AuditEntry
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.service_principal import SERVICE_PRINCIPAL_ACTION
from appmux.sandbox.self_check import CHECK_IDS

pytestmark = pytest.mark.anyio

UNTIL_ATTEMPTS: Final = 4000
PLATFORM_SHUTDOWN_S: Final = 15


@pytest.fixture
def doubles(monkeypatch: pytest.MonkeyPatch) -> ServiceDoubles:
    return install_service_doubles(monkeypatch)


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(UNTIL_ATTEMPTS):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the condition did not become true")


async def lifespan(gate: Gate, events: Sequence[str]) -> list[str]:
    pending: list[Message] = [{"type": event} for event in events]
    sent: list[str] = []

    async def receive() -> Message:
        return pending.pop(0)

    async def send(message: Message) -> None:
        sent.append(message["type"])

    await gate({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
    return sent


def script_canary(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles, *, failing: str | None = None
) -> None:
    """The self-check's canary prints a pass (or fails ``failing``) and notes that it ran."""
    text = canary_text(failing={failing: "broken"} if failing else None)

    async def run_canary() -> str:
        doubles.log.append("self-check")
        return text

    monkeypatch.setattr(entry, "_canary_runner", lambda settings: run_canary)


async def start_like_serve(
    monkeypatch: pytest.MonkeyPatch, registry: ManagedRegistry, gate: Gate
) -> asyncio.Task[None]:
    """Run the real start-up the way ``serve`` does: the stop hook of the task goes in first."""
    # ``_load_wiring`` builds the registry client once and hands it to ``Service`` and to ``serve``;
    # the stand-in goes in at the same place, so both hold one registry as they do in production.
    monkeypatch.setattr(registry_client, "RegistryClient", lambda **kwargs: registry)
    task = asyncio.create_task(entry._start_with_real_wiring(gate, settings_for_wiring()))
    entry.stop_background_on_shutdown(gate, task)
    await until(lambda: gate.phase is not Phase.STARTING)
    return task


# --- the order of the start-up steps ------------------------------------------------------------


async def test_the_store_folder_is_made_first_then_the_self_check_runs_then_the_children_start(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    gate = Gate()

    task = await start_like_serve(monkeypatch, managed_registry(doubles.log), gate)

    assert gate.phase is Phase.OPEN
    assert doubles.log == [
        "workspace POST /api/2.0/workspace/mkdirs",
        "self-check",
        "reconciler start",
    ]
    await entry.stop_background(task)


WIRING_CLIENT_ID: Final = "client-id-placeholder"
EARLIER_CLIENT_ID: Final = "5b7e2c10-91d4-4c3e-8a67-2f0e5d8a1c44"


class WriteLoggingRegistry(ManagedRegistry):
    """A managed registry that notes each write in the log, to show where a step ran."""

    async def write(self, *args: Any, **kwargs: Any) -> Any:
        self.log.append("registry write")
        return await super().write(*args, **kwargs)


async def remember_service_principal(registry: ManagedRegistry, client_id: str) -> None:
    """The row an earlier start wrote, noted as the id appmux ran as."""
    await registry.write(
        [],
        [
            AuditEntry.model_validate(
                {
                    "actor": "appmux",
                    "action": SERVICE_PRINCIPAL_ACTION,
                    "detail": {"client_id": client_id},
                    "automatic": True,
                }
            )
        ],
    )
    registry.log.clear()


def desired_states(registry: ManagedRegistry) -> dict[str, str]:
    return {c.app_slug: c.desired_state for c in registry.snapshot.app_configs}


async def test_a_new_service_principal_stops_every_app_before_anything_else_starts(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = seed_standard_rows(WriteLoggingRegistry(log=doubles.log))
    await remember_service_principal(registry, EARLIER_CLIENT_ID)
    gate = Gate()

    task = await start_like_serve(monkeypatch, registry, gate)

    assert gate.phase is Phase.OPEN
    assert doubles.log == [
        "registry write",
        "workspace POST /api/2.0/workspace/mkdirs",
        "self-check",
        "reconciler start",
    ]
    assert desired_states(registry) == {"sales": "stopped", "docs": "stopped"}
    await entry.stop_background(task)


async def test_the_same_service_principal_leaves_the_apps_running(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = seed_standard_rows(WriteLoggingRegistry(log=doubles.log))
    await remember_service_principal(registry, WIRING_CLIENT_ID)
    gate = Gate()

    task = await start_like_serve(monkeypatch, registry, gate)

    assert gate.phase is Phase.OPEN
    assert doubles.log == [
        "workspace POST /api/2.0/workspace/mkdirs",
        "self-check",
        "reconciler start",
    ]
    assert desired_states(registry) == {"sales": "running", "docs": "running"}
    await entry.stop_background(task)


async def test_a_registry_without_a_record_only_notes_the_service_principal(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = seed_standard_rows(WriteLoggingRegistry(log=doubles.log))
    gate = Gate()

    task = await start_like_serve(monkeypatch, registry, gate)

    assert gate.phase is Phase.OPEN
    assert desired_states(registry) == {"sales": "running", "docs": "running"}
    assert [row.detail["client_id"] for row in registry.audit_rows] == [WIRING_CLIENT_ID]
    await entry.stop_background(task)


async def test_a_service_principal_check_that_cannot_be_read_keeps_the_gate_shut(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    class CannotReadAudit(ManagedRegistry):
        async def read_audit_of(self, **kwargs: Any) -> Any:
            raise RegistryUnavailable

    script_canary(monkeypatch, doubles)
    gate = Gate()

    task = await start_like_serve(
        monkeypatch, seed_standard_rows(CannotReadAudit(log=doubles.log)), gate
    )

    assert gate.phase is Phase.BLOCKED
    assert gate.reason is BlockReason.STARTUP_FAILED
    assert doubles.log == ["registry closed"]  # no folder, no self-check, no app
    await entry.stop_background(task)


async def test_a_store_folder_that_cannot_be_made_keeps_the_gate_shut_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    doubles.workspace.status = 500
    gate = Gate()

    task = await start_like_serve(monkeypatch, managed_registry(doubles.log), gate)

    assert gate.phase is Phase.BLOCKED
    assert gate.reason is BlockReason.STARTUP_FAILED
    assert doubles.log == ["workspace POST /api/2.0/workspace/mkdirs", "registry closed"]
    await entry.stop_background(task)


async def test_a_failed_self_check_opens_the_gate_but_starts_no_app(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles, failing=CHECK_IDS[0])
    gate = Gate()

    task = await start_like_serve(monkeypatch, managed_registry(doubles.log), gate)

    assert gate.phase is Phase.OPEN
    assert "self-check" in doubles.log
    assert doubles.reconcilers == []
    await entry.stop_background(task)


# --- what the edge and the reconciler share ----------------------------------------------------


async def test_the_edge_and_the_reconciler_share_the_routes_and_the_open_connections(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    given_to_edge: dict[str, Any] = {}
    real_create_edge_app = edge_app.create_edge_app

    def spy(**kwargs: Any) -> Any:
        given_to_edge.update(kwargs)
        return real_create_edge_app(**kwargs)

    monkeypatch.setattr(edge_app, "create_edge_app", spy)
    task = await start_like_serve(monkeypatch, managed_registry(doubles.log), Gate())

    (reconciler,) = doubles.reconcilers
    assert reconciler.kwargs["routes"] is given_to_edge["routes"]
    assert reconciler.kwargs["closer"] is given_to_edge["connections"]
    # A release that is created tells the reconciler which app to look at.
    given_to_edge["on_release_created"]("sales")
    assert ("wake", "sales") in reconciler.calls
    await entry.stop_background(task)


async def test_the_edge_is_given_the_parts_of_the_release_flow_the_resources_and_the_source_viewer(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    given_to_edge: dict[str, Any] = {}
    real_create_edge_app = edge_app.create_edge_app

    def spy(**kwargs: Any) -> Any:
        given_to_edge.update(kwargs)
        return real_create_edge_app(**kwargs)

    monkeypatch.setattr(edge_app, "create_edge_app", spy)
    task = await start_like_serve(monkeypatch, managed_registry(doubles.log), Gate())

    # `test_edge_wiring.py` pins that each of these mounts its routes; here: that they are passed.
    for part in (
        "release_updater",
        "attach_checker",
        "source_store",
        "open_mode_cookies",
        "app_output",
        "child_setup",
    ):
        assert given_to_edge[part] is not None, part
    await entry.stop_background(task)


async def test_the_resource_routes_and_open_mode_work_through_the_real_wiring(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = managed_registry(doubles.log)
    registry.seed("apps", {**app_row("sales", owner=ALICE), "obo_delegated": True})
    gate = Gate()
    task = await start_like_serve(monkeypatch, registry, gate)

    requirements = await call(gate, "GET", f"{MOUNT}/apps/sales/requirements", email=DANA)
    chosen = await call(
        gate, "POST", f"{MOUNT}/apps/sales/open-mode", email=DANA, json_body={"mode": "as_user"}
    )

    assert requirements.status == 200
    assert chosen.status == 204
    await entry.stop_background(task)


# --- the parts of the service the routes reach ---------------------------------------------------


def runnable_sales(registry: ManagedRegistry) -> None:
    """``sales`` has an active release, so Restart has something to restart."""
    registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": 1,
            "snapshot_digest": "sha256:" + "ab" * 32,
            "detected": {"runtime": "python", "command": ["python", "app.py"], "env": []},
            "config": {"env": []},
            "wheels": [],
            "status": "active",
            "status_at": NOW,
            "created_by": ALICE,
            "created_at": NOW,
        },
    )
    registry.seed(
        "app_config",
        {
            "app_slug": "sales",
            "desired_state": "running",
            "target_release_no": 1,
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )


async def test_a_restart_by_an_administrator_reaches_the_reconciler_through_the_real_wiring(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = managed_registry(doubles.log)
    runnable_sales(registry)
    gate = Gate()
    task = await start_like_serve(monkeypatch, registry, gate)
    (reconciler,) = doubles.reconcilers

    restarted = await call(gate, "POST", f"{MOUNT}/apps/sales/restart", email=DANA)

    assert restarted.status == 200, restarted.body
    assert ("request_restart", "sales") in reconciler.calls
    assert restarted.json()["state"] == "crashed"  # the state is the reconciler's own answer
    await entry.stop_background(task)


async def test_an_egress_save_by_an_administrator_reaches_the_running_sandbox_through_real_wiring(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles, tmp_path: Path
) -> None:
    script_canary(monkeypatch, doubles)
    registry = managed_registry(doubles.log)
    gate = Gate()
    task = await start_like_serve(monkeypatch, registry, gate)
    (reconciler,) = doubles.reconcilers
    sandbox, supervisor = sandbox_with_rules(tmp_path, "sales", "1-1")
    reconciler.children = [("sales", sandbox)]
    rules = [{"action": "allow", "cidr": "198.51.100.0/24", "port": "443", "comment": "an API"}]

    saved = await call(
        gate,
        "PUT",
        f"{MOUNT}/apps/sales/egress",
        email=DANA,
        json_body={"version": 1, "rules": rules},
    )

    assert saved.status == 200, saved.body
    (seen,) = supervisor.rules_seen
    assert [rule["cidr"] for rule in seen["app_rules"]] == ["198.51.100.0/24"]
    await entry.stop_background(task)


async def test_a_delete_by_an_administrator_stops_the_app_first_through_the_real_wiring(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = managed_registry(doubles.log)
    runnable_sales(registry)
    gate = Gate()
    task = await start_like_serve(monkeypatch, registry, gate)
    (reconciler,) = doubles.reconcilers

    deleted = await call(gate, "DELETE", f"{MOUNT}/apps/sales", email=DANA)

    assert deleted.status == 204, deleted.body
    assert registry.snapshot.app("sales") is None
    asked = [call_made for call_made in reconciler.calls if call_made[1] == "sales"]
    assert asked == [("stop_app", "sales"), ("wake", "sales")]
    await entry.stop_background(task)


async def test_a_rename_by_edit_moves_the_running_app_through_the_real_wiring(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = managed_registry(doubles.log)
    runnable_sales(registry)
    gate = Gate()
    task = await start_like_serve(monkeypatch, registry, gate)
    (reconciler,) = doubles.reconcilers
    sales = registry.snapshot.app("sales")
    assert sales is not None

    renamed = await call(
        gate,
        "PATCH",
        f"{MOUNT}/apps/sales",
        email=DANA,
        json_body={"slug": "revenue", "version": sales.version},
    )

    assert renamed.status == 200, renamed.body
    assert ("app_renamed", ("sales", "revenue")) in reconciler.calls
    await entry.stop_background(task)


async def test_the_detail_of_an_app_shows_the_state_the_reconciler_reports(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = managed_registry(doubles.log)
    runnable_sales(registry)
    gate = Gate()
    task = await start_like_serve(monkeypatch, registry, gate)
    (reconciler,) = doubles.reconcilers

    detail = await call(gate, "GET", f"{MOUNT}/apps/sales", email=DANA)

    assert detail.status == 200, detail.body
    assert ("run_state", ("sales", "running")) in reconciler.calls
    assert detail.json()["state"] == "crashed"  # not the "running" the registry alone would say
    await entry.stop_background(task)


async def test_the_detail_of_an_app_names_why_its_last_restart_did_not_come_up(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = managed_registry(doubles.log)
    runnable_sales(registry)
    gate = Gate()
    task = await start_like_serve(monkeypatch, registry, gate)
    (reconciler,) = doubles.reconcilers
    reason = "health check did not pass within 60 s (last error: connection refused)"

    before = await call(gate, "GET", f"{MOUNT}/apps/sales", email=DANA)
    reconciler.restart_failures["sales"] = reason
    after = await call(gate, "GET", f"{MOUNT}/apps/sales", email=DANA)

    assert before.status == after.status == 200, after.body
    assert before.json()["restart_failure"] is None
    assert after.json()["restart_failure"] == reason
    await entry.stop_background(task)


async def test_an_approved_rename_request_moves_the_running_app_through_the_real_wiring(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    registry = managed_registry(doubles.log)
    runnable_sales(registry)
    registry.seed(
        "permission_requests",
        {
            "request_id": 1,
            "app_slug": "sales",
            "kind": "settings",
            "payload": {"slug": "sales-v2"},
            "reason": "seeded",
            "requested_by": ALICE,
            "requested_at": NOW,
            "status": "pending",
        },
    )
    gate = Gate()
    task = await start_like_serve(monkeypatch, registry, gate)
    (reconciler,) = doubles.reconcilers

    approved = await call(gate, "POST", f"{MOUNT}/requests/1/approve", email=DANA, json_body={})

    assert approved.status == 200, approved.body
    assert ("app_renamed", ("sales", "sales-v2")) in reconciler.calls
    await entry.stop_background(task)


async def test_the_platform_page_shows_the_memory_books_of_the_reconciler(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    gate = Gate()
    task = await start_like_serve(monkeypatch, managed_registry(doubles.log), gate)

    page = await call(gate, "GET", f"{MOUNT}/platform", email=DANA)

    assert page.status == 200, page.body
    memory = page.json()["memory"]
    assert (memory["limit_mb"], memory["reserve_mb"], memory["committed_mb"]) == (
        RECORDED_LEDGER.limit_mb,
        RECORDED_LEDGER.reserve_mb,
        RECORDED_LEDGER.committed_mb,
    )
    await entry.stop_background(task)


async def test_the_pipeline_counts_into_the_metrics_that_are_stored_at_sigterm(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    given_to_edge: dict[str, Any] = {}
    real_create_edge_app = edge_app.create_edge_app

    def spy(**kwargs: Any) -> Any:
        given_to_edge.update(kwargs)
        return real_create_edge_app(**kwargs)

    monkeypatch.setattr(edge_app, "create_edge_app", spy)
    registry = managed_registry(doubles.log)
    gate = Gate()
    task = await start_like_serve(monkeypatch, registry, gate)
    assert isinstance(given_to_edge["metrics"], MetricsAggregator)

    given_to_edge["metrics"].record_request("sales", status=200, latency_ms=7.0)
    await lifespan(gate, ["lifespan.startup", "lifespan.shutdown"])

    now = datetime.now(UTC)
    rows = await registry.read_metrics(
        app_slug="sales", start=now - timedelta(days=1), end=now + timedelta(days=1), limit=10
    )
    assert [row.requests for row in rows] == [1]
    assert task.done()


async def test_the_daily_cleanup_of_the_content_store_is_started_with_the_children(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    cleaned: list[object] = []

    async def clean(files: object, registry: object) -> None:
        cleaned.append(files)

    monkeypatch.setattr(assembly, "CLEANUP_FIRST_DELAY_S", 0.0)
    monkeypatch.setattr(assembly, "run_cleanup_once", clean)
    task = await start_like_serve(monkeypatch, managed_registry(doubles.log), Gate())

    await until(lambda: len(cleaned) == 1)

    await entry.stop_background(task)


# --- SIGTERM ------------------------------------------------------------------------------------


async def test_sigterm_stops_the_children_within_the_budget_before_the_registry_closes(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    script_canary(monkeypatch, doubles)
    gate = Gate()
    task = await start_like_serve(monkeypatch, managed_registry(doubles.log), gate)
    (reconciler,) = doubles.reconcilers

    sent = await lifespan(gate, ["lifespan.startup", "lifespan.shutdown"])

    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert reconciler.calls[-1] == ("stop_all", entry.CHILD_STOP_BUDGET_S)
    assert doubles.log[-2:] == ["reconciler stop_all", "registry closed"]
    assert doubles.workspace.client.is_closed
    assert task.done()


async def test_uvicorns_wait_the_children_and_the_last_metrics_flush_fit_in_the_platforms_15_s(
    monkeypatch: pytest.MonkeyPatch, doubles: ServiceDoubles
) -> None:
    # The platform ends the process 15 s after SIGTERM. uvicorn first waits up to
    # GRACEFUL_SHUTDOWN_S for open connections; then the hooks run one after the other: the
    # children (CHILD_STOP_BUDGET_S), then the last metrics flush (the budget the wiring gives
    # ``MetricsService``). The worst case of all three must leave the margin unused.
    script_canary(monkeypatch, doubles)
    given_to_metrics: dict[str, Any] = {}
    real_metrics_service = assembly.MetricsService

    def spy(*args: Any, **kwargs: Any) -> Any:
        given_to_metrics.update(kwargs)
        return real_metrics_service(*args, **kwargs)

    monkeypatch.setattr(assembly, "MetricsService", spy)
    task = await start_like_serve(monkeypatch, managed_registry(doubles.log), Gate())
    flush_budget_s = given_to_metrics.get("stop_budget_s", DEFAULT_STOP_BUDGET_S)

    assert entry.CHILD_STOP_BUDGET_S > 0
    assert flush_budget_s > 0
    worst_case_s = entry.GRACEFUL_SHUTDOWN_S + entry.CHILD_STOP_BUDGET_S + flush_budget_s
    assert worst_case_s + entry.SHUTDOWN_MARGIN_S <= PLATFORM_SHUTDOWN_S

    await entry.stop_background(task)
