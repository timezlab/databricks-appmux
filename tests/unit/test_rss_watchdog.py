"""The best-effort RSS watchdog and the ``RLIMIT_AS`` layer (design 5.7, FR-050, T158).

Three kinds of test, none of which needs a container:

* the ``/proc`` reader against a fake ``/proc`` directory (parsing, tree walk, a process that
  vanishes mid-scan);
* the decision (who is over budget with headroom) over a fake RSS reading;
* the real thing on this Linux machine: short-lived Python processes that hold a known amount of
  memory, some of them with a descendant, in their own session so that the fixture can always
  kill the whole tree. Margins are tens of megabytes; nothing waits on a fixed sleep.

What only a container run proves: that the host can read the sandbox's processes through the pid
namespace and that the stop callback of the launcher ends the sandbox (the callback here kills a
process group).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import resource
import select
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from appmux.sandbox.watchdog import (
    Offence,
    WatchedChild,
    address_space_limit_bytes,
    apply_address_space_limit,
    find_offenders,
    process_tree_pids,
    process_tree_rss_bytes,
    watch,
)

MIB = 1024 * 1024
PAGE_BYTES = resource.getpagesize()
READY_TIMEOUT_S = 15.0
EVENT_TIMEOUT_S = 10.0
SHORT_INTERVAL_S = 0.02

# Holds ``megabytes`` of written (so resident) memory in this process and in ``descendants``
# nested child processes, prints "ready" once the whole chain is up, then waits. The sleep is a
# safety net only: the fixture kills the session long before it ends.
HOLDER_SCRIPT = """\
import subprocess, sys, time
megabytes, descendants = int(sys.argv[1]), int(sys.argv[2])
if descendants:
    child = subprocess.Popen(
        [sys.executable, "-I", __file__, str(megabytes), str(descendants - 1)],
        stdout=subprocess.PIPE, text=True)
    child.stdout.readline()
held = bytearray(b"\\x01") * (megabytes * 1024 * 1024)
print("ready", flush=True)
time.sleep(60)
"""


# ---- fixtures: real process trees ----------------------------------------------------------------


def wait_until_ready(process: subprocess.Popen[str]) -> None:
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], READY_TIMEOUT_S)
    assert readable, "the memory holder did not report ready in time"
    assert process.stdout.readline().strip() == "ready"


@pytest.fixture
def start_holder(tmp_path: Path) -> Iterator[Callable[[int, int], subprocess.Popen[str]]]:
    """Start a tree holding ``megabytes`` per process; every tree is killed at teardown."""
    script = tmp_path / "holder.py"
    script.write_text(HOLDER_SCRIPT)
    started: list[subprocess.Popen[str]] = []

    def start(megabytes: int, descendants: int = 0) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-I", str(script), str(megabytes), str(descendants)],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,  # its own process group: killpg reaches every descendant
        )
        started.append(process)
        wait_until_ready(process)
        return process

    yield start

    for process in started:
        kill_tree(process.pid)
        process.wait(timeout=READY_TIMEOUT_S)
        if process.stdout is not None:
            process.stdout.close()


def kill_tree(root_pid: int) -> None:
    """Kill the process group led by ``root_pid`` (the holders are session leaders)."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(root_pid, signal.SIGKILL)


# ---- the /proc reader against a fake /proc -------------------------------------------------------


def add_fake_process(
    proc_root: Path, pid: int, parent_pid: int, resident_pages: int | None, comm: str = "worker"
) -> None:
    """``stat`` always; ``statm`` unless ``resident_pages`` is None (the process just exited)."""
    directory = proc_root / str(pid)
    directory.mkdir()
    # Fields after the closing parenthesis of the name: state, ppid, then filler.
    (directory / "stat").write_text(f"{pid} ({comm}) S {parent_pid} {pid} {pid} 0 -1\n")
    if resident_pages is not None:
        (directory / "statm").write_text(f"9999 {resident_pages} 100 1 0 50 0\n")


@pytest.fixture
def fake_proc(tmp_path: Path) -> Path:
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    # Entries of a real /proc that are not processes.
    (proc_root / "cpuinfo").write_text("processor : 0\n")
    (proc_root / "self").mkdir()
    return proc_root


def test_the_tree_is_the_root_and_all_its_descendants_and_nothing_else(fake_proc: Path) -> None:
    add_fake_process(fake_proc, 1, 0, 10)
    add_fake_process(fake_proc, 100, 1, 10)  # the root of the tree
    add_fake_process(fake_proc, 101, 100, 10)
    add_fake_process(fake_proc, 102, 100, 10)
    add_fake_process(fake_proc, 103, 102, 10)  # a grandchild
    add_fake_process(fake_proc, 200, 1, 10)  # a sibling app's root
    add_fake_process(fake_proc, 201, 200, 10)

    assert sorted(process_tree_pids(100, proc_root=fake_proc)) == [100, 101, 102, 103]
    assert sorted(process_tree_pids(200, proc_root=fake_proc)) == [200, 201]


