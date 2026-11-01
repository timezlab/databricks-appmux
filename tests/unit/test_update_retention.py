"""The Update flow meets the release cap and the requirements check (T114, T128; design 5.2, 5.4).

Two things happen to an Update after its folder was found to differ from every live release:

* at the cap the oldest unprotected release is deleted before the new release is created, recorded
  as ``release.delete`` by ``appmux``; with every release protected the Update is refused and
  nothing is written;
* the requirements check runs before any build: a release whose ``valueFrom`` keys the app lacks is
  stored ``failed`` with every missing key in its reason, and so is never picked up for a build.

The workspace and the content store are fakes; the registry is the in-memory one. The ``write``
handed to the flow applies the changes like the admin route does, so a test reads the rows.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

import pytest
from pydantic import SecretStr
from reconciler_fakes import (
    NOW,
    FakeArchiveStore,
    archive_bytes,
    digest_of,
    seed_app,
    stub_registry_class,
)
from retention_world import full_app_statuses, seed_releases, seed_setting

from appmux.identity.actor import Actor
from appmux.reconciler.ports import UpdateRefused, UpdateResult
from appmux.reconciler.snapshot_source import Snapshot
from appmux.reconciler.update import UpdateService
from appmux.registry.errors import ChangedByOthers, RegistryUnavailable
from appmux.registry.models import RowModel
from appmux.registry.protocol import Change
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio

ACTOR = Actor(email="dana@example.com")
TOKEN = SecretStr("tok-test")
NEW_DIGEST = digest_of(900)
PLAIN_APP_YAML = b"command:\n  - python\n  - main.py\nenv:\n  - name: MODE\n    value: demo\n"
KEYED_APP_YAML = (
    b"command:\n  - python\n  - main.py\n"
    b"env:\n"
    b"  - name: OPENAI_API_KEY\n    valueFrom: openai-api-key\n"
    b"  - name: WAREHOUSE_ID\n    valueFrom: sql-warehouse\n"
    b"  - name: MODE\n    value: demo\n"
)
FULL_CAP_MESSAGE = (
    "app 'sales' has 10 releases and all are protected (active, target or locked); unlock one"
)


class FakeSnapshots:
    def __init__(self) -> None:
        self.digest = NEW_DIGEST
        self.calls = 0

    async def take(
        self, *, source_path: str, token: str, live_digests: Collection[str]
    ) -> Snapshot:
        self.calls += 1
        return Snapshot(
            digest=self.digest,
            file_count=2,
            size_bytes=100,
            already_up_to_date=self.digest in live_digests,
        )


class RecordingWriter:
    """What the admin route passes as ``write``: it applies the changes, like the real one."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self.calls: list[Sequence[Change]] = []

    async def __call__(
        self, changes: Sequence[Change], *, release_no: int
    ) -> tuple[RowModel | None, ...]:
        self.calls.append(changes)
        return await self.registry.write(changes)


def flaky_registry_class() -> Any:
    """The in-memory registry whose next ``write`` can be made to lose a race."""

    class FlakyRegistry(stub_registry_class()):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_write_with: Exception | None = None
            self.write_attempts = 0

        async def write(self, changes: Any, audit: Any = (), **kwargs: Any) -> Any:
            self.write_attempts += 1
            if self.fail_next_write_with is not None:
                failure, self.fail_next_write_with = self.fail_next_write_with, None
                raise failure
            return await super().write(changes, audit, **kwargs)

    return FlakyRegistry


class Flow:
    """An app `sales` with the given releases (number to status), and a service over fakes."""

    def __init__(
        self,
        statuses: dict[int, str] | None = None,
        *,
        target: int | None = None,
        locked: Sequence[int] = (),
        app_yaml: bytes = PLAIN_APP_YAML,
    ) -> None:
        self.registry = flaky_registry_class()()
        seed_app(self.registry, target_release_no=target, desired_state="running")
        seed_releases(self.registry, statuses or {}, locked=locked)
        self.snapshots = FakeSnapshots()
        self.store = FakeArchiveStore()
        self.store.archives[NEW_DIGEST] = archive_bytes(
            {"app.yaml": app_yaml, "main.py": b"print(1)\n"}
        )
        self.writer = RecordingWriter(self.registry)
        self.service = UpdateService(
            registry=self.registry, snapshots=self.snapshots, store=self.store
        )

    async def press(self) -> UpdateResult:
        return await self.service.update(
            actor=ACTOR, app_slug="sales", user_token=TOKEN, write=self.writer
        )

    def release(self, number: int) -> Any:
        return self.registry.snapshot.release("sales", number)

    def config(self) -> Any:
        return self.registry.snapshot.app_config("sales")

    def live_numbers(self) -> list[int]:
        return [
            release.release_no
            for release in self.registry.snapshot.releases_for("sales")
            if release.deleted_at is None
        ]

    def untouched(self) -> bool:
        return self.registry.writes == () and self.registry.audit_rows == ()


