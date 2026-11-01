"""The run-directory size cap and its watchdog fallback (design 6.4 "Disk", TD-004).

The cap itself is a size-limited tmpfs, which only a container run can prove. What runs here is
the fallback: measuring a host directory and stopping the child that filled it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from appmux.sandbox.run_dir import (
    RunDirOffence,
    check_run_dir,
    directory_size_bytes,
    watch_run_dir,
)

pytestmark = pytest.mark.anyio

BLOCK_BYTES = 4096


def write_file(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size_bytes)


def test_an_empty_or_missing_directory_is_zero_bytes(tmp_path: Path) -> None:
    assert directory_size_bytes(tmp_path) == 0
    assert directory_size_bytes(tmp_path / "gone") == 0


def test_the_size_is_the_disk_a_tree_holds_not_the_length_of_its_files(tmp_path: Path) -> None:
    write_file(tmp_path / "a", 10 * BLOCK_BYTES)
    write_file(tmp_path / "deep" / "er" / "b", 5 * BLOCK_BYTES)

    assert directory_size_bytes(tmp_path) >= 15 * BLOCK_BYTES


def test_a_sparse_file_costs_what_it_allocates_not_what_it_claims(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse"
    with sparse.open("wb") as handle:
        handle.truncate(10**9)

    assert directory_size_bytes(tmp_path) < 10**6


def test_a_symlink_is_not_followed_out_of_the_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write_file(outside / "big", 100 * BLOCK_BYTES)
    inside = tmp_path / "run"
    inside.mkdir()
    os.symlink(outside, inside / "link")

    assert directory_size_bytes(inside) < 100 * BLOCK_BYTES


def test_a_file_that_vanishes_while_the_tree_is_walked_is_skipped(tmp_path: Path) -> None:
    write_file(tmp_path / "kept", BLOCK_BYTES)

    def vanishing(entry: os.DirEntry[str]) -> os.stat_result:
        if entry.name == "ghost":
            raise FileNotFoundError(entry.path)
        return entry.stat(follow_symlinks=False)

    write_file(tmp_path / "ghost", BLOCK_BYTES)

    assert directory_size_bytes(tmp_path, stat_entry=vanishing) >= BLOCK_BYTES


def test_check_reports_an_offence_only_beyond_the_limit(tmp_path: Path) -> None:
    write_file(tmp_path / "f", 3 * BLOCK_BYTES)
    size = directory_size_bytes(tmp_path)

    assert check_run_dir(tmp_path, limit_bytes=size) is None
    offence = check_run_dir(tmp_path, limit_bytes=size - 1)
    assert offence == RunDirOffence(path=tmp_path, size_bytes=size, limit_bytes=size - 1)
    assert "limit" in offence.reason


async def test_the_watchdog_stops_the_child_once_the_directory_is_over_the_limit(
    tmp_path: Path,
) -> None:
    sizes = iter([10, 50, 200, 200, 200])
    stopped: list[RunDirOffence] = []

    async def stop(offence: RunDirOffence) -> None:
        stopped.append(offence)

    await asyncio.wait_for(
        watch_run_dir(tmp_path, 100, stop, interval_s=0.001, measure_bytes=lambda _: next(sizes)),
        timeout=5,
    )

    assert [offence.size_bytes for offence in stopped] == [200]


async def test_a_stop_that_fails_is_retried_at_the_next_round(tmp_path: Path) -> None:
    attempts = 0

    async def stop(_: RunDirOffence) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("the supervisor is busy")

    await asyncio.wait_for(
        watch_run_dir(tmp_path, 1, stop, interval_s=0.001, measure_bytes=lambda _: 99),
        timeout=5,
    )

    assert attempts == 3


async def test_a_failed_measurement_does_not_end_the_watch(tmp_path: Path) -> None:
    readings = iter([OSError("disk gone"), 500])

    def measure(_: Path) -> int:
        reading = next(readings)
        if isinstance(reading, OSError):
            raise reading
        return reading

    stopped: list[RunDirOffence] = []

    async def stop(offence: RunDirOffence) -> None:
        stopped.append(offence)

    await asyncio.wait_for(
        watch_run_dir(tmp_path, 100, stop, interval_s=0.001, measure_bytes=measure), timeout=5
    )

    assert len(stopped) == 1
