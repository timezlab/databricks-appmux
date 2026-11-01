"""The registry snapshot: what it holds, what it quarantines and how it behaves when Lakebase fails.

Design 4.3: the snapshot is re-read every few seconds and after each write; with Lakebase down the
last snapshot keeps serving and the error is reported; down at startup, the load raises so that the
caller can show the status page. A row that fails its model is left out, counted and reported by
table and key, and never fails the whole table (data model 4, the ``jsonb`` columns).
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from appmux.registry.errors import RegistryUnavailable
from appmux.registry.models import (
    App,
    AppConfig,
    AppGrant,
    AppResource,
    PermissionRequest,
    PlatformSetting,
    Release,
)
from appmux.registry.snapshot import (
    PRIMARY_KEYS,
    SNAPSHOT_TABLES,
    FrozenDict,
    FrozenList,
    QuarantinedRow,
    RegistrySnapshot,
    SnapshotLoader,
    deep_freeze,
)

pytestmark = pytest.mark.anyio

BASELINE_PATH = Path(__file__).resolve().parents[2] / "migrations" / "0001_baseline.sql"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
LEAKY_TEXT = "hunter2-this-must-never-be-logged"

Row = Mapping[str, object]


def app_row(slug: str = "sales") -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": "Sales",
        "owner": "owner@example.com",
        "source_path": f"/Workspace/Users/owner@example.com/{slug}",
        "memory_budget_mb": 256,
        "created_by": "admin@example.com",
        "created_at": NOW,
        "updated_by": "admin@example.com",
        "updated_at": NOW,
    }


def config_row(slug: str = "sales") -> dict[str, object]:
    return {"app_slug": slug, "updated_by": "owner@example.com", "updated_at": NOW}


def release_row(slug: str = "sales", release_no: int = 1) -> dict[str, object]:
    return {
        "app_slug": slug,
        "release_no": release_no,
        "snapshot_digest": "sha256:" + "a" * 64,
        "detected": {"runtime": "python", "command": ["python", "app.py"]},
        "status_at": NOW,
        "created_by": "owner@example.com",
        "created_at": NOW,
    }


def grant_row(slug: str = "sales", principal: str = "reader@example.com") -> dict[str, object]:
    return {
        "app_slug": slug,
        "principal_type": "user",
        "principal": principal,
        "permission": "CAN_USE",
        "granted_by": "owner@example.com",
        "granted_at": NOW,
    }


def resource_row(slug: str = "sales", key: str = "warehouse") -> dict[str, object]:
    return {
        "app_slug": slug,
        "key": key,
        "type": "sql_warehouse",
        "target": "a1b2c3d4e5f67890",
        "permission": "CAN_USE",
        "access_as": "obo",
        "check_result": "ok",
        "attached_by": "owner@example.com",
        "attached_at": NOW,
    }


def request_row(request_id: int = 1, slug: str = "sales") -> dict[str, object]:
    return {
        "request_id": request_id,
        "app_slug": slug,
        "kind": "delegation",
        "payload": {"obo": True},
        "reason": "needs the visitor's identity",
        "requested_by": "owner@example.com",
        "requested_at": NOW,
    }


def setting_row(key: str = "max_releases_per_app", value: object = 10) -> dict[str, object]:
    return {"key": key, "value": value, "updated_by": "admin@example.com", "updated_at": NOW}


def good_tables() -> dict[str, list[dict[str, object]]]:
    return {
        "platform_settings": [setting_row()],
        "apps": [app_row("sales"), app_row("docs")],
        "permission_requests": [request_row()],
        "app_config": [config_row("sales"), config_row("docs")],
        "app_grants": [grant_row()],
        "releases": [release_row(release_no=2), release_row(release_no=1)],
        "app_resources": [resource_row()],
    }


class FakeRowSource:
    """A RowSource over in-memory rows that can start failing, and counts its reads."""

    def __init__(self, tables: dict[str, list[dict[str, object]]] | None = None) -> None:
        self.tables = tables if tables is not None else good_tables()
        self.is_down = False
        self.failing_tables: set[str] = set()
        self.unexpected: Exception | None = None
        self.fetched: list[str] = []

    async def fetch_rows(self, table: str) -> Sequence[Row]:
        self.fetched.append(table)
        if self.unexpected is not None:
            raise self.unexpected
        if self.is_down or table in self.failing_tables:
            raise RegistryUnavailable
        return list(self.tables.get(table, []))


class Ticker:
    """An injected sleep: records the delay, and ends the poll loop after ``ticks`` sleeps."""

    def __init__(self, ticks: int) -> None:
        self.ticks = ticks
        self.delays: list[float] = []

    async def __call__(self, delay_s: float) -> None:
        self.delays.append(delay_s)
        if len(self.delays) > self.ticks:
            raise asyncio.CancelledError


def loader_for(source: FakeRowSource, sleep: Ticker | None = None) -> SnapshotLoader:
    return SnapshotLoader(
        source,
        poll_interval_s=5.0,
        sleep=sleep if sleep is not None else Ticker(0),
        clock=lambda: NOW,
    )


# --- loading -------------------------------------------------------------------------------------


async def test_loads_every_table_except_metrics_and_audit_into_typed_rows() -> None:
    source = FakeRowSource()
    loader = loader_for(source)

    await loader.refresh()
    snapshot = loader.snapshot

    assert sorted(source.fetched) == sorted(SNAPSHOT_TABLES)
    assert "metrics" not in SNAPSHOT_TABLES
    assert "audit_log" not in SNAPSHOT_TABLES
    assert {type(row) for row in snapshot.apps} == {App}
    assert [app.slug for app in snapshot.apps] == ["docs", "sales"]
    assert isinstance(snapshot.platform_settings[0], PlatformSetting)
    assert isinstance(snapshot.permission_requests[0], PermissionRequest)
    assert isinstance(snapshot.app_configs[0], AppConfig)
    assert isinstance(snapshot.app_grants[0], AppGrant)
    assert isinstance(snapshot.releases[0], Release)
    assert isinstance(snapshot.app_resources[0], AppResource)
    assert snapshot.quarantined == ()
    assert snapshot.loaded_at == NOW
    assert loader.last_error is None


async def test_lookups_by_slug_key_and_id() -> None:
    loader = loader_for(FakeRowSource())
    await loader.refresh()
    snapshot = loader.snapshot

    app = snapshot.app("sales")
    assert app is not None
    assert app.display_name == "Sales"
    assert snapshot.app("missing") is None
    config = snapshot.app_config("docs")
    assert config is not None
    assert config.app_slug == "docs"
    assert snapshot.app_config("missing") is None
    assert [grant.principal for grant in snapshot.grants_for("sales")] == ["reader@example.com"]
    assert snapshot.grants_for("docs") == ()
    assert [release.release_no for release in snapshot.releases_for("sales")] == [1, 2]
    release = snapshot.release("sales", 2)
    assert release is not None
    assert release.release_no == 2
    assert snapshot.release("sales", 9) is None
    assert [resource.key for resource in snapshot.resources_for("sales")] == ["warehouse"]
    resource = snapshot.resource("sales", "warehouse")
    assert resource is not None
    assert resource.target == "a1b2c3d4e5f67890"
    assert snapshot.resource("sales", "nope") is None
    request = snapshot.request(1)
    assert request is not None
    assert request.kind == "delegation"
    assert snapshot.request(99) is None
    setting = snapshot.setting("max_releases_per_app")
    assert setting is not None
    assert setting.value == 10
    assert snapshot.setting("nope") is None


async def test_snapshot_is_immutable() -> None:
    loader = loader_for(FakeRowSource())
    await loader.refresh()
    snapshot = loader.snapshot

    assert isinstance(snapshot.apps, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.apps = ()
    with pytest.raises(ValidationError):  # pydantic reports a write to a frozen model this way
        snapshot.apps[0].slug = "other"
    with pytest.raises(TypeError):
        snapshot.releases_for("sales")[0] = snapshot.releases[0]


EGRESS_RULE = {"action": "allow", "cidr": "10.0.0.0/8", "port": "443", "comment": "intranet"}


def tables_with_nested_values() -> dict[str, list[dict[str, object]]]:
    tables = good_tables()
    tables["apps"][0] = {**app_row("sales"), "egress": [EGRESS_RULE]}
    tables["releases"][1] = {
        **release_row(release_no=1),
        "config": {"env": [{"name": "LEVEL", "value": "debug"}], "wheel_sources": ["pypi"]},
        "tags": ["stable"],
    }
    tables["permission_requests"][0] = {
        **request_row(),
        "payload": {"obo": True, "x": [1, {"a": 2}]},
    }
    tables["platform_settings"][0] = setting_row("egress_rules", [EGRESS_RULE])
    return tables


async def test_a_list_or_dict_inside_a_row_cannot_be_changed_in_place() -> None:
    # The release's `detected.command` is the child's start command and `apps.egress` is the
    # sandbox's rule list: a reader that edits either in place would change what every other
    # reader of the snapshot sees (design 4.3, "nothing here can be changed").
    loader = loader_for(FakeRowSource(tables_with_nested_values()))
    await loader.refresh()
    snapshot = loader.snapshot
    release = snapshot.release("sales", 1)
    sales = snapshot.app("sales")
    assert release is not None
    assert sales is not None

    with pytest.raises(TypeError):
        release.detected.command.append("rm -rf /")
    with pytest.raises(TypeError):
        release.detected.command[0] = "evil"
    with pytest.raises(TypeError):
        sales.egress.clear()
    with pytest.raises(TypeError):
        sales.egress.append(sales.egress[0])
    with pytest.raises(TypeError):
        release.config.env.append(release.config.env[0])
    with pytest.raises(TypeError):
        release.tags.extend(["x"])
    with pytest.raises(TypeError):
        release.tags += ["x"]  # an in-place operator must not slip through either
    request = snapshot.request(1)
    assert request is not None
    extra = request.payload.model_extra
    assert extra is not None
    with pytest.raises(TypeError):
        extra["x"][1]["a"] = 3
    with pytest.raises(TypeError):
        extra["y"] = 1
    setting = snapshot.setting("egress_rules")
    assert setting is not None
    assert isinstance(setting.value, list)
    with pytest.raises(TypeError):
        setting.value.pop()

    again = (await _reloaded(loader)).release("sales", 1)
    assert again is not None
    assert again.detected.command == ["python", "app.py"]
    assert [rule.cidr for rule in sales.egress] == ["10.0.0.0/8"]


async def _reloaded(loader: SnapshotLoader) -> RegistrySnapshot:
    await loader.refresh()
    return loader.snapshot


async def test_a_frozen_row_still_reads_and_serialises_like_a_plain_one() -> None:
    loader = loader_for(FakeRowSource(tables_with_nested_values()))
    await loader.refresh()
    release = loader.snapshot.release("sales", 1)
    sales = loader.snapshot.app("sales")
    assert release is not None
    assert sales is not None

    assert release.detected.command == ["python", "app.py"]
    assert isinstance(release.detected.command, list)  # callers' isinstance checks keep working
    assert release.model_dump(mode="json")["detected"]["command"] == ["python", "app.py"]
    assert sales.model_dump(mode="json")["egress"] == [EGRESS_RULE]
    editable = copy.deepcopy(release.detected.command)
    editable.append("--flag")  # a copy is the way to edit
    assert editable == ["python", "app.py", "--flag"]
    assert copy.copy(release.detected.command) == ["python", "app.py"]
    assert release.model_copy(deep=True).detected.command == ["python", "app.py"]


async def test_a_copy_of_a_frozen_dict_is_a_plain_editable_dict() -> None:
    # The client adapts `Change.values` for psycopg with `copy.deepcopy`, and the nested dicts of a
    # snapshot row are FrozenDicts: without `FrozenDict.__reduce__` that copy raises TypeError.
    loader = loader_for(FakeRowSource(tables_with_nested_values()))
    await loader.refresh()
    request = loader.snapshot.request(1)
    assert request is not None
    extra = request.payload.model_extra
    assert isinstance(extra, FrozenDict)

    for copied in (copy.deepcopy(extra), copy.copy(extra)):
        assert type(copied) is dict
        assert copied == extra
        copied["added"] = 1  # a copy is the way to edit
    assert type(copy.deepcopy(extra)["x"]) is list  # the nesting is thawed too
    assert "added" not in extra

    editable = copy.deepcopy(request)
    assert editable.payload.model_extra is not None
    assert type(editable.payload.model_extra) is dict
    with pytest.raises(TypeError):
        extra["y"] = 1  # the snapshot's own dict stays closed


def test_deep_freeze_closes_every_mutator_of_a_list_and_a_dict() -> None:
    frozen_list = deep_freeze([1, [2], {"a": [3]}])
    frozen_dict = deep_freeze({"a": [1], "b": {"c": 2}})
    assert isinstance(frozen_list, FrozenList)
    assert isinstance(frozen_dict, FrozenDict)

    list_mutations = [
        lambda: frozen_list.append(1),
        lambda: frozen_list.extend([1]),
        lambda: frozen_list.insert(0, 1),
        lambda: frozen_list.remove(1),
        lambda: frozen_list.pop(),
        lambda: frozen_list.clear(),
        lambda: frozen_list.sort(key=str),
        lambda: frozen_list.reverse(),
        lambda: frozen_list.__setitem__(0, 9),
        lambda: frozen_list.__delitem__(0),
        lambda: frozen_list.__iadd__([1]),
        lambda: frozen_list.__imul__(2),
        lambda: frozen_list[1].append(1),
        lambda: frozen_list[2]["a"].append(1),
    ]
    dict_mutations = [
        lambda: frozen_dict.update(a=1),
        lambda: frozen_dict.setdefault("z", 1),
        lambda: frozen_dict.pop("a"),
        lambda: frozen_dict.popitem(),
        lambda: frozen_dict.clear(),
        lambda: frozen_dict.__setitem__("a", 1),
        lambda: frozen_dict.__delitem__("a"),
        lambda: frozen_dict.__ior__({"z": 1}),
        lambda: frozen_dict["a"].append(2),
        lambda: frozen_dict["b"].update(d=1),
    ]
    for mutate in [*list_mutations, *dict_mutations]:
        with pytest.raises(TypeError):
            mutate()
    assert frozen_list == [1, [2], {"a": [3]}]
    assert frozen_dict == {"a": [1], "b": {"c": 2}}
    assert [*frozen_list, 4][-1] == 4  # building a new list from it is fine


def test_deep_freeze_leaves_the_original_alone_and_freezes_tuples_and_sets_inside() -> None:
    original = {"a": [1, 2], "t": ([3],)}

    frozen = deep_freeze(original)

    original["a"].append(3)  # the caller's own list stays the caller's
    assert frozen["a"] == [1, 2]
    with pytest.raises(TypeError):
        frozen["t"][0].append(4)


def test_deep_freeze_freezes_a_set_and_a_model_by_copy() -> None:
    original = {"names": {"a", "b"}, "frozen_names": frozenset({"c"})}
    frozen = deep_freeze(original)
    assert frozen["names"] == frozenset({"a", "b"})
    assert type(frozen["names"]) is frozenset  # a set can no longer be added to
    assert type(frozen["frozen_names"]) is frozenset
    original["names"].add("z")
    assert frozen["names"] == frozenset({"a", "b"})  # the caller's set stays the caller's

    release = Release.model_validate(release_row())
    frozen_release = deep_freeze(release)
    assert frozen_release is not release  # models are copied, not edited
    assert type(release.tags) is list  # the original stays editable
    assert type(frozen_release.tags) is FrozenList


async def test_a_reload_replaces_the_snapshot_and_keeps_the_old_value_intact() -> None:
    source = FakeRowSource()
    loader = loader_for(source)
    await loader.refresh()
    before = loader.snapshot

    source.tables["apps"].append(app_row("new-app"))
    await loader.refresh()

    assert before.app("new-app") is None
    assert loader.snapshot.app("new-app") is not None


# --- quarantine ---------------------------------------------------------------------------------


def malformed_release() -> dict[str, object]:
    # `command` must be a list; a holder of the shared-SP token can write this (data model 4.6).
    row = release_row(release_no=3)
    row["detected"] = {"runtime": "python", "command": LEAKY_TEXT}
    return row


async def test_a_malformed_releases_detected_is_quarantined_and_the_rest_loads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tables = good_tables()
    tables["releases"].append(malformed_release())
    loader = loader_for(FakeRowSource(tables))

    with caplog.at_level(logging.DEBUG, logger="appmux"):
        await loader.refresh()
    snapshot = loader.snapshot

    assert [release.release_no for release in snapshot.releases_for("sales")] == [1, 2]
    assert snapshot.release("sales", 3) is None
    assert snapshot.quarantined == (QuarantinedRow(table="releases", key="sales/3"),)
    assert len(snapshot.apps) == 2  # the other tables are not touched
    assert loader.last_error is None  # a bad row is not a registry failure
    assert LEAKY_TEXT not in caplog.text
    assert LEAKY_TEXT not in repr(snapshot.quarantined)
    assert "releases" in caplog.text
    assert "sales/3" in caplog.text


async def test_quarantine_covers_a_row_with_a_column_that_does_not_exist() -> None:
    tables = good_tables()
    tables["app_grants"].append({**grant_row(principal="other@example.com"), "extra": LEAKY_TEXT})
    loader = loader_for(FakeRowSource(tables))

    await loader.refresh()

    assert [grant.principal for grant in loader.snapshot.grants_for("sales")] == [
        "reader@example.com"
    ]
    assert loader.snapshot.quarantined == (
        QuarantinedRow(table="app_grants", key="sales/user/other@example.com"),
    )


async def test_a_row_without_its_key_columns_is_still_quarantined_without_failing() -> None:
    tables = good_tables()
    tables["apps"].append({"display_name": LEAKY_TEXT})
    loader = loader_for(FakeRowSource(tables))

    await loader.refresh()

    assert len(loader.snapshot.apps) == 2
    assert loader.snapshot.quarantined == (QuarantinedRow(table="apps", key="?"),)


async def test_a_long_or_odd_key_is_shortened_and_made_printable() -> None:
    tables = good_tables()
    tables["apps"].append({**app_row("x"), "slug": "Bad\nslug" + "z" * 200})
    loader = loader_for(FakeRowSource(tables))

    await loader.refresh()

    (quarantined,) = loader.snapshot.quarantined
    assert quarantined.table == "apps"
    assert "\n" not in quarantined.key
    assert len(quarantined.key) <= 64


async def test_a_quarantined_row_is_logged_once_not_at_every_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tables = good_tables()
    tables["releases"].append(malformed_release())
    loader = loader_for(FakeRowSource(tables))

    with caplog.at_level(logging.WARNING, logger="appmux"):
        await loader.refresh()
        await loader.refresh()
        await loader.refresh()

    assert caplog.text.count("sales/3") == 1
    assert len(loader.snapshot.quarantined) == 1  # but every snapshot still reports it


async def test_a_fixed_row_leaves_quarantine_and_is_reported_again_if_it_breaks_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tables = good_tables()
    broken = malformed_release()
    tables["releases"].append(broken)
    loader = loader_for(FakeRowSource(tables))
    with caplog.at_level(logging.WARNING, logger="appmux"):
        await loader.refresh()
        tables["releases"][-1] = release_row(release_no=3)
        await loader.refresh()
        assert loader.snapshot.quarantined == ()
        assert loader.snapshot.release("sales", 3) is not None
        tables["releases"][-1] = broken
        await loader.refresh()

    assert caplog.text.count("sales/3") == 2


# --- failure -----------------------------------------------------------------------------------


async def test_the_first_load_failing_raises_registry_unavailable() -> None:
    source = FakeRowSource()
    source.is_down = True
    loader = loader_for(source)

    with pytest.raises(RegistryUnavailable):
        await loader.refresh()

    assert loader.last_error is not None
    with pytest.raises(RegistryUnavailable):
        _ = loader.snapshot  # fail closed: there is nothing to serve


async def test_a_source_that_fails_after_the_first_load_keeps_the_last_good_snapshot() -> None:
    source = FakeRowSource()
    loader = loader_for(source)
    await loader.refresh()
    good = loader.snapshot

    source.is_down = True
    source.tables["apps"].append(app_row("new-app"))
    await loader.refresh()  # does not raise: the caller serves from memory

    assert loader.snapshot is good
    assert loader.snapshot.app("new-app") is None
    assert loader.last_error == RegistryUnavailable.default_message


async def test_a_failure_halfway_through_publishes_no_partial_snapshot() -> None:
    source = FakeRowSource()
    loader = loader_for(source)
    await loader.refresh()
    good = loader.snapshot

    source.tables["apps"].append(app_row("new-app"))  # read before the failing table
    source.failing_tables = {"releases"}
    await loader.refresh()

    assert loader.snapshot is good
    assert loader.snapshot.app("new-app") is None
    assert loader.last_error is not None


async def test_recovery_clears_the_error_and_serves_the_new_data() -> None:
    source = FakeRowSource()
    loader = loader_for(source)
    await loader.refresh()
    source.is_down = True
    await loader.refresh()
    assert loader.last_error is not None

    source.is_down = False
    source.tables["apps"].append(app_row("new-app"))
    await loader.refresh()

    assert loader.last_error is None
    assert loader.snapshot.app("new-app") is not None


# --- polling -----------------------------------------------------------------------------------


async def test_poll_sleeps_the_interval_then_reloads_until_cancelled() -> None:
    source = FakeRowSource()
    ticker = Ticker(ticks=2)
    loader = loader_for(source, ticker)
    await loader.refresh()
    source.fetched.clear()

    with pytest.raises(asyncio.CancelledError):
        await loader.poll()

    assert ticker.delays == [5.0, 5.0, 5.0]
    assert len(source.fetched) == 2 * len(SNAPSHOT_TABLES)  # two reloads between three sleeps


async def test_poll_survives_a_down_registry_and_picks_up_on_recovery() -> None:
    source = FakeRowSource()
    errors_seen_at_each_sleep: list[str | None] = []

    async def scripted_sleep(delay_s: float) -> None:
        tick = len(errors_seen_at_each_sleep)
        errors_seen_at_each_sleep.append(loader.last_error)
        if tick == 0:
            source.is_down = True
        if tick == 2:
            source.is_down = False
            source.tables["apps"].append(app_row("back-again"))
        if tick == 3:
            raise asyncio.CancelledError

    loader = SnapshotLoader(source, poll_interval_s=5.0, sleep=scripted_sleep, clock=lambda: NOW)
    await loader.refresh()

    with pytest.raises(asyncio.CancelledError):
        await loader.poll()

    down = RegistryUnavailable.default_message
    assert errors_seen_at_each_sleep == [None, down, down, None]
    assert loader.snapshot.app("back-again") is not None


async def test_poll_survives_an_unexpected_error_and_keeps_its_text_out_of_last_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FakeRowSource()
    ticker = Ticker(ticks=2)
    loader = loader_for(source, ticker)
    await loader.refresh()
    source.unexpected = RuntimeError(LEAKY_TEXT)

    with caplog.at_level(logging.ERROR, logger="appmux"), pytest.raises(asyncio.CancelledError):
        await loader.poll()

    assert len(ticker.delays) == 3  # it kept polling
    assert loader.last_error is not None
    assert LEAKY_TEXT not in loader.last_error  # what a status page shows
    # The server-side log keeps the traceback for the operator; that is where the detail belongs.
    assert [record.levelno for record in caplog.records if record.exc_info] == [logging.ERROR] * 2
    assert loader.snapshot.app("sales") is not None


def test_primary_keys_cover_every_table_and_name_real_columns() -> None:
    from appmux.registry.models import ROW_MODELS

    assert set(PRIMARY_KEYS) == set(ROW_MODELS)
    for table, columns in PRIMARY_KEYS.items():
        assert set(columns) <= set(ROW_MODELS[table].model_fields)


def test_an_empty_snapshot_looks_up_nothing() -> None:
    snapshot = RegistrySnapshot(loaded_at=NOW)

    assert snapshot.apps == ()
    assert snapshot.app("sales") is None
    assert snapshot.releases_for("sales") == ()


# --- ordering ----------------------------------------------------------------------------------


async def test_grants_resources_and_requests_come_back_in_their_documented_order() -> None:
    tables = good_tables()
    tables["app_grants"] = [
        grant_row(principal="zoe@example.com"),
        {**grant_row(), "principal_type": "group", "principal": "analysts"},
        grant_row(principal="amy@example.com"),
        {**grant_row(), "principal_type": "everyone", "principal": "*"},
    ]
    tables["app_resources"] = [resource_row(key="zeta"), resource_row(key="alpha")]
    tables["permission_requests"] = [
        request_row(5),
        {**request_row(2), "kind": "settings"},
        {**request_row(9), "kind": "register"},
    ]
    loader = loader_for(FakeRowSource(tables))

    await loader.refresh()
    snapshot = loader.snapshot

    assert [(g.principal_type, g.principal) for g in snapshot.grants_for("sales")] == [
        ("everyone", "*"),
        ("group", "analysts"),
        ("user", "amy@example.com"),
        ("user", "zoe@example.com"),
    ]
    assert [r.key for r in snapshot.resources_for("sales")] == ["alpha", "zeta"]
    assert [r.request_id for r in snapshot.permission_requests] == [2, 5, 9]


# --- the keys the loader reports a row by ---------------------------------------------------------


def primary_keys_of_the_baseline_migration() -> dict[str, tuple[str, ...]]:
    sql = "\n".join(line.split("--")[0] for line in BASELINE_PATH.read_text().splitlines())
    keys: dict[str, tuple[str, ...]] = {}
    for body in re.split(r"CREATE TABLE ", sql)[1:]:
        table = body.split()[0]
        table_level = re.search(r"^\s*PRIMARY KEY \(([^)]*)\)", body, re.MULTILINE)
        column_level = re.search(r"^\s*(\w+)\s[^\n]*\bPRIMARY KEY\b(?! \()", body, re.MULTILINE)
        if table_level is not None:
            keys[table] = tuple(column.strip() for column in table_level.group(1).split(","))
        elif column_level is not None:
            keys[table] = (column_level.group(1),)
    return keys


def test_the_primary_keys_are_the_ones_the_baseline_migration_declares() -> None:
    declared = primary_keys_of_the_baseline_migration()

    assert set(declared) == set(PRIMARY_KEYS)
    assert declared == dict(PRIMARY_KEYS)


# --- reloads one at a time ---------------------------------------------------------------------


class CountingSource(FakeRowSource):
    """Records how many reads are in flight at once; each read yields to the event loop."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = 0
        self.most_in_flight = 0

    async def fetch_rows(self, table: str) -> Sequence[Row]:
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            return await super().fetch_rows(table)
        finally:
            self.in_flight -= 1