def attach(registry: Any, key: str) -> None:
    registry.seed(
        "app_resources",
        {
            "app_slug": "sales",
            "key": key,
            "type": "sql_warehouse",
            "target": "a1b2c3d4e5f67890",
            "permission": "CAN_USE",
            "access_as": "obo",
            "check_result": "ok",
            "attached_by": "alice@example.com",
            "attached_at": NOW,
        },
    )


# --- the cap ------------------------------------------------------------------------------------


async def test_an_update_at_the_cap_deletes_the_oldest_unprotected_release_first() -> None:
    flow = Flow(full_app_statuses(10), target=10)

    result = await flow.press()

    assert result == UpdateResult(release_no=11, is_up_to_date=False)
    gone = flow.release(1)
    assert (gone.deleted_by, gone.deleted_at is not None) == ("appmux", True)
    assert (gone.snapshot_digest, gone.wheels, gone.tags) == (None, [], [])
    assert flow.live_numbers() == [*range(2, 12)]
    assert flow.release(11).status == "pending"
    assert flow.config().target_release_no == 11


async def test_the_deletion_is_recorded_as_release_delete_by_appmux() -> None:
    flow = Flow(full_app_statuses(10), target=10)

    await flow.press()

    (row,) = flow.registry.audit_rows
    assert (row.actor, row.action, row.app_slug) == ("appmux", "release.delete", "sales")
    assert row.detail == {"release": 1, "by": "retention", "cap": 10}


async def test_the_old_release_goes_before_the_new_one_is_created() -> None:
    flow = Flow(full_app_statuses(10), target=10)

    await flow.press()

    kinds = [write.kinds for write in flow.registry.writes]
    assert kinds == [
        (WriteKind.APP_CONFIG_UPDATE, WriteKind.RELEASE_UPDATE),
        (WriteKind.RELEASE_INSERT, WriteKind.APP_CONFIG_UPDATE),
    ]
    assert len(flow.writer.calls) == 1  # the route's write carries only the Update itself


async def test_release_numbers_are_not_reused_after_the_clean_up() -> None:
    flow = Flow(full_app_statuses(10), target=10)
    await flow.press()
    flow.snapshots.digest = digest_of(901)
    flow.store.archives[digest_of(901)] = flow.store.archives[NEW_DIGEST]

    second = await flow.press()

    assert second.release_no == 12
    assert flow.release(2).deleted_at is not None  # the next oldest went for the twelfth


async def test_below_the_cap_nothing_is_deleted_and_nothing_extra_is_recorded() -> None:
    flow = Flow(full_app_statuses(9), target=9)

    result = await flow.press()

    assert result.release_no == 10
    assert flow.live_numbers() == [*range(1, 11)]
    assert flow.registry.audit_rows == ()
    assert len(flow.registry.writes) == 1


async def test_a_locked_old_release_and_the_target_are_passed_over() -> None:
    statuses = full_app_statuses(10)
    flow = Flow(statuses, target=2, locked=(1,))

    await flow.press()

    assert flow.release(1).deleted_at is None
    assert flow.release(2).deleted_at is None
    assert flow.release(3).deleted_at is not None


async def test_a_full_cap_with_every_release_protected_refuses_the_update_and_writes_nothing() -> (
    None
):
    statuses = {
        1: "superseded",
        2: "superseded",
        3: "superseded",
        4: "failed",
        5: "pending",
        6: "building",
        7: "starting",
        8: "healthy",
        9: "pending",
        10: "active",
    }
    flow = Flow(statuses, target=4, locked=(1, 2, 3))
    flow.snapshots.digest = NEW_DIGEST

    with pytest.raises(UpdateRefused) as refusal:
        await flow.press()

    assert (refusal.value.kind, refusal.value.message) == ("conflict", FULL_CAP_MESSAGE)
    assert flow.untouched()
    assert flow.writer.calls == []


async def test_a_folder_equal_to_a_live_release_is_not_refused_at_a_full_protected_cap() -> None:
    statuses = full_app_statuses(10)
    flow = Flow(statuses, target=10, locked=(1, 2, 3, 4, 5, 6, 7, 8, 9))
    flow.snapshots.digest = digest_of(10)

    result = await flow.press()

    assert result == UpdateResult(release_no=10, is_up_to_date=True)
    assert flow.untouched()


async def test_the_cap_is_read_from_the_platform_setting() -> None:
    flow = Flow(full_app_statuses(5), target=5)
    seed_setting(flow.registry, "max_releases_per_app", 5)

    await flow.press()

    assert flow.release(1).deleted_at is not None
    assert flow.live_numbers() == [2, 3, 4, 5, 6]
    (row,) = flow.registry.audit_rows
    assert row.detail == {"release": 1, "by": "retention", "cap": 5}


