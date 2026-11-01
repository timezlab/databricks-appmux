"""Which releases a cap may delete, how many hand locks an app may hold (T111; rules 8, 9, 10, 13).

The rules, over the in-memory registry so that a test sees the rows a change really produces:

* the active, the target, an in-progress and a hand-locked release are protected; a tag or a name
  protects nothing, and a deleted release is neither protected nor counted;
* an Update at the cap deletes the oldest unprotected release, and a full cap with every release
  protected refuses the Update instead;
* the hand locks of an app are capped, and two lockers racing for the last place admit one;
* a release is deletable the moment it stops being active; a deleted release keeps its number and
  cannot be activated.

Every person here is a placeholder.
"""

from __future__ import annotations

from typing import Any

import pytest
from reconciler_fakes import NOW, seed_app, stub_registry_class
from retention_world import (
    LOCKER,
    full_app_statuses,
    seed_numbered_release,
    seed_releases,
    seed_setting,
)

from appmux.reconciler.retention import (
    IN_PROGRESS_STATUSES,
    RetentionRefused,
    delete_changes,
    lock_changes,
    protections,
    releases_to_delete,
    retention_limits,
    unlock_changes,
)
from appmux.reconciler.states import IllegalTransition
from appmux.registry.errors import ChangedByOthers
from appmux.registry.models import Release
from appmux.registry.releases import (
    activation_changes,
    next_release_no,
    release_status_change,
)
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio

FULL_CAP_MESSAGE = (
    "app 'sales' has 10 releases and all are protected (active, target or locked); unlock one"
)
FOURTH_LOCK_MESSAGE = "app 'sales' already has 3 locked releases; unlock one first"


def app_with(
    statuses: dict[int, str], *, target: int | None = None, locked: tuple[int, ...] = ()
) -> Any:
    registry = stub_registry_class()()
    seed_app(registry, desired_state="running", target_release_no=target)
    seed_releases(registry, statuses, locked=locked)
    return registry


def release_of(registry: Any, release_no: int) -> Release:
    release = registry.snapshot.release("sales", release_no)
    assert release is not None
    return release


# --- what is protected --------------------------------------------------------------------------


async def test_the_active_release_is_protected() -> None:
    registry = app_with({1: "active"})

    assert protections(release_of(registry, 1), target_release_no=None) == ("active",)


async def test_the_target_release_is_protected_whatever_its_status() -> None:
    registry = app_with({1: "active", 2: "failed"})

    assert protections(release_of(registry, 2), target_release_no=2) == ("target",)


@pytest.mark.parametrize("status", sorted(IN_PROGRESS_STATUSES))
async def test_an_in_progress_release_is_protected(status: str) -> None:
    registry = app_with({1: status})

    assert protections(release_of(registry, 1), target_release_no=None) == ("in progress",)


async def test_a_hand_locked_release_is_protected() -> None:
    registry = app_with({1: "superseded"}, locked=(1,))

    assert protections(release_of(registry, 1), target_release_no=None) == ("locked",)


@pytest.mark.parametrize("status", ["failed", "superseded"])
async def test_a_failed_or_superseded_release_with_no_lock_is_not_protected(status: str) -> None:
    registry = app_with({1: status})

    assert protections(release_of(registry, 1), target_release_no=None) == ()


async def test_tags_and_a_name_protect_nothing() -> None:
    registry = stub_registry_class()()
    seed_app(registry)
    seed_numbered_release(registry, 1, "superseded", label="before pagination", tags=["stable"])

    assert protections(release_of(registry, 1), target_release_no=None) == ()


async def test_a_release_with_several_reasons_lists_them_all() -> None:
    registry = app_with({1: "active"}, locked=(1,))

    assert protections(release_of(registry, 1), target_release_no=1) == (
        "locked",
        "active",
        "target",
    )


# --- the cap ------------------------------------------------------------------------------------


async def test_below_the_cap_no_release_is_deleted() -> None:
    registry = app_with(full_app_statuses(9), target=9)

    assert releases_to_delete(registry.snapshot, "sales") == ()


async def test_at_the_cap_the_oldest_unprotected_release_is_the_one_to_delete() -> None:
    registry = app_with(full_app_statuses(10), target=10)

    doomed = releases_to_delete(registry.snapshot, "sales")

    assert [release.release_no for release in doomed] == [1]


