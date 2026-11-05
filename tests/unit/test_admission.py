"""Admission control (design 5.7, FR-050, T157): the memory formula over plain numbers.

The books are plain parameters (the container limit, the reserve, the running apps and the
clock), so nothing here touches the registry or the machine, except the limit reader, which is
tested against fake cgroup and ``/proc/meminfo`` files.

The worked example of design 5.7: an 8192 MB limit, a 1024 MB reserve, so 7168 MB available;
ten apps of 512 MB plus 64 MB of sandbox overhead each commit 5760 MB (1408 MB free); an update of
``sales`` next to its running release commits 576 MB more (6336 MB, 832 MB free).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from appmux.reconciler.admission import (
    OVERHEAD_PER_CHILD_MB,
    AdmissionDecision,
    MemoryLedger,
    MemoryLimitUnreadableError,
    RunningApp,
    SecondCopy,
    check_admission,
    memory_ledger,
    read_container_memory_limit_mb,
)

LIMIT_MB = 8192
RESERVE_MB = 1024
AVAILABLE_MB = LIMIT_MB - RESERVE_MB
APP_BUDGET_MB = 512
HEALTH_DEADLINE_S = 60
# test_capacity.py on a container, 2026-11-05: 60.7 to 61.1 MB per sandbox (TD-032).
MEASURED_SANDBOX_OVERHEAD_MB = 61.1
NOW_S = 1000.0  # a reading of the monotonic clock; only differences matter
REASON_SHAPE = re.compile(r"needs \d+ MB, \d+ MB free")


def ten_apps() -> tuple[RunningApp, ...]:
    return tuple(RunningApp(slug=f"app{n}", budget_mb=APP_BUDGET_MB) for n in range(10))


def with_sales_update_in_flight(
    started_at_s: float, deadline_s: int = HEALTH_DEADLINE_S
) -> tuple[RunningApp, ...]:
    """The ten apps, the first one named ``sales`` and swapping to a second copy."""
    apps = list(ten_apps())
    apps[0] = RunningApp(
        slug="sales",
        budget_mb=APP_BUDGET_MB,
        second_copy=SecondCopy(
            budget_mb=APP_BUDGET_MB, started_at_s=started_at_s, health_deadline_s=deadline_s
        ),
    )
    return tuple(apps)


def ledger(running: tuple[RunningApp, ...], now_s: float = NOW_S) -> MemoryLedger:
    return memory_ledger(limit_mb=LIMIT_MB, reserve_mb=RESERVE_MB, running=running, now_s=now_s)


def admit(
    running: tuple[RunningApp, ...], budget_mb: int, now_s: float = NOW_S
) -> AdmissionDecision:
    return check_admission(
        limit_mb=LIMIT_MB,
        reserve_mb=RESERVE_MB,
        running=running,
        candidate_budget_mb=budget_mb,
        now_s=now_s,
    )


# ---- the worked example of design 5.7 -----------------------------------------------------------


def test_ten_apps_of_512_commit_5760_and_leave_1408_free() -> None:
    books = ledger(ten_apps())

    # Admission must never count less per sandbox than the capacity run measured.
    assert OVERHEAD_PER_CHILD_MB >= MEASURED_SANDBOX_OVERHEAD_MB
    assert (books.limit_mb, books.reserve_mb) == (LIMIT_MB, RESERVE_MB)
    assert books.committed_mb == 5760
    assert books.free_mb == 1408


def test_apps_are_admitted_one_by_one_until_the_ten_are_running() -> None:
    running: list[RunningApp] = []
    for number in range(10):
        decision = admit(tuple(running), APP_BUDGET_MB)
        assert decision.admitted, f"app {number} should fit: {decision.reason}"
        running.append(RunningApp(slug=f"app{number}", budget_mb=APP_BUDGET_MB))


def test_an_update_beside_its_running_release_adds_576_and_is_allowed() -> None:
    sales_update = admit(ten_apps(), APP_BUDGET_MB)

    assert sales_update.admitted
    assert sales_update.needed_mb == 576
    assert sales_update.reason is None
    books = ledger(with_sales_update_in_flight(started_at_s=NOW_S))
    assert books.committed_mb == 6336
    assert books.free_mb == 832


def test_a_2048_mb_app_is_refused_naming_what_it_needs_and_what_is_free() -> None:
    # Free is 7168 - 6336 = 832, the figure of design 5.7's worked example.
    decision = admit(with_sales_update_in_flight(started_at_s=NOW_S), 2048)

    assert not decision.admitted
    assert decision.needed_mb == 2112
    assert decision.free_mb == 832
    assert decision.reason == "needs 2112 MB, 832 MB free"


def test_the_same_app_beside_the_ten_steady_apps_is_refused_with_their_free_memory() -> None:
    decision = admit(ten_apps(), 2048)

    assert not decision.admitted
    assert decision.reason == "needs 2112 MB, 1408 MB free"


def test_an_app_that_needs_exactly_the_free_memory_fits_and_one_megabyte_more_does_not() -> None:
    free_after_ten = 1408

    fits = admit(ten_apps(), free_after_ten - OVERHEAD_PER_CHILD_MB)
    refused = admit(ten_apps(), free_after_ten - OVERHEAD_PER_CHILD_MB + 1)

    assert fits.admitted
    assert not refused.admitted
    assert refused.reason == "needs 1409 MB, 1408 MB free"


def test_a_refusal_changes_nothing_in_the_books() -> None:
    running = with_sales_update_in_flight(started_at_s=NOW_S)
    before = ledger(running)

    first = admit(running, 2048)
    second = admit(running, 2048)

    assert not first.admitted
    assert second == first
    assert ledger(running) == before
    # What fits still fits after the refusal.
    assert admit(running, 256).admitted


def test_an_over_committed_slot_reports_zero_free_in_the_reason_and_the_debt_in_the_books() -> None:
    # The reserve was raised past what the running apps leave: free is negative in the books
    # (the contract's free_mb is a plain integer) but the reason never says "-300 MB free".
    books = memory_ledger(limit_mb=5500, reserve_mb=1024, running=ten_apps(), now_s=NOW_S)
    decision = check_admission(
        limit_mb=5500,
        reserve_mb=1024,
        running=ten_apps(),
        candidate_budget_mb=64,
        now_s=NOW_S,
    )

    assert books.free_mb == 5500 - 1024 - 5760 < 0
    assert not decision.admitted
    assert decision.reason == "needs 128 MB, 0 MB free"
    assert REASON_SHAPE.fullmatch(decision.reason)


# ---- the second copy of a swap ------------------------------------------------------------------


def test_the_second_copy_counts_until_its_health_deadline_and_not_after() -> None:
    started_at_s = NOW_S
    running = with_sales_update_in_flight(started_at_s)
    swap_committed = 6336
    steady_committed = 5760

    just_before = ledger(running, now_s=started_at_s + HEALTH_DEADLINE_S - 0.5)
    at_deadline = ledger(running, now_s=started_at_s + HEALTH_DEADLINE_S)
    long_after = ledger(running, now_s=started_at_s + 10 * HEALTH_DEADLINE_S)

    assert just_before.committed_mb == swap_committed
    assert at_deadline.committed_mb == steady_committed
    assert long_after.committed_mb == steady_committed


def test_each_app_uses_its_own_health_deadline_for_its_second_copy() -> None:
    quick = RunningApp(
        slug="quick",
        budget_mb=100,
        second_copy=SecondCopy(budget_mb=100, started_at_s=NOW_S, health_deadline_s=5),
    )
    slow = RunningApp(
        slug="slow",
        budget_mb=100,
        second_copy=SecondCopy(budget_mb=100, started_at_s=NOW_S, health_deadline_s=600),
    )
    one_copy_mb = 100 + OVERHEAD_PER_CHILD_MB

    during_both = ledger((quick, slow), now_s=NOW_S + 1)
    after_quick = ledger((quick, slow), now_s=NOW_S + 30)
    after_both = ledger((quick, slow), now_s=NOW_S + 601)

    assert during_both.committed_mb == 4 * one_copy_mb
    assert after_quick.committed_mb == 3 * one_copy_mb
    assert after_both.committed_mb == 2 * one_copy_mb


def test_a_second_copy_with_a_new_budget_counts_its_own_budget() -> None:
    # A settings change takes effect on the next start (data model, settings request): the
    # running release holds the old budget while the new copy holds the new one.
    app = RunningApp(
        slug="docs",
        budget_mb=128,
        second_copy=SecondCopy(budget_mb=256, started_at_s=NOW_S, health_deadline_s=60),
    )

    assert ledger((app,)).committed_mb == (128 + OVERHEAD_PER_CHILD_MB) + (
        256 + OVERHEAD_PER_CHILD_MB
    )


# ---- reading the container's memory limit -------------------------------------------------------

GIB = 1024**3
V1_UNLIMITED_BYTES = 9223372036854771712  # what cgroup v1 reports for "no limit"


class FakeSources:
    """Fake cgroup v2, cgroup v1 and ``/proc/meminfo`` files under one temporary directory."""

    def __init__(self, root: Path) -> None:
        self.v2 = root / "memory.max"
        self.v1 = root / "memory.limit_in_bytes"
        self.meminfo = root / "meminfo"

    def read(self) -> int:
        return read_container_memory_limit_mb(
            cgroup_v2_path=self.v2, cgroup_v1_path=self.v1, meminfo_path=self.meminfo
        )


@pytest.fixture
def sources(tmp_path: Path) -> FakeSources:
    return FakeSources(tmp_path)


def test_cgroup_v2_memory_max_is_the_limit(sources: FakeSources) -> None:
    sources.v2.write_text(f"{8 * GIB}\n")

    assert sources.read() == 8192


def test_cgroup_v1_is_read_when_there_is_no_v2_file(sources: FakeSources) -> None:
    # The Free Edition container measured cgroup v1 (docs/reference/databricks-free-edition.md).
    sources.v1.write_text(f"{8 * GIB}\n")

    assert sources.read() == 8192


def test_v2_wins_over_v1_when_both_state_a_limit(sources: FakeSources) -> None:
    sources.v2.write_text(f"{4 * GIB}\n")
    sources.v1.write_text(f"{8 * GIB}\n")

    assert sources.read() == 4096


@pytest.mark.parametrize(
    ("v2_text", "v1_text"),
    [
        ("max\n", None),  # cgroup v2: no limit
        (None, f"{V1_UNLIMITED_BYTES}\n"),  # cgroup v1: no limit
        ("not a number\n", ""),  # unreadable content is skipped, never guessed
    ],
)
def test_an_unlimited_or_unreadable_cgroup_falls_back_to_meminfo(
    sources: FakeSources, v2_text: str | None, v1_text: str | None
) -> None:
    if v2_text is not None:
        sources.v2.write_text(v2_text)
    if v1_text is not None:
        sources.v1.write_text(v1_text)
    sources.meminfo.write_text(f"MemTotal:       {6 * 1024 * 1024} kB\nMemFree:  1 kB\n")

    assert sources.read() == 6144


def test_the_limit_is_rounded_down_to_whole_megabytes(sources: FakeSources) -> None:
    sources.v2.write_text(str(8 * GIB + 1024 * 1024 - 1))

    assert sources.read() == 8192


def test_no_readable_source_fails_closed_naming_every_place_tried(sources: FakeSources) -> None:
    sources.v2.write_text("max\n")  # present but states no limit; the other two are missing

    with pytest.raises(MemoryLimitUnreadableError) as raised:
        sources.read()

    message = str(raised.value)
    for path in (sources.v2, sources.v1, sources.meminfo):
        assert str(path) in message


def test_this_machine_reports_a_plausible_limit() -> None:
    # The default sources are the real files of the machine running the test.
    assert read_container_memory_limit_mb() > 0