def test_a_process_name_with_spaces_and_parentheses_does_not_break_the_parent_field(
    fake_proc: Path,
) -> None:
    add_fake_process(fake_proc, 100, 1, 10)
    add_fake_process(fake_proc, 101, 100, 10, comm="a) S 1 (b c")

    assert sorted(process_tree_pids(100, proc_root=fake_proc)) == [100, 101]


def test_rss_of_the_tree_is_the_sum_of_resident_pages_in_bytes(fake_proc: Path) -> None:
    add_fake_process(fake_proc, 100, 1, 300)
    add_fake_process(fake_proc, 101, 100, 200)
    add_fake_process(fake_proc, 200, 1, 5000)  # another app: not counted

    assert process_tree_rss_bytes(100, proc_root=fake_proc) == 500 * PAGE_BYTES


def test_a_process_that_exits_mid_scan_is_skipped_not_fatal(fake_proc: Path) -> None:
    add_fake_process(fake_proc, 100, 1, 300)
    add_fake_process(fake_proc, 101, 100, None)  # listed with a stat, statm already gone

    assert process_tree_rss_bytes(100, proc_root=fake_proc) == 300 * PAGE_BYTES


def test_a_missing_root_has_no_tree_and_no_memory(fake_proc: Path) -> None:
    add_fake_process(fake_proc, 200, 1, 50)

    assert process_tree_pids(100, proc_root=fake_proc) == []
    assert process_tree_rss_bytes(100, proc_root=fake_proc) == 0


def test_a_missing_proc_is_an_error_not_an_empty_machine(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="needs a Linux /proc"):
        process_tree_rss_bytes(100, proc_root=tmp_path / "no-proc-here")


# ---- who is over budget --------------------------------------------------------------------------


def fixed_readings(readings_mb: dict[int, int]) -> Callable[[int], int]:
    """A measure that returns the stated megabytes for a root pid."""
    return lambda root_pid: readings_mb[root_pid] * MIB


def test_a_child_is_an_offender_only_beyond_its_budget_times_the_headroom() -> None:
    child = WatchedChild(slug="sales", root_pid=100, budget_mb=100)
    headroom = 1.5  # the limit is 150 MB

    inside_headroom = find_offenders(
        [child], headroom_factor=headroom, measure_rss_bytes=fixed_readings({100: 149})
    )
    at_limit = find_offenders(
        [child], headroom_factor=headroom, measure_rss_bytes=fixed_readings({100: 150})
    )
    beyond = find_offenders(
        [child], headroom_factor=headroom, measure_rss_bytes=fixed_readings({100: 151})
    )

    assert inside_headroom == []
    assert at_limit == []
    assert [offence.slug for offence in beyond] == ["sales"]


def test_an_offence_says_which_app_how_much_and_what_limit_for_the_reason_shown() -> None:
    child = WatchedChild(slug="ml", root_pid=300, budget_mb=200)

    (offence,) = find_offenders(
        [child], headroom_factor=1.25, measure_rss_bytes=fixed_readings({300: 400})
    )

    assert offence == Offence(slug="ml", root_pid=300, budget_mb=200, rss_mb=400, limit_mb=250)
    assert offence.reason == "uses 400 MB, over its limit of 250 MB (budget 200 MB)"


def test_only_the_children_over_budget_are_returned_each_against_its_own_budget() -> None:
    children = [
        WatchedChild(slug="small", root_pid=1, budget_mb=64),
        WatchedChild(slug="large", root_pid=2, budget_mb=1024),
        WatchedChild(slug="stopped", root_pid=3, budget_mb=64),
    ]
    readings = {1: 100, 2: 900, 3: 0}  # small is over 64 x 1.25 = 80; large is not over 1280

    offenders = find_offenders(
        children, headroom_factor=1.25, measure_rss_bytes=fixed_readings(readings)
    )

    assert [offence.slug for offence in offenders] == ["small"]


# ---- real processes: the tree is measured, only the offender is stopped --------------------------


def test_the_measured_tree_includes_a_descendant_not_just_the_root(
    start_holder: Callable[[int, int], subprocess.Popen[str]],
) -> None:
    alone = start_holder(60, 0)
    with_descendant = start_holder(60, 1)

    root_only_bytes = process_tree_rss_bytes(alone.pid)
    tree_bytes = process_tree_rss_bytes(with_descendant.pid)

    assert len(process_tree_pids(alone.pid)) == 1
    assert len(process_tree_pids(with_descendant.pid)) == 2
    assert root_only_bytes > 60 * MIB
    assert tree_bytes > 2 * 60 * MIB  # two processes of 60 MB each, plus the interpreters
    assert tree_bytes > root_only_bytes + 50 * MIB


