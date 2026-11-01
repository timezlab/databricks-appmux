"""Flushing the aggregates to the registry in one batch per interval (T164, T168; FR-052).

Lakebase going down must not touch requests: the flusher keeps a bounded buffer, drops the oldest
rows when it is full (a visible gap in the charts, by design: design 11) and sends the rest again
when the registry answers. The registry ignores a (app, minute) it already holds, so a batch that
is sent twice is not counted twice.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from appmux.metrics.aggregate import MetricsAggregator
from appmux.metrics.flush import MetricsFlusher
from appmux.registry.errors import InvalidRow, RegistryUnavailable
from appmux.registry.models import PLATFORM_METRICS_SLUG, MetricRow

pytestmark = pytest.mark.anyio

START = datetime(2026, 10, 30, 9, 15, 0, tzinfo=UTC)
LOGGER = "appmux.metrics.flush"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance_minutes(self, minutes: int = 1) -> None:
        self.now += timedelta(minutes=minutes)


class FakeStore:
    """The metrics half of the registry: ``failures`` are raised by the next calls, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[MetricRow, ...]] = []
        self.stored: dict[tuple[str, datetime], MetricRow] = {}
        self.failures: list[BaseException] = []
        self.hang = False
        self.something_stored = asyncio.Event()

    async def append_metrics(self, rows: Sequence[MetricRow]) -> None:
        self.calls.append(tuple(rows))
        if self.hang:
            await asyncio.sleep(3600)
        if self.failures:
            raise self.failures.pop(0)
        for row in rows:
            self.stored.setdefault((row.app_slug, row.minute), row)
        self.something_stored.set()

    async def prune_metrics(self, *, older_than: datetime) -> int:
        raise AssertionError("the flusher never prunes")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def aggregator(clock: Clock) -> MetricsAggregator:
    return MetricsAggregator(clock=clock)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def one_minute_of_traffic(aggregator: MetricsAggregator, clock: Clock, slug: str = "sales") -> None:
    """A request, a platform sample, then the minute ends: two rows ready to flush."""
    aggregator.record_request(slug, status=200, latency_ms=1)
    aggregator.observe_platform(
        mem_used_mb=1500,
        mem_limit_mb=6144,
        children_running=1,
        children_registered=2,
        edge_rss_mb=160,
    )
    clock.advance_minutes()


# --- one batch per flush --------------------------------------------------------------------------


async def test_all_finished_rows_go_in_one_call(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore
) -> None:
    for slug in ("sales", "hr"):
        aggregator.record_request(slug, status=200, latency_ms=1)
    one_minute_of_traffic(aggregator, clock, "ops")
    flusher = MetricsFlusher(aggregator, store)

    result = await flusher.flush()

    assert len(store.calls) == 1  # one batch, not a call per row
    assert {row.app_slug for row in store.calls[0]} == {"sales", "hr", "ops", PLATFORM_METRICS_SLUG}
    assert (result.sent, result.buffered, result.dropped) == (4, 0, 0)


async def test_nothing_to_send_makes_no_call(
    aggregator: MetricsAggregator, store: FakeStore
) -> None:
    result = await MetricsFlusher(aggregator, store).flush()
    assert store.calls == []
    assert (result.sent, result.buffered) == (0, 0)


async def test_rows_already_sent_are_not_sent_again(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore
) -> None:
    flusher = MetricsFlusher(aggregator, store)
    one_minute_of_traffic(aggregator, clock)
    await flusher.flush()
    await flusher.flush()
    assert len(store.calls) == 1


async def test_a_flush_leaves_the_minute_still_open_for_the_next_one(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore
) -> None:
    aggregator.record_request("sales", status=200, latency_ms=1)
    await MetricsFlusher(aggregator, store).flush()
    assert store.calls == []
    assert len(aggregator.drain(include_open_minute=True)) >= 1  # it kept the open minute


# --- the registry is down -------------------------------------------------------------------------


