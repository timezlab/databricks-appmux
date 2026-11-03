"""Admission control: does a start or an update fit the container's memory (design 5.7, FR-050)?

Pure arithmetic over plain numbers. The caller (``update.py``, ``activate.py``, the start path)
passes the books: the container limit it read at start, the ``memory_reserve_mb`` setting, what
runs now and a reading of the monotonic clock. Nothing here touches the registry, the process
table or a global; asking twice with the same books gives the same answer, and a refusal leaves
every running release alone because it only returns a decision.

The formula, in megabytes (1 MB here is 1 MiB, as in the design's "8192 MB" for 8 GiB)::

    available = limit - reserve
    committed = sum(budget + OVERHEAD_PER_CHILD_MB) over every running copy
    fits      = candidate budget + OVERHEAD_PER_CHILD_MB <= available - committed

The running copy of an app being updated stays in ``committed`` while the new copy is checked,
which is the update overlap. A copy that is still waiting for its first healthy answer counts as
a second copy for at most its app's ``health_deadline_s`` (design 5.5): by then it has either
become the running release or failed and been stopped.

The limit reader tries cgroup v2, then cgroup v1 (the Free Edition container measured v1), then
``/proc/meminfo``, and raises when none gives a figure, so admission fails closed instead of
guessing a size.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Measured in design 5.7: the egress pump (10 MB) plus slirp4netns (3 MB) that every child has,
# on top of its budget. SC-003 replaces this with the real per-child figure.
OVERHEAD_PER_CHILD_MB: Final = 13

_BYTES_PER_MB: Final = 1024 * 1024
_BYTES_PER_KB: Final = 1024
# cgroup v1 reports "no limit" as a page-aligned value just under 2**63; anything this large is
# not a real container size (it is far beyond any machine's memory).
_UNLIMITED_BYTES_FLOOR: Final = 2**60

CGROUP_V2_LIMIT_PATH: Final = Path("/sys/fs/cgroup/memory.max")
CGROUP_V1_LIMIT_PATH: Final = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
MEMINFO_PATH: Final = Path("/proc/meminfo")


class MemoryLimitUnreadableError(RuntimeError):
    """No source states the container's memory limit; admission cannot decide anything."""


@dataclass(frozen=True, slots=True)
class SecondCopy:
    """The copy of an app that is starting beside its running release (update, activate, rename).

    ``started_at_s`` is a reading of the monotonic clock when its process started;
    ``health_deadline_s`` is the app's own deadline for a first healthy answer.
    """

    budget_mb: int
    started_at_s: float
    health_deadline_s: int

    def is_counted_at(self, now_s: float) -> bool:
        return now_s - self.started_at_s < self.health_deadline_s


@dataclass(frozen=True, slots=True)
class RunningApp:
    """An app with a running release and, during a swap, the copy that will replace it."""

    slug: str
    budget_mb: int
    second_copy: SecondCopy | None = None


@dataclass(frozen=True, slots=True)
class MemoryLedger:
    """The slot's memory at one instant; the names are those of the ``PlatformView`` contract.

    ``free_mb`` is ``limit_mb - reserve_mb - committed_mb`` and is negative when the slot is
    over-committed (for example after the reserve was raised): the debt is shown, not hidden.
    """

    limit_mb: int
    reserve_mb: int
    committed_mb: int

    @property
    def free_mb(self) -> int:
        return self.limit_mb - self.reserve_mb - self.committed_mb


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """Whether a start or an update fits; ``reason`` is the text shown when it does not."""

    admitted: bool
    needed_mb: int
    free_mb: int

    @property
    def reason(self) -> str | None:
        if self.admitted:
            return None
        return f"needs {self.needed_mb} MB, {max(self.free_mb, 0)} MB free"


def memory_ledger(
    *, limit_mb: int, reserve_mb: int, running: Iterable[RunningApp], now_s: float
) -> MemoryLedger:
    """Add up what the running copies commit at ``now_s``, overhead included."""
    committed_mb = 0
    for app in running:
        committed_mb += app.budget_mb + OVERHEAD_PER_CHILD_MB
        if app.second_copy is not None and app.second_copy.is_counted_at(now_s):
            committed_mb += app.second_copy.budget_mb + OVERHEAD_PER_CHILD_MB
    return MemoryLedger(limit_mb=limit_mb, reserve_mb=reserve_mb, committed_mb=committed_mb)


def check_admission(
    *,
    limit_mb: int,
    reserve_mb: int,
    running: Iterable[RunningApp],
    candidate_budget_mb: int,
    now_s: float,
) -> AdmissionDecision:
    """Decide whether one more copy of ``candidate_budget_mb`` fits next to ``running``.

    For a new app, ``running`` does not hold it; for an update or an activation, ``running``
    still holds the app's current release, which is what makes the overlap count.
    """
    books = memory_ledger(limit_mb=limit_mb, reserve_mb=reserve_mb, running=running, now_s=now_s)
    needed_mb = candidate_budget_mb + OVERHEAD_PER_CHILD_MB
    return AdmissionDecision(
        admitted=needed_mb <= books.free_mb, needed_mb=needed_mb, free_mb=books.free_mb
    )


def read_container_memory_limit_mb(
    *,
    cgroup_v2_path: Path = CGROUP_V2_LIMIT_PATH,
    cgroup_v1_path: Path = CGROUP_V1_LIMIT_PATH,
    meminfo_path: Path = MEMINFO_PATH,
) -> int:
    """The container's memory limit in whole megabytes, rounded down; read once at start.

    The first source that states a finite figure wins. A source that is missing, says "no limit"
    or holds something unparsable is skipped; ``MemoryLimitUnreadableError`` names every place
    tried when none is left. ``/proc/meminfo`` is last because inside a container it can show the
    whole host.
    """
    for path in (cgroup_v2_path, cgroup_v1_path):
        limit_bytes = read_cgroup_bytes(path)
        if limit_bytes is not None:
            return limit_bytes // _BYTES_PER_MB
    total_kb = _meminfo_total_kb(meminfo_path)
    if total_kb is not None:
        return total_kb * _BYTES_PER_KB // _BYTES_PER_MB
    tried = ", ".join(str(path) for path in (cgroup_v2_path, cgroup_v1_path, meminfo_path))
    raise MemoryLimitUnreadableError(
        f"cannot tell the container's memory limit (tried {tried}); "
        "run on a Linux container that exposes one of them, or fix the mount"
    )


def read_cgroup_bytes(path: Path) -> int | None:
    """A byte count a cgroup file states (a limit or a usage), or ``None`` when it states none.

    ``None`` for a missing or unreadable file, "max", anything not decimal, zero, and v1's "no
    limit" (a value too large to be a real size). The metrics sampler reads usage with it too.
    """
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None  # absent on this cgroup version (or unreadable): try the next source
    if not text.isdecimal():
        return None  # "max" (v2: no limit) or garbage: this source states no figure
    limit_bytes = int(text)
    return limit_bytes if 0 < limit_bytes < _UNLIMITED_BYTES_FLOOR else None


def _meminfo_total_kb(path: Path) -> int | None:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        name, _, rest = line.partition(":")
        if name == "MemTotal":
            fields = rest.split()
            if len(fields) == 2 and fields[0].isdecimal() and fields[1] == "kB":
                return int(fields[0])
            return None
    return None
