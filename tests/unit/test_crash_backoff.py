"""A child that dies is restarted with a growing delay, then the app is ``crashed`` (T099; FR-007).

Design 5.6: the same release restarts after 1, 2, 4, 8 and 16 seconds; after that the app is
``crashed`` and only its own route answers ``503``. Nothing falls back to an older release, and the
exit and the end of the child's log are kept and logged without any value of its environment.

Two parts:

* ``RestartBudget`` (``reconciler/restart.py``) is the pure schedule: which delay comes next, and
  when a copy that ran long enough starts the count again;
* the rest runs a real ``Reconciler`` on fakes (``reconciler_world``), where a child "dies" when the
  test calls ``FakeChild.exit`` and every delay is a number on the fake clock.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

import pytest
from reconciler_fakes import FakeChild, FakeClock
from reconciler_world import World, socket_of

from appmux.reconciler.restart import (
    RESTART_BACKOFF_S,
    STABLE_RUN_S,
    RestartBudget,
)

pytestmark = pytest.mark.anyio

HIDDEN_VALUE: Final = "value-of-a-secret-0123456789"


# --- the schedule ------------------------------------------------------------------------------


def test_the_delays_are_one_two_four_eight_and_sixteen_seconds() -> None:
    assert RESTART_BACKOFF_S == (1.0, 2.0, 4.0, 8.0, 16.0)


def test_each_restart_uses_the_next_delay_and_the_sixth_crash_has_none() -> None:
    budget = RestartBudget()
    delays: list[float | None] = []
    for _ in range(6):
        delays.append(budget.next_delay())
        budget.record_restart()

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, None]


def test_a_copy_that_ran_for_the_stable_period_starts_the_count_again() -> None:
    budget = RestartBudget()
    for _ in range(3):
        budget.record_restart()

    budget.note_crash(ran_for_s=STABLE_RUN_S)

    assert budget.next_delay() == 1.0


def test_a_copy_that_died_sooner_keeps_the_count() -> None:
    budget = RestartBudget()
    for _ in range(3):
        budget.record_restart()

    budget.note_crash(ran_for_s=STABLE_RUN_S - 0.5)

    assert budget.next_delay() == 8.0


# --- the reconciler ----------------------------------------------------------------------------


async def running_world(*, slug: str = "sales", release_no: int = 1, **options: object) -> World:
    """A world where ``slug`` runs ``release_no`` (the active release, restored at start)."""
    world = World(**options)  # type: ignore[arg-type]
    world.app(slug)
    world.release(release_no, "active", slug=slug)
    await world.run()
    assert world.route_of(slug) == release_no
    return world


async def let_the_watchers_run() -> None:
    """Give the task that waits for a child's exit the turns it needs to notice it."""
    for _ in range(8):
        await asyncio.sleep(0)


async def crash(world: World, child: FakeChild, description: str = "exited with status 7") -> None:
    child.exit(description)
    await let_the_watchers_run()
    await world.settle()


def newest_child(world: World, slug: str = "sales") -> FakeChild:
    return world.launcher.children[
        max(i for i, started in enumerate(world.launcher.started) if started.slug == slug)
    ]


async def test_a_crashed_child_restarts_the_same_release_after_one_second() -> None:
    world = await running_world()
    first = newest_child(world)

    await crash(world, first)

    assert [(s.slug, s.release_no) for s in world.launcher.started] == [("sales", 1), ("sales", 1)]
    assert world.clock.sleeps == [1.0]
    assert world.route_of() == 1
    assert world.reconciler.run_state("sales", "running") == "running"


async def test_the_route_is_closed_from_the_crash_until_the_restart_is_healthy() -> None:
    world = await running_world()

    await crash(world, newest_child(world))

    assert [slug for slug, _ in world.routes.closed] == ["sales"]
    opened = [(slug, no) for slug, no, _ in world.routes.opened]
    assert opened == [("sales", 1), ("sales", 1)]  # the restore at start, then the restart


async def test_each_crash_waits_twice_as_long_as_the_last_up_to_sixteen_seconds() -> None:
    world = await running_world()

    for crash_number in range(1, 6):
        await crash(world, newest_child(world))
        assert world.clock.sleeps == list(RESTART_BACKOFF_S[:crash_number])
        assert len(world.launcher.started) == crash_number + 1


