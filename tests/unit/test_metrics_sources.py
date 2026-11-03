"""Where the sampled figures come from (T192, TD-016 (1)): the container's cgroup and ``/proc``.

Every reader takes the root of the file system as an argument, so these tests lay out fake
``sys/fs/cgroup`` and ``proc`` trees under a temporary directory. A figure the files do not state
(a missing file, garbage, "no limit") is ``None``: the ``_platform`` row is left out rather than
filled with an invented number.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from appmux.metrics.service import PlatformSample
from appmux.metrics.sources import (
    read_edge_rss_mb,
    read_memory_limit_mb,
    read_memory_used_mb,
    read_platform_sample,
    read_process_tree_rss_mb,
    rss_mb_from_statm,
)

MB: Final = 1024 * 1024
PAGE_SIZE: Final = os.sysconf("SC_PAGE_SIZE")
# cgroup v1's "no limit": the largest page-aligned signed 64-bit value.
V1_UNLIMITED: Final = 9223372036854771712


def put(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def cgroup_v2(root: Path, *, current_bytes: int | str, max_bytes: int | str) -> None:
    put(root, "sys/fs/cgroup/memory.current", f"{current_bytes}\n")
    put(root, "sys/fs/cgroup/memory.max", f"{max_bytes}\n")


def cgroup_v1(root: Path, *, usage_bytes: int | str, limit_bytes: int | str) -> None:
    put(root, "sys/fs/cgroup/memory/memory.usage_in_bytes", f"{usage_bytes}\n")
    put(root, "sys/fs/cgroup/memory/memory.limit_in_bytes", f"{limit_bytes}\n")


def meminfo(root: Path, *, total_kb: int) -> None:
    put(root, "proc/meminfo", f"MemTotal:       {total_kb} kB\nMemFree:         1000 kB\n")


def edge_statm(root: Path, *, resident_pages: int) -> None:
    put(root, "proc/self/statm", f"90000 {resident_pages} 3000 100 0 40000 0\n")


def process(root: Path, pid: int, parent_pid: int, resident_pages: int | None) -> None:
    put(root, f"proc/{pid}/stat", f"{pid} (worker) S {parent_pid} {pid} {pid} 0 -1\n")
    if resident_pages is not None:
        put(root, f"proc/{pid}/statm", f"9999 {resident_pages} 100 1 0 50 0\n")


def pages(megabytes: int) -> int:
    return megabytes * MB // PAGE_SIZE


# --- the container's memory in use ----------------------------------------------------------------


def test_cgroup_v2_states_the_memory_in_use(tmp_path: Path) -> None:
    cgroup_v2(tmp_path, current_bytes=2100 * MB, max_bytes=6144 * MB)

    assert read_memory_used_mb(tmp_path) == 2100


def test_cgroup_v1_states_the_memory_in_use_where_v2_is_absent(tmp_path: Path) -> None:
    # The Free Edition container measured cgroup v1 (design 5.7), so v1 is a real source, not "no
    # figure".
    cgroup_v1(tmp_path, usage_bytes=900 * MB + 5, limit_bytes=8192 * MB)

    assert read_memory_used_mb(tmp_path) == 900  # rounded down


def test_v2_wins_when_both_versions_answer(tmp_path: Path) -> None:
    cgroup_v2(tmp_path, current_bytes=100 * MB, max_bytes="max")
    cgroup_v1(tmp_path, usage_bytes=999 * MB, limit_bytes=8192 * MB)

    assert read_memory_used_mb(tmp_path) == 100


def test_no_cgroup_file_means_no_memory_figure(tmp_path: Path) -> None:
    meminfo(tmp_path, total_kb=16_000_000)  # the host's total says nothing of what is in use

    assert read_memory_used_mb(tmp_path) is None


@pytest.mark.parametrize("text", ["", "lots", "-5", "0"])
def test_a_usage_file_that_states_no_figure_is_none(tmp_path: Path, text: str) -> None:
    put(tmp_path, "sys/fs/cgroup/memory.current", text)

    assert read_memory_used_mb(tmp_path) is None


# --- the container's memory limit -----------------------------------------------------------------


def test_cgroup_v2_states_the_limit(tmp_path: Path) -> None:
    cgroup_v2(tmp_path, current_bytes=MB, max_bytes=6144 * MB)

    assert read_memory_limit_mb(tmp_path) == 6144


def test_cgroup_v1_states_the_limit_where_v2_is_absent(tmp_path: Path) -> None:
    cgroup_v1(tmp_path, usage_bytes=MB, limit_bytes=8192 * MB)

    assert read_memory_limit_mb(tmp_path) == 8192


@pytest.mark.parametrize(
    "lay_out",
    [
        pytest.param(lambda root: cgroup_v2(root, current_bytes=MB, max_bytes="max"), id="v2-max"),
        pytest.param(
            lambda root: cgroup_v1(root, usage_bytes=MB, limit_bytes=V1_UNLIMITED), id="v1-huge"
        ),
        pytest.param(lambda root: None, id="no-cgroup"),
    ],
)
def test_no_limit_and_no_meminfo_is_no_limit_figure(
    tmp_path: Path, lay_out: Callable[[Path], None]
) -> None:
    lay_out(tmp_path)

    assert read_memory_limit_mb(tmp_path) is None


def test_an_unlimited_cgroup_falls_back_to_meminfo_as_admission_control_does(
    tmp_path: Path,
) -> None:
    cgroup_v2(tmp_path, current_bytes=MB, max_bytes="max")
    meminfo(tmp_path, total_kb=8 * 1024 * 1024)

    assert read_memory_limit_mb(tmp_path) == 8192


# --- the edge's own memory ------------------------------------------------------------------------


def test_the_edge_reads_its_own_resident_memory(tmp_path: Path) -> None:
    edge_statm(tmp_path, resident_pages=pages(180))

    assert read_edge_rss_mb(tmp_path) == 180


def test_the_edge_without_a_readable_statm_has_no_figure(tmp_path: Path) -> None:
    assert read_edge_rss_mb(tmp_path) is None
    put(tmp_path, "proc/self/statm", "1 two 3")
    assert read_edge_rss_mb(tmp_path) is None


def test_the_resident_pages_of_statm_become_megabytes() -> None:
    # statm: size resident shared text lib data dt, all in pages
    assert rss_mb_from_statm("100000 51200 3000 100 0 40000 0", page_size_bytes=4096) == 200


def test_a_statm_that_cannot_be_read_is_not_a_figure() -> None:
    assert rss_mb_from_statm("", page_size_bytes=4096) is None
    assert rss_mb_from_statm("1 two 3", page_size_bytes=4096) is None


def test_the_resident_memory_of_this_very_process_is_read_from_the_real_root() -> None:
    measured = read_edge_rss_mb()

    assert measured is not None
    assert measured > 0


# --- one child's process tree ---------------------------------------------------------------------


def test_a_childs_memory_is_the_sum_over_its_process_tree(tmp_path: Path) -> None:
    process(tmp_path, 100, 1, pages(10))  # the sandbox's root (unshare)
    process(tmp_path, 101, 100, pages(150))  # the child
    process(tmp_path, 102, 101, pages(40))  # a worker the child forked
    process(tmp_path, 200, 1, pages(999))  # another sandbox

    assert read_process_tree_rss_mb(100, tmp_path) == 200


def test_a_process_that_exits_mid_read_counts_for_nothing(tmp_path: Path) -> None:
    process(tmp_path, 100, 1, pages(10))
    process(tmp_path, 101, 100, None)  # listed with a stat, its statm already gone

    assert read_process_tree_rss_mb(100, tmp_path) == 10


def test_a_tree_whose_root_is_gone_has_no_figure(tmp_path: Path) -> None:
    process(tmp_path, 200, 1, pages(999))

    assert read_process_tree_rss_mb(100, tmp_path) is None


def test_a_sandbox_that_has_not_reported_its_pid_has_no_figure(tmp_path: Path) -> None:
    assert read_process_tree_rss_mb(None, tmp_path) is None


def test_a_proc_that_cannot_be_listed_has_no_figure(tmp_path: Path) -> None:
    assert read_process_tree_rss_mb(100, tmp_path / "nowhere") is None


# --- the platform sample --------------------------------------------------------------------------


def test_the_platform_sample_holds_the_files_figures_and_the_counts_it_is_given(
    tmp_path: Path,
) -> None:
    cgroup_v1(tmp_path, usage_bytes=2100 * MB, limit_bytes=8192 * MB)
    edge_statm(tmp_path, resident_pages=pages(180))

    sample = read_platform_sample(children_running=2, children_registered=3, root=tmp_path)

    assert sample == PlatformSample(
        mem_used_mb=2100,
        mem_limit_mb=8192,
        children_running=2,
        children_registered=3,
        edge_rss_mb=180,
    )


@pytest.mark.parametrize("missing", ["usage", "limit", "edge"])
def test_a_platform_sample_missing_a_figure_is_none(tmp_path: Path, missing: str) -> None:
    put(tmp_path, "sys/fs/cgroup/memory.current", "0" if missing == "usage" else str(MB))
    put(tmp_path, "sys/fs/cgroup/memory.max", "max" if missing == "limit" else str(4096 * MB))
    if missing != "edge":
        edge_statm(tmp_path, resident_pages=pages(50))

    assert read_platform_sample(children_running=0, children_registered=0, root=tmp_path) is None
