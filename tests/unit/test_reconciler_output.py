"""What the reconciler keeps of a child's output, and how a failed start explains it (T189, T190).

Every start writes into its app's output ring (``reconciler/output.py``): ``started`` when the
sandbox is up, the child's lines as they come, and how the child ended. A release that fails at
start stores the reason with the exit code or the signal and the last 50 lines of that start;
a crashed copy's report takes its lines from the same ring. These run a real ``Reconciler`` on
the fakes of ``reconciler_world``, where a child "prints" what ``FakeChild.log`` holds.
"""

from __future__ import annotations

import asyncio
from typing import Final

import pytest
from pydantic import SecretStr
from reconciler_fakes import FakeChild
from reconciler_world import World

from appmux.reconciler.output import OutputRing
from appmux.registry.apps import platform_fields_change
from appmux.registry.protocol import Change
from appmux.registry.releases import START_LOG_TAIL_LINES
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio

HIDDEN_VALUE: Final = "value-of-a-secret-0123456789"
DANA: Final = "dana@example.com"
PLATFORM_ADMIN_TOKEN: Final = SecretStr("tok-test")


def pending_world(**options: object) -> World:
    world = World(**options)  # type: ignore[arg-type]
    world.app(target_release_no=1)
    world.release(1)
    return world


def crash_at_start(world: World, *, log: str = "", **ending: object) -> None:
    """The next child prints ``log`` and ends before its first health answer."""
    started = world.launcher.start

    async def start_then_end(child: object) -> FakeChild:
        running = await started(child)  # type: ignore[arg-type]
        running.log = log
        running.exit("ended", **ending)  # type: ignore[arg-type]
        return running

    world.launcher.start = start_then_end  # type: ignore[method-assign]


def ring_of(world: World, slug: str = "sales") -> OutputRing:
    ring = world.reconciler.output(slug)
    assert ring is not None
    return ring


def ring_texts(world: World, slug: str = "sales") -> list[str]:
    return [line.text for line in ring_of(world, slug).read(after=0, limit=1000).lines]


async def let_the_watchers_run() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


# --- the reason and the tail of a failed start -------------------------------------------------


async def test_an_exit_at_start_names_its_code_and_how_long_the_app_ran() -> None:
    world = pending_world(probe_answer="connection refused")
    crash_at_start(world, log="Traceback: boom", code=1)

    await world.run()

    row = world.row(1)
    assert row.status == "failed"
    assert row.status_reason.startswith("the app exited with code 1 after ")
    assert row.status_reason.endswith(" s")
    assert row.build_log_tail == "Traceback: boom"
    assert world.clock.now < 60, "it did not wait for the deadline"


async def test_a_death_by_signal_at_start_names_the_signal() -> None:
    world = pending_world(probe_answer="connection refused")
    crash_at_start(world, log="# Fatal process out of memory", signal="SIGTRAP")

    await world.run()

    assert world.row(1).status_reason.startswith("the app was stopped by signal SIGTRAP after ")


async def test_a_failed_start_keeps_the_last_50_lines_of_that_start() -> None:
    world = pending_world(probe_answer="connection refused")
    lines = [f"line {number}" for number in range(80)]
    world.launcher.log_of_next_child = "\n".join(lines)

    await world.run()

    assert START_LOG_TAIL_LINES == 50
    assert world.row(1).build_log_tail == "\n".join(lines[-START_LOG_TAIL_LINES:])


async def test_a_child_that_hangs_silently_fails_at_the_deadline_with_an_empty_tail() -> None:
    # The Node.js shape at a 64 MB budget: the process neither exits nor prints.
    world = pending_world(probe_answer="connection refused")

    await world.run()

    row = world.row(1)
    assert row.status_reason == (
        "health check did not pass within 60 s (last error: connection refused)"
    )
    assert row.build_log_tail is None
    assert ring_texts(world)[0] == "started"
    assert any(
        text.startswith("start failed: health check did not pass") for text in ring_texts(world)
    )


async def test_a_sandbox_that_does_not_start_says_so_in_the_ring() -> None:
    world = pending_world()
    world.launcher.fail_with = "stage 1 failed: mount denied"

    await world.run()

    assert world.row(1).status_reason == "stage 1 failed: mount denied"
    assert ring_texts(world) == ["start failed: stage 1 failed: mount denied"]


# --- the ring ----------------------------------------------------------------------------------


