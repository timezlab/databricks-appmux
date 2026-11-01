"""``Reconciler.stop_app``: what the delete of an app asks before its rows go.

The route closes the app's route and stops its child first and writes the delete second, so a child
never outlives its registration. ``stop_app`` changes no registry row; the app's own task does the
work (it is the only writer of what runs), so a call made while that task builds or checks health
waits for it. A pass that finds the registry still wants the app running starts it again, which is
what a delete that fails afterwards leaves behind.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr
from reconciler_world import World

from appmux.registry.protocol import Change
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio

ENTERED_TIMEOUT_S = 5.0
PLATFORM_ADMIN_TOKEN = SecretStr("tok-test")


class GatedProbe:
    """A health probe that does not answer until the test opens the gate."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def probe(self, socket_path: Path, health_path: str, *, timeout_s: float) -> int:
        self.entered.set()
        await self.gate.wait()
        return 200


async def serving() -> World:
    """Release 1 of ``sales`` is active, started and routed."""
    world = World()
    world.app(target_release_no=1)
    world.release(1, "active")
    await world.run()
    assert world.route_of() == 1
    return world


async def delete_rows(world: World, slug: str = "sales") -> None:
    await world.registry.write(
        [Change(WriteKind.APP_DELETE, key={"slug": slug})], admin_token=PLATFORM_ADMIN_TOKEN
    )


async def test_the_route_closes_and_the_child_stops_before_stop_app_returns() -> None:
    world = await serving()

    await asyncio.wait_for(world.reconciler.stop_app("sales"), ENTERED_TIMEOUT_S)

    assert world.route_of() is None
    assert [child.is_running for child in world.launcher.children] == [False]
    assert world.reconciler.run_state("sales", "running") == "starting"


async def test_stopping_changes_no_row_of_the_registry() -> None:
    world = await serving()
    writes = list(world.status_writes)

    await world.reconciler.stop_app("sales")
    await world.settle()

    assert world.status_writes == writes
    assert world.status(1) == "active"
    assert world.registry.snapshot.app("sales") is not None
    assert world.registry.snapshot.app_config("sales") is not None


async def test_an_app_that_is_not_running_or_not_known_is_stopped_at_once() -> None:
    world = await serving()
    await world.set_desired_state("stopped")
    await world.wake()

    await asyncio.wait_for(world.reconciler.stop_app("sales"), ENTERED_TIMEOUT_S)
    await asyncio.wait_for(world.reconciler.stop_app("nothing"), ENTERED_TIMEOUT_S)

    assert world.route_of() is None


async def test_stopping_twice_is_the_same_as_once() -> None:
    world = await serving()

    await world.reconciler.stop_app("sales")
    await world.reconciler.stop_app("sales")

    assert len(world.launcher.children) == 1
    assert world.route_of() is None


async def test_once_the_rows_are_deleted_nothing_starts_the_app_again() -> None:
    world = await serving()
    await world.reconciler.stop_app("sales")
    await delete_rows(world)
    started = len(world.launcher.started)

    world.reconciler.wake("sales")
    await world.settle()

    assert world.route_of() is None
    assert len(world.launcher.started) == started
    assert world.registry.snapshot.app("sales") is None


async def test_a_copy_started_between_the_stop_and_the_delete_is_taken_down_by_the_next_pass() -> (
    None
):
    world = await serving()
    await world.reconciler.stop_app("sales")
    world.reconciler.wake("sales")  # a pass lands in the window: the registry still wants it
    await world.settle()
    assert world.route_of() == 1, "the registry still says running, so the app came back"

    await delete_rows(world)
    world.reconciler.wake("sales")
    await world.settle()

    assert world.route_of() is None
    assert [child.is_running for child in world.launcher.children] == [False, False]


async def test_a_later_pass_starts_an_app_whose_delete_did_not_happen() -> None:
    world = await serving()

    await world.reconciler.stop_app("sales")
    world.reconciler.wake("sales")  # nothing was deleted: the next pass makes the registry true
    await world.settle()

    assert world.route_of() == 1
    assert world.launcher.children[-1].is_running


async def test_a_stop_during_an_update_waits_for_it_and_leaves_nothing_running() -> None:
    world = await serving()
    probe = GatedProbe()
    world.probe = probe
    world.release(2)
    await world.target(2)
    world.reconciler.wake("sales")
    await asyncio.wait_for(probe.entered.wait(), ENTERED_TIMEOUT_S)

    stopping = asyncio.create_task(world.reconciler.stop_app("sales"))
    await asyncio.sleep(0.05)
    assert not stopping.done(), "the app's own task is busy with the new copy"

    probe.gate.set()
    await asyncio.wait_for(stopping, ENTERED_TIMEOUT_S)

    assert world.route_of() is None
    assert [child.is_running for child in world.launcher.children] == [False, False]


async def test_a_stop_that_is_waiting_when_the_reconciler_shuts_down_returns() -> None:
    world = await serving()
    probe = GatedProbe()
    world.probe = probe
    world.release(2)
    await world.target(2)
    world.reconciler.wake("sales")
    await asyncio.wait_for(probe.entered.wait(), ENTERED_TIMEOUT_S)
    stopping = asyncio.create_task(world.reconciler.stop_app("sales"))
    await asyncio.sleep(0.05)
    assert not stopping.done()

    await world.stop()

    await asyncio.wait_for(stopping, ENTERED_TIMEOUT_S)


async def test_a_reconciler_that_is_shutting_down_has_nothing_for_stop_app_to_do() -> None:
    world = await serving()
    await world.stop()

    await asyncio.wait_for(world.reconciler.stop_app("sales"), ENTERED_TIMEOUT_S)

    assert world.route_of() is None
