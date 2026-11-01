"""One-minute metric aggregates held in memory (T164; FR-052, design 10.2, data model 4.8).

The edge feeds a ``MetricsAggregator`` as it works; ``drain`` hands back the finished minutes as
``MetricRow``s, which the flusher writes. The clock is injected, so a test moves time by hand.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from appmux.metrics.aggregate import (
    LATENCY_BOUNDS_MS,
    MetricsAggregator,
    latency_bucket,
    percentile_upper_bound_ms,
)
from appmux.registry.models import LATENCY_BUCKET_COUNT, PLATFORM_METRICS_SLUG, MetricRow

START = datetime(2026, 10, 30, 9, 15, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def aggregator(clock: Clock) -> MetricsAggregator:
    return MetricsAggregator(clock=clock)


def sample_platform(aggregator: MetricsAggregator, **overrides: int) -> None:
    values = {
        "mem_used_mb": 1500,
        "mem_limit_mb": 6144,
        "children_running": 1,
        "children_registered": 2,
        "edge_rss_mb": 160,
    }
    aggregator.observe_platform(**{**values, **overrides})


def by_app(rows: list[MetricRow]) -> dict[str, MetricRow]:
    assert len({row.app_slug for row in rows}) == len(rows), "one row per app per minute"
    return {row.app_slug: row for row in rows}


# --- the latency histogram ------------------------------------------------------------------------


def test_the_histogram_has_the_eleven_buckets_of_the_data_model() -> None:
    assert len(LATENCY_BOUNDS_MS) + 1 == LATENCY_BUCKET_COUNT == 11


@pytest.mark.parametrize(
    ("latency_ms", "bucket"),
    [
        (0, 0),
        (4.9, 0),
        (5, 0),  # an upper bound is inclusive
        (5.01, 1),
        (10, 1),
        (25, 2),
        (26, 3),
        (250, 5),
        (999, 7),
        (1000, 7),
        (2500, 8),
        (5000, 9),
        (5000.5, 10),
        (600_000, 10),
    ],
)
def test_a_latency_goes_to_the_first_bucket_whose_upper_bound_it_does_not_pass(
    latency_ms: float, bucket: int
) -> None:
    assert latency_bucket(latency_ms) == bucket


def test_a_negative_latency_is_counted_in_the_first_bucket() -> None:
    assert latency_bucket(-3) == 0  # a clock that stepped back must not break a request


# The design's own example (10.2): 412 requests, running totals 18, 79, 219, 307, ...
DESIGN_HISTOGRAM = [18, 61, 140, 88, 52, 29, 14, 6, 3, 1, 0]


@pytest.mark.parametrize(
    ("percent", "upper_bound_ms"),
    [
        (50, 25),  # the 206th of 412 requests falls in the third bucket
        (1, 5),
        (95, 500),
        (99, 1000),
        (100, 5000),
        (0, 5),  # the first request: the rank never drops below 1
    ],
)
def test_a_percentile_is_the_upper_bound_of_the_bucket_that_holds_its_rank(
    percent: float, upper_bound_ms: int
) -> None:
    assert percentile_upper_bound_ms(DESIGN_HISTOGRAM, percent) == upper_bound_ms


def test_a_percentile_in_the_open_ended_bucket_has_no_upper_bound() -> None:
    histogram = [0] * 10 + [4]
    assert percentile_upper_bound_ms(histogram, 50) == float("inf")


def test_an_empty_histogram_has_no_percentile() -> None:
    assert percentile_upper_bound_ms([0] * 11, 50) is None


def test_a_percentile_of_summed_histograms_is_not_the_sum_of_percentiles() -> None:
    quick = [10] + [0] * 10
    slow = [0] * 9 + [10, 0]
    summed = [a + b for a, b in zip(quick, slow, strict=True)]
    assert percentile_upper_bound_ms(summed, 50) == 5
    assert percentile_upper_bound_ms(summed, 51) == 5000


# --- one request, one minute ----------------------------------------------------------------------


def test_requests_add_up_in_the_columns_of_the_data_model(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.record_request("sales", status=200, latency_ms=3, bytes_in=100, bytes_out=1000)
    aggregator.record_request("sales", status=204, latency_ms=30, bytes_in=0, bytes_out=0)
    aggregator.record_request("sales", status=302, latency_ms=8)
    aggregator.record_request("sales", status=404, latency_ms=12, bytes_in=7, bytes_out=70)
    aggregator.record_request("sales", status=500, latency_ms=900)  # the child answered 500
    aggregator.record_request("sales", status=502, latency_ms=7000, upstream_error=True)
    aggregator.record_request("sales", status=429, latency_ms=1, limited=True)
    aggregator.record_request("sales", status=413, latency_ms=1, bytes_in=9_999_999, limited=True)
    sample_platform(aggregator)
    clock.advance(minutes=1)

    row = by_app(aggregator.drain())["sales"]

    assert row.minute == START
    assert row.requests == 8
    assert (row.r2xx, row.r3xx, row.r4xx, row.r5xx) == (2, 1, 3, 2)
    assert row.upstream_errors == 1  # only what the edge itself made
    assert row.limited == 2
    assert row.bytes_in == 100 + 7 + 9_999_999
    assert row.bytes_out == 1070
    assert row.lat_hist == [3, 1, 1, 1, 0, 0, 0, 1, 0, 0, 1]
    assert sum(row.lat_hist) == row.requests


def test_a_child_that_answers_502_itself_is_not_an_upstream_error(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.record_request("sales", status=502, latency_ms=4)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    assert by_app(aggregator.drain())["sales"].upstream_errors == 0


@pytest.mark.parametrize("status", [0, 99, 100, 199, 600, 999])
def test_a_status_outside_200_to_599_counts_as_a_server_failure_so_classes_still_add_up(
    aggregator: MetricsAggregator, clock: Clock, status: int
) -> None:
    aggregator.record_request("sales", status=status, latency_ms=1)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    row = by_app(aggregator.drain())["sales"]
    assert row.r5xx == row.requests == 1
    assert row.r2xx + row.r3xx + row.r4xx + row.r5xx == row.requests


def test_a_bad_number_never_raises_into_a_request(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.record_request("sales", status=200, latency_ms=-5, bytes_in=-1, bytes_out=-1)
    aggregator.observe_in_flight("sales", -4)
    aggregator.observe_rss("sales", -10)
    aggregator.record_egress("sales", bytes_out=-1, bytes_in=-2, dropped=-3)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    row = by_app(aggregator.drain())["sales"]
    assert (row.bytes_in, row.bytes_out, row.in_flight_max, row.rss_mb_max) == (0, 0, 0, 0)
    assert (row.egress_bytes_out, row.egress_bytes_in, row.egress_dropped) == (0, 0, 0)
    assert row.lat_hist[0] == 1


# --- minutes --------------------------------------------------------------------------------------


def test_each_minute_is_a_row_and_the_open_minute_waits_until_it_is_over(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    clock.now = START + timedelta(seconds=59, microseconds=999_000)
    aggregator.record_request("sales", status=200, latency_ms=1)
    sample_platform(aggregator)
    clock.now = START + timedelta(minutes=1)
    aggregator.record_request("sales", status=200, latency_ms=1)
    aggregator.record_request("sales", status=200, latency_ms=1)
    sample_platform(aggregator)

    first = aggregator.drain()
    assert {(row.app_slug, row.minute, row.requests) for row in first} == {
        ("sales", START, 1),
        (PLATFORM_METRICS_SLUG, START, 1),
    }
    assert aggregator.drain() == []  # the second minute is still open

    clock.advance(seconds=60)
    second = by_app(aggregator.drain())
    assert second["sales"].minute == START + timedelta(minutes=1)
    assert second["sales"].requests == 2


def test_drain_removes_what_it_returns(aggregator: MetricsAggregator, clock: Clock) -> None:
    aggregator.record_request("sales", status=200, latency_ms=1)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    assert len(aggregator.drain()) == 2
    assert aggregator.drain() == []


def test_the_rows_come_out_oldest_minute_first_then_by_app(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    for slug in ("zeta", "alpha"):
        aggregator.record_request(slug, status=200, latency_ms=1)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    aggregator.record_request("alpha", status=200, latency_ms=1)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    rows = aggregator.drain()
    assert [(row.minute, row.app_slug) for row in rows] == sorted(
        (row.minute, row.app_slug) for row in rows
    )
    assert len(rows) == 5


def test_a_final_drain_can_include_the_minute_still_open(
    aggregator: MetricsAggregator,
) -> None:
    aggregator.record_request("sales", status=200, latency_ms=1)
    sample_platform(aggregator)
    assert aggregator.drain() == []
    assert len(aggregator.drain(include_open_minute=True)) == 2


def test_an_app_that_does_nothing_has_no_row(aggregator: MetricsAggregator, clock: Clock) -> None:
    aggregator.record_request("sales", status=200, latency_ms=1)
    sample_platform(aggregator)
    clock.advance(minutes=3)
    assert "ghost" not in by_app(aggregator.drain())


def test_every_row_is_a_valid_metric_row(aggregator: MetricsAggregator, clock: Clock) -> None:
    aggregator.record_request("sales", status=200, latency_ms=1)
    aggregator.record_egress("sales", bytes_out=5, dropped=2, dropped_by_rule={"app:2": 2})
    sample_platform(aggregator)
    clock.advance(minutes=1)
    for row in aggregator.drain():
        assert MetricRow.model_validate(row.model_dump()) == row
        assert row.minute.tzinfo is not None
        assert (row.minute.second, row.minute.microsecond) == (0, 0)


# --- gauges of one app ----------------------------------------------------------------------------


def test_in_flight_keeps_the_peak_of_the_minute(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    for current in (1, 5, 3, 0):
        aggregator.observe_in_flight("sales", current)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    assert by_app(aggregator.drain())["sales"].in_flight_max == 5


def test_requests_still_in_flight_count_towards_the_next_minute_peak(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.observe_in_flight("sales", 4)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    aggregator.observe_rss("sales", 10)  # a new minute opens with no new request
    sample_platform(aggregator)
    clock.advance(minutes=1)
    rows = [row for row in aggregator.drain() if row.app_slug == "sales"]
    assert [row.in_flight_max for row in rows] == [4, 4]


def test_the_platform_in_flight_peak_is_the_most_apps_had_at_once(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.observe_in_flight("sales", 3)
    aggregator.observe_in_flight("hr", 4)  # 7 together
    aggregator.observe_in_flight("sales", 0)
    aggregator.observe_in_flight("hr", 1)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    rows = by_app(aggregator.drain())
    assert rows[PLATFORM_METRICS_SLUG].in_flight_max == 7
    assert rows["sales"].in_flight_max == 3
    assert rows["hr"].in_flight_max == 4


def test_websockets_count_those_opened_in_the_minute_and_those_open_at_its_end(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    for _ in range(3):
        aggregator.websocket_opened("sales")
    aggregator.websocket_closed("sales")
    sample_platform(aggregator)
    clock.advance(minutes=1)
    aggregator.observe_rss("sales", 10)  # nothing happens to the sockets in minute two
    sample_platform(aggregator)
    clock.advance(minutes=1)

    first, second = (row for row in aggregator.drain() if row.app_slug == "sales")
    assert (first.ws_opened, first.ws_open) == (3, 2)
    assert (second.ws_opened, second.ws_open) == (0, 2)


def test_closing_more_websockets_than_were_opened_does_not_go_negative(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.websocket_closed("sales")
    aggregator.websocket_opened("sales")
    sample_platform(aggregator)
    clock.advance(minutes=1)
    assert by_app(aggregator.drain())["sales"].ws_open == 1


def test_rss_keeps_the_peak_and_restarts_add_up(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    for rss_mb in (200, 341, 280):
        aggregator.observe_rss("sales", rss_mb)
    aggregator.record_restart("sales")
    aggregator.record_restart("sales")
    sample_platform(aggregator)
    clock.advance(minutes=1)
    row = by_app(aggregator.drain())["sales"]
    assert (row.rss_mb_max, row.restarts) == (341, 2)


def test_an_app_that_only_reports_its_memory_still_gets_a_row(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.observe_rss("quiet", 120)
    sample_platform(aggregator)
    clock.advance(minutes=1)
    row = by_app(aggregator.drain())["quiet"]
    assert (row.requests, row.rss_mb_max) == (0, 120)
    assert row.lat_hist == [0] * 11


def test_egress_adds_up_and_the_drops_by_rule_are_merged(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.record_egress(
        "sales", bytes_out=100, bytes_in=1000, dropped=3, dropped_by_rule={"app:2": 2}
    )
    aggregator.record_egress(
        "sales", bytes_out=1, bytes_in=2, dropped=2, dropped_by_rule={"app:2": 1, "global:1": 1}
    )
    sample_platform(aggregator)
    clock.advance(minutes=1)
    row = by_app(aggregator.drain())["sales"]
    assert (row.egress_bytes_out, row.egress_bytes_in) == (101, 1002)
    assert row.egress_dropped == 5  # includes drops no rule made
    assert row.egress_dropped_by_rule.root == {"app:2": 3, "global:1": 1}


# --- the platform row -----------------------------------------------------------------------------


def test_the_platform_row_sums_the_traffic_of_all_requests_including_those_of_no_app(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.record_request("sales", status=200, latency_ms=3, bytes_in=10, bytes_out=100)
    aggregator.record_request("hr", status=503, latency_ms=300, bytes_in=20, bytes_out=200)
    aggregator.record_request(None, status=404, latency_ms=1, bytes_in=5, bytes_out=50)
    aggregator.record_request("sales", status=502, latency_ms=2, upstream_error=True)
    aggregator.record_request("hr", status=429, latency_ms=2, limited=True)
    aggregator.record_restart("hr")
    aggregator.websocket_opened("sales")
    aggregator.websocket_opened("hr")
    aggregator.record_egress(
        "sales", bytes_out=7, bytes_in=8, dropped=1, dropped_by_rule={"global:1": 1}
    )
    aggregator.record_egress(
        "hr", bytes_out=1, bytes_in=1, dropped=1, dropped_by_rule={"global:1": 1}
    )
    sample_platform(aggregator)
    clock.advance(minutes=1)

    rows = by_app(aggregator.drain())
    platform = rows[PLATFORM_METRICS_SLUG]
    apps = [rows["sales"], rows["hr"]]

    assert platform.requests == 5
    assert (platform.r2xx, platform.r4xx, platform.r5xx) == (1, 2, 2)
    assert (platform.upstream_errors, platform.limited) == (1, 1)
    assert (platform.bytes_in, platform.bytes_out) == (35, 350)
    assert platform.lat_hist == [4, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0]
    assert platform.restarts == 1
    assert (platform.ws_opened, platform.ws_open) == (2, 2)
    assert platform.egress_bytes_out == sum(row.egress_bytes_out for row in apps) == 8
    assert platform.egress_dropped == 2
    assert platform.egress_dropped_by_rule.root == {"global:1": 2}
    assert platform.rss_mb_max == 0  # the edge's own memory is edge_rss_mb
    assert platform.requests == sum(row.requests for row in apps) + 1  # plus the unattributed one


def test_the_platform_row_holds_the_eight_platform_columns_and_an_app_row_holds_none(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    aggregator.record_request("sales", status=200, latency_ms=1)
    sample_platform(aggregator)
    aggregator.record_admission_rejection()
    aggregator.record_admission_rejection()
    aggregator.record_registry_call(12.4, failed=False)
    aggregator.record_registry_call(850.2, failed=True)
    aggregator.record_registry_call(40, failed=True)
    clock.advance(minutes=1)

    rows = by_app(aggregator.drain())
    platform = rows[PLATFORM_METRICS_SLUG]
    assert platform.mem_used_mb == 1500
    assert platform.mem_limit_mb == 6144
    assert platform.children_running == 1
    assert platform.children_registered == 2
    assert platform.edge_rss_mb == 160
    assert platform.admission_rejections == 2
    assert platform.registry_errors == 2
    assert platform.registry_ms_max == 851  # the slowest call, rounded up to a whole ms
    sales = rows["sales"]
    assert sales.mem_used_mb is None
    assert sales.registry_ms_max is None


def test_memory_gauges_keep_the_peak_and_the_counts_keep_the_latest_sample(
    aggregator: MetricsAggregator, clock: Clock
) -> None:
    sample_platform(aggregator, mem_used_mb=1000, edge_rss_mb=150, children_running=1)
    sample_platform(aggregator, mem_used_mb=1800, edge_rss_mb=170, children_running=2)
    sample_platform(
        aggregator, mem_used_mb=1200, edge_rss_mb=160, children_running=3, children_registered=4
    )
    clock.advance(minutes=1)
    platform = by_app(aggregator.drain())[PLATFORM_METRICS_SLUG]
    assert (platform.mem_used_mb, platform.edge_rss_mb) == (1800, 170)
    assert (platform.children_running, platform.children_registered) == (3, 4)


def test_a_platform_minute_with_no_sample_is_left_out_and_said_so(
    aggregator: MetricsAggregator, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    # The table refuses a '_platform' row without mem_used_mb, so the minute cannot be stored.
    aggregator.record_request("sales", status=200, latency_ms=1)
    clock.advance(minutes=1)
    with caplog.at_level(logging.WARNING, logger="appmux.metrics.aggregate"):
        rows = aggregator.drain()
    assert [row.app_slug for row in rows] == ["sales"]
    assert any("platform" in record.getMessage() for record in caplog.records)
