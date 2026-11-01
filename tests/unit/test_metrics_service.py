"""The metrics feeds besides the request pipeline, and the service that runs them (T168, FR-052).

The pump keeps cumulative counters (``PumpStats``); the aggregator wants what happened in the
minute, so ``EgressTracker`` turns the first into the second. ``MetricsService`` is the object the
service assembly starts and stops: it samples the container and the pumps, flushes the aggregates
once a minute, prunes old rows, and on stop flushes once more within a budget.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import pytest

from appmux.edge.pipeline import MetricsSink
from appmux.metrics.aggregate import MetricsAggregator
from appmux.metrics.egress_feed import EgressDelta, EgressTracker
from appmux.metrics.service import (
    ChildProbe,
    MetricsService,
    PlatformSample,
    rss_mb_from_statm,
)
from appmux.registry.models import PLATFORM_METRICS_SLUG, MetricRow
from appmux.sandbox.pump import PumpStats

pytestmark = pytest.mark.anyio

START = datetime(2026, 10, 31, 9, 15, 0, tzinfo=UTC)
LOGGER = "appmux.metrics.service"
LEAKED_TEXT = "tok-test-do-not-log"


def pump_stats(
    *,
    out: int = 0,
    inbound: int = 0,
    dropped: int = 0,
    by_rule: dict[str, int] | None = None,
) -> PumpStats:
    return {
        "dropped_total": dropped,
        "by_rule": by_rule or {},
        "by_reason": {},
        "recent": [],
        "frames_to_slirp": 0,
        "bytes_to_slirp": out,
        "frames_to_child": 0,
        "bytes_to_child": inbound,
        "rules_generation": 0,
    }


# --- cumulative counters become differences ------------------------------------------------------


def test_the_first_reading_of_a_pump_counts_everything_it_has_moved() -> None:
    tracker = EgressTracker()

    delta = tracker.delta(
        "sales", "pid-1", pump_stats(out=900, inbound=300, dropped=4, by_rule={"app:1": 3})
    )

    assert delta == EgressDelta(
        bytes_out=900, bytes_in=300, dropped=4, dropped_by_rule={"app:1": 3}
    )


def test_a_later_reading_counts_only_what_is_new() -> None:
    tracker = EgressTracker()
    tracker.delta(
        "sales", "pid-1", pump_stats(out=900, inbound=300, dropped=4, by_rule={"app:1": 3})
    )

    delta = tracker.delta(
        "sales",
        "pid-1",
        pump_stats(out=1000, inbound=350, dropped=6, by_rule={"app:1": 4, "global:1": 1}),
    )

    assert delta == EgressDelta(
        bytes_out=100, bytes_in=50, dropped=2, dropped_by_rule={"app:1": 1, "global:1": 1}
    )


def test_a_reading_with_nothing_new_is_empty_and_leaves_out_rules_with_no_new_drops() -> None:
    tracker = EgressTracker()
    stats = pump_stats(out=10, dropped=2, by_rule={"app:1": 2})
    tracker.delta("sales", "pid-1", stats)

    delta = tracker.delta("sales", "pid-1", stats)

    assert delta.is_empty
    assert delta.dropped_by_rule == {}


def test_a_counter_that_went_down_is_read_as_a_restart_of_that_counter() -> None:
    tracker = EgressTracker()
    tracker.delta("sales", "pid-1", pump_stats(out=5000, dropped=9, by_rule={"app:1": 9}))

    delta = tracker.delta("sales", "pid-1", pump_stats(out=40, dropped=1, by_rule={"app:1": 1}))

    assert (delta.bytes_out, delta.dropped, delta.dropped_by_rule) == (40, 1, {"app:1": 1})


def test_a_new_instance_of_the_same_app_starts_from_zero_and_does_not_disturb_the_old_one() -> None:
    tracker = EgressTracker()
    tracker.delta("sales", "old-copy", pump_stats(out=5000))
    # During an update two copies of one app run at once; each is counted against itself.
    fresh = tracker.delta("sales", "new-copy", pump_stats(out=70))
    old = tracker.delta("sales", "old-copy", pump_stats(out=5100))

    assert fresh.bytes_out == 70
    assert old.bytes_out == 100


def test_retaining_forgets_the_pumps_that_are_gone() -> None:
    tracker = EgressTracker()
    tracker.delta("sales", "pid-1", pump_stats(out=100))
    tracker.delta("hr", "pid-2", pump_stats(out=100))

    tracker.retain({("hr", "pid-2")})

    assert tracker.delta("sales", "pid-1", pump_stats(out=130)).bytes_out == 130  # from zero
    assert tracker.delta("hr", "pid-2", pump_stats(out=130)).bytes_out == 30
    assert tracker.tracked == 2


# --- the service ----------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class FakeStore:
    def __init__(self) -> None:
        self.appended: list[tuple[MetricRow, ...]] = []
        self.pruned: list[datetime] = []
        self.hang_on_append = False

    async def append_metrics(self, rows: Sequence[MetricRow]) -> None:
        if self.hang_on_append:
            await asyncio.sleep(3600)
        self.appended.append(tuple(rows))

    async def prune_metrics(self, *, older_than: datetime) -> int:
        self.pruned.append(older_than)
        return 0

    def stored(self, slug: str) -> list[MetricRow]:
        return [row for batch in self.appended for row in batch if row.app_slug == slug]


SAMPLE = PlatformSample(
    mem_used_mb=2100, mem_limit_mb=6144, children_running=2, children_registered=3, edge_rss_mb=180
)


def probe(
    slug: str,
    *,
    instance: str = "pid-1",
    stats: Callable[[], PumpStats | None] | None = None,
    rss_mb: int | None = None,
) -> ChildProbe:
    async def read_stats() -> PumpStats | None:
        return stats() if stats is not None else None

    return ChildProbe(slug=slug, instance=instance, pump_stats=read_stats, rss_mb=lambda: rss_mb)


class World:
    """What the service samples: the platform's figures and the live children, both mutable."""

    def __init__(self) -> None:
        self.platform: PlatformSample | None = SAMPLE
        self.children: list[ChildProbe] = []
        self.platform_reads = 0

    def read_platform(self) -> PlatformSample | None:
        self.platform_reads += 1
        return self.platform

    def read_children(self) -> Sequence[ChildProbe]:
        return list(self.children)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def aggregator(clock: Clock) -> MetricsAggregator:
    return MetricsAggregator(clock=clock)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def service(aggregator: MetricsAggregator, store: FakeStore, world: World) -> MetricsService:
    return MetricsService(
        aggregator,
        store,
        platform=world.read_platform,
        children=world.read_children,
        flush_interval_s=3600,
        sample_interval_s=3600,
        retention_interval_s=3600,
    )


