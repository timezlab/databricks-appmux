"""The writes that register an app (T094; data model 5.1 and 6).

``register_app_changes`` is the one place that names the tables and columns of a registration: the
``apps`` row and the app's first ``app_config`` row. These tests pin what makes a registration safe
to store: both rows are written by a platform admin's own role and nothing else, the app starts
stopped with no release and with both delegations off, and every column the builder sets fits the
row models and no column the database owns (``version``, timestamps) is set.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import pytest
from pydantic import SecretStr

from appmux.registry.apps import register_app_changes
from appmux.registry.errors import RegistryRoleMissing
from appmux.registry.models import App, AppConfig
from appmux.registry.protocol import Change, resolve_identity
from appmux.registry.writers import Identity, WriteKind

ADMIN: Final = "dana@example.com"
NOW: Final = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


def registration() -> tuple[Change, Change]:
    return register_app_changes(
        slug="reports",
        display_name="Weekly reports",
        owner="grace@example.com",
        source_path="/Workspace/Shared/apps/reports",
        memory_budget_mb=256,
        registered_by=ADMIN,
    )


def test_a_registration_is_the_app_row_then_its_first_config_row() -> None:
    app_change, config_change = registration()

    assert app_change.kind is WriteKind.APP_INSERT
    assert config_change.kind is WriteKind.APP_CONFIG_INSERT


def test_the_app_row_carries_the_platform_fields_and_the_admin_as_its_author() -> None:
    app_change, _ = registration()

    assert dict(app_change.values) == {
        "slug": "reports",
        "display_name": "Weekly reports",
        "owner": "grace@example.com",
        "source_path": "/Workspace/Shared/apps/reports",
        "memory_budget_mb": 256,
        "obo_delegated": False,
        "shared_sp_delegated": False,
        "created_by": ADMIN,
        "updated_by": ADMIN,
    }


def test_the_config_row_is_stopped_with_no_target_release() -> None:
    _, config_change = registration()

    assert dict(config_change.values) == {
        "app_slug": "reports",
        "desired_state": "stopped",
        "updated_by": ADMIN,
    }


def test_the_columns_the_database_owns_are_left_to_it() -> None:
    app_change, config_change = registration()

    owned = {"version", "created_at", "updated_at", "target_release_no"}
    assert owned.isdisjoint(app_change.values)
    assert owned.isdisjoint(config_change.values)


def test_the_values_fit_the_row_models_once_the_database_fills_its_columns() -> None:
    app_change, config_change = registration()

    app = App.model_validate({**app_change.values, "created_at": NOW, "updated_at": NOW})
    config = AppConfig.model_validate({**config_change.values, "updated_at": NOW})

    assert (app.version, list(app.egress)) == (1, [])
    assert (config.version, config.target_release_no, config.desired_state) == (1, None, "stopped")


def test_both_rows_are_written_by_a_platform_admins_own_role_in_one_transaction() -> None:
    changes = registration()

    assert resolve_identity(changes, SecretStr("admin-token-value-for-tests")) is (
        Identity.PLATFORM_ADMIN
    )


def test_the_service_principal_cannot_register_an_app() -> None:
    changes = registration()

    with pytest.raises(RegistryRoleMissing):
        resolve_identity(changes, None)
