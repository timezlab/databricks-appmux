"""The Deploys that are still reading their folder: shown on the app page before a release exists.

A Deploy (``POST /apps/{app}/releases``) reads the source folder one file at a time and resolves the
packages before it writes the release row, which can take minutes. Until that row exists the
registry has nothing that says a Deploy is under way, so a page reloaded meanwhile showed nothing
until the release appeared (owner's report, 2026-11-18). This book holds, in the edge's memory, the
Deploys between the press and the answer; ``AppDetail.deploy_preparing`` reads it.

It is memory on purpose: the flow runs inside the request that started it, in this one process, so
a restart of appmux ends both the request and the entry. Once the release row is written the
release itself (``pending`` onwards) tells the page where the Deploy stands.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DeployUnderWay:
    started_at: datetime
    started_by: str


class DeploysUnderWay:
    """Per app, the Deploys whose flow has not answered yet; the earliest is the one shown."""

    def __init__(self, *, now: Callable[[], datetime] = _utc_now) -> None:
        self._now = now
        self._by_slug: dict[str, list[DeployUnderWay]] = {}

    @contextmanager
    def preparing(self, slug: str, *, started_by: str) -> Iterator[None]:
        """Hold the Deploy in the book while the block runs, whatever way the block ends."""
        entry = DeployUnderWay(started_at=self._now(), started_by=started_by)
        self._by_slug.setdefault(slug, []).append(entry)
        try:
            yield
        finally:
            entries = self._by_slug[slug]
            entries.remove(entry)
            if not entries:
                del self._by_slug[slug]

    def of(self, slug: str) -> DeployUnderWay | None:
        entries = self._by_slug.get(slug)
        return None if not entries else entries[0]


def no_deploy_under_way(slug: str) -> DeployUnderWay | None:
    """The reader when no Deploy route runs in this process."""
    return None
