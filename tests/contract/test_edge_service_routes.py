"""The edge mounts the routes of the parts the running service gives it (T107, T145, T169).

``create_edge_app`` mounts the routes that need no part of the service by themselves (the request
inbox, delegation, ``GET /metrics`` and ``GET /audit``), and the ones that do only when the start-up
passes the part: the reconciler's control (start, stop, restart, activate, config, the delete of an
app and the rename of a running app), the cascade of an approved rename request, and the memory
books (``GET /platform``). Each route has its own tests; these pin that the *assembled* edge
reaches them.

Every person is a placeholder (``edge_harness``).
"""

from __future__ import annotations

from typing import Final

import pytest
from edge_harness import (
    CAROL,
    DANA,
    MOUNT,
    NOW,
    FakeGroups,
    Reply,
    ScriptedCanary,
    call,
    seeded_registry,
)
from stub_registry import StubRegistry

from appmux.admin.requests import RenameCascadeFailed
from appmux.edge.app import create_edge_app
from appmux.edge.gate import ASGIApplication
from appmux.reconciler.admission import MemoryLedger
from appmux.reconciler.app_control import RunState
from appmux.sandbox.self_check import SelfCheckState

pytestmark = pytest.mark.anyio

LEDGER: Final = MemoryLedger(limit_mb=4096, reserve_mb=512, committed_mb=1024)
DIGEST: Final = "sha256:" + "ab" * 32


class FakeControl:
    """The reconciler as the routes see it, and the cascade of an approved rename."""

    def __init__(self) -> None:
        self.restarts: list[str] = []
        self.renames: list[tuple[str, str]] = []
        self.cascades: list[tuple[str, str]] = []
        self.woken: list[str] = []
        self.stopped: list[str] = []
        self.cascade_failure: RenameCascadeFailed | None = None

    def run_state(self, slug: str, desired_state: str) -> RunState:
        return "stopped" if desired_state == "stopped" else "running"

    def request_restart(self, slug: str) -> None:
        self.restarts.append(slug)

    def app_renamed(self, old_slug: str, new_slug: str) -> None:
        self.renames.append((old_slug, new_slug))

    async def stop_app(self, slug: str) -> None:
        self.stopped.append(slug)

    def wake(self, slug: str) -> None:
        self.woken.append(slug)

    async def renamed(self, old_slug: str, new_slug: str) -> None:
        if self.cascade_failure is not None:
            raise self.cascade_failure
        self.cascades.append((old_slug, new_slug))


async def passing_self_check() -> SelfCheckState:
    state = SelfCheckState(ScriptedCanary(), clock=lambda: NOW)
    await state.run()
    return state


def runnable_registry() -> StubRegistry:
    """``sales`` has an active release, so it can be restarted."""
    registry = seeded_registry()
    registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": 1,
            "snapshot_digest": DIGEST,
            "detected": {"runtime": "python", "command": ["python", "app.py"], "env": []},
            "config": {"env": []},
            "wheels": [],
            "status": "active",
            "status_at": NOW,
            "created_by": CAROL,
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
    return registry


def edge(
    registry: StubRegistry,
    *,
    control: FakeControl | None = None,
    with_memory: bool = True,
    with_cascade: bool = True,
    self_check: SelfCheckState | None = None,
) -> ASGIApplication:
    return create_edge_app(
        registry=registry,
        group_source=FakeGroups(),
        self_check=self_check,
        app_control=control,
        rename_cascade=control if with_cascade else None,
        memory=(lambda: LEDGER) if with_memory else None,
    )


async def admin_call(
    app: ASGIApplication, method: str, path: str, email: str | None, body: object = None
) -> Reply:
    return await call(app, method, f"{MOUNT}{path}", email=email, json_body=body)


# --- mounted without any part of the service -----------------------------------------------------


@pytest.mark.parametrize("path", ["/requests", "/audit", "/metrics"])
async def test_the_inbox_the_audit_and_the_metrics_are_always_mounted(path: str) -> None:
    app = edge(seeded_registry())

    reply = await admin_call(app, "GET", path, DANA)

    assert reply.status == 200, reply.body


