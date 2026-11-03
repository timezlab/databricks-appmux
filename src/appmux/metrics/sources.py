"""Where the sampled figures come from: the container's cgroup files and ``/proc`` (T192).

``MetricsService`` samples what nobody reports on their own; these readers are what it samples.
Each is a plain function over ``root``, the root of the file system (``/`` in the container, a
temporary directory in the tests), and answers ``None`` when the files do not state the figure: a
missing file, garbage, or "no limit". Nothing here guesses a number, so a ``_platform`` minute with
no figure is left out (the table refuses one without ``mem_used_mb``) rather than stored wrong.

* Memory in use: cgroup v2 ``memory.current``, else cgroup v1 ``memory.usage_in_bytes`` (the Free
  Edition container is v1, ``docs/reference/databricks-free-edition.md``). Both count the page
  cache. The host's ``/proc/meminfo`` is not a fallback: it says nothing of this container's use.
* Memory limit: the reader admission control uses (v2, v1, then ``/proc/meminfo``), so the
  dashboard and the admission books agree on the size of the slot.
* The edge's own resident memory: ``/proc/self/statm``.
* A child's resident memory: the sum over the process tree under its sandbox's host pid, read the
  way the memory watchdog reads it (``sandbox.watchdog``). Shared pages count once per process.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from appmux.metrics.service import PlatformSample
from appmux.reconciler.admission import (
    MemoryLimitUnreadableError,
    read_cgroup_bytes,
    read_container_memory_limit_mb,
)
from appmux.sandbox.watchdog import process_tree_rss_bytes

ROOT: Final = Path("/")

_BYTES_PER_MB: Final = 1024 * 1024
_CGROUP: Final = Path("sys/fs/cgroup")
_V2_USAGE: Final = _CGROUP / "memory.current"
_V2_LIMIT: Final = _CGROUP / "memory.max"
_V1_USAGE: Final = _CGROUP / "memory" / "memory.usage_in_bytes"
_V1_LIMIT: Final = _CGROUP / "memory" / "memory.limit_in_bytes"
_PROC: Final = Path("proc")
_MEMINFO: Final = _PROC / "meminfo"
_SELF_STATM: Final = _PROC / "self" / "statm"
_STATM_RESIDENT_FIELD: Final = 1


def read_memory_used_mb(root: Path = ROOT) -> int | None:
    """The container's memory in use, in whole megabytes, from its cgroup (v2, then v1)."""
    for relative in (_V2_USAGE, _V1_USAGE):
        used_bytes = read_cgroup_bytes(root / relative)
        if used_bytes is not None:
            return used_bytes // _BYTES_PER_MB
    return None


def read_memory_limit_mb(root: Path = ROOT) -> int | None:
    """The container's memory limit in whole megabytes, as admission control reads it."""
    try:
        return read_container_memory_limit_mb(
            cgroup_v2_path=root / _V2_LIMIT,
            cgroup_v1_path=root / _V1_LIMIT,
            meminfo_path=root / _MEMINFO,
        )
    except MemoryLimitUnreadableError:
        return None  # no source states a size: no figure, not a guess


def rss_mb_from_statm(text: str, *, page_size_bytes: int) -> int | None:
    """Resident megabytes from the text of ``/proc/<pid>/statm`` (its second field, in pages)."""
    fields = text.split()
    if len(fields) <= _STATM_RESIDENT_FIELD:
        return None
    try:
        pages = int(fields[_STATM_RESIDENT_FIELD])
    except ValueError:
        return None
    return pages * page_size_bytes // _BYTES_PER_MB


def read_edge_rss_mb(root: Path = ROOT) -> int | None:
    """The resident memory of this process (the edge and everything else appmux runs in it)."""
    try:
        text = (root / _SELF_STATM).read_text(encoding="ascii")
        page_size_bytes = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None
    return rss_mb_from_statm(text, page_size_bytes=page_size_bytes)


def read_process_tree_rss_mb(root_pid: int | None, root: Path = ROOT) -> int | None:
    """The resident memory of ``root_pid`` and its descendants, or ``None`` with no figure.

    ``None`` when the sandbox has not said its pid yet, when that process is gone, and when
    ``/proc`` cannot be listed.
    """
    if root_pid is None:
        return None
    try:
        resident_bytes = process_tree_rss_bytes(root_pid, proc_root=root / _PROC)
    except OSError:
        return None
    # Zero is what the reader answers for a root that is gone; a live process holds some memory.
    return resident_bytes // _BYTES_PER_MB if resident_bytes > 0 else None


def read_platform_sample(
    *, children_running: int, children_registered: int, root: Path = ROOT
) -> PlatformSample | None:
    """The ``_platform`` figures now, or ``None`` when a file states no memory figure.

    The two counts come from the caller (the reconciler and the registry); the rest from files.
    """
    used_mb = read_memory_used_mb(root)
    limit_mb = read_memory_limit_mb(root)
    edge_rss_mb = read_edge_rss_mb(root)
    if used_mb is None or limit_mb is None or edge_rss_mb is None:
        return None
    return PlatformSample(
        mem_used_mb=used_mb,
        mem_limit_mb=limit_mb,
        children_running=children_running,
        children_registered=children_registered,
        edge_rss_mb=edge_rss_mb,
    )