async def test_protected_old_releases_are_passed_over() -> None:
    # 1 is locked, 2 is the target, 3 is still building; 4 is the oldest that nothing protects.
    statuses = full_app_statuses(10)
    statuses[3] = "building"
    registry = app_with(statuses, target=2, locked=(1,))

    doomed = releases_to_delete(registry.snapshot, "sales")

    assert [release.release_no for release in doomed] == [4]


async def test_a_deleted_release_does_not_count_toward_the_cap() -> None:
    registry = app_with(full_app_statuses(10), target=10)
    seed_numbered_release(registry, 1, "superseded", deleted=True)

    assert releases_to_delete(registry.snapshot, "sales") == ()


async def test_a_full_cap_with_every_release_protected_refuses_the_update() -> None:
    # Active, target and building plus three locks, and four more in progress: ten, all protected.
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
    registry = app_with(statuses, target=4, locked=(1, 2, 3))

    with pytest.raises(RetentionRefused) as refusal:
        releases_to_delete(registry.snapshot, "sales")

    assert str(refusal.value) == FULL_CAP_MESSAGE


async def test_the_cap_is_the_platform_setting_when_one_is_stored() -> None:
    registry = app_with(full_app_statuses(5), target=5)
    seed_setting(registry, "max_releases_per_app", 5)

    assert retention_limits(registry.snapshot).max_releases == 5
    assert [r.release_no for r in releases_to_delete(registry.snapshot, "sales")] == [1]


async def test_the_limits_are_ten_releases_and_three_locks_when_no_setting_is_stored() -> None:
    registry = app_with({1: "active"})

    limits = retention_limits(registry.snapshot)

    assert (limits.max_releases, limits.max_locked) == (10, 3)


@pytest.mark.parametrize("unusable", ["ten", True, 2])
async def test_a_setting_that_is_not_usable_falls_back_to_the_default(unusable: object) -> None:
    # `max_releases_per_app` is at least 4 (RELEASES_KEPT_FREE): 2 is as unusable as text. (A null
    # or a fraction cannot be in the snapshot: a row that fails its model is quarantined.)
    registry = app_with({1: "active"})
    seed_setting(registry, "max_releases_per_app", unusable)

    assert retention_limits(registry.snapshot).max_releases == 10


async def test_a_lowered_cap_deletes_enough_of_the_oldest_unprotected_releases_to_fit() -> None:
    registry = app_with(full_app_statuses(8), target=8)
    seed_setting(registry, "max_releases_per_app", 5)

    doomed = releases_to_delete(registry.snapshot, "sales")

    # Eight live, cap five, and one more is coming: four must go, 1 to 4.
    assert [release.release_no for release in doomed] == [1, 2, 3, 4]


async def test_a_lowered_cap_that_protection_cannot_meet_deletes_nothing() -> None:
    statuses = full_app_statuses(8)
    registry = app_with(statuses, target=8, locked=(1, 2, 3, 4, 5))
    seed_setting(registry, "max_releases_per_app", 5)

    with pytest.raises(RetentionRefused) as refusal:
        releases_to_delete(registry.snapshot, "sales")

    assert str(refusal.value) == (
        "app 'sales' has 8 releases, over its limit of 5, and only 2 of them are unprotected "
        "(not active, target, in progress or locked); unlock some"
    )


# --- deleting -----------------------------------------------------------------------------------


async def test_deleting_clears_the_pointers_and_keeps_the_row() -> None:
    registry = stub_registry_class()()
    seed_app(registry)
    seed_numbered_release(registry, 1, "superseded", label="old", tags=["legacy"])
    seed_numbered_release(registry, 2, "active")

    changes = delete_changes(registry.snapshot, "sales", 1, deleted_by="alice@example.com", now=NOW)
    await registry.write(changes)

    row = release_of(registry, 1)
    assert row.deleted_at == NOW
    assert row.deleted_by == "alice@example.com"
    assert (row.snapshot_digest, row.wheels, row.tags) == (None, [], [])
    assert (row.label, row.status) == ("old", "superseded")
    assert release_of(registry, 2).deleted_at is None


