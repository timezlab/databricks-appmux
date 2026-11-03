"""The metrics service: sample, flush, prune, and flush once more on stop (T168, FR-052).

``MetricsService`` is the one object the service assembly creates, starts and stops. It runs three
jobs on the event loop:

* the **sampler** reads the figures nobody reports on their own, every ``sample_interval_s``: the
  container's memory and child counts (the ``_platform`` columns), each child's resident memory,
  and each pump's counters, which ``EgressTracker`` turns into what moved since the last sample;
* the **flusher** (``MetricsFlusher``) writes the finished minutes to the registry once a minute;
* the **retention** job (``run_retention``) deletes rows older than seven days.

Where the figures come from is injected, so this module imports neither the reconciler nor the
sandbox launcher: ``platform`` answers one ``PlatformSample`` (or ``None`` while there is nothing to
report) and ``children`` answers one ``ChildProbe`` per running sandbox. The readings of the
container's files are in ``metrics/sources.py``; ``Service`` (``assembly.py``) joins them to the
reconciler. The request counters do not pass through here; the edge feeds the aggregator directly
(``edge/pipeline.py``).

Metrics are best effort (design 11). A source that raises or hangs costs one sample of one figure:
the failure is logged once when it starts and once when it ends, by the exception's class and
never its message, and the loop goes on.

``stop`` ends the sampler and the retention job, then lets the flusher make its last flush, which
also takes the minute still open. The whole of it must fit ``stop_budget_s``; past that the flusher
is cancelled and the rows still in memory are lost, which is logged. The platform ends the process
15 seconds after ``SIGTERM`` and the children get their time first (TD-015), so the default budget
leaves the rest.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from appmux.metrics.aggregate import MetricsAggregator
from appmux.metrics.egress_feed import EgressTracker
from appmux.metrics.flush import MetricsFlusher, MetricsStore
from appmux.metrics.retention import PRUNE_INTERVAL_S, run_retention
from appmux.sandbox.pump import PumpStats

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL_S: Final = 15.0
DEFAULT_FLUSH_INTERVAL_S: Final = 60.0
DEFAULT_PROBE_TIMEOUT_S: Final = 3.0
DEFAULT_FLUSH_TIMEOUT_S: Final = 5.0
DEFAULT_STOP_BUDGET_S: Final = 8.0


@dataclass(frozen=True, slots=True)
class PlatformSample:
    """The container's figures at one moment; they fill the ``_platform`` row."""

    mem_used_mb: int
    mem_limit_mb: int
    children_running: int
    children_registered: int
    edge_rss_mb: int


def _no_rss() -> int | None:
    return None


@dataclass(frozen=True, slots=True)
class ChildProbe:
    """How to look at one running sandbox.

    ``instance`` differs for every sandbox of the app (two run at once during an update), so the
    pump's running totals of one are never taken for the other's. ``pump_stats`` answers the
    pump's counters, or ``None`` when the supervisor gave none in time. ``rss_mb`` answers the
    resident memory of the child's processes, or ``None`` when it cannot be read.
    """

    slug: str
    instance: str
    pump_stats: Callable[[], Awaitable[PumpStats | None]]
    rss_mb: Callable[[], int | None] = _no_rss


PlatformSource = Callable[[], PlatformSample | None]
ChildSource = Callable[[], Sequence[ChildProbe]]