# --- GET /platform needs the reconciler's books --------------------------------------------------


async def test_the_platform_page_reads_the_memory_books_it_was_given() -> None:
    app = edge(seeded_registry(), self_check=await passing_self_check())

    reply = await admin_call(app, "GET", "/platform", DANA)

    assert reply.status == 200, reply.body
    memory = reply.json()["memory"]
    assert (memory["limit_mb"], memory["reserve_mb"], memory["committed_mb"]) == (4096, 512, 1024)


async def test_the_platform_page_is_unavailable_without_books_not_a_made_up_figure() -> None:
    app = edge(seeded_registry(), with_memory=False, self_check=await passing_self_check())

    reply = await admin_call(app, "GET", "/platform", DANA)

    assert reply.status == 503


# --- the control of the reconciler ---------------------------------------------------------------


async def test_a_restart_reaches_the_reconciler() -> None:
    control = FakeControl()
    app = edge(runnable_registry(), control=control)

    reply = await admin_call(app, "POST", "/apps/sales/restart", CAROL)

    assert reply.status == 200, reply.body
    assert control.restarts == ["sales"]


async def test_without_a_reconciler_the_control_routes_are_not_mounted() -> None:
    app = edge(runnable_registry(), control=None)

    reply = await admin_call(app, "POST", "/apps/sales/restart", CAROL)

    assert reply.status == 404


async def test_a_rename_by_edit_moves_the_running_app() -> None:
    control = FakeControl()
    registry = runnable_registry()
    app = edge(registry, control=control)
    app_row = registry.snapshot.app("sales")
    assert app_row is not None

    reply = await admin_call(
        app, "PATCH", "/apps/sales", DANA, {"slug": "revenue", "version": app_row.version}
    )

    assert reply.status == 200, reply.body
    assert control.renames == [("sales", "revenue")]


async def test_a_delete_stops_the_app_through_the_reconciler_and_removes_its_rows() -> None:
    control = FakeControl()
    registry = runnable_registry()
    app = edge(registry, control=control)

    reply = await admin_call(app, "DELETE", "/apps/sales", DANA)

    assert reply.status == 204, reply.body
    assert control.stopped == ["sales"]
    assert registry.snapshot.app("sales") is None


async def test_without_a_reconciler_an_app_cannot_be_deleted_because_nothing_could_stop_it() -> (
    None
):
    registry = runnable_registry()
    app = edge(registry, control=None)

    reply = await admin_call(app, "DELETE", "/apps/sales", DANA)

    assert reply.status == 405
    assert registry.snapshot.app("sales") is not None
    assert registry.writes == ()


# --- the cascade of an approved rename request ---------------------------------------------------


def seed_rename_request(registry: StubRegistry) -> None:
    registry.seed(
        "permission_requests",
        {
            "request_id": 1,
            "app_slug": "sales",
            "kind": "settings",
            "payload": {"slug": "sales-v2"},
            "reason": "seeded",
            "requested_by": CAROL,
            "requested_at": NOW,
            "status": "pending",
        },
    )


async def test_an_approved_rename_request_runs_the_cascade() -> None:
    control = FakeControl()
    registry = runnable_registry()
    seed_rename_request(registry)
    app = edge(registry, control=control)

    reply = await admin_call(app, "POST", "/requests/1/approve", DANA, {})

    assert reply.status == 200, reply.body
    assert control.cascades == [("sales", "sales-v2")]


async def test_a_rename_request_is_refused_when_no_cascade_is_wired() -> None:
    registry = runnable_registry()
    seed_rename_request(registry)
    app = edge(registry, control=FakeControl(), with_cascade=False)

    reply = await admin_call(app, "POST", "/requests/1/approve", DANA, {})

    assert (reply.status, reply.json()["error"]["code"]) == (503, "rename_unavailable")
    assert registry.snapshot.app("sales") is not None
