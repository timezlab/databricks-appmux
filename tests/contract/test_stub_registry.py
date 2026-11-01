"""The in-memory registry that contract tests of later tasks run against.

The stub is a test double, not a database, so these tests pin the behaviour the routes rely on: the
write identity, the ``version`` check, the models and the keys that matter, one audit row per write
and nothing written when a write fails. The ``Change`` value and ``resolve_identity`` of
``appmux.registry.protocol`` are covered here too because the stub is their first user.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from stub_registry import StubRegistry

from appmux.registry.audit import AuditEntry
from appmux.registry.errors import (
    ChangedByOthers,
    InvalidRow,
    RegistryRoleMissing,
    RegistryUnavailable,
    RowAlreadyExists,
    RowMissing,
)
from appmux.registry.models import App, AppConfig, MetricRow, PermissionRequest, Release
from appmux.registry.protocol import Change, resolve_identity
from appmux.registry.writers import Identity, WriteIdentityError, WriteKind

pytestmark = pytest.mark.anyio

START = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
ADMIN_TOKEN = SecretStr("admin-token-value-for-tests")
ADMIN = "admin@example.com"
OWNER = "owner@example.com"
MINUTE = timedelta(minutes=1)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def new_registry(clock: Clock | None = None) -> StubRegistry:
    return StubRegistry(clock=clock if clock is not None else Clock())


def app_values(slug: str = "sales") -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": "Sales",
        "owner": OWNER,
        "source_path": f"/Workspace/Users/{OWNER}/{slug}",
        "memory_budget_mb": 256,
        "created_by": ADMIN,
        "updated_by": ADMIN,
    }


def audit(action: str = "app.register", slug: str | None = "sales") -> AuditEntry:
    return AuditEntry.model_validate({"actor": ADMIN, "action": action, "app_slug": slug})


async def register(registry: StubRegistry, slug: str = "sales") -> None:
    """Data model 5.1: the app, its first config row and the audit row, under the admin's role."""
    await registry.write(
        [
            Change(WriteKind.APP_INSERT, values=app_values(slug)),
            Change(WriteKind.APP_CONFIG_INSERT, values={"app_slug": slug, "updated_by": ADMIN}),
        ],
        [audit(slug=slug)],
        admin_token=ADMIN_TOKEN,
    )


def release_values(slug: str, release_no: int, status: str = "pending") -> dict[str, object]:
    return {
        "app_slug": slug,
        "release_no": release_no,
        "snapshot_digest": "sha256:" + "b" * 64,
        "detected": {},
        "status": status,
        "created_by": OWNER,
    }


def request_values(slug: str | None = "sales", kind: str = "delegation") -> dict[str, object]:
    values: dict[str, object] = {
        "app_slug": slug,
        "kind": kind,
        "payload": {"obo": True},
        "reason": "needs the visitor's identity",
        "requested_by": OWNER,
    }
    return values


# --- Change and resolve_identity ---------------------------------------------------------------


def test_a_change_to_a_table_outside_the_snapshot_is_refused() -> None:
    for kind in (WriteKind.METRIC_INSERT, WriteKind.METRIC_DELETE, WriteKind.AUDIT_INSERT):
        with pytest.raises(ValueError, match="not a change of a snapshot table"):
            Change(kind, values={"x": 1})