async def test_a_release_is_deletable_the_moment_it_stops_being_active() -> None:
    registry = app_with({7: "active", 9: "healthy"}, target=9)
    with pytest.raises(RetentionRefused):
        delete_changes(registry.snapshot, "sales", 7, deleted_by=LOCKER, now=NOW)

    await registry.write(activation_changes(release_of(registry, 9), release_of(registry, 7)))

    changes = delete_changes(registry.snapshot, "sales", 7, deleted_by=LOCKER, now=NOW)
    await registry.write(changes)
    assert release_of(registry, 7).deleted_at == NOW
    with pytest.raises(RetentionRefused):
        delete_changes(registry.snapshot, "sales", 9, deleted_by=LOCKER, now=NOW)


@pytest.mark.parametrize(
    ("statuses", "target", "locked", "release_no", "message"),
    [
        ({5: "superseded"}, None, (5,), 5, "release 5 is locked; unlock it first"),
        (
            {5: "active"},
            None,
            (),
            5,
            "release 5 is the active release; activate another release first",
        ),
        (
            {5: "failed"},
            5,
            (),
            5,
            "release 5 is the target of the app; activate another release first",
        ),
        (
            {5: "building"},
            None,
            (),
            5,
            "release 5 is in progress (building); wait for it to finish",
        ),
    ],
)
async def test_a_protected_release_is_refused_with_the_reason_in_words(
    statuses: dict[int, str],
    target: int | None,
    locked: tuple[int, ...],
    release_no: int,
    message: str,
) -> None:
    registry = app_with(statuses, target=target, locked=locked)

    with pytest.raises(RetentionRefused) as refusal:
        delete_changes(registry.snapshot, "sales", release_no, deleted_by=LOCKER, now=NOW)

    assert str(refusal.value) == message


async def test_a_delete_guards_the_app_config_row_before_it_touches_the_release() -> None:
    registry = app_with({1: "superseded", 2: "active"}, target=2)

    changes = delete_changes(registry.snapshot, "sales", 1, deleted_by=LOCKER, now=NOW)

    assert [change.kind for change in changes] == [
        WriteKind.APP_CONFIG_UPDATE,
        WriteKind.RELEASE_UPDATE,
    ]
    config = registry.snapshot.app_config("sales")
    assert config is not None
    assert changes[0].expected_version == config.version


async def test_release_numbers_are_never_reused() -> None:
    registry = app_with({1: "superseded", 2: "superseded", 3: "active"}, target=3)
    await registry.write(delete_changes(registry.snapshot, "sales", 2, deleted_by=LOCKER, now=NOW))
    seed_numbered_release(registry, 4, "superseded")
    await registry.write(delete_changes(registry.snapshot, "sales", 4, deleted_by=LOCKER, now=NOW))

    assert next_release_no(registry.snapshot, "sales") == 5


async def test_a_deleted_release_cannot_be_activated() -> None:
    registry = app_with({1: "superseded", 2: "active"}, target=2)
    await registry.write(delete_changes(registry.snapshot, "sales", 1, deleted_by=LOCKER, now=NOW))

    with pytest.raises(IllegalTransition, match="release 1 was deleted"):
        release_status_change(release_of(registry, 1), "pending", by_hand=True)
    with pytest.raises(IllegalTransition):
        activation_changes(release_of(registry, 1), release_of(registry, 2))


# --- hand locks ---------------------------------------------------------------------------------


async def test_a_lock_records_who_and_when_and_how_many_locks_the_app_holds() -> None:
    registry = app_with({1: "superseded", 2: "superseded", 3: "active"}, target=3, locked=(1,))

    plan = lock_changes(registry.snapshot, "sales", 2, locked_by=LOCKER, now=NOW)
    await registry.write(plan.changes)

    row = release_of(registry, 2)
    assert (row.locked_by, row.locked_at) == (LOCKER, NOW)
    assert (plan.locks_after, plan.max_locked) == (2, 3)


async def test_a_fourth_lock_is_refused() -> None:
    registry = app_with(full_app_statuses(6), target=6, locked=(1, 2, 3))

    with pytest.raises(RetentionRefused) as refusal:
        lock_changes(registry.snapshot, "sales", 4, locked_by=LOCKER, now=NOW)

    assert str(refusal.value) == FOURTH_LOCK_MESSAGE