class MetricsService:
    def __init__(
        self,
        aggregator: MetricsAggregator,
        store: MetricsStore,
        *,
        platform: PlatformSource,
        children: ChildSource,
        sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL_S,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        retention_interval_s: float = PRUNE_INTERVAL_S,
        probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        flush_timeout_s: float = DEFAULT_FLUSH_TIMEOUT_S,
        stop_budget_s: float = DEFAULT_STOP_BUDGET_S,
    ) -> None:
        self._aggregator = aggregator
        self._store = store
        self._platform = platform
        self._children = children
        self._sample_interval_s = sample_interval_s
        self._retention_interval_s = retention_interval_s
        self._probe_timeout_s = probe_timeout_s
        self._stop_budget_s = stop_budget_s
        self._flusher = MetricsFlusher(
            aggregator, store, interval_s=flush_interval_s, send_timeout_s=flush_timeout_s
        )
        self._egress = EgressTracker()
        self._failing: set[str] = set()
        self._samples_taken = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._retention_stop = asyncio.Event()
        self._flush_stop = asyncio.Event()

    @property
    def samples_taken(self) -> int:
        """How many samples have been completed (counting those where a source failed)."""
        return self._samples_taken

    @property
    def tracked_pumps(self) -> int:
        """How many pumps the egress tracker remembers; it follows the live sandboxes."""
        return self._egress.tracked

    def start(self) -> None:
        """Start the three jobs on the running event loop."""
        if self._tasks:
            raise RuntimeError("the metrics service is already started")
        self._retention_stop = asyncio.Event()
        self._flush_stop = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._sample_forever(), name="metrics-sampler"),
            asyncio.create_task(
                run_retention(
                    self._store, self._retention_stop, interval_s=self._retention_interval_s
                ),
                name="metrics-retention",
            ),
            asyncio.create_task(self._flusher.run(self._flush_stop), name="metrics-flusher"),
        ]

    async def stop(self) -> None:
        """Stop sampling and pruning, then flush everything once within ``stop_budget_s``.

        Safe to call when not started and to call twice.
        """
        tasks, self._tasks = self._tasks, []
        if not tasks:
            return
        sampler, retention, flusher = tasks
        # Cancelling is enough for both: a half-finished sample or prune costs nothing that the
        # next process would not redo, and neither holds anything the last flush needs.
        sampler.cancel()
        retention.cancel()
        await asyncio.gather(sampler, retention, return_exceptions=True)
        self._flush_stop.set()
        _, unfinished = await asyncio.wait({flusher}, timeout=self._stop_budget_s)
        if unfinished:
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)
            logger.warning(
                "metrics: the last flush did not finish within %g s; it was cancelled and the "
                "minutes still in memory are lost",
                self._stop_budget_s,
            )
        elif not flusher.cancelled() and flusher.exception() is not None:
            logger.error("metrics: the flusher ended with %s", type(flusher.exception()).__name__)

    async def sample_once(self) -> None:
        """Take one sample of the platform, of each child's memory, and of each pump."""
        self._sample_platform()
        await self._sample_children()
        self._samples_taken += 1

    async def _sample_forever(self) -> None:
        while True:
            await self.sample_once()
            await asyncio.sleep(self._sample_interval_s)

    def _sample_platform(self) -> None:
        try:
            sample = self._platform()
            if sample is not None:
                self._aggregator.observe_platform(
                    mem_used_mb=sample.mem_used_mb,
                    mem_limit_mb=sample.mem_limit_mb,
                    children_running=sample.children_running,
                    children_registered=sample.children_registered,
                    edge_rss_mb=sample.edge_rss_mb,
                )
        except Exception as exc:
            # Broad on purpose: the source is injected and metrics are best effort (design 11).
            self._failed("the platform figures", exc)
        else:
            self._recovered("the platform figures")

    async def _sample_children(self) -> None:
        try:
            probes = list(self._children())
        except Exception as exc:
            # Broad on purpose, as above: the loop must outlive a source that is not ready.
            self._failed("the list of running apps", exc)
            return
        self._recovered("the list of running apps")
        self._egress.retain({(probe.slug, probe.instance) for probe in probes})
        live = {self._child_name(probe) for probe in probes}
        self._failing = {
            name for name in self._failing if name in live or not name.startswith("app ")
        }
        await asyncio.gather(*(self._sample_child(probe) for probe in probes))

    async def _sample_child(self, probe: ChildProbe) -> None:
        name = self._child_name(probe)
        try:
            # In a thread: a reading walks /proc, once per child, and would stall the edge.
            rss_mb = await asyncio.to_thread(probe.rss_mb)
            if rss_mb is not None:
                self._aggregator.observe_rss(probe.slug, rss_mb)
            async with asyncio.timeout(self._probe_timeout_s):
                stats = await probe.pump_stats()
            if stats is not None:
                delta = self._egress.delta(probe.slug, probe.instance, stats)
                if not delta.is_empty:
                    self._aggregator.record_egress(
                        probe.slug,
                        bytes_out=delta.bytes_out,
                        bytes_in=delta.bytes_in,
                        dropped=delta.dropped,
                        dropped_by_rule=delta.dropped_by_rule,
                    )
        except Exception as exc:
            # Broad on purpose, as above; a TimeoutError of the probe lands here too.
            self._failed(name, exc)
        else:
            self._recovered(name)

    @staticmethod
    def _child_name(probe: ChildProbe) -> str:
        return f"app {probe.slug}"

    def _failed(self, name: str, exc: Exception) -> None:
        if name in self._failing:
            return
        self._failing.add(name)
        logger.warning(
            "metrics: sampling %s failed (%s); trying again at the next sample",
            name,
            type(exc).__name__,
        )

    def _recovered(self, name: str) -> None:
        if name in self._failing:
            self._failing.discard(name)
            logger.info("metrics: sampling %s recovered", name)
