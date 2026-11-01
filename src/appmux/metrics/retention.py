"""Keeping metrics for 7 days: an hourly delete by ``minute`` (data model 8, FR-052).

``prune_once`` asks the registry to delete the rows older than the retention; ``run_retention`` is
the job to start as a task: one prune at start (a process that was down for a while catches up at
once), then one every hour until ``stop`` is set. A failed prune is logged by its code or class
name and tried again at the next interval; it never ends the job.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Final

from appmux.metrics.flush import MetricsStore
from appmux.registry.errors import RegistryError

logger = logging.getLogger(__name__)

RETENTION: Final = timedelta(days=7)
PRUNE_INTERVAL_S: Final = 3600.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def prune_once(store: MetricsStore, *, now: datetime) -> int:
    """Delete the rows whose minute is before ``now`` minus the retention; returns how many."""
    return await store.prune_metrics(older_than=now - RETENTION)


async def run_retention(
    store: MetricsStore,
    stop: asyncio.Event,
    *,
    interval_s: float = PRUNE_INTERVAL_S,
    clock: Callable[[], datetime] = _utc_now,
) -> None:
    """Prune now and then every ``interval_s`` until ``stop`` is set."""
    while not stop.is_set():
        try:
            deleted = await prune_once(store, now=clock())
        except RegistryError as exc:
            logger.warning("metrics: the retention delete failed (%s); will try again", exc.code)
        except Exception as exc:
            # Broad on purpose: a background job logs the class and keeps running.
            logger.warning(
                "metrics: the retention delete failed (%s); will try again", type(exc).__name__
            )
        else:
            if deleted:
                logger.info("metrics: deleted %d rows older than %s", deleted, RETENTION)
        # A timeout is the normal wake-up: the interval is over and nobody asked to stop.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