@pytest.mark.parametrize(
    ("kind", "key", "values", "expected_version", "message"),
    [
        (WriteKind.APP_INSERT, {"slug": "a"}, {"slug": "a"}, None, "its key belongs in values"),
        (WriteKind.APP_INSERT, {}, {}, None, "needs values"),
        (WriteKind.APP_UPDATE, {}, {"display_name": "A"}, 1, "key columns"),
        (WriteKind.APP_UPDATE, {"slug": "a"}, {}, 1, "columns to change"),
        (
            WriteKind.APP_UPDATE,
            {"slug": "a"},
            {"display_name": "A"},
            None,
            "needs expected_version",
        ),
        (WriteKind.APP_DELETE, {"slug": "a"}, {"display_name": "A"}, None, "takes no values"),
        (WriteKind.APP_CONFIG_UPDATE, {"app_slug": "a"}, {"version": 3}, 1, "sets 'version'"),
        (WriteKind.APP_GRANT_DELETE, {"app_slug": "a"}, {}, None, "key columns"),
        (
            WriteKind.RELEASE_UPDATE,
            {"app_slug": "a", "release_no": 1},
            {"label": "x"},
            1,
            "takes no expected_version",
        ),
    ],
)
def test_a_malformed_change_is_refused_when_built(
    kind: WriteKind,
    key: dict[str, object],
    values: dict[str, object],
    expected_version: int | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Change(kind, values=values, key=key, expected_version=expected_version)


def test_a_nested_list_or_dict_of_a_change_cannot_be_edited_after_it_is_built() -> None:
    rules: list[dict[str, str]] = [{"action": "deny"}]
    values: dict[str, object] = {"egress": rules, "nested": {"inner": [1]}}
    change = Change(WriteKind.APP_UPDATE, key={"slug": "a"}, values=values, expected_version=1)

    rules.append({"action": "allow-everything"})  # the caller's own list is not the change's
    stored: Any = change.values["egress"]
    nested: Any = change.values["nested"]
    assert stored == [{"action": "deny"}]
    with pytest.raises(TypeError):
        stored.append({"action": "allow-everything"})
    with pytest.raises(TypeError):
        stored[0]["action"] = "allow"
    with pytest.raises(TypeError):
        nested["inner"].append(2)


@pytest.mark.parametrize("kind", [WriteKind.REQUEST_ANSWER, WriteKind.REQUEST_WITHDRAW])
def test_a_request_update_may_state_the_status_it_expects(kind: WriteKind) -> None:
    change = Change(
        kind, key={"request_id": 1}, values={"status": "denied"}, expected_status="pending"
    )

    assert change.expected_status == "pending"


@pytest.mark.parametrize(
    ("kind", "key", "values", "expected_status", "message"),
    [
        (WriteKind.REQUEST_CREATE, {}, {"kind": "x"}, "pending", "takes no expected_status"),
        (WriteKind.APP_DELETE, {"slug": "a"}, {}, "pending", "takes no expected_status"),
        (
            WriteKind.RELEASE_UPDATE,
            {"app_slug": "a", "release_no": 1},
            {"label": "x"},
            "pending",
            "takes no expected_status",
        ),
        (
            WriteKind.REQUEST_ANSWER,
            {"request_id": 1},
            {"status": "denied"},
            "open",
            "not a request status",
        ),
    ],
)
def test_expected_status_is_for_request_updates_and_names_a_real_status(
    kind: WriteKind,
    key: dict[str, object],
    values: dict[str, object],
    expected_status: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Change(kind, values=values, key=key, expected_status=expected_status)


def test_a_change_cannot_be_edited_after_it_is_built() -> None:
    values: dict[str, object] = {"slug": "a"}
    change = Change(WriteKind.APP_INSERT, values=values)

    values["slug"] = "b"

    assert change.values["slug"] == "a"
    with pytest.raises(TypeError):
        change.values["slug"] = "c"  # a read-only mapping


def test_the_identity_of_a_transaction_follows_the_token_and_the_kinds() -> None:
    platform = Change(WriteKind.APP_INSERT, values=app_values())
    own = Change(
        WriteKind.APP_GRANT_INSERT,
        values={"app_slug": "sales", "principal_type": "everyone", "principal": "*"},
    )

    assert resolve_identity([platform], ADMIN_TOKEN) is Identity.PLATFORM_ADMIN
    assert resolve_identity([own], None) is Identity.APPMUX_SP
    with pytest.raises(RegistryRoleMissing):
        resolve_identity([platform], None)
    with pytest.raises(WriteIdentityError):
        resolve_identity([own], ADMIN_TOKEN)
    with pytest.raises(WriteIdentityError):
        resolve_identity([platform, own], ADMIN_TOKEN)
    with pytest.raises(RegistryRoleMissing):
        resolve_identity([own, platform], None)


def test_a_write_of_audit_rows_alone_runs_as_the_service_principal_unless_a_token_is_given() -> (
    None
):
    # View-source and view-as-user record an action and change no row (decision D1).
    assert resolve_identity([], None) is Identity.APPMUX_SP
    assert resolve_identity([], ADMIN_TOKEN) is Identity.PLATFORM_ADMIN


# --- write identity ----------------------------------------------------------------------------


async def test_a_platform_write_without_an_admin_token_says_the_role_is_missing() -> None:
    registry = new_registry()

    with pytest.raises(RegistryRoleMissing) as raised:
        await registry.write([Change(WriteKind.APP_INSERT, values=app_values())], [audit()])

    assert raised.value.message == (
        "your database role cannot write platform data; ask the Lakebase project owner"
    )
    assert raised.value.code == "registry_role_missing"
    assert registry.snapshot.apps == ()
    assert registry.audit_rows == ()


async def test_a_token_whose_role_is_not_a_platform_writer_is_refused() -> None:
    registry = StubRegistry(clock=Clock(), platform_writer_tokens={"someone-elses-token"})

    with pytest.raises(RegistryRoleMissing):
        await registry.write(
            [Change(WriteKind.APP_INSERT, values=app_values())], [audit()], admin_token=ADMIN_TOKEN
        )

    await registry.write(
        [Change(WriteKind.APP_INSERT, values=app_values())],
        [audit()],
        admin_token=SecretStr("someone-elses-token"),
    )
    assert registry.snapshot.app("sales") is not None


async def test_an_sp_only_write_given_an_admin_token_is_refused_and_writes_nothing() -> None:
    registry = new_registry()
    await register(registry)
    grant = Change(
        WriteKind.APP_GRANT_INSERT,
        values={
            "app_slug": "sales",
            "principal_type": "user",
            "principal": "reader@example.com",
            "permission": "CAN_USE",
            "granted_by": OWNER,
        },
    )

    with pytest.raises(WriteIdentityError):
        await registry.write([grant], [audit("grant.app")], admin_token=ADMIN_TOKEN)

    assert registry.snapshot.grants_for("sales") == ()
    await registry.write([grant], [audit("grant.app")])
    assert len(registry.snapshot.grants_for("sales")) == 1


async def test_an_error_never_carries_the_admin_token() -> None:
    registry = new_registry()

    with pytest.raises(WriteIdentityError) as raised:
        await registry.write(
            [Change(WriteKind.APP_GRANT_INSERT, values={"app_slug": "x"})],
            [],
            admin_token=ADMIN_TOKEN,
        )

    assert ADMIN_TOKEN.get_secret_value() not in str(raised.value)
    assert ADMIN_TOKEN.get_secret_value() not in repr(raised.value)


# --- registering, keys and models -------------------------------------------------------------


async def test_register_writes_the_app_its_config_and_the_audit_row_together() -> None:
    clock = Clock()
    registry = new_registry(clock)

    rows = await registry.write(
        [
            Change(WriteKind.APP_INSERT, values=app_values()),
            Change(WriteKind.APP_CONFIG_INSERT, values={"app_slug": "sales", "updated_by": ADMIN}),
        ],
        [audit()],
        admin_token=ADMIN_TOKEN,
    )

    app, config = rows
    assert isinstance(app, App)
    assert app.version == 1
    assert app.created_at == START
    assert isinstance(config, AppConfig)
    assert config.desired_state == "stopped"
    assert registry.snapshot.app("sales") == app
    assert registry.snapshot.app_config("sales") == config
    (row,) = registry.audit_rows
    assert (row.actor, row.action, row.app_slug, row.ts) == (ADMIN, "app.register", "sales", START)


async def test_a_duplicate_slug_is_refused_and_nothing_of_the_transaction_stays() -> None:
    registry = new_registry()
    await register(registry, "sales")

    with pytest.raises(RowAlreadyExists):
        await registry.write(
            [
                Change(WriteKind.APP_INSERT, values=app_values("docs")),
                Change(WriteKind.APP_INSERT, values=app_values("sales")),
            ],
            [audit(slug="docs")],
            admin_token=ADMIN_TOKEN,
        )

    assert [app.slug for app in registry.snapshot.apps] == ["sales"]
    assert len(registry.audit_rows) == 1  # only the first registration's


async def test_an_app_config_for_an_unknown_app_is_refused() -> None:
    registry = new_registry()

    with pytest.raises(RowMissing):
        await registry.write(
            [
                Change(
                    WriteKind.APP_CONFIG_INSERT, values={"app_slug": "ghost", "updated_by": ADMIN}
                )
            ],
            [audit(slug="ghost")],
            admin_token=ADMIN_TOKEN,
        )


async def test_children_of_an_unknown_app_are_refused() -> None:
    registry = new_registry()

    with pytest.raises(RowMissing):
        await registry.write(
            [Change(WriteKind.RELEASE_INSERT, values=release_values("ghost", 1))],
            [audit("release.create", "ghost")],
        )
    with pytest.raises(RowMissing):
        await registry.write(
            [Change(WriteKind.REQUEST_CREATE, values=request_values("ghost"))],
            [audit("request.create", "ghost")],
        )


async def test_a_register_request_names_no_app_yet() -> None:
    registry = new_registry()
    values = request_values(None, "register")
    values["payload"] = {"slug": "reports", "display_name": "Reports"}

    (row,) = await registry.write(
        [Change(WriteKind.REQUEST_CREATE, values=values)], [audit("request.create", None)]
    )

    assert isinstance(row, PermissionRequest)
    assert row.app_slug is None


async def test_values_that_break_the_model_are_refused_without_echoing_them() -> None:
    registry = new_registry()
    bad = app_values()
    bad["memory_budget_mb"] = 10

    with pytest.raises(InvalidRow) as raised:
        await registry.write(
            [Change(WriteKind.APP_INSERT, values=bad)], [audit()], admin_token=ADMIN_TOKEN
        )

    assert "10" not in str(raised.value)
    assert "memory_budget_mb" in str(raised.value)

    unknown_column = {**app_values(), "is_admin": "super-secret-value"}
    with pytest.raises(InvalidRow) as raised:
        await registry.write(
            [Change(WriteKind.APP_INSERT, values=unknown_column)],
            [audit()],
            admin_token=ADMIN_TOKEN,
        )
    assert "super-secret-value" not in str(raised.value)
    assert registry.snapshot.apps == ()


# --- version ----------------------------------------------------------------------------------


async def test_an_update_with_the_version_it_read_succeeds_and_bumps_it() -> None:
    clock = Clock()
    registry = new_registry(clock)
    await register(registry)
    clock.now = START + timedelta(minutes=5)

    (app,) = await registry.write(
        [
            Change(
                WriteKind.APP_UPDATE,
                key={"slug": "sales"},
                values={"display_name": "Sales EMEA", "updated_by": ADMIN},
                expected_version=1,
            )
        ],
        [audit("app.edit_platform_fields")],
        admin_token=ADMIN_TOKEN,
    )

    assert isinstance(app, App)
    assert (app.version, app.display_name, app.updated_at) == (2, "Sales EMEA", clock.now)
    assert app.created_at == START
    assert registry.snapshot.app("sales") == app


async def test_a_stale_app_version_raises_changed_by_others_and_writes_nothing() -> None:
    registry = new_registry()
    await register(registry)
    edit = Change(
        WriteKind.APP_UPDATE,
        key={"slug": "sales"},
        values={"display_name": "First", "updated_by": ADMIN},
        expected_version=1,
    )
    await registry.write([edit], [audit("app.edit_platform_fields")], admin_token=ADMIN_TOKEN)

    with pytest.raises(ChangedByOthers) as raised:
        await registry.write([edit], [audit("app.edit_platform_fields")], admin_token=ADMIN_TOKEN)

    assert raised.value.message == "changed by someone else, reload and retry"
    assert raised.value.code == "changed_by_others"
    app = registry.snapshot.app("sales")
    assert app is not None
    assert app.version == 2
    assert len(registry.audit_rows) == 2  # the registration and the first edit


async def test_app_config_updates_check_and_bump_their_own_version() -> None:
    registry = new_registry()
    await register(registry)

    def edit(expected_version: int, health_path: str) -> Change:
        return Change(
            WriteKind.APP_CONFIG_UPDATE,
            key={"app_slug": "sales"},
            values={"health_path": health_path, "updated_by": OWNER},
            expected_version=expected_version,
        )

    (config,) = await registry.write([edit(1, "/healthz")], [audit("config.edit")])
    assert isinstance(config, AppConfig)
    assert (config.version, config.health_path) == (2, "/healthz")
    with pytest.raises(ChangedByOthers):
        await registry.write([edit(1, "/other")], [audit("config.edit")])
    app = registry.snapshot.app("sales")
    assert app is not None
    assert app.version == 1  # the two versions are independent


async def test_updating_or_deleting_a_row_that_is_not_there_is_refused() -> None:
    registry = new_registry()

    with pytest.raises(RowMissing):
        await registry.write(
            [
                Change(
                    WriteKind.APP_UPDATE,
                    key={"slug": "ghost"},
                    values={"display_name": "x", "updated_by": ADMIN},
                    expected_version=1,
                )
            ],
            [audit("app.edit_platform_fields", "ghost")],
            admin_token=ADMIN_TOKEN,
        )
    with pytest.raises(RowMissing):
        await registry.write(
            [Change(WriteKind.APP_DELETE, key={"slug": "ghost"})],
            [audit("app.delete", "ghost")],
            admin_token=ADMIN_TOKEN,
        )


# --- rename, delete ---------------------------------------------------------------------------


async def test_a_rename_follows_into_the_child_tables_and_refuses_a_taken_slug() -> None:
    registry = new_registry()
    await register(registry, "sales")
    await register(registry, "docs")
    await registry.write(
        [Change(WriteKind.RELEASE_INSERT, values=release_values("sales", 1))],
        [audit("release.create")],
    )

    with pytest.raises(RowAlreadyExists):
        await registry.write(
            [
                Change(
                    WriteKind.APP_UPDATE,
                    key={"slug": "sales"},
                    values={"slug": "docs", "updated_by": ADMIN},
                    expected_version=1,
                )
            ],
            [audit("app.rename")],
            admin_token=ADMIN_TOKEN,
        )

    (app,) = await registry.write(
        [
            Change(
                WriteKind.APP_UPDATE,
                key={"slug": "sales"},
                values={"slug": "revenue", "updated_by": ADMIN},
                expected_version=1,
            )
        ],
        [audit("app.rename", "revenue")],
        admin_token=ADMIN_TOKEN,
    )

    assert isinstance(app, App)
    assert app.slug == "revenue"
    snapshot = registry.snapshot
    assert snapshot.app("sales") is None
    assert snapshot.app_config("revenue") is not None
    assert snapshot.app_config("sales") is None
    assert snapshot.release("revenue", 1) is not None


async def test_deleting_an_app_cascades_but_keeps_audit_and_metrics() -> None:
    registry = new_registry()
    await register(registry)
    await registry.write(
        [Change(WriteKind.RELEASE_INSERT, values=release_values("sales", 1))],
        [audit("release.create")],
    )
    await registry.append_metrics([metric("sales", START)])

    (deleted,) = await registry.write(
        [Change(WriteKind.APP_DELETE, key={"slug": "sales"})],
        [audit("app.delete")],
        admin_token=ADMIN_TOKEN,
    )

    assert deleted is None
    snapshot = registry.snapshot
    assert snapshot.apps == ()
    assert snapshot.app_configs == ()
    assert snapshot.releases == ()
    assert len(registry.audit_rows) == 3
    assert (
        len(await registry.read_metrics(app_slug="sales", start=START, end=START + MINUTE, limit=9))
        == 1
    )


# --- requests and releases ---------------------------------------------------------------------


async def test_a_request_gets_its_id_and_a_second_pending_one_of_the_kind_is_refused() -> None:
    registry = new_registry()
    await register(registry)

    (first,) = await registry.write(
        [Change(WriteKind.REQUEST_CREATE, values=request_values())], [audit("request.create")]
    )
    assert isinstance(first, PermissionRequest)
    assert (first.request_id, first.status) == (1, "pending")

    with pytest.raises(RowAlreadyExists):
        await registry.write(
            [Change(WriteKind.REQUEST_CREATE, values=request_values())], [audit("request.create")]
        )
    with pytest.raises(InvalidRow):
        await registry.write(
            [
                Change(
                    WriteKind.REQUEST_CREATE,
                    values={**request_values("sales", "settings"), "request_id": 7},
                )
            ],
            [audit("request.create")],
        )

    (second,) = await registry.write(
        [Change(WriteKind.REQUEST_CREATE, values=request_values("sales", "settings"))],
        [audit("request.create")],
    )
    assert isinstance(second, PermissionRequest)
    assert second.request_id == 2


async def test_an_admin_answers_a_request_and_a_withdrawn_one_frees_the_slot() -> None:
    registry = new_registry()
    await register(registry)
    await registry.write(
        [Change(WriteKind.REQUEST_CREATE, values=request_values())], [audit("request.create")]
    )

    (answered,) = await registry.write(
        [
            Change(
                WriteKind.REQUEST_ANSWER,
                key={"request_id": 1},
                values={"status": "denied", "closed_by": ADMIN, "closed_at": START},
            )
        ],
        [audit("request.deny")],
        admin_token=ADMIN_TOKEN,
    )

    assert isinstance(answered, PermissionRequest)
    assert answered.status == "denied"
    await registry.write(
        [Change(WriteKind.REQUEST_CREATE, values=request_values())], [audit("request.create")]
    )


async def test_one_active_release_per_app_so_a_swap_flips_the_old_one_first() -> None:
    registry = new_registry()
    await register(registry)
    await registry.write(
        [
            Change(WriteKind.RELEASE_INSERT, values=release_values("sales", 1, "active")),
            Change(WriteKind.RELEASE_INSERT, values=release_values("sales", 2, "healthy")),
        ],
        [audit("release.create")],
    )

    def status_change(release_no: int, status: str) -> Change:
        return Change(
            WriteKind.RELEASE_UPDATE,
            key={"app_slug": "sales", "release_no": release_no},
            values={"status": status},
        )

    with pytest.raises(RowAlreadyExists):
        await registry.write([status_change(2, "active"), status_change(1, "superseded")])

    clock_before = registry.snapshot.release("sales", 1)
    assert clock_before is not None
    assert clock_before.status == "active"
    await registry.write([status_change(1, "superseded"), status_change(2, "active")])
    statuses = {release.release_no: release.status for release in registry.snapshot.releases}
    assert statuses == {1: "superseded", 2: "active"}


async def test_a_release_status_update_stamps_status_at_and_needs_no_audit_row() -> None:
    clock = Clock()
    registry = new_registry(clock)
    await register(registry)
    await registry.write(
        [Change(WriteKind.RELEASE_INSERT, values=release_values("sales", 1))],
        [audit("release.create")],
    )
    clock.now = START + timedelta(minutes=2)

    (release,) = await registry.write(
        [
            Change(
                WriteKind.RELEASE_UPDATE,
                key={"app_slug": "sales", "release_no": 1},
                values={"status": "building"},
            )
        ]
    )

    assert isinstance(release, Release)
    assert (release.status, release.status_at) == ("building", clock.now)
    assert len(registry.audit_rows) == 2  # register and release.create


# --- audit ------------------------------------------------------------------------------------


async def test_every_write_records_its_audit_entries_and_read_audit_filters_them() -> None:
    clock = Clock()
    registry = new_registry(clock)
    await register(registry, "sales")
    clock.now = START + timedelta(hours=1)
    await register(registry, "docs")

    both = await registry.read_audit(
        app_slug=None, start=START, end=START + timedelta(hours=2), limit=10
    )
    only_docs = await registry.read_audit(
        app_slug="docs", start=START, end=START + timedelta(hours=2), limit=10
    )
    first_hour = await registry.read_audit(
        app_slug=None, start=START, end=START + timedelta(minutes=30), limit=10
    )
    limited = await registry.read_audit(
        app_slug=None, start=START, end=START + timedelta(hours=2), limit=1
    )

    assert [row.app_slug for row in both] == ["docs", "sales"]  # newest first
    assert [row.app_slug for row in only_docs] == ["docs"]
    assert [row.app_slug for row in first_hour] == ["sales"]
    assert [row.app_slug for row in limited] == ["docs"]
    assert [row.id for row in both] == [2, 1]


async def test_the_audit_detail_is_stored_as_the_entry_writes_it() -> None:
    registry = new_registry()
    entry = AuditEntry.model_validate(
        {
            "actor": "appmux",
            "action": "release.delete",
            "app_slug": "sales",
            "detail": {"release": 4},
            "automatic": True,
        }
    )

    await registry.write(
        [Change(WriteKind.PLATFORM_SETTING_INSERT, values=setting_values())],
        [entry],
        admin_token=ADMIN_TOKEN,
    )

    (row,) = registry.audit_rows
    assert row.detail == {"release": 4, "automatic": True}


def setting_values() -> dict[str, object]:
    return {"key": "max_releases_per_app", "value": 10, "updated_by": ADMIN}


# --- registry down ----------------------------------------------------------------------------


async def test_a_registry_that_is_down_refuses_writes_and_serves_its_last_snapshot() -> None:
    registry = new_registry()
    await register(registry)

    registry.go_down()

    assert registry.last_error is not None
    assert registry.snapshot.app("sales") is not None
    with pytest.raises(RegistryUnavailable):
        await registry.write(
            [Change(WriteKind.APP_INSERT, values=app_values("docs"))],
            [audit(slug="docs")],
            admin_token=ADMIN_TOKEN,
        )
    with pytest.raises(RegistryUnavailable):
        await registry.append_metrics([metric("sales", START)])
    with pytest.raises(RegistryUnavailable):
        await registry.read_audit(app_slug=None, start=START, end=START + MINUTE, limit=1)
    await registry.refresh()  # does not raise once a snapshot exists
    assert registry.last_error is not None

    registry.come_back()

    assert registry.last_error is None
    assert registry.snapshot.app("docs") is None
    await registry.write(
        [Change(WriteKind.APP_INSERT, values=app_values("docs"))],
        [audit(slug="docs")],
        admin_token=ADMIN_TOKEN,
    )
    assert registry.snapshot.app("docs") is not None


# --- metrics ----------------------------------------------------------------------------------


def metric(slug: str, minute: datetime, requests: int = 1) -> MetricRow:
    return MetricRow(app_slug=slug, minute=minute, requests=requests, lat_hist=[0] * 11)


async def test_metrics_append_ignores_a_repeated_minute_and_reads_by_window() -> None:
    registry = new_registry()
    await registry.append_metrics([metric("sales", START), metric("docs", START)])
    await registry.append_metrics(
        [metric("sales", START, requests=99), metric("sales", START + MINUTE)]
    )

    rows = await registry.read_metrics(app_slug=None, start=START, end=START + 2 * MINUTE, limit=10)
    sales = await registry.read_metrics(
        app_slug="sales", start=START, end=START + 2 * MINUTE, limit=10
    )
    narrow = await registry.read_metrics(app_slug=None, start=START, end=START + MINUTE, limit=10)

    assert [(row.minute, row.app_slug) for row in rows] == [
        (START, "docs"),
        (START, "sales"),
        (START + MINUTE, "sales"),
    ]
    assert [row.requests for row in sales] == [1, 1]  # the repeated minute kept the first
    assert len(narrow) == 2


async def test_metrics_limit_drops_the_oldest_minutes_and_prune_deletes_before_a_time() -> None:
    registry = new_registry()
    await registry.append_metrics([metric("sales", START + i * MINUTE) for i in range(5)])

    newest = await registry.read_metrics(
        app_slug="sales", start=START, end=START + 9 * MINUTE, limit=2
    )
    deleted = await registry.prune_metrics(older_than=START + 3 * MINUTE)
    left = await registry.read_metrics(
        app_slug="sales", start=START, end=START + 9 * MINUTE, limit=10
    )

    assert [row.minute for row in newest] == [START + 3 * MINUTE, START + 4 * MINUTE]
    assert deleted == 3
    assert [row.minute for row in left] == [START + 3 * MINUTE, START + 4 * MINUTE]


# --- every child table follows its app ----------------------------------------------------------


def grant_values(slug: str) -> dict[str, object]:
    return {
        "app_slug": slug,
        "principal_type": "user",
        "principal": "reader@example.com",
        "permission": "CAN_USE",
        "granted_by": OWNER,
    }


def resource_values(slug: str) -> dict[str, object]:
    return {
        "app_slug": slug,
        "key": "warehouse",
        "type": "sql_warehouse",
        "target": "a1b2c3d4e5f67890",
        "permission": "CAN_USE",
        "access_as": "obo",
        "check_result": "ok",
        "attached_by": OWNER,
    }


def child_inserts(slug: str) -> list[Change]:
    """One insert for each of the five child tables (the app_config row comes with register)."""
    return [
        Change(WriteKind.APP_GRANT_INSERT, values=grant_values(slug)),
        Change(WriteKind.APP_RESOURCE_INSERT, values=resource_values(slug)),
        Change(WriteKind.RELEASE_INSERT, values=release_values(slug, 1)),
        Change(WriteKind.REQUEST_CREATE, values=request_values(slug)),
    ]


def rows_of_each_child_table(registry: StubRegistry, slug: str) -> dict[str, int]:
    snapshot = registry.snapshot
    return {
        "app_config": int(snapshot.app_config(slug) is not None),
        "app_grants": len(snapshot.grants_for(slug)),
        "app_resources": len(snapshot.resources_for(slug)),
        "releases": len(snapshot.releases_for(slug)),
        "permission_requests": sum(1 for r in snapshot.permission_requests if r.app_slug == slug),
    }


async def test_a_rename_moves_the_rows_of_all_five_child_tables() -> None:
    registry = new_registry()
    await register(registry)
    await registry.write(child_inserts("sales"), [audit("release.create")])
    assert set(rows_of_each_child_table(registry, "sales").values()) == {1}

    await registry.write(
        [
            Change(
                WriteKind.APP_UPDATE,
                key={"slug": "sales"},
                values={"slug": "revenue", "updated_by": ADMIN},
                expected_version=1,
            )
        ],
        [audit("app.rename", "revenue")],
        admin_token=ADMIN_TOKEN,
    )

    assert rows_of_each_child_table(registry, "sales") == dict.fromkeys(
        ("app_config", "app_grants", "app_resources", "releases", "permission_requests"), 0
    )
    assert rows_of_each_child_table(registry, "revenue") == dict.fromkeys(
        ("app_config", "app_grants", "app_resources", "releases", "permission_requests"), 1
    )


async def test_deleting_an_app_removes_the_rows_of_all_five_child_tables() -> None:
    registry = new_registry()
    await register(registry, "sales")
    await register(registry, "docs")
    await registry.write(child_inserts("sales") + child_inserts("docs"), [audit("release.create")])

    await registry.write(
        [Change(WriteKind.APP_DELETE, key={"slug": "sales"})],
        [audit("app.delete")],
        admin_token=ADMIN_TOKEN,
    )

    assert set(rows_of_each_child_table(registry, "sales").values()) == {0}
    assert set(rows_of_each_child_table(registry, "docs").values()) == {1}  # only its own


@pytest.mark.parametrize(
    "child",
    [
        Change(WriteKind.APP_GRANT_INSERT, values=grant_values("ghost")),
        Change(WriteKind.APP_RESOURCE_INSERT, values=resource_values("ghost")),
        Change(WriteKind.RELEASE_INSERT, values=release_values("ghost", 1)),
        Change(WriteKind.REQUEST_CREATE, values=request_values("ghost")),
    ],
    ids=lambda change: change.kind.value,
)
async def test_a_child_row_of_an_unknown_app_is_refused(child: Change) -> None:
    registry = new_registry()

    with pytest.raises(RowMissing):
        await registry.write([child], [audit("release.create", "ghost")])

    assert registry.audit_rows == ()


# --- a request update that states the status it expects (decision D2) ---------------------------


def answer(status: str, expected_status: str | None = "pending") -> Change:
    return Change(
        WriteKind.REQUEST_ANSWER,
        key={"request_id": 1},
        values={"status": status, "closed_by": ADMIN, "closed_at": START},
        expected_status=expected_status,
    )


async def registry_with_a_pending_request() -> StubRegistry:
    registry = new_registry()
    await register(registry)
    await registry.write(
        [Change(WriteKind.REQUEST_CREATE, values=request_values())], [audit("request.create")]
    )
    return registry


async def test_answering_a_request_that_is_still_pending_succeeds() -> None:
    registry = await registry_with_a_pending_request()

    (row,) = await registry.write(
        [answer("approved")], [audit("request.approve")], admin_token=ADMIN_TOKEN
    )

    assert isinstance(row, PermissionRequest)
    assert row.status == "approved"


async def test_a_deny_that_races_an_approve_is_refused_and_the_first_answer_stands() -> None:
    registry = await registry_with_a_pending_request()
    await registry.write([answer("approved")], [audit("request.approve")], admin_token=ADMIN_TOKEN)

    with pytest.raises(ChangedByOthers):
        await registry.write([answer("denied")], [audit("request.deny")], admin_token=ADMIN_TOKEN)

    request = registry.snapshot.request(1)
    assert request is not None
    assert request.status == "approved"
    assert [row.action for row in registry.audit_rows].count("request.deny") == 0


async def test_a_withdraw_after_an_answer_is_refused() -> None:
    registry = await registry_with_a_pending_request()
    await registry.write([answer("denied")], [audit("request.deny")], admin_token=ADMIN_TOKEN)
    withdraw = Change(
        WriteKind.REQUEST_WITHDRAW,
        key={"request_id": 1},
        values={"status": "withdrawn", "closed_by": OWNER, "closed_at": START},
        expected_status="pending",
    )

    with pytest.raises(ChangedByOthers):
        await registry.write([withdraw], [audit("request.withdraw")])

    request = registry.snapshot.request(1)
    assert request is not None
    assert request.status == "denied"


async def test_a_request_update_for_a_request_that_is_not_there_says_missing_not_changed() -> None:
    registry = new_registry()
    await register(registry)

    with pytest.raises(RowMissing):
        await registry.write([answer("denied")], [audit("request.deny")], admin_token=ADMIN_TOKEN)


async def test_a_failed_status_check_leaves_the_other_changes_of_the_write_unwritten() -> None:
    registry = await registry_with_a_pending_request()
    await registry.write([answer("approved")], [audit("request.approve")], admin_token=ADMIN_TOKEN)
    rename = Change(
        WriteKind.APP_UPDATE,
        key={"slug": "sales"},
        values={"display_name": "Renamed", "updated_by": ADMIN},
        expected_version=1,
    )

    with pytest.raises(ChangedByOthers):
        await registry.write(
            [rename, answer("denied")], [audit("request.deny")], admin_token=ADMIN_TOKEN
        )

    app = registry.snapshot.app("sales")
    assert app is not None
    assert (app.display_name, app.version) == ("Sales", 1)


async def test_without_expected_status_the_update_is_not_checked() -> None:
    registry = await registry_with_a_pending_request()
    await registry.write([answer("approved")], [audit("request.approve")], admin_token=ADMIN_TOKEN)

    await registry.write(
        [answer("denied", expected_status=None)], [audit("request.deny")], admin_token=ADMIN_TOKEN
    )

    request = registry.snapshot.request(1)
    assert request is not None
    assert request.status == "denied"


# --- a write of audit rows alone (decision D1) --------------------------------------------------


async def test_audit_rows_alone_are_written_without_changing_any_row() -> None:
    registry = new_registry()
    await register(registry)
    before = registry.snapshot
    entry = audit("release.view_source", "sales")

    rows = await registry.write([], [entry])

    assert rows == ()
    assert [row.action for row in registry.audit_rows] == ["app.register", "release.view_source"]
    assert registry.snapshot.apps == before.apps
    assert registry.snapshot.app_configs == before.app_configs


async def test_an_audit_only_write_with_an_admin_token_needs_a_platform_writer() -> None:
    registry = StubRegistry(clock=Clock(), platform_writer_tokens={"someone-elses-token"})

    with pytest.raises(RegistryRoleMissing):
        await registry.write([], [audit("app.view_as_user")], admin_token=ADMIN_TOKEN)
    assert registry.audit_rows == ()
    await registry.write([], [audit("app.view_as_user")])  # as the service principal
    assert len(registry.audit_rows) == 1


async def test_a_write_with_neither_a_change_nor_an_audit_entry_is_refused() -> None:
    registry = new_registry()

    with pytest.raises(ValueError, match="nothing to write"):
        await registry.write([], [])


async def test_an_empty_write_is_a_caller_bug_even_while_the_registry_is_down() -> None:
    registry = new_registry()
    registry.go_down()

    with pytest.raises(ValueError, match="nothing to write"):
        await registry.write([], [])


async def test_audit_only_write_to_a_registry_that_is_down_is_refused() -> None:
    registry = new_registry()
    registry.go_down()

    with pytest.raises(RegistryUnavailable):
        await registry.write([], [audit("app.view_as_user")])


# --- reads with a limit -------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, -5])
async def test_a_limit_below_one_returns_nothing_instead_of_slicing_from_the_end(
    limit: int,
) -> None:
    registry = new_registry()
    await register(registry, "sales")
    await register(registry, "docs")
    await registry.append_metrics([metric("sales", START), metric("docs", START)])

    audit_rows = await registry.read_audit(
        app_slug=None, start=START, end=START + MINUTE, limit=limit
    )
    metric_rows = await registry.read_metrics(
        app_slug=None, start=START, end=START + MINUTE, limit=limit
    )

    assert audit_rows == ()
    assert metric_rows == ()


async def test_the_metrics_limit_counts_rows_so_the_caller_trims_to_whole_minutes() -> None:
    # Two apps per minute: a limit of 3 cuts the second-oldest minute in half. The registry
    # returns the newest `limit` rows; the admin route asks for one more than it will show and
    # drops the oldest minute it only half has, which also tells it the answer is truncated.
    registry = new_registry()
    await registry.append_metrics(
        [metric(slug, START + i * MINUTE) for i in range(3) for slug in ("docs", "sales")]
    )

    rows = await registry.read_metrics(app_slug=None, start=START, end=START + 9 * MINUTE, limit=3)

    assert [(row.minute, row.app_slug) for row in rows] == [
        (START + 1 * MINUTE, "sales"),
        (START + 2 * MINUTE, "docs"),
        (START + 2 * MINUTE, "sales"),
    ]
