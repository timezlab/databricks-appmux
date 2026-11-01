"""Shutdown (T093 ``stop_all``): every route closes, every child stops, inside the budget.

appmux has 15 seconds between SIGTERM and the end of the container, so ``stop_all`` stops all
children at once and returns when its budget ends whatever is left. Registry statuses are not
touched: the active release stays active so the next start restores it.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from reconciler_fakes import FakeChild
from reconciler_world import World

from appmux.reconciler.build import BuiltRelease
from appmux.registry.models import Release

pytestmark = pytest.mark.anyio

ENTERED_TIMEOUT_S = 5.0


class BlockingProbe:
    """A health probe whose first poll never returns, so a release stays in ``starting``."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def probe(self, socket_path: Path, health_path: str, *, timeout_s: float) -> int:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def two_running_apps(world: World) -> None:
    for slug in ("alpha", "beta"):
        world.app(slug, target_release_no=1)
        world.release(1, "active", slug)


async def test_stopping_closes_every_route_and_stops_every_child() -> None:
    world = World()
    two_running_apps(world)
    await world.run()
    assert len(world.routes.routes()) == 2

    await world.stop()

    assert world.routes.routes() == {}
    assert [child.is_running for child in world.launcher.children] == [False, False]


async def test_stopping_leaves_the_active_release_active_for_the_next_start() -> None:
    world = World()
    two_running_apps(world)
    await world.run()
    writes_before = list(world.status_writes)

    await world.stop()

    assert world.status_writes == writes_before
    assert world.status(1, "alpha") == "active"


async def test_children_are_stopped_all_at_once_not_one_after_another() -> None:
    world = World()
    two_running_apps(world)
    await world.run()
    alpha, beta = (world.launcher.child_of(slug, 1) for slug in ("alpha", "beta"))
    beta_is_stopping = asyncio.Event()

    async def alpha_waits_for_beta(*, grace_s: float) -> object:
        await beta_is_stopping.wait()
        return await FakeChild.stop(alpha, grace_s=grace_s)

    async def beta_signals(*, grace_s: float) -> object:
        beta_is_stopping.set()
        return await FakeChild.stop(beta, grace_s=grace_s)

    alpha.stop = alpha_waits_for_beta  # type: ignore[method-assign,assignment]
    beta.stop = beta_signals  # type: ignore[method-assign,assignment]

    await world.reconciler.stop_all(within_s=2.0)

    assert not alpha.is_running
    assert not beta.is_running


async def test_a_child_that_will_not_stop_does_not_keep_stop_all_past_its_budget() -> None:
    world = World()
    two_running_apps(world)
    await world.run()
    stubborn = world.launcher.child_of("alpha", 1)

    async def never_stops(*, grace_s: float) -> object:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    stubborn.stop = never_stops  # type: ignore[method-assign,assignment]
    started = time.monotonic()

    await world.reconciler.stop_all(within_s=0.3)

    assert time.monotonic() - started < 3.0
    assert not world.launcher.child_of("beta", 1).is_running, "the others were still stopped"
    assert world.routes.routes() == {}


async def test_the_grace_given_to_children_fits_inside_the_budget() -> None:
    world = World()
    two_running_apps(world)
    await world.run()

    await world.reconciler.stop_all(within_s=4.0)

    assert [grace for child in world.launcher.children for grace in child.stops][:2] == [2.0, 2.0]


async def test_stopping_while_a_release_builds_hands_it_back_as_pending() -> None:
    world = World()
    world.app(target_release_no=1)
    world.release(1)
    entered = asyncio.Event()

    async def build_forever(release: Release) -> BuiltRelease:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    world.builder.build = build_forever  # type: ignore[method-assign,assignment]
    await world.reconciler.start()
    await asyncio.wait_for(entered.wait(), ENTERED_TIMEOUT_S)

    await world.stop()

    assert world.status(1) == "pending"
    assert world.route_of() is None
    assert world.launcher.started == []


async def test_stopping_while_a_release_waits_for_health_stops_its_child_and_opens_no_route() -> (
    None
):
    world = World()
    world.app(target_release_no=1)
    world.release(1)
    blocking = BlockingProbe()
    world.probe = blocking  # type: ignore[assignment]
    await world.reconciler.start()
    await asyncio.wait_for(blocking.entered.wait(), ENTERED_TIMEOUT_S)

    await world.stop()

    assert world.status(1) == "pending"
    assert world.route_of() is None
    assert world.routes.opened == []
    assert not world.launcher.children[0].is_running


async def test_stopping_during_a_drain_stops_the_old_and_the_new_copy() -> None:
    world = World()
    world.app(target_release_no=1)
    world.release(1, "active")
    await world.run()
    drain_started = asyncio.Event()

    async def drain_forever(seconds: float) -> None:
        drain_started.set()
        await asyncio.Event().wait()

    world.clock.sleep = drain_forever  # type: ignore[method-assign,assignment]
    world.release(2)
    await world.target(2)
    world.reconciler.wake("sales")
    await asyncio.wait_for(drain_started.wait(), ENTERED_TIMEOUT_S)
    assert world.status(2) == "active"

    await world.stop()

    assert not world.launcher.child_of("sales", 1).is_running
    assert not world.launcher.child_of("sales", 2).is_running


async def test_nothing_is_started_after_stopping() -> None:
    world = World()
    world.app(target_release_no=1)
    world.release(1, "active")
    await world.run()
    await world.stop()
    started = len(world.launcher.started)

    world.release(2)
    await world.target(2)
    await world.wake()

    assert len(world.launcher.started) == started
    assert world.status(2) == "pending"


async def test_stopping_twice_is_harmless() -> None:
    world = World()
    world.app(target_release_no=1)
    world.release(1, "active")
    await world.run()

    await world.stop()
    await world.stop()

    assert world.routes.routes() == {}


async def test_stopping_a_reconciler_that_never_started_is_harmless() -> None:
    world = World()

    await world.stop()

    assert world.routes.routes() == {}