async def test_a_lowered_cap_deletes_enough_releases_and_records_each() -> None:
    flow = Flow(full_app_statuses(8), target=8)
    seed_setting(flow.registry, "max_releases_per_app", 5)

    await flow.press()

    assert flow.live_numbers() == [5, 6, 7, 8, 9]
    assert [row.detail["release"] for row in flow.registry.audit_rows] == [1, 2, 3, 4]
    assert {row.actor for row in flow.registry.audit_rows} == {"appmux"}
    assert len(flow.registry.writes) == 2  # one clean-up transaction, then the Update


async def test_a_clean_up_that_loses_a_race_reads_again_and_tries_again() -> None:
    flow = Flow(full_app_statuses(10), target=10)
    flow.registry.fail_next_write_with = ChangedByOthers()

    result = await flow.press()

    assert result.release_no == 11
    assert flow.registry.write_attempts == 3  # lost, clean-up, Update
    assert flow.release(1).deleted_at is not None


async def test_a_clean_up_that_keeps_losing_asks_the_person_to_press_again() -> None:
    flow = Flow(full_app_statuses(10), target=10)

    applies = flow.registry.write

    async def lose_every_clean_up(changes: Any, audit: Any = (), **kwargs: Any) -> Any:
        if any(change.kind is WriteKind.RELEASE_UPDATE for change in changes):
            raise ChangedByOthers
        return await applies(changes, audit, **kwargs)

    flow.registry.write = lose_every_clean_up

    with pytest.raises(UpdateRefused) as refusal:
        await flow.press()

    assert refusal.value.kind == "conflict"
    assert "press Update again" in refusal.value.message
    assert flow.release(1).deleted_at is None


async def test_a_registry_that_goes_down_during_the_clean_up_propagates_as_unavailable() -> None:
    flow = Flow(full_app_statuses(10), target=10)
    flow.registry.fail_next_write_with = RegistryUnavailable()

    with pytest.raises(RegistryUnavailable):
        await flow.press()

    assert flow.release(1).deleted_at is None
    assert flow.writer.calls == []


# --- the requirements check ---------------------------------------------------------------------


async def test_a_release_naming_resources_the_app_lacks_is_stored_failed_listing_every_key() -> (
    None
):
    flow = Flow(app_yaml=KEYED_APP_YAML)

    result = await flow.press()

    assert result == UpdateResult(release_no=1, is_up_to_date=False)
    release = flow.release(1)
    assert release.status == "failed"
    assert release.status_reason == "missing resources: openai-api-key, sql-warehouse"
    assert release.build_log_tail is None
    assert flow.config().target_release_no == 1


async def test_only_the_keys_still_missing_are_listed() -> None:
    flow = Flow(app_yaml=KEYED_APP_YAML)
    attach(flow.registry, "sql-warehouse")

    await flow.press()

    assert flow.release(1).status_reason == "missing resources: openai-api-key"


async def test_with_every_key_attached_the_release_is_pending_and_will_be_built() -> None:
    flow = Flow(app_yaml=KEYED_APP_YAML)
    attach(flow.registry, "sql-warehouse")
    attach(flow.registry, "openai-api-key")

    await flow.press()

    release = flow.release(1)
    assert (release.status, release.status_reason) == ("pending", None)


async def test_a_release_with_only_plain_values_needs_no_resource() -> None:
    flow = Flow()

    await flow.press()

    assert flow.release(1).status == "pending"


async def test_a_key_the_app_admin_added_to_the_config_counts_too() -> None:
    flow = Flow({1: "active"}, target=1)
    await flow.registry.write(
        [
            Change(
                WriteKind.RELEASE_UPDATE,
                key={"app_slug": "sales", "release_no": 1},
                values={
                    "config": {
                        "env": [{"name": "REGION_TABLE", "value_from": "region-table"}],
                        "wheel_sources": [],
                    }
                },
            )
        ]
    )

    await flow.press()

    assert flow.release(2).status == "failed"
    assert flow.release(2).status_reason == "missing resources: region-table"


async def test_a_failed_release_is_not_a_live_match_so_the_same_folder_gets_a_new_release() -> None:
    flow = Flow(app_yaml=KEYED_APP_YAML)
    await flow.press()
    attach(flow.registry, "sql-warehouse")
    attach(flow.registry, "openai-api-key")

    second = await flow.press()

    assert second == UpdateResult(release_no=2, is_up_to_date=False)
    assert flow.release(2).status == "pending"


async def test_a_failed_insert_keeps_the_cap_clean_up_in_front_of_it() -> None:
    flow = Flow(full_app_statuses(10), target=10, app_yaml=KEYED_APP_YAML)

    await flow.press()

    assert flow.release(1).deleted_at is not None
    assert flow.release(11).status == "failed"
