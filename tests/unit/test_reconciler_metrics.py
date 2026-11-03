"""What the reconciler tells the metrics (T192, TD-016 (1)): restarts, admission refusals, children.

The reconciler counts through a small ``ReconcilerMetrics`` seam (the aggregator in the running
service, a recorder here), and answers which children run so the sampler can read their memory
and their pumps. The world is the shared one of ``reconciler_world.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from reconciler_world import World

pytestmark = pytest.mark.anyio


async def running_world(**options: object) -> World:
    world = World(**options)  # type: ignore[arg-type]
    world.app()
    world.release(1, "active")
    await world.run()
    assert world.route_of() == 1
    return world


async def crash(world: World) -> None:
    world.launcher.children[-1].exit("exited with status 7")
    for _ in range(8):  # the turns the exit watcher needs to notice
        await asyncio.sleep(0)
    await world.settle()


# --- restarts -------------------------------------------------------------------------------------


async def test_bringing_up_the_active_release_at_start_is_not_a_restart() -> None:
    world = await running_world()

    assert world.metrics.restarts == []


async def test_a_crash_restart_is_counted_for_its_app() -> None:
    world = await running_world()

    await crash(world)

    assert world.metrics.restarts == ["sales"]


async def test_each_attempt_after_a_crash_is_counted() -> None:
    world = await running_world()

    await crash(world)
    await crash(world)

    assert world.metrics.restarts == ["sales", "sales"]


async def test_a_restart_a_person_asked_for_is_counted() -> None:
    world = await running_world()

    world.reconciler.request_restart("sales")
    await world.settle()

    assert len(world.launcher.started) == 2
    assert world.metrics.restarts == ["sales"]


async def test_an_update_to_a_new_release_is_not_a_restart() -> None:
    world = await running_world()
    world.release(2)
    await world.target(2)

    await world.wake()

    assert world.route_of() == 2
    assert world.metrics.restarts == []


# --- admission refusals ---------------------------------------------------------------------------


async def test_a_release_refused_for_memory_is_counted_once() -> None:
    world = World(memory_limit_mb=200)
    world.app(target_release_no=1, memory_budget_mb=256)
    world.release(1)

    await world.run()

    assert world.status(1) == "failed"
    assert world.metrics.admission_rejections == 1


async def test_a_release_that_fits_is_not_counted_as_refused() -> None:
    world = await running_world()

    assert world.metrics.admission_rejections == 0


# --- which children run ---------------------------------------------------------------------------


async def test_the_running_children_are_listed_with_their_app() -> None:
    world = await running_world()
    world.app("hr")
    world.release(1, "active", slug="hr")
    await world.wake("hr")

    children = world.reconciler.running_children()

    assert sorted(slug for slug, _ in children) == ["hr", "sales"]
    assert {id(child) for _, child in children} == {id(c) for c in world.launcher.children}
    assert world.reconciler.running_app_count() == 2


async def test_a_stopped_app_has_no_running_child() -> None:
    world = await running_world()

    await world.set_desired_state("stopped")
    await world.wake()

    assert world.reconciler.running_children() == []
    assert world.reconciler.running_app_count() == 0