async def test_after_the_fifth_restart_the_next_crash_leaves_the_app_crashed() -> None:
    world = await running_world()
    for _ in range(5):
        await crash(world, newest_child(world))

    await crash(world, newest_child(world))

    assert len(world.launcher.started) == 6  # the first start and five restarts, no sixth
    assert world.clock.sleeps == list(RESTART_BACKOFF_S)
    assert world.route_of() is None
    assert world.reconciler.run_state("sales", "running") == "crashed"


async def test_a_crashed_app_is_not_started_again_by_later_passes() -> None:
    world = await running_world()
    for _ in range(6):
        await crash(world, newest_child(world))

    await world.wake()
    world.reconciler.wake_all()
    await world.settle()

    assert len(world.launcher.started) == 6
    assert world.reconciler.run_state("sales", "running") == "crashed"


async def test_only_the_route_of_the_crashed_app_closes() -> None:
    world = World()
    world.app("sales")
    world.app("docs")
    world.release(1, "active", slug="sales")
    world.release(1, "active", slug="docs")
    await world.run()
    docs_child = newest_child(world, "docs")

    for _ in range(6):
        await crash(world, newest_child(world, "sales"))

    assert world.route_of("sales") is None
    assert world.route_of("docs") == 1
    assert docs_child.is_running
    assert {slug for slug, _ in world.routes.closed} == {"sales"}
    assert world.reconciler.run_state("docs", "running") == "running"
    assert world.reconciler.run_state("sales", "running") == "crashed"


async def test_no_older_release_is_started_or_activated_after_repeated_crashes() -> None:
    world = World()
    world.app()
    world.release(1, "superseded")
    world.release(2, "active")
    await world.run()

    for _ in range(6):
        await crash(world, newest_child(world))

    assert {s.release_no for s in world.launcher.started} == {2}
    assert world.status(1) == "superseded"
    assert world.status(2) == "active"
    assert world.history(1) == []
    assert world.history(2) == []  # not even a status write: a crash is not a release's failure


async def test_a_restart_that_never_gets_healthy_counts_as_the_next_attempt() -> None:
    world = await running_world()
    world.probe = _Refusing(world.clock)

    await crash(world, newest_child(world))

    assert len(world.launcher.started) == 6  # the first start and five attempts, all refused
    assert world.reconciler.run_state("sales", "running") == "crashed"
    assert world.status(1) == "active"  # the release itself is not marked failed
    assert world.route_of() is None
    assert all(not child.is_running for child in world.launcher.children)


async def test_a_copy_that_ran_for_a_minute_gets_the_first_delay_again() -> None:
    world = await running_world()
    await crash(world, newest_child(world))
    world.clock.now += STABLE_RUN_S

    await crash(world, newest_child(world))

    assert world.clock.sleeps == [1.0, 1.0]


async def test_restart_brings_a_crashed_app_back_with_a_fresh_count() -> None:
    world = await running_world()
    for _ in range(6):
        await crash(world, newest_child(world))

    world.reconciler.request_restart("sales")
    await world.settle()

    assert len(world.launcher.started) == 7
    assert world.route_of() == 1
    assert world.reconciler.run_state("sales", "running") == "running"
    assert world.reconciler.last_crash("sales") is None
    await crash(world, newest_child(world))
    assert world.clock.sleeps[-1] == 1.0


async def test_stopping_and_starting_a_crashed_app_runs_the_active_release_again() -> None:
    world = await running_world()
    for _ in range(6):
        await crash(world, newest_child(world))

    await world.set_desired_state("stopped")
    await world.wake()
    assert world.reconciler.run_state("sales", "stopped") == "stopped"
    await world.set_desired_state("running")
    await world.wake()

    assert len(world.launcher.started) == 7
    assert world.route_of() == 1
    assert world.reconciler.run_state("sales", "running") == "running"


async def test_a_new_release_replaces_a_crashed_one_and_clears_the_crash() -> None:
    world = await running_world()
    for _ in range(6):
        await crash(world, newest_child(world))
    world.release(2, "pending")
    await world.target(2)

    await world.wake()

    assert world.route_of() == 2
    assert world.status(2) == "active"
    assert world.status(1) == "superseded"
    assert world.reconciler.run_state("sales", "running") == "running"