def rows_of(aggregator: MetricsAggregator) -> dict[str, MetricRow]:
    return {row.app_slug: row for row in aggregator.drain(include_open_minute=True)}


async def test_one_sample_records_the_platform_figures_the_rss_and_the_egress_of_each_child(
    service: MetricsService, aggregator: MetricsAggregator, world: World
) -> None:
    world.children = [
        probe(
            "sales",
            rss_mb=210,
            stats=lambda: pump_stats(out=900, inbound=300, dropped=2, by_rule={"app:1": 2}),
        )
    ]

    await service.sample_once()

    rows = rows_of(aggregator)
    assert rows[PLATFORM_METRICS_SLUG].mem_used_mb == 2100
    assert rows[PLATFORM_METRICS_SLUG].children_running == 2
    assert rows[PLATFORM_METRICS_SLUG].edge_rss_mb == 180
    sales = rows["sales"]
    assert (sales.rss_mb_max, sales.egress_bytes_out, sales.egress_bytes_in) == (210, 900, 300)
    assert sales.egress_dropped == 2
    assert dict(sales.egress_dropped_by_rule.root) == {"app:1": 2}
    assert rows[PLATFORM_METRICS_SLUG].egress_bytes_out == 900  # the platform row sums the apps


async def test_a_second_sample_adds_only_what_the_pump_moved_since_the_first(
    service: MetricsService, aggregator: MetricsAggregator, world: World
) -> None:
    counters = {"out": 900}
    world.children = [probe("sales", stats=lambda: pump_stats(out=counters["out"]))]
    await service.sample_once()
    counters["out"] = 1000

    await service.sample_once()

    assert rows_of(aggregator)["sales"].egress_bytes_out == 1000  # 900, then 100 more


