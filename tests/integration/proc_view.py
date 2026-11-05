"""Reading ``/proc`` of the container: which process belongs to which app, and what it holds.

Shared by the integration tests that look at the processes appmux runs (``test_capacity.py``,
``test_failure_table.py``). It needs a Linux ``/proc`` and the same user as the processes it reads,
which is how the container runner (``scripts/run-container-tests.sh``) runs the tests; it reads
nothing else and changes nothing.

A child app runs in a sandbox: a supervisor that is a direct child of the edge (the appmux
process), and below it ``slirp4netns``, the relay and the app's own processes. The app's processes
are the ones whose environment names the slug (``DATABRICKS_APP_NAME``), which is how a slug is
found without its name appearing on any command line.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from appmux.sandbox.watchdog import process_tree_pids

PROC: Final = Path("/proc")
BYTES_PER_MB: Final = 1024 * 1024
PAGE_SIZE_BYTES: Final = os.sysconf("SC_PAGE_SIZE")
CLOCK_TICKS_PER_S: Final = os.sysconf("SC_CLK_TCK")
SLIRP_COMMAND: Final = "slirp4netns"


def stat_fields(pid: int) -> list[str] | None:
    """The fields of ``/proc/<pid>/stat`` after the command name (index 0 is field 3, the state)."""
    try:
        text = (PROC / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None  # the process exited
    return text.rpartition(")")[2].split()


def parent_of(pid: int) -> int | None:
    fields = stat_fields(pid)
    return int(fields[1]) if fields and fields[1].isdecimal() else None


def cpu_ticks(pid: int) -> int | None:
    """User plus system time of one process, in clock ticks (fields 14 and 15 of the stat line)."""
    fields = stat_fields(pid)
    if not fields or len(fields) < 13:
        return None
    return int(fields[11]) + int(fields[12])


def resident_bytes(pid: int) -> int:
    try:
        resident_pages = int((PROC / str(pid) / "statm").read_text(encoding="ascii").split()[1])
    except (OSError, IndexError, ValueError):
        return 0
    return resident_pages * PAGE_SIZE_BYTES


def app_name_of(pid: int) -> str | None:
    """The ``DATABRICKS_APP_NAME`` appmux gave the process (a child app's), else ``None``."""
    try:
        raw = (PROC / str(pid) / "environ").read_bytes()
    except OSError:
        return None  # not readable: the caller reports an app it could not find
    for entry in raw.split(b"\0"):
        if entry.startswith(b"DATABRICKS_APP_NAME="):
            return entry.partition(b"=")[2].decode("utf-8", errors="replace")
    return None


def command_name_of(pid: int) -> str | None:
    """The short command name of a process (``comm``), else ``None`` if it is gone."""
    try:
        return (PROC / str(pid) / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def top_below(pid: int, parents: dict[int, int | None], edge_pid: int) -> int:
    """The ancestor of ``pid`` (or ``pid`` itself) that is a direct child of the edge."""
    step = pid
    while True:
        parent = parents.get(step)
        if parent is None or parent == edge_pid:
            return step
        step = parent


def to_mb(size_bytes: float) -> float:
    return round(size_bytes / BYTES_PER_MB, 1)


def processes_of_apps(edge_pid: int, slugs: set[str]) -> dict[str, list[int]]:
    """For each slug, the pids below ``edge_pid`` whose environment names it; absent if none."""
    found: dict[str, list[int]] = {}
    for pid in process_tree_pids(edge_pid):
        name = app_name_of(pid)
        if name in slugs:
            found.setdefault(name, []).append(pid)
    return found


def supervisor_of(edge_pid: int, slug: str) -> int | None:
    """The supervisor of the sandbox that runs ``slug``: the direct child of the edge above it."""
    app_pids = processes_of_apps(edge_pid, {slug}).get(slug)
    if not app_pids:
        return None
    parents = {pid: parent_of(pid) for pid in process_tree_pids(edge_pid)}
    return top_below(app_pids[0], parents, edge_pid)


def processes_named(root_pid: int, command: str) -> list[int]:
    """The pids at or below ``root_pid`` whose command name is ``command``."""
    return [pid for pid in process_tree_pids(root_pid) if command_name_of(pid) == command]