async def test_rows_that_could_not_be_stored_are_sent_again_with_the_next_ones(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    flusher = MetricsFlusher(aggregator, store)
    store.failures.append(RegistryUnavailable())
    one_minute_of_traffic(aggregator, clock)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        failed = await flusher.flush()
    assert (failed.sent, failed.buffered) == (0, 2)
    assert any(RegistryUnavailable.code in record.getMessage() for record in caplog.records)

    one_minute_of_traffic(aggregator, clock)
    ok = await flusher.flush()

    assert ok.sent == 4
    assert [row.minute for row in store.calls[-1]] == sorted(row.minute for row in store.calls[-1])
    assert {row.minute for row in store.stored.values()} == {START, START + timedelta(minutes=1)}
    assert store.calls[1][:2] == store.calls[0]  # the same rows, offered again, oldest first


async def test_when_the_buffer_is_full_the_oldest_rows_go_and_the_gap_is_logged(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    flusher = MetricsFlusher(aggregator, store, max_buffered_rows=3)
    store.failures.extend(RegistryUnavailable() for _ in range(3))
    for _ in range(3):  # three minutes, two rows each, registry down
        one_minute_of_traffic(aggregator, clock)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = await flusher.flush()

    assert result.buffered == 3
    assert flusher.dropped_rows_total == 3
    assert any("dropped 1" in record.getMessage() for record in caplog.records)

    recovered = await flusher.flush()
    assert recovered.sent == 3
    # The newest three of the six rows survive; the first minute and half of the second are gone.
    assert sorted(row.minute for row in store.stored.values()) == [
        START + timedelta(minutes=1),
        START + timedelta(minutes=2),
        START + timedelta(minutes=2),
    ]


async def test_a_batch_the_registry_refuses_for_good_is_dropped_not_retried_for_ever(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    flusher = MetricsFlusher(aggregator, store)
    store.failures.append(InvalidRow("the values do not fit metrics"))
    one_minute_of_traffic(aggregator, clock)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        refused = await flusher.flush()
    assert (refused.sent, refused.buffered, refused.dropped) == (0, 0, 2)
    assert any(record.levelno == logging.ERROR for record in caplog.records)

    one_minute_of_traffic(aggregator, clock)
    assert (await flusher.flush()).sent == 2  # the next minute goes through


async def test_an_unexpected_error_keeps_the_rows_and_logs_only_its_class(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    flusher = MetricsFlusher(aggregator, store)
    store.failures.append(RuntimeError("password=hunter2 in a driver message"))
    one_minute_of_traffic(aggregator, clock)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        failed = await flusher.flush()
    assert (failed.sent, failed.buffered) == (0, 2)
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert "hunter2" not in logged
    assert all(record.exc_info is None for record in caplog.records)
    assert (await flusher.flush()).sent == 2


async def test_an_aggregator_that_breaks_does_not_end_the_flush(
    clock: Clock, store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    class BrokenAggregator(MetricsAggregator):
        def drain(self, *, include_open_minute: bool = False) -> list[MetricRow]:
            raise ValueError("a detail that must stay out of the log")

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        result = await MetricsFlusher(BrokenAggregator(clock=clock), store).flush()

    assert (result.sent, result.buffered) == (0, 0)
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "ValueError" in logged
    assert "must stay out" not in logged


async def test_a_registry_that_does_not_answer_is_given_up_on_after_the_timeout(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore
) -> None:
    flusher = MetricsFlusher(aggregator, store, send_timeout_s=0.05)
    store.hang = True
    one_minute_of_traffic(aggregator, clock)

    result = await asyncio.wait_for(flusher.flush(), timeout=2)

    assert (result.sent, result.buffered) == (0, 2)
    store.hang = False
    assert (await flusher.flush()).sent == 2


# --- the loop and the SIGTERM flush ---------------------------------------------------------------


async def test_the_loop_flushes_every_interval_until_it_is_told_to_stop(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore
) -> None:
    flusher = MetricsFlusher(aggregator, store, interval_s=0.01)
    stop = asyncio.Event()
    one_minute_of_traffic(aggregator, clock)
    task = asyncio.create_task(flusher.run(stop))
    try:
        await asyncio.wait_for(store.something_stored.wait(), timeout=2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert len(store.stored) == 2


async def test_on_stop_the_loop_flushes_the_minute_still_open_without_waiting_for_the_interval(
    aggregator: MetricsAggregator, store: FakeStore
) -> None:
    flusher = MetricsFlusher(aggregator, store, interval_s=3600)
    aggregator.record_request("sales", status=200, latency_ms=1)
    aggregator.observe_platform(
        mem_used_mb=1500,
        mem_limit_mb=6144,
        children_running=1,
        children_registered=2,
        edge_rss_mb=160,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(flusher.run(stop))
    await asyncio.sleep(0)  # let the loop start waiting
    stop.set()

    await asyncio.wait_for(task, timeout=2)

    assert {row.app_slug for row in store.stored.values()} == {"sales", PLATFORM_METRICS_SLUG}


async def test_the_final_flush_with_the_registry_down_ends_the_loop_anyway(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore, caplog: pytest.LogCaptureFixture
) -> None:
    flusher = MetricsFlusher(aggregator, store, interval_s=3600)
    store.failures.append(RegistryUnavailable())
    one_minute_of_traffic(aggregator, clock)
    stop = asyncio.Event()
    stop.set()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await asyncio.wait_for(flusher.run(stop), timeout=2)
    assert any("lost" in record.getMessage() for record in caplog.records)


async def test_a_flush_that_fails_does_not_end_the_loop(
    aggregator: MetricsAggregator, clock: Clock, store: FakeStore
) -> None:
    flusher = MetricsFlusher(aggregator, store, interval_s=0.01)
    store.failures.extend([RegistryUnavailable(), RuntimeError("boom")])
    one_minute_of_traffic(aggregator, clock)
    stop = asyncio.Event()
    task = asyncio.create_task(flusher.run(stop))
    try:
        await asyncio.wait_for(store.something_stored.wait(), timeout=2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)
    assert len(store.stored) == 2
    assert len(store.calls) >= 3