def test_a_tree_over_budget_only_through_its_descendants_is_found_and_a_tidy_one_is_not(
    start_holder: Callable[[int, int], subprocess.Popen[str]],
) -> None:
    budget_mb = 100
    # Each process of the hog holds 60 MB (about 70 with the interpreter): under the 100 MB
    # budget alone, over it as a tree.
    hog = start_holder(60, 1)
    tidy = start_holder(20, 0)
    children = [
        WatchedChild(slug="hog", root_pid=hog.pid, budget_mb=budget_mb),
        WatchedChild(slug="tidy", root_pid=tidy.pid, budget_mb=budget_mb),
    ]

    offenders = find_offenders(children, headroom_factor=1.0)

    assert [offence.slug for offence in offenders] == ["hog"]
    assert offenders[0].rss_mb > budget_mb
    assert process_tree_rss_bytes(hog.pid) > budget_mb * MIB


@pytest.mark.anyio
async def test_the_watch_loop_stops_the_offender_and_no_other_app(
    start_holder: Callable[[int, int], subprocess.Popen[str]],
) -> None:
    budget_mb = 100
    hog = start_holder(60, 1)
    tidy = start_holder(20, 0)
    neighbour = start_holder(60, 0)  # large for its tree, but under its own budget
    watched = [
        WatchedChild(slug="hog", root_pid=hog.pid, budget_mb=budget_mb),
        WatchedChild(slug="tidy", root_pid=tidy.pid, budget_mb=budget_mb),
        WatchedChild(slug="neighbour", root_pid=neighbour.pid, budget_mb=budget_mb),
    ]
    stopped: list[str] = []
    first_stop = asyncio.Event()

    async def stop(offence: Offence) -> None:
        stopped.append(offence.slug)
        kill_tree(offence.root_pid)
        first_stop.set()

    task = asyncio.create_task(
        watch(
            lambda: watched,
            stop,
            interval_s=SHORT_INTERVAL_S,
            headroom_factor=1.0,
        )
    )
    try:
        await asyncio.wait_for(first_stop.wait(), timeout=EVENT_TIMEOUT_S)
        # Give the loop a few more rounds to show it does not go after anyone else.
        for _ in range(5):
            await asyncio.sleep(SHORT_INTERVAL_S)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert stopped == ["hog"]
    assert hog.wait(timeout=READY_TIMEOUT_S) == -signal.SIGKILL
    assert tidy.poll() is None
    assert neighbour.poll() is None


# ---- the watch loop's own rules (fake readings, no processes) ------------------------------------


async def run_rounds(
    watched: list[WatchedChild],
    stop: Callable[[Offence], None],
    readings_mb: dict[int, int],
    rounds: int,
    list_children: Callable[[], list[WatchedChild]] | None = None,
) -> None:
    """Run the loop until the provider has been asked ``rounds`` times, then cancel it."""
    asked = 0
    enough = asyncio.Event()

    def children() -> list[WatchedChild]:
        nonlocal asked
        asked += 1
        if asked >= rounds:
            enough.set()
        return list_children() if list_children is not None else watched

    async def stop_async(offence: Offence) -> None:
        stop(offence)

    task = asyncio.create_task(
        watch(
            children,
            stop_async,
            interval_s=SHORT_INTERVAL_S,
            headroom_factor=1.0,
            measure_rss_bytes=fixed_readings(readings_mb),
        )
    )
    try:
        await asyncio.wait_for(enough.wait(), timeout=EVENT_TIMEOUT_S)
        await asyncio.sleep(SHORT_INTERVAL_S)  # let the round that set the event finish
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_an_offender_that_stays_listed_while_it_stops_is_stopped_once() -> None:
    stops: list[str] = []
    child = WatchedChild(slug="hog", root_pid=7, budget_mb=10)

    await run_rounds([child], lambda offence: stops.append(offence.slug), {7: 500}, rounds=4)

    assert stops == ["hog"]


@pytest.mark.anyio
async def test_a_restarted_app_with_a_new_root_is_watched_afresh() -> None:
    stops: list[tuple[str, int]] = []
    current = [WatchedChild(slug="hog", root_pid=7, budget_mb=10)]
    calls = 0

    def list_children() -> list[WatchedChild]:
        nonlocal calls
        calls += 1
        if calls == 3:
            current[0] = WatchedChild(slug="hog", root_pid=8, budget_mb=10)  # restarted
        return list(current)

    await run_rounds(
        [],
        lambda offence: stops.append((offence.slug, offence.root_pid)),
        {7: 500, 8: 500},
        rounds=6,
        list_children=list_children,
    )

    assert stops == [("hog", 7), ("hog", 8)]


