"""The memory the watchdog adds for a child's tmpfs mounts (TD-004, T219).

tmpfs pages are charged to the container's cgroup but are not in any process's RSS, so a child
could fill its mounts with files and never be stopped by the RSS watchdog. The watchdog now adds
the used bytes of the child's memory-backed mounts (plus a per-file cost for the inodes) to the
figure it compares with ``budget * headroom + allowance``.

Everything here runs against a fake ``/proc`` and an injected ``statvfs``. What only a container
run proves: that ``/proc/<pid>/root/<mount>`` can be read by appmux for a process of a sandbox
(``tests/integration/test_tmpfs_memory_stop.py``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from appmux.sandbox.rootfs import HOME_PATH, SHM_PATH, TMP_PATH
from appmux.sandbox.watchdog import (
    TMPFS_BYTES_PER_FILE,
    Offence,
    WatchedChild,
    find_offenders,
    tmpfs_used_bytes,
)

MIB = 1024 * 1024
BLOCK_BYTES = 4096
UNSHARE_PID = 100
INIT_PID = 101
WORKER_PID = 102


def statvfs_result(*, used_bytes: int, files_in_use: int) -> os.statvfs_result:
    """What ``os.statvfs`` returns for a mount of 1 GiB and 100 000 files that holds this much."""
    total_blocks = 262144
    total_files = 100_000
    return os.statvfs_result(
        (
            BLOCK_BYTES,  # f_bsize
            BLOCK_BYTES,  # f_frsize
            total_blocks,
            total_blocks - used_bytes // BLOCK_BYTES,  # f_bfree
            total_blocks - used_bytes // BLOCK_BYTES,  # f_bavail
            total_files,
            total_files - files_in_use,  # f_ffree
            total_files - files_in_use,  # f_favail
            0,
            255,
        )
    )


def add_process(proc_root: Path, pid: int, parent_pid: int) -> None:
    directory = proc_root / str(pid)
    directory.mkdir()
    (directory / "stat").write_text(f"{pid} (worker) S {parent_pid} {pid} {pid} 0 -1\n")
    (directory / "statm").write_text("9999 10 100 1 0 50 0\n")


@pytest.fixture
def sandbox_proc(tmp_path: Path) -> Path:
    """``unshare`` (100) with its only child, the sandbox init (101), which has a worker (102)."""
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    add_process(proc_root, UNSHARE_PID, 1)
    add_process(proc_root, INIT_PID, UNSHARE_PID)
    add_process(proc_root, WORKER_PID, INIT_PID)
    return proc_root


def statvfs_by_path(
    readings: dict[str, os.statvfs_result], asked: list[str] | None = None
) -> Callable[[str | Path], os.statvfs_result]:
    """A ``statvfs`` that answers by the part of the path after ``/root`` and records what asked."""

    def statvfs(path: str | Path) -> os.statvfs_result:
        text = str(path)
        if asked is not None:
            asked.append(text)
        inside_root = "/" + text.partition("/root/")[2]
        return readings[inside_root]

    return statvfs


# ---- tmpfs_used_bytes ---------------------------------------------------------------------------


def test_the_used_bytes_and_the_files_of_every_mount_are_added(sandbox_proc: Path) -> None:
    readings = {
        TMP_PATH: statvfs_result(used_bytes=10 * MIB, files_in_use=3),
        HOME_PATH: statvfs_result(used_bytes=5 * MIB, files_in_use=2),
    }

    measured = tmpfs_used_bytes(
        UNSHARE_PID,
        (TMP_PATH, HOME_PATH),
        proc_root=sandbox_proc,
        statvfs=statvfs_by_path(readings),
    )

    assert measured == 15 * MIB + 5 * TMPFS_BYTES_PER_FILE


def test_a_mount_is_read_through_a_process_inside_the_sandbox_not_through_unshare(
    sandbox_proc: Path,
) -> None:
    # unshare's own root is not the child's root, so /proc/100/root/tmp would be the wrong tmpfs.
    asked: list[str] = []
    readings = {TMP_PATH: statvfs_result(used_bytes=0, files_in_use=0)}

    tmpfs_used_bytes(
        UNSHARE_PID, (TMP_PATH,), proc_root=sandbox_proc, statvfs=statvfs_by_path(readings, asked)
    )

    assert asked == [str(sandbox_proc / str(INIT_PID) / "root" / "tmp")]


def test_a_sandbox_with_no_process_inside_yet_holds_nothing(sandbox_proc: Path) -> None:
    only_unshare = sandbox_proc.parent / "proc-only-unshare"
    only_unshare.mkdir()
    add_process(only_unshare, UNSHARE_PID, 1)

    measured = tmpfs_used_bytes(
        UNSHARE_PID,
        (TMP_PATH,),
        proc_root=only_unshare,
        statvfs=statvfs_by_path({}),
    )

    assert measured == 0


def test_a_process_that_exited_between_the_walk_and_the_read_is_skipped_for_the_next(
    sandbox_proc: Path,
) -> None:
    readings = {TMP_PATH: statvfs_result(used_bytes=8 * MIB, files_in_use=0)}

    def statvfs(path: str | Path) -> os.statvfs_result:
        if f"/{INIT_PID}/" in str(path):
            raise FileNotFoundError(path)
        return statvfs_by_path(readings)(path)

    measured = tmpfs_used_bytes(UNSHARE_PID, (TMP_PATH,), proc_root=sandbox_proc, statvfs=statvfs)

    assert measured == 8 * MIB


def test_a_mount_no_process_of_the_sandbox_lets_us_read_is_an_error_not_zero(
    sandbox_proc: Path,
) -> None:
    def refused(path: str | Path) -> os.statvfs_result:
        raise PermissionError(13, "Permission denied", str(path))

    with pytest.raises(OSError, match=TMP_PATH):
        tmpfs_used_bytes(UNSHARE_PID, (TMP_PATH,), proc_root=sandbox_proc, statvfs=refused)


# ---- find_offenders ----------------------------------------------------------------------------


def measure_rss_mb(readings_mb: dict[int, int]) -> Callable[[int], int]:
    return lambda root_pid: readings_mb[root_pid] * MIB


def measure_tmpfs_mb(
    readings_mb: dict[int, int],
) -> Callable[[int, tuple[str, ...]], int]:
    return lambda root_pid, _paths: readings_mb[root_pid] * MIB


def test_resident_memory_and_tmpfs_over_the_limit_together_stop_a_child_neither_part_would() -> (
    None
):
    child = WatchedChild(
        slug="filler", root_pid=7, budget_mb=100, allowance_mb=20, tmpfs_paths=(TMP_PATH,)
    )  # the limit is 100 * 1.25 + 20 = 145 MB

    (offence,) = find_offenders(
        [child],
        headroom_factor=1.25,
        measure_rss_bytes=measure_rss_mb({7: 90}),  # below the limit on its own
        measure_tmpfs_bytes=measure_tmpfs_mb({7: 80}),  # below the limit on its own
    )

    assert offence == Offence(
        slug="filler",
        root_pid=7,
        budget_mb=100,
        rss_mb=90,
        limit_mb=145,
        allowance_mb=20,
        tmpfs_mb=80,
    )
    assert offence.reason == (
        "uses 170 MB (90 MB resident, 80 MB in its temporary directories), "
        "over its limit of 145 MB (budget 100 MB, plus 20 MB for the sandbox itself)"
    )


def test_tmpfs_that_fits_with_the_resident_memory_does_not_stop_a_child() -> None:
    child = WatchedChild(slug="tidy", root_pid=7, budget_mb=100, tmpfs_paths=(TMP_PATH,))

    offenders = find_offenders(
        [child],
        headroom_factor=1.25,
        measure_rss_bytes=measure_rss_mb({7: 70}),
        measure_tmpfs_bytes=measure_tmpfs_mb({7: 55}),  # 125 MB in all, exactly the limit
    )

    assert offenders == []


def test_a_child_with_no_memory_backed_mount_is_not_asked_for_tmpfs() -> None:
    child = WatchedChild(slug="plain", root_pid=7, budget_mb=100)

    def must_not_be_called(_root_pid: int, _paths: tuple[str, ...]) -> int:
        raise AssertionError("tmpfs was measured for a child that lists no mount")

    offenders = find_offenders(
        [child],
        headroom_factor=1.25,
        measure_rss_bytes=measure_rss_mb({7: 200}),
        measure_tmpfs_bytes=must_not_be_called,
    )

    assert [offence.reason for offence in offenders] == [
        "uses 200 MB, over its limit of 125 MB (budget 100 MB)"
    ]


def test_the_mounts_asked_about_are_the_ones_the_child_lists() -> None:
    child = WatchedChild(slug="two", root_pid=7, budget_mb=100, tmpfs_paths=(TMP_PATH, SHM_PATH))
    asked: list[tuple[int, tuple[str, ...]]] = []

    def record(root_pid: int, paths: tuple[str, ...]) -> int:
        asked.append((root_pid, paths))
        return 0

    find_offenders(
        [child],
        headroom_factor=1.25,
        measure_rss_bytes=measure_rss_mb({7: 1}),
        measure_tmpfs_bytes=record,
    )

    assert asked == [(7, (TMP_PATH, SHM_PATH))]


def test_a_child_whose_tmpfs_cannot_be_read_is_still_judged_on_its_resident_memory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unreadable = WatchedChild(slug="locked", root_pid=7, budget_mb=100, tmpfs_paths=(TMP_PATH,))
    other = WatchedChild(slug="filler", root_pid=8, budget_mb=100, tmpfs_paths=(TMP_PATH,))

    def tmpfs(root_pid: int, _paths: tuple[str, ...]) -> int:
        if root_pid == unreadable.root_pid:
            raise PermissionError(13, "Permission denied", "/proc/101/root/tmp")
        return 100 * MIB

    with caplog.at_level(logging.WARNING, logger="appmux.sandbox.watchdog"):
        offenders = find_offenders(
            [unreadable, other],
            headroom_factor=1.25,
            measure_rss_bytes=measure_rss_mb({7: 50, 8: 50}),
            measure_tmpfs_bytes=tmpfs,
        )

    # "locked" is under its limit on RSS alone; "filler" is not blinded by the other's failure.
    assert [offence.slug for offence in offenders] == ["filler"]
    assert any(
        "locked" in record.getMessage() and TMP_PATH in record.getMessage()
        for record in caplog.records
    ), "the unreadable tmpfs of 'locked' was not reported"


def test_the_same_unreadable_child_is_reported_once_across_rounds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    child = WatchedChild(slug="locked", root_pid=7, budget_mb=100, tmpfs_paths=(TMP_PATH,))
    already_reported: set[tuple[str, int]] = set()

    def refused(_root_pid: int, _paths: tuple[str, ...]) -> int:
        raise PermissionError(13, "Permission denied", "/proc/101/root/tmp")

    with caplog.at_level(logging.WARNING, logger="appmux.sandbox.watchdog"):
        for _round in range(3):
            find_offenders(
                [child],
                headroom_factor=1.25,
                measure_rss_bytes=measure_rss_mb({7: 1}),
                measure_tmpfs_bytes=refused,
                unreadable_reported=already_reported,
            )

    assert already_reported == {("locked", 7)}
    assert len([record for record in caplog.records if "locked" in record.getMessage()]) == 1
