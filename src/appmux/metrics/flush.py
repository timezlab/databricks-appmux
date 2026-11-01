"""Flushing the minute aggregates to the registry: one batch per interval (design 10.2 and 11).

``MetricsFlusher`` drains the finished minutes from the ``MetricsAggregator`` into a bounded buffer
and sends the whole buffer to the registry in one ``append_metrics`` call. Metrics are the one
thing that fails open on purpose:

* When the registry cannot be reached the rows stay in the buffer and go out again with the next
  batch. The registry ignores an (app, minute) it already holds, so a batch sent twice (for
  example one that was stored but whose answer was lost) is not counted twice.
* The buffer is bounded. When it is full the OLDEST rows are dropped and the drop is logged, so an
  outage leaves a gap in the charts instead of growing memory without limit.
* A batch the database refuses for good (``InvalidRow``) is dropped and logged as an error:
  sending it again cannot succeed and would block every row behind it.
* No failure reaches the edge or ends the loop; the log names the failure's class or code, never
  its message (a driver message can quote a host or a credential).

``run`` is the loop to start as a task. When its ``stop`` event is set (the shutdown hook, on
``SIGTERM``) it makes one last flush that also takes the minute still open.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from pydantic import AwareDatetime

from appmux.metrics.aggregate import MetricsAggregator
from appmux.registry.errors import InvalidRow, RegistryError
from appmux.registry.models import MetricRow

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S: Final = 60.0
# Rows, not minutes: 100 apps plus the platform row make about 6,000 rows an hour, so this keeps
# roughly the last hour and a half of an outage at that size, in a few MB.
DEFAULT_MAX_BUFFERED_ROWS: Final = 10_000
# A registry call that hangs must not stall the loop (the aggregator would pile up behind it) or
# hold up the 15 seconds SIGTERM allows.
DEFAULT_SEND_TIMEOUT_S: Final = 10.0


class MetricsStore(Protocol):
    """The two ``Registry`` methods the metrics code uses; the registry satisfies it as it is."""

    async def append_metrics(self, rows: Sequence[MetricRow]) -> None: ...

    async def prune_metrics(self, *, older_than: AwareDatetime) -> int: ...


@dataclass(frozen=True, slots=True)
class FlushResult:
    """What one flush did: rows stored, rows left in the buffer, rows dropped for good."""

    sent: int
    buffered: int
    dropped: int


class MetricsFlusher:
    def __init__(
        self,
        aggregator: MetricsAggregator,
        store: MetricsStore,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        max_buffered_rows: int = DEFAULT_MAX_BUFFERED_ROWS,
        send_timeout_s: float = DEFAULT_SEND_TIMEOUT_S,
    ) -> None:
        self._aggregator = aggregator
        self._store = store
        self._interval_s = interval_s
        self._max_buffered_rows = max_buffered_rows
        self._send_timeout_s = send_timeout_s
        self._buffer: deque[MetricRow] = deque()
        self._lock = asyncio.Lock()
        self._dropped_rows_total = 0

    @property
    def dropped_rows_total(self) -> int:
        """Rows lost since start: pushed out of a full buffer, or refused for good."""
        return self._dropped_rows_total

    async def flush(self, *, final: bool = False) -> FlushResult:
        """Collect the finished minutes and send everything buffered in one call.

        ``final`` also collects the minute in progress. Never raises (a cancelled task excepted).
        """
        async with self._lock:
            dropped = self._buffer_rows(self._collect(final=final))
            if not self._buffer:
                return FlushResult(sent=0, buffered=0, dropped=dropped)
            batch = tuple(self._buffer)
            try:
                async with asyncio.timeout(self._send_timeout_s):
                    await self._store.append_metrics(batch)
            except InvalidRow as exc:
                logger.error(
                    "metrics: the registry refused a batch of %d rows (%s); dropped, a retry "
                    "cannot succeed",
                    len(batch),
                    exc.code,
                )
                self._buffer.clear()
                self._dropped_rows_total += len(batch)
                return FlushResult(sent=0, buffered=0, dropped=dropped + len(batch))
            except RegistryError as exc:
                self._keep_after_failure(len(batch), f"the registry said {exc.code}")
            except TimeoutError:
                self._keep_after_failure(
                    len(batch), f"the registry did not answer in {self._send_timeout_s:g} s"
                )
            except Exception as exc:
                # Broad on purpose: metrics are best effort, so whatever goes wrong with a send,
                # the loop and the requests carry on and the rows wait for the next batch.
                self._keep_after_failure(len(batch), f"unexpected {type(exc).__name__}")
            else:
                self._buffer.clear()
                return FlushResult(sent=len(batch), buffered=0, dropped=dropped)
            return FlushResult(sent=0, buffered=len(self._buffer), dropped=dropped)

    async def run(self, stop: asyncio.Event) -> None:
        """Flush every interval until ``stop`` is set, then once more with the open minute too."""
        while not stop.is_set():
            # A timeout is the normal wake-up: the interval is over and nobody asked to stop.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            if not stop.is_set():
                await self.flush()
        last = await self.flush(final=True)
        if last.buffered:
            logger.warning(
                "metrics: the last flush before stopping failed; %d rows lost", last.buffered
            )

    def _collect(self, *, final: bool) -> list[MetricRow]:
        try:
            return self._aggregator.drain(include_open_minute=final)
        except Exception as exc:
            # Broad on purpose: a bug in the aggregator must not end the flush loop.
            logger.error("metrics: could not collect the minute rows (%s)", type(exc).__name__)
            return []

    def _buffer_rows(self, rows: Sequence[MetricRow]) -> int:
        """Append ``rows`` and push the oldest out past the bound; returns how many went."""
        self._buffer.extend(rows)
        overflow = len(self._buffer) - self._max_buffered_rows
        if overflow <= 0:
            return 0
        for _ in range(overflow):
            self._buffer.popleft()
        self._dropped_rows_total += overflow
        logger.warning(
            "metrics: the buffer is full while the registry is unreachable; dropped %d of the "
            "oldest rows (a gap in the charts)",
            overflow,
        )
        return overflow

    def _keep_after_failure(self, row_count: int, reason: str) -> None:
        logger.warning("metrics: %s; keeping %d rows for the next batch", reason, row_count)