async def test_the_lock_limit_is_the_platform_setting_when_one_is_stored() -> None:
    registry = app_with(full_app_statuses(6), target=6, locked=(1,))
    seed_setting(registry, "max_locked_releases", 1)

    with pytest.raises(RetentionRefused) as refusal:
        lock_changes(registry.snapshot, "sales", 2, locked_by=LOCKER, now=NOW)

    assert str(refusal.value) == "app 'sales' already has 1 locked release; unlock one first"


async def test_with_locking_turned_off_a_lock_is_refused_and_says_why() -> None:
    registry = app_with(full_app_statuses(6), target=6)
    seed_setting(registry, "max_locked_releases", 0)

    with pytest.raises(RetentionRefused) as refusal:
        lock_changes(registry.snapshot, "sales", 1, locked_by=LOCKER, now=NOW)

    assert str(refusal.value) == (
        "app 'sales' cannot lock releases: max_locked_releases is 0; ask a platform admin"
    )


async def test_a_deleted_release_cannot_be_locked() -> None:
    registry = app_with({2: "active"}, target=2)
    seed_numbered_release(registry, 1, "superseded", deleted=True)

    with pytest.raises(RetentionRefused) as refusal:
        lock_changes(registry.snapshot, "sales", 1, locked_by=LOCKER, now=NOW)

    assert str(refusal.value) == "release 1 was deleted; a deleted release cannot be locked"


async def test_only_hand_locks_count_the_active_release_holds_no_lock_of_its_own() -> None:
    registry = app_with(full_app_statuses(6), target=6, locked=(1, 2))

    plan = lock_changes(registry.snapshot, "sales", 3, locked_by=LOCKER, now=NOW)

    assert plan.locks_after == 3  # the active release 6 was not counted


async def test_two_lockers_racing_for_the_last_place_admit_one() -> None:
    registry = app_with(full_app_statuses(6), target=6, locked=(1, 2))
    stale_view = registry.snapshot  # both lockers read the app with two of three places taken
    first = lock_changes(stale_view, "sales", 3, locked_by=LOCKER, now=NOW)
    second = lock_changes(stale_view, "sales", 4, locked_by="carol@example.com", now=NOW)

    await registry.write(first.changes)
    with pytest.raises(ChangedByOthers):
        await registry.write(second.changes)

    assert release_of(registry, 3).locked_by == LOCKER
    assert release_of(registry, 4).locked_by is None
    # The loser reads the app again and is refused by the count, not by luck.
    with pytest.raises(RetentionRefused) as refusal:
        lock_changes(registry.snapshot, "sales", 4, locked_by="carol@example.com", now=NOW)
    assert str(refusal.value) == FOURTH_LOCK_MESSAGE


async def test_a_lock_and_a_delete_of_the_same_release_do_not_both_win() -> None:
    registry = app_with(full_app_statuses(6), target=6)
    stale_view = registry.snapshot
    lock = lock_changes(stale_view, "sales", 2, locked_by=LOCKER, now=NOW)
    delete = delete_changes(stale_view, "sales", 2, deleted_by="carol@example.com", now=NOW)

    await registry.write(lock.changes)
    with pytest.raises(ChangedByOthers):
        await registry.write(delete)

    row = release_of(registry, 2)
    assert (row.locked_by, row.deleted_at) == (LOCKER, None)


async def test_unlocking_clears_the_lock_and_makes_the_release_deletable() -> None:
    registry = app_with({1: "superseded", 2: "active"}, target=2, locked=(1,))

    await registry.write(unlock_changes(registry.snapshot, "sales", 1).changes)

    row = release_of(registry, 1)
    assert (row.locked_by, row.locked_at) == (None, None)
    delete_changes(registry.snapshot, "sales", 1, deleted_by=LOCKER, now=NOW)


async def test_unlocking_reports_the_locks_that_remain() -> None:
    registry = app_with({1: "superseded", 2: "superseded", 3: "active"}, target=3, locked=(1, 2))

    plan = unlock_changes(registry.snapshot, "sales", 1)

    assert (plan.locks_after, plan.max_locked) == (1, 3)
