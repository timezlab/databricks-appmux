"""``Reconciler.run_state`` says ``no_release`` for an app that wants to run and has nothing to run.

An app whose first release failed its health check, or that was registered and never updated, has
no active release and nothing building. Its route is closed and will stay closed until a person
acts (Update, or Activate on a release by hand), so ``starting`` would be a lie: the badge of the
admin UI read "Starting" for ever beside "Release 1 failed" (owner feedback, 2026-11-04, T210).
``starting`` stays for an app that has something on its way: a release being built, started or
checked, a target waiting to be built, or an active release whose copy is not up yet.
"""

from __future__ import annotations

import pytest
from reconciler_world import World

pytestmark = pytest.mark.anyio


def app_with(*releases: tuple[int, str], **settings: object) -> World:
    world = World()
    world.app(**settings)
    for number, status in releases:
        world.release(number, status)
    return world


def test_an_app_that_never_had_a_release_has_none() -> None:
    world = app_with()

    assert world.reconciler.run_state("sales", "running") == "no_release"


def test_an_app_whose_only_release_failed_has_none() -> None:
    world = app_with((1, "failed"))

    assert world.reconciler.run_state("sales", "running") == "no_release"


def test_a_release_that_was_deleted_does_not_count_as_on_its_way() -> None:
    world = World()
    world.app()
    world.release(1, "building", deleted=True)

    assert world.reconciler.run_state("sales", "running") == "no_release"


@pytest.mark.parametrize("status", ["building", "starting", "healthy"])
def test_a_release_on_its_way_up_is_starting(status: str) -> None:
    world = app_with((1, status))

    assert world.reconciler.run_state("sales", "running") == "starting"


def test_a_pending_target_waiting_to_be_built_is_starting() -> None:
    world = app_with((1, "pending"), target_release_no=1)

    assert world.reconciler.run_state("sales", "running") == "starting"


def test_a_pending_release_that_is_not_the_target_is_not_on_its_way() -> None:
    world = app_with((1, "pending"))

    assert world.reconciler.run_state("sales", "running") == "no_release"


def test_an_active_release_whose_copy_is_not_up_yet_is_starting() -> None:
    world = app_with((1, "active"))

    assert world.reconciler.run_state("sales", "running") == "starting"


def test_a_stopped_app_is_stopped_whatever_its_releases() -> None:
    world = app_with((1, "failed"))

    assert world.reconciler.run_state("sales", "stopped") == "stopped"


async def test_after_a_first_release_fails_its_health_check_the_app_has_no_release() -> None:
    world = World(probe_answer="connection refused")
    world.app(target_release_no=1)
    world.release(1)

    await world.run()

    assert world.status(1) == "failed"
    assert world.reconciler.run_state("sales", "running") == "no_release"


async def test_after_a_first_release_becomes_active_the_app_is_running() -> None:
    world = World()
    world.app(target_release_no=1)
    world.release(1)

    await world.run()

    assert world.status(1) == "active"
    assert world.reconciler.run_state("sales", "running") == "running"


async def test_a_failed_update_beside_an_active_release_leaves_the_app_running() -> None:
    world = World()
    world.app(target_release_no=1)
    world.release(1)
    await world.run()
    world.release(2, "failed")

    assert world.reconciler.run_state("sales", "running") == "running"
