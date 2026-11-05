"""The memory layers that sit under admission control (design 5.7, FR-050, TD-003).

The container denies cgroup and ``oom_score_adj`` writes, so a child has no hard cap. Two
best-effort layers stand in for one:

* ``RLIMIT_AS`` (:func:`apply_address_space_limit`), set by the launcher just before ``exec``:
  the kernel refuses a process's allocations beyond the budget times a headroom factor. It
  bounds each process, not the tree, and counts address space, not resident memory.
* an RSS watchdog (:func:`watch`) that sums the resident memory of each child's process tree and
  stops the one child that is beyond its budget times a headroom factor, plus an allowance for
  the sandbox's own processes (they sit in the same tree and do not grow with the budget). It
  polls, so a fast allocator can still reach the container's limit between two readings: the
  known gap in TD-003, stated, not hidden. ``Service.start_children`` starts it (TD-029).

Both factors are untuned defaults. They are the plan's to set once SC-003 measures the fixtures
(``memory_fastapi``, ``memory_hog``) inside a sandbox; the figures below are only the reasoning
for a first value.

The watchdog reads ``/proc`` from outside the sandbox: a child's processes are visible to appmux
by their host pids even though the child's own pid namespace hides them from each other. RSS is
summed per process, so a page shared by several processes (a forked worker, a mapped library) is
counted once per process; that over-counts, which is why the stop threshold has headroom.

Stopping is the caller's job: :func:`watch` takes a ``stop`` callback so the reconciler can end
the sandbox through the launcher and record why. This module imports nothing from them
(design 3.4: ``sandbox`` does not import ``reconciler``).
"""

from __future__ import annotations

import asyncio
import logging
import resource
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# A tree may use 25 % more than its budget before it is stopped: a short peak is not a leak, and
# summing RSS over several processes counts shared pages more than once.
RSS_HEADROOM_FACTOR: Final = 1.25
# Virtual size is routinely a multiple of resident size (thread arenas, mapped libraries, stacks):
# an idle uvicorn + Starlette process on the development machine was 115 MB of address space for
# 31 MB resident. A budget near the 64 MB minimum of the data model may need a larger factor.
ADDRESS_SPACE_HEADROOM_FACTOR: Final = 2.0
# One second keeps the cost of scanning /proc small and bounds how far a steady allocator gets.
CHECK_INTERVAL_S: Final = 1.0

PROC_ROOT: Final = Path("/proc")
_BYTES_PER_MB: Final = 1024 * 1024
_PAGE_SIZE_BYTES: Final = resource.getpagesize()


@dataclass(frozen=True, slots=True)
class WatchedChild:
    """One running child: the host pid at the root of its process tree and its budget.

    ``allowance_mb`` is what the tree holds besides the app: the sandbox's own processes (the
    egress pump, ``slirp4netns``, the supervisor). It is added to the limit after the headroom,
    because it does not grow with the budget; leaving it out would stop a small app that stays
    inside its budget.
    """

    slug: str
    root_pid: int
    budget_mb: int
    allowance_mb: int = 0


@dataclass(frozen=True, slots=True)
class Offence:
    """A child whose process tree holds more than its budget times the headroom, plus allowance."""

    slug: str
    root_pid: int
    budget_mb: int
    rss_mb: int
    limit_mb: int
    allowance_mb: int = 0

    @property
    def reason(self) -> str:
        budget = f"budget {self.budget_mb} MB"
        if self.allowance_mb:
            budget += f", plus {self.allowance_mb} MB for the sandbox itself"
        return f"uses {self.rss_mb} MB, over its limit of {self.limit_mb} MB ({budget})"


# ---- reading /proc ------------------------------------------------------------------------------


def process_tree_pids(root_pid: int, *, proc_root: Path = PROC_ROOT) -> list[int]:
    """``root_pid`` and every descendant, or an empty list when the root no longer exists."""
    children_of: dict[int, list[int]] = {}
    root_exists = False
    for pid in _listed_pids(proc_root):
        parent_pid = _parent_pid(proc_root, pid)
        if parent_pid is None:
            continue  # exited while the table was read
        root_exists = root_exists or pid == root_pid
        children_of.setdefault(parent_pid, []).append(pid)
    if not root_exists:
        return []
    tree = [root_pid]
    for pid in tree:  # the list grows while it is walked: a breadth-first walk
        tree.extend(children_of.get(pid, ()))
    return tree


def process_tree_rss_bytes(root_pid: int, *, proc_root: Path = PROC_ROOT) -> int:
    """Resident memory of ``root_pid`` and its descendants, in bytes; 0 when the root is gone."""
    total_bytes = 0
    for pid in process_tree_pids(root_pid, proc_root=proc_root):
        resident_pages = _resident_pages(proc_root, pid)
        if resident_pages is not None:
            total_bytes += resident_pages * _PAGE_SIZE_BYTES
    return total_bytes


def _listed_pids(proc_root: Path) -> list[int]:
    try:
        return [
            int(name) for name in (entry.name for entry in proc_root.iterdir()) if name.isdecimal()
        ]
    except OSError as exc:
        # Without /proc nothing can be measured; saying "no processes" would read as "all fine".
        raise OSError(f"cannot list {proc_root}; the memory watchdog needs a Linux /proc") from exc