async def test_the_ring_holds_the_start_the_childs_lines_and_how_it_ended() -> None:
    world = pending_world()
    world.launcher.log_of_next_child = "listening on 8000\nGET / 200"

    await world.run()
    world.launcher.children[0].exit("exited with status 7", code=7)
    await let_the_watchers_run()

    texts = ring_texts(world)
    assert texts[:3] == ["started", "listening on 8000", "GET / 200"]
    assert texts[3].startswith("exited with code 7 after ")
    page = ring_of(world).read(after=0, limit=1000)
    assert page.last_exit is not None
    assert page.last_exit.code == 7
    await world.stop()


async def test_a_running_child_is_read_while_it_runs() -> None:
    world = pending_world(output_poll_s=0.01)
    await world.run()
    child = world.launcher.children[0]

    child.log = "first request"
    for _ in range(500):  # the follower reads every 10 ms of real time; give it up to 5 s
        if "first request" in ring_texts(world):
            break
        await asyncio.sleep(0.01)

    assert "first request" in ring_texts(world)
    assert child.is_running
    await world.stop()


async def test_the_ring_survives_a_crash_and_the_restart_gets_a_new_instance() -> None:
    world = World()
    world.app()
    world.release(1, "active")
    world.launcher.log_of_next_child = "first copy"
    await world.run()
    first_instance = ring_of(world).read(after=0, limit=1).instance

    world.launcher.log_of_next_child = "second copy"
    world.launcher.children[0].exit("exited with status 7", code=7)
    await let_the_watchers_run()
    await world.settle()

    texts = ring_texts(world)
    assert "first copy" in texts
    assert "second copy" not in texts  # the new copy's lines come when it is read
    assert texts.count("started") == 2
    assert ring_of(world).read(after=0, limit=1).instance != first_instance
    crash = world.reconciler.last_crash("sales")
    assert crash is not None
    assert crash.log_tail == "first copy"
    await world.stop()


async def test_the_childs_environment_values_never_reach_the_ring() -> None:
    world = pending_world(probe_answer="connection refused")
    world.environments.environment = {"API_KEY": HIDDEN_VALUE}
    world.launcher.log_of_next_child = f"connecting with {HIDDEN_VALUE}"

    await world.run()

    assert all(HIDDEN_VALUE not in text for text in ring_texts(world))
    assert "connecting with ***" in ring_texts(world)
    assert HIDDEN_VALUE not in (world.row(1).build_log_tail or "")


async def test_the_values_to_hide_are_gathered_once_per_start_not_once_per_line() -> None:
    # Gathering them reads the length of every value; done per line, a flood of lines costs as
    # many passes over the environment, all in the shared event loop.
    length_reads = 0

    class CountedValue(str):
        def __len__(self) -> int:
            nonlocal length_reads
            length_reads += 1
            return str.__len__(self)

    line_count = 200
    world = pending_world()
    world.environments.environment = {"API_KEY": CountedValue(HIDDEN_VALUE)}
    world.launcher.log_of_next_child = "\n".join(f"line {n}" for n in range(line_count))

    await world.run()
    world.launcher.children[0].exit("exited with status 7", code=7)
    await let_the_watchers_run()

    assert f"line {line_count - 1}" in ring_texts(world)
    assert length_reads < line_count
    await world.stop()


async def test_an_app_that_never_started_here_has_no_ring() -> None:
    world = World()
    world.app()

    await world.run()

    assert world.reconciler.output("sales") is None


async def test_the_ring_follows_a_rename_and_goes_with_a_deleted_app() -> None:
    world = World()
    world.app()
    world.release(1, "active")
    await world.run()
    assert ring_of(world).read(after=0, limit=10).lines

    app = world.registry.snapshot.app("sales")
    change = platform_fields_change(app, updated_by=DANA, slug="revenue")
    await world.registry.write([change], admin_token=PLATFORM_ADMIN_TOKEN)
    world.reconciler.app_renamed("sales", "revenue")
    await world.settle()

    assert world.reconciler.output("sales") is None
    assert ring_texts(world, "revenue").count("started") == 2  # the copy it had, and the new one
    await world.reconciler.stop_app("revenue")
    await world.registry.write(
        [Change(WriteKind.APP_DELETE, key={"slug": "revenue"})], admin_token=PLATFORM_ADMIN_TOKEN
    )
    await world.wake("revenue")
    assert world.reconciler.output("revenue") is None
    await world.stop()
