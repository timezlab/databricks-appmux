"""Keeping metrics for 7 days: an hourly delete by ``minute`` (T164, T168; data model 8)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import AwareDatetime

from appmux.metrics.retention import RETENTION, prune_once, run_retention
from appmux.registry.errors import RegistryUnavailable

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
LOGGER = "appmux.metrics.retention"


class FakeStore:
    def __init__(self, calls_wanted: int = 1) -> None:
        self.enough_calls = asyncio.Event()
        self.calls_wanted = calls_wanted
        self.cutoffs: list[AwareDatetime] = []
        self.failures: list[BaseException] = []
        self.pruned = 3

    async def append_metrics(self, rows: object) -> None:
        raise AssertionError("retention never writes metrics")

    async def prune_metrics(self, *, older_than: AwareDatetime) -> int:
        self.cutoffs.append(older_than)
        if len(self.cutoffs) >= self.calls_wanted:
            self.enough_calls.set()
        if self.failures:
            raise self.failures.pop(0)
        return self.pruned


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


async def test_prune_asks_for_the_rows_older_than_seven_days(store: FakeStore) -> None:
    deleted = await prune_once(store, now=NOW)
    assert store.cutoffs == [NOW - timedelta(days=7)]
    assert deleted == 3


async def test_the_job_prunes_at_start_and_then_every_interval(store: FakeStore) -> None:
    clock_values = iter(NOW + timedelta(hours=hour) for hour in range(100))
    store.calls_wanted = 3
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_retention(store, stop, interval_s=0.01, clock=lambda: next(clock_values))
    )
    try:
        await asyncio.wait_for(store.enough_calls.wait(), timeout=2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert store.cutoffs[:3] == [NOW + timedelta(hours=h) - RETENTION for h in range(3)]


async def test_the_job_stops_at_once_when_told_to_stop(store: FakeStore) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(run_retention(store, stop, interval_s=3600, clock=lambda: NOW))
    await asyncio.wait_for(store.enough_calls.wait(), timeout=2)
    stop.set()
    await asyncio.wait_for(task, timeout=2)  # not an hour later
    assert len(store.cutoffs) == 1


async def test_a_failed_prune_is_logged_and_tried_again_at_the_next_interval(
    store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.failures.extend([RegistryUnavailable(), RuntimeError("password=hunter2")])
    store.calls_wanted = 3
    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        task = asyncio.create_task(run_retention(store, stop, interval_s=0.01, clock=lambda: NOW))
        try:
            await asyncio.wait_for(store.enough_calls.wait(), timeout=2)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=2)
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert RegistryUnavailable.code in logged
    assert "RuntimeError" in logged
    assert "hunter2" not in logged