@pytest.mark.anyio
async def test_a_stop_that_fails_is_logged_and_retried_and_the_loop_survives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts: list[str] = []
    child = WatchedChild(slug="hog", root_pid=7, budget_mb=10)

    def flaky_stop(offence: Offence) -> None:
        attempts.append(offence.slug)
        if len(attempts) == 1:
            raise OSError("the launcher could not signal the sandbox")

    with caplog.at_level(logging.ERROR, logger="appmux.sandbox.watchdog"):
        await run_rounds([child], flaky_stop, {7: 500}, rounds=5)

    assert attempts == ["hog", "hog"]  # failed once, retried, succeeded, then left alone
    assert any("hog" in record.getMessage() for record in caplog.records)


@pytest.mark.anyio
async def test_a_stop_that_fails_for_one_app_does_not_spare_the_other_offender() -> None:
    attempts: list[str] = []
    children = [
        WatchedChild(slug="first", root_pid=7, budget_mb=10),
        WatchedChild(slug="second", root_pid=8, budget_mb=10),
    ]

    def stop(offence: Offence) -> None:
        attempts.append(offence.slug)
        if offence.slug == "first":
            raise OSError("the launcher could not signal the sandbox")

    await run_rounds(children, stop, {7: 500, 8: 500}, rounds=4)

    assert attempts.count("second") == 1  # stopped in the first round, then left alone
    assert attempts.count("first") >= 2  # still over its limit, so tried again


@pytest.mark.anyio
async def test_a_failing_reading_does_not_end_the_watch(caplog: pytest.LogCaptureFixture) -> None:
    stops: list[str] = []
    calls = 0

    def children() -> list[WatchedChild]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("the registry snapshot was not ready")
        return [WatchedChild(slug="hog", root_pid=7, budget_mb=10)]

    stopped = asyncio.Event()

    async def stop(offence: Offence) -> None:
        stops.append(offence.slug)
        stopped.set()

    task = asyncio.create_task(
        watch(
            children,
            stop,
            interval_s=SHORT_INTERVAL_S,
            headroom_factor=1.0,
            measure_rss_bytes=fixed_readings({7: 500}),
        )
    )
    try:
        with caplog.at_level(logging.ERROR, logger="appmux.sandbox.watchdog"):
            await asyncio.wait_for(stopped.wait(), timeout=EVENT_TIMEOUT_S)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert stops == ["hog"]
    assert caplog.records, "the failed round should be logged"


# ---- RLIMIT_AS -----------------------------------------------------------------------------------


def test_the_address_space_limit_is_the_budget_times_the_headroom_factor_in_bytes() -> None:
    assert address_space_limit_bytes(100, headroom_factor=2.0) == 200 * MIB
    assert address_space_limit_bytes(512, headroom_factor=1.5) == 768 * MIB


@pytest.mark.parametrize(("budget_mb", "headroom_factor"), [(0, 2.0), (-1, 2.0), (100, 0.99)])
def test_a_budget_or_factor_that_would_not_fit_the_child_is_refused(
    budget_mb: int, headroom_factor: float
) -> None:
    with pytest.raises(ValueError, match=r"budget|headroom"):
        address_space_limit_bytes(budget_mb, headroom_factor=headroom_factor)


def test_applying_the_limit_sets_soft_and_hard_address_space_from_the_budget() -> None:
    calls: list[tuple[int, tuple[int, int]]] = []

    applied = apply_address_space_limit(
        100,
        headroom_factor=1.5,
        setrlimit=lambda resource_id, limits: calls.append((resource_id, limits)),
    )

    # Soft and hard together: a child that can lift its own soft limit has no limit.
    assert calls == [(resource.RLIMIT_AS, (150 * MIB, 150 * MIB))]
    assert applied == 150 * MIB


def test_the_limit_is_enforced_on_a_real_process_started_with_it() -> None:
    budget_mb = 512
    probe = (
        "import sys\n"
        "small = bytearray(64 * 1024 * 1024)\n"
        "try:\n"
        "    bytearray(1024 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    print('refused')\n"
    )

    def limit_like_the_launcher() -> None:
        apply_address_space_limit(budget_mb, headroom_factor=1.0)

    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        timeout=READY_TIMEOUT_S,
        check=False,
        preexec_fn=limit_like_the_launcher,  # the launcher applies the limit the same way
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refused"