async def test_a_pump_that_did_not_answer_leaves_the_baseline_where_it_was(
    service: MetricsService, aggregator: MetricsAggregator, world: World
) -> None:
    answers: list[PumpStats | None] = [pump_stats(out=900), None, pump_stats(out=1000)]
    world.children = [probe("sales", stats=lambda: answers.pop(0))]

    for _ in range(3):
        await service.sample_once()

    assert rows_of(aggregator)["sales"].egress_bytes_out == 1000


async def test_a_child_with_no_rss_source_still_gets_its_egress(
    service: MetricsService, aggregator: MetricsAggregator, world: World
) -> None:
    world.children = [probe("sales", rss_mb=None, stats=lambda: pump_stats(out=5))]

    await service.sample_once()

    assert rows_of(aggregator)["sales"].rss_mb_max == 0


async def test_without_a_platform_sample_the_children_are_still_sampled(
    service: MetricsService, aggregator: MetricsAggregator, world: World
) -> None:
    world.platform = None
    world.children = [probe("sales", stats=lambda: pump_stats(out=5))]

    await service.sample_once()

    rows = rows_of(aggregator)
    assert rows["sales"].egress_bytes_out == 5
    assert PLATFORM_METRICS_SLUG not in rows  # the table refuses a platform minute with no sample