async def test_two_reloads_started_together_do_not_read_at_the_same_time() -> None:
    # A slow poll must not install its older read over the newer one a write just triggered.
    source = CountingSource()
    loader = loader_for(source)

    await asyncio.gather(loader.refresh(), loader.refresh(), loader.refresh())

    assert source.most_in_flight == 1
    assert len(source.fetched) == 3 * len(SNAPSHOT_TABLES)
    assert loader.snapshot.app("sales") is not None


# --- polling before the first load -------------------------------------------------------------


async def test_poll_keeps_trying_when_the_first_load_has_not_succeeded_yet() -> None:
    # The retry path after a startup failure: the status page shows last_error while this runs.
    source = FakeRowSource()
    source.is_down = True
    sleeps = 0

    async def scripted_sleep(delay_s: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            source.is_down = False  # Lakebase is back before the third reload
        if sleeps == 4:
            raise asyncio.CancelledError

    loader = SnapshotLoader(source, poll_interval_s=5.0, sleep=scripted_sleep, clock=lambda: NOW)
    with pytest.raises(RegistryUnavailable):
        await loader.refresh()  # the startup load fails and there is no snapshot yet

    with pytest.raises(asyncio.CancelledError):
        await loader.poll()

    assert sleeps == 4  # two failed reloads with nothing loaded did not end the loop
    assert loader.last_error is None
    assert loader.snapshot.app("sales") is not None