async def test_the_app_is_starting_while_it_waits_to_be_restarted() -> None:
    world = await running_world()
    gate = asyncio.Event()
    real_sleep = world.clock.sleep

    async def held(seconds: float) -> None:
        await gate.wait()
        await real_sleep(seconds)

    world.clock.sleep = held  # type: ignore[method-assign]

    newest_child(world).exit("exited with status 7")
    await let_the_watchers_run()

    assert world.route_of() is None
    assert world.reconciler.run_state("sales", "running") == "starting"
    gate.set()
    await world.settle()
    assert world.reconciler.run_state("sales", "running") == "running"


async def test_a_stop_during_the_wait_is_honoured_after_it_without_a_restart() -> None:
    world = await running_world()
    gate = asyncio.Event()
    real_sleep = world.clock.sleep

    async def held(seconds: float) -> None:
        await gate.wait()
        await real_sleep(seconds)

    world.clock.sleep = held  # type: ignore[method-assign]
    newest_child(world).exit("exited with status 7")
    await let_the_watchers_run()

    await world.set_desired_state("stopped")
    world.reconciler.wake("sales")
    gate.set()
    await world.settle()

    assert len(world.launcher.started) == 1
    assert world.route_of() is None
    assert world.reconciler.run_state("sales", "stopped") == "stopped"


async def test_the_stop_of_a_running_app_is_not_taken_for_a_crash() -> None:
    world = await running_world()

    await world.set_desired_state("stopped")
    await world.wake()
    await let_the_watchers_run()
    await world.settle()

    assert len(world.launcher.started) == 1
    assert world.clock.sleeps == []
    assert world.reconciler.last_crash("sales") is None


async def test_a_replaced_copy_that_exits_while_draining_is_not_a_crash() -> None:
    world = await running_world()
    world.release(2, "pending")
    await world.target(2)
    await world.wake()

    assert world.route_of() == 2
    assert [s.release_no for s in world.launcher.started] == [1, 2]
    assert world.reconciler.last_crash("sales") is None
    assert world.reconciler.run_state("sales", "running") == "running"


# --- what is kept ------------------------------------------------------------------------------


async def test_the_exit_and_the_end_of_the_log_are_kept_for_the_last_crash() -> None:
    world = World()
    world.app()
    world.release(1, "active")
    world.launcher.log_of_next_child = "starting up\nTraceback: boom\nValueError: no config"
    await world.run()

    await crash(world, newest_child(world), "exited with status 7")

    report = world.reconciler.last_crash("sales")
    assert report is not None
    assert report.release_no == 1
    assert report.exit_description == "exited with status 7"
    assert "ValueError: no config" in report.log_tail


async def test_a_crash_is_logged_with_the_exit_and_the_log_without_environment_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = World()
    world.app()
    world.release(1, "active")
    world.environments.environment = {"API_KEY": HIDDEN_VALUE}
    world.launcher.log_of_next_child = f"connecting with {HIDDEN_VALUE}\nValueError: no config"
    await world.run()

    with caplog.at_level(logging.WARNING, logger="appmux.reconciler"):
        await crash(world, newest_child(world), "exited with status 7")

    assert "sales" in caplog.text
    assert "exited with status 7" in caplog.text
    assert "ValueError: no config" in caplog.text
    assert HIDDEN_VALUE not in caplog.text
    report = world.reconciler.last_crash("sales")
    assert report is not None
    assert HIDDEN_VALUE not in report.log_tail
    assert HIDDEN_VALUE not in report.exit_description


async def test_the_socket_of_a_restart_is_the_one_of_its_release() -> None:
    world = await running_world()

    await crash(world, newest_child(world))

    route = world.routes.lookup("sales")
    assert route is not None
    assert route.socket_path == socket_of("sales", 1)


class _Refusing:
    """A probe that never gets an answer, for the restarts that come back unhealthy."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    async def probe(self, socket_path: object, health_path: str, *, timeout_s: float) -> int:
        from appmux.reconciler.health import ProbeFailed

        await asyncio.sleep(0)
        raise ProbeFailed("connection refused")