def _parent_pid(proc_root: Path, pid: int) -> int | None:
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    # "pid (name) state ppid ...": the name can hold spaces and parentheses, so cut at the last ")".
    fields_after_name = stat.rpartition(")")[2].split()
    if len(fields_after_name) < 2 or not fields_after_name[1].isdecimal():
        return None
    return int(fields_after_name[1])


def _resident_pages(proc_root: Path, pid: int) -> int | None:
    try:
        fields = (proc_root / str(pid) / "statm").read_text(encoding="ascii").split()
    except (OSError, UnicodeDecodeError):
        return None  # the process exited between the tree walk and this read
    if len(fields) < 2 or not fields[1].isdecimal():
        return None
    return int(fields[1])


# ---- who is over budget -------------------------------------------------------------------------


def find_offenders(
    children: Iterable[WatchedChild],
    *,
    headroom_factor: float = RSS_HEADROOM_FACTOR,
    measure_rss_bytes: Callable[[int], int] = process_tree_rss_bytes,
) -> list[Offence]:
    """The children whose tree holds more than ``budget_mb * headroom_factor + allowance_mb``."""
    offenders: list[Offence] = []
    for child in children:
        limit_bytes = int((child.budget_mb * headroom_factor + child.allowance_mb) * _BYTES_PER_MB)
        rss_bytes = measure_rss_bytes(child.root_pid)
        if rss_bytes > limit_bytes:
            offenders.append(
                Offence(
                    slug=child.slug,
                    root_pid=child.root_pid,
                    budget_mb=child.budget_mb,
                    rss_mb=rss_bytes // _BYTES_PER_MB,
                    limit_mb=limit_bytes // _BYTES_PER_MB,
                    allowance_mb=child.allowance_mb,
                )
            )
    return offenders


async def watch(
    list_children: Callable[[], Iterable[WatchedChild]],
    stop: Callable[[Offence], Awaitable[None]],
    *,
    interval_s: float = CHECK_INTERVAL_S,
    headroom_factor: float = RSS_HEADROOM_FACTOR,
    measure_rss_bytes: Callable[[int], int] = process_tree_rss_bytes,
) -> None:
    """Check every ``interval_s`` and ``await stop(offence)`` for each child over its limit.

    Runs until cancelled. ``list_children`` returns the children running now, so a stopped child
    that left the list is no longer measured. A child that is stopped is not stopped again while
    it is still listed under the same root pid, but a restart (a new root pid) is watched afresh,
    and a ``stop`` that raises is retried at the next round.
    """
    stopped: set[tuple[str, int]] = set()
    while True:
        try:
            children = list(list_children())
            offenders = await asyncio.to_thread(
                find_offenders,
                children,
                headroom_factor=headroom_factor,
                measure_rss_bytes=measure_rss_bytes,
            )
        except Exception:
            # A background loop: one failed reading (an unreadable /proc, a provider that was not
            # ready) must not end the watch; a watchdog that quietly stopped is worse than a noisy
            # one. The next round reads again.
            logger.exception("memory watchdog could not read the children's memory; will retry")
        else:
            for offence in offenders:
                key = (offence.slug, offence.root_pid)
                if key not in stopped and await _try_stop(stop, offence):
                    stopped.add(key)
            # Forget children that left the list, so the set cannot grow without bound.
            stopped.intersection_update((child.slug, child.root_pid) for child in children)
        await asyncio.sleep(interval_s)


async def _try_stop(stop: Callable[[Offence], Awaitable[None]], offence: Offence) -> bool:
    """Stop one offender; a failure is logged and leaves the other offenders of the round alone."""
    logger.warning("stopping %s: %s", offence.slug, offence.reason)
    try:
        await stop(offence)
    except Exception:
        # Same boundary as the loop: a stop that raised for one app must not spare the others, and
        # the app stays over its limit, so the next round tries it again.
        logger.exception("could not stop %s (%s); will retry", offence.slug, offence.reason)
        return False
    return True


# ---- RLIMIT_AS ----------------------------------------------------------------------------------


def address_space_limit_bytes(
    budget_mb: int, *, headroom_factor: float = ADDRESS_SPACE_HEADROOM_FACTOR
) -> int:
    """The ``RLIMIT_AS`` value for a budget: ``budget_mb * headroom_factor`` megabytes, in bytes."""
    if budget_mb < 1:
        raise ValueError(f"memory budget must be at least 1 MB, got {budget_mb}")
    if headroom_factor < 1:
        raise ValueError(f"headroom factor must be at least 1, got {headroom_factor}")
    return int(budget_mb * headroom_factor * _BYTES_PER_MB)


def apply_address_space_limit(
    budget_mb: int,
    *,
    headroom_factor: float = ADDRESS_SPACE_HEADROOM_FACTOR,
    setrlimit: Callable[[int, tuple[int, int]], None] = resource.setrlimit,
) -> int:
    """Set the soft and hard ``RLIMIT_AS`` of this process from the budget; return the bytes.

    The launcher calls this in the process that is about to ``exec`` the child, after its own
    setup, because the limit applies to the caller too and is inherited by everything it starts.
    Soft and hard are set together so the child cannot lift its own limit.
    """
    limit_bytes = address_space_limit_bytes(budget_mb, headroom_factor=headroom_factor)
    setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    return limit_bytes
