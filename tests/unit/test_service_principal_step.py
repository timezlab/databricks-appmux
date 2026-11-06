"""The start-up step that stops every running app when appmux's service principal changed (T224).

ADR 0022, FR-055. The registry is the in-memory one, so a test sees the rows the step's single
``write`` produces and whether it wrote at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from reconciler_fakes import seed_app, stub_registry_class

from appmux.registry.audit import AuditEntry
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.service_principal import (
    SERVICE_PRINCIPAL_ACTION,
    STOP_REASON,
    stop_apps_when_the_service_principal_changed,
)

pytestmark = pytest.mark.anyio

OLD_ID = "5b7e2c10-91d4-4c3e-8a67-2f0e5d8a1c44"
NEW_ID = "0d4c1b84-3f5a-4a52-9a52-6a9c1f3e7b10"


def new_registry() -> Any:
    return stub_registry_class()()


async def remember(registry: Any, client_id: str) -> None:
    """The row an earlier start wrote: the platform record of the id appmux ran as."""
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


def desired_states(registry: Any) -> dict[str, str]:
    snapshot = registry.snapshot
    return {config.app_slug: config.desired_state for config in snapshot.app_configs}


async def test_a_registry_with_no_record_only_gets_the_row_and_nothing_is_stopped() -> None:
    registry = new_registry()
    seed_app(registry, "sales", desired_state="running")

    stopped = await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)

    assert stopped == ()
    assert desired_states(registry) == {"sales": "running"}
    rows = registry.audit_rows
    assert [(r.actor, r.action) for r in rows] == [("appmux", SERVICE_PRINCIPAL_ACTION)]
    assert rows[0].detail["client_id"] == NEW_ID
    assert "previous_client_id" not in rows[0].detail


async def test_the_same_id_changes_nothing_and_writes_nothing() -> None:
    registry = new_registry()
    seed_app(registry, "sales", desired_state="running")
    await remember(registry, NEW_ID)
    writes_before = len(registry.writes)

    stopped = await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)

    assert stopped == ()
    assert len(registry.writes) == writes_before
    assert desired_states(registry) == {"sales": "running"}


async def test_a_different_id_stops_every_running_app_and_leaves_the_stopped_ones() -> None:
    registry = new_registry()
    seed_app(registry, "sales", desired_state="running")
    seed_app(registry, "docs", desired_state="running")
    seed_app(registry, "wiki", desired_state="stopped")
    await remember(registry, OLD_ID)
    writes_before = len(registry.writes)

    stopped = await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)

    assert sorted(stopped) == ["docs", "sales"]
    assert desired_states(registry) == {"sales": "stopped", "docs": "stopped", "wiki": "stopped"}
    snapshot = registry.snapshot
    assert snapshot.app_config("sales").updated_by == "appmux"
    assert snapshot.app_config("docs").updated_by == "appmux"
    assert snapshot.app_config("wiki").updated_by != "appmux"  # it was not touched
    # One transaction: one write for the apps and both kinds of audit row.
    assert len(registry.writes) == writes_before + 1


async def test_each_stop_and_the_change_of_id_are_audited() -> None:
    registry = new_registry()
    seed_app(registry, "sales", desired_state="running")
    seed_app(registry, "wiki", desired_state="stopped")
    await remember(registry, OLD_ID)

    await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)

    rows = registry.audit_rows[1:]
    stops = [r for r in rows if r.action == "app.stop"]
    assert [(r.actor, r.app_slug, r.reason) for r in stops] == [("appmux", "sales", STOP_REASON)]
    assert stops[0].detail.get("automatic") is True
    changes = [r for r in rows if r.action == SERVICE_PRINCIPAL_ACTION]
    assert len(changes) == 1
    assert changes[0].detail["client_id"] == NEW_ID
    assert changes[0].detail["previous_client_id"] == OLD_ID
    assert changes[0].app_slug is None


async def test_the_next_start_with_the_same_id_stops_nothing_more() -> None:
    registry = new_registry()
    seed_app(registry, "sales", desired_state="running")
    await remember(registry, OLD_ID)
    await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)
    # The admin presses Start; appmux restarts under the same service principal.
    seed_app_running_again(registry)
    writes_before = len(registry.writes)

    stopped = await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)

    assert stopped == ()
    assert len(registry.writes) == writes_before
    assert desired_states(registry) == {"sales": "running"}


def seed_app_running_again(registry: Any) -> None:
    config = registry.snapshot.app_config("sales")
    registry.seed(
        "app_config",
        {**config.model_dump(), "desired_state": "running", "updated_by": "dana@example.com"},
    )


async def test_the_newest_row_that_names_no_usable_id_is_skipped() -> None:
    registry = new_registry()
    seed_app(registry, "sales", desired_state="running")
    await remember(registry, OLD_ID)
    # A holder of the shared-SP token can insert a row whose detail has no usable id.
    await registry.write(
        [],
        [
            AuditEntry.model_validate(
                {"actor": "appmux", "action": SERVICE_PRINCIPAL_ACTION, "automatic": True}
            )
        ],
    )

    stopped = await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)

    assert stopped == ("sales",)  # compared with OLD_ID, the newest row that has an id


async def test_rows_with_no_usable_id_at_all_count_as_no_record() -> None:
    registry = new_registry()
    seed_app(registry, "sales", desired_state="running")
    await registry.write(
        [],
        [
            AuditEntry.model_validate(
                {"actor": "appmux", "action": SERVICE_PRINCIPAL_ACTION, "automatic": True}
            )
        ],
    )

    stopped = await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)

    assert stopped == ()
    assert desired_states(registry) == {"sales": "running"}
    assert registry.audit_rows[-1].detail["client_id"] == NEW_ID


async def test_a_row_written_by_someone_else_is_not_a_record() -> None:
    registry = new_registry()
    seed_app(registry, "sales", desired_state="running")
    await registry.write(
        [],
        [
            AuditEntry.model_validate(
                {
                    "actor": "mallory@example.com",
                    "action": SERVICE_PRINCIPAL_ACTION,
                    "detail": {"client_id": OLD_ID},
                }
            )
        ],
    )

    stopped = await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)

    assert stopped == ()  # no record by appmux, so only the row is written


async def test_a_registry_that_cannot_be_read_fails_the_step() -> None:
    registry = new_registry()
    seed_app(registry, "sales", desired_state="running")
    registry.go_down()

    with pytest.raises(RegistryUnavailable):
        await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)

    registry.come_back()
    assert desired_states(registry) == {"sales": "running"}
    assert registry.audit_rows == ()


async def test_a_snapshot_that_still_shows_a_stopped_app_as_running_fails_the_step() -> None:
    # The write commits but the reload after it fails (RegistryClient.write logs and goes on), so
    # the reconciler would start the app this step has just stopped.
    class StaleAfterWrite(stub_registry_class()):
        async def write(self, *args: Any, **kwargs: Any) -> Any:
            before = self.snapshot
            rows = await super().write(*args, **kwargs)
            self._snapshot = before
            return rows

    registry = StaleAfterWrite()
    seed_app(registry, "sales", desired_state="running")
    await remember(registry, OLD_ID)

    with pytest.raises(RuntimeError, match="sales"):
        await stop_apps_when_the_service_principal_changed(registry, client_id=NEW_ID)