async def test_a_child_that_fails_is_logged_by_class_only_and_the_others_are_sampled(
    service: MetricsService,
    aggregator: MetricsAggregator,
    world: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken() -> PumpStats | None:
        raise OSError(f"cannot read the stats at /run/{LEAKED_TEXT}")

    world.children = [probe("sales", stats=broken), probe("hr", stats=lambda: pump_stats(out=7))]

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await service.sample_once()

    assert rows_of(aggregator)["hr"].egress_bytes_out == 7
    assert "sales" in caplog.text and "OSError" in caplog.text
    assert LEAKED_TEXT not in caplog.text


async def test_a_failure_is_logged_when_it_starts_and_when_it_ends_not_at_every_sample(
    service: MetricsService, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    healthy = {"yes": False}

    def sometimes() -> PumpStats | None:
        if not healthy["yes"]:
            raise OSError("down")
        return pump_stats()

    world.children = [probe("sales", stats=sometimes)]

    with caplog.at_level(logging.INFO, logger=LOGGER):
        for _ in range(3):
            await service.sample_once()
        healthy["yes"] = True
        await service.sample_once()
        await service.sample_once()

    messages = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    assert len(messages) == 2
    assert "failed" in messages[0] and "sales" in messages[0]
    assert "recovered" in messages[1] and "sales" in messages[1]


async def test_a_platform_source_that_fails_does_not_stop_the_children_being_sampled(
    aggregator: MetricsAggregator, store: FakeStore, world: World
) -> None:
    def broken() -> PlatformSample | None:
        raise RuntimeError(LEAKED_TEXT)

    world.children = [probe("sales", stats=lambda: pump_stats(out=3))]
    service = MetricsService(
        aggregator, store, platform=broken, children=world.read_children, sample_interval_s=3600
    )

    await service.sample_once()

    assert rows_of(aggregator)["sales"].egress_bytes_out == 3


async def test_a_pump_that_never_answers_is_given_up_on_within_the_probe_timeout(
    aggregator: MetricsAggregator, store: FakeStore, world: World
) -> None:
    async def silent() -> PumpStats | None:
        await asyncio.sleep(3600)
        return None

    world.children = [
        ChildProbe(slug="sales", instance="pid-1", pump_stats=silent, rss_mb=lambda: 50),
        probe("hr", stats=lambda: pump_stats(out=2)),
    ]
    service = MetricsService(
        aggregator,
        store,
        platform=world.read_platform,
        children=world.read_children,
        probe_timeout_s=0.05,
        sample_interval_s=3600,
    )

    await asyncio.wait_for(service.sample_once(), timeout=2)

    rows = rows_of(aggregator)
    assert rows["hr"].egress_bytes_out == 2
    assert rows["sales"].rss_mb_max == 50  # what was read before the silence still counts


async def test_children_that_stopped_are_forgotten_so_the_state_stays_bounded(
    service: MetricsService, world: World
) -> None:
    world.children = [probe(f"app-{n}", stats=lambda: pump_stats(out=1)) for n in range(5)]
    await service.sample_once()
    assert service.tracked_pumps == 5

    world.children = world.children[:1]
    await service.sample_once()

    assert service.tracked_pumps == 1


# --- start and stop -------------------------------------------------------------------------------


async def wait_until(condition: Callable[[], bool], *, timeout_s: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the condition did not become true in time")
        await asyncio.sleep(0.005)


async def test_start_samples_at_once_and_then_every_interval(
    aggregator: MetricsAggregator, store: FakeStore, world: World
) -> None:
    service = MetricsService(
        aggregator,
        store,
        platform=world.read_platform,
        children=world.read_children,
        sample_interval_s=0.01,
    )

    service.start()
    try:
        await wait_until(lambda: world.platform_reads >= 3)
    finally:
        await service.stop()


async def test_start_prunes_old_rows_once_at_the_start(
    service: MetricsService, store: FakeStore
) -> None:
    service.start()
    try:
        await wait_until(lambda: len(store.pruned) == 1)
    finally:
        await service.stop()


async def test_stop_flushes_what_is_collected_including_the_minute_still_open(
    service: MetricsService, aggregator: MetricsAggregator, store: FakeStore
) -> None:
    aggregator.record_request("sales", status=200, latency_ms=3)
    service.start()
    await wait_until(lambda: service.samples_taken >= 1)

    await service.stop()

    assert len(store.stored("sales")) == 1
    assert len(store.stored(PLATFORM_METRICS_SLUG)) == 1  # the first sample came before the stop


async def test_stop_does_not_wait_longer_than_its_budget_for_a_registry_that_hangs(
    aggregator: MetricsAggregator, store: FakeStore, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    store.hang_on_append = True
    service = MetricsService(
        aggregator,
        store,
        platform=world.read_platform,
        children=world.read_children,
        sample_interval_s=3600,
        flush_timeout_s=30,
        stop_budget_s=0.2,
    )
    aggregator.record_request("sales", status=200, latency_ms=3)
    service.start()
    await wait_until(lambda: service.samples_taken >= 1)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await asyncio.wait_for(service.stop(), timeout=2)

    assert "did not finish" in caplog.text


async def test_stop_is_safe_to_call_twice_or_without_a_start(service: MetricsService) -> None:
    await service.stop()
    service.start()
    await service.stop()
    await service.stop()


async def test_the_service_cannot_be_started_twice(service: MetricsService) -> None:
    service.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            service.start()
    finally:
        await service.stop()


async def test_a_sampling_failure_does_not_end_the_loop(
    aggregator: MetricsAggregator, store: FakeStore, world: World
) -> None:
    calls = {"n": 0}

    def flaky() -> Sequence[ChildProbe]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the reconciler is not ready")
        return [probe("sales", stats=lambda: pump_stats(out=11))]

    service = MetricsService(
        aggregator, store, platform=world.read_platform, children=flaky, sample_interval_s=0.01
    )

    service.start()
    try:
        await wait_until(lambda: service.samples_taken >= 2)
    finally:
        await service.stop()

    assert store.stored("sales")[0].egress_bytes_out == 11


# --- the edge's sink and the aggregator agree -----------------------------------------------------


@pytest.mark.parametrize(
    "method", ["record_request", "observe_in_flight", "websocket_opened", "websocket_closed"]
)
def test_the_aggregator_has_the_signature_the_edge_pipeline_calls(method: str) -> None:
    # ``edge`` and ``metrics`` do not import each other (the edge feeds a Protocol), so nothing
    # type-checks the two against each other; this does.
    wanted = inspect.signature(getattr(MetricsSink, method))
    actual = inspect.signature(getattr(MetricsAggregator, method))

    assert list(actual.parameters.values()) == list(wanted.parameters.values())


# --- the edge's own memory ------------------------------------------------------------------------


def test_the_resident_pages_of_statm_become_megabytes() -> None:
    # statm: size resident shared text lib data dt, all in pages
    assert rss_mb_from_statm("100000 51200 3000 100 0 40000 0", page_size_bytes=4096) == 200


def test_a_statm_that_cannot_be_read_is_not_a_figure() -> None:
    assert rss_mb_from_statm("", page_size_bytes=4096) is None
    assert rss_mb_from_statm("1 two 3", page_size_bytes=4096) is None
