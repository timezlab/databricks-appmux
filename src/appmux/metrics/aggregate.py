"""One-minute metric aggregates held in memory (design 10.2, data model 4.8, FR-052).

The edge calls the ``record_*`` and ``observe_*`` methods as it works; each call adds to the bucket
of the current minute, for the app and for the ``_platform`` row. ``drain`` hands back the buckets
of finished minutes as ``MetricRow``s and forgets them; the flusher writes them (``flush.py``).

* Counts add up (requests, status classes, bytes, restarts, drops); gauges keep a peak
  (``in_flight_max``, ``rss_mb_max``, ``mem_used_mb``, ``edge_rss_mb``, ``registry_ms_max``) or the
  latest sample (``ws_open``, ``mem_limit_mb``, ``children_running``, ``children_registered``).
* Latency is an 11-count histogram, because percentiles do not add up across minutes but
  histograms do. A percentile is read as the upper bound of the bucket that holds its rank
  (``percentile_upper_bound_ms``), so it is coarse by design.
* The ``_platform`` row sums the traffic of every request the edge saw, including requests that
  belong to no app (an unknown path, the admin UI), and adds eight platform columns. The table
  refuses a ``_platform`` row without ``mem_used_mb``, so a platform minute with no sample at all
  cannot be stored: it is left out and logged.

Metrics are best effort (design 11): a bad number must not break the request that produced it, so
these methods never raise; a negative count is read as zero. The aggregator is not thread safe: the
edge calls it from the event loop only.
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from appmux.registry.models import (
    LATENCY_BUCKET_COUNT,
    PLATFORM_METRICS_SLUG,
    DroppedByRule,
    MetricRow,
)

logger = logging.getLogger(__name__)

# Upper bounds in ms, inclusive; one more bucket counts everything above the last bound.
LATENCY_BOUNDS_MS: Final = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)
OVERFLOW_BUCKET: Final = len(LATENCY_BOUNDS_MS)
if OVERFLOW_BUCKET + 1 != LATENCY_BUCKET_COUNT:
    raise AssertionError("the latency bounds and the histogram column disagree")

_FIRST_CLIENT_ERROR, _FIRST_REDIRECT, _FIRST_SUCCESS = 400, 300, 200
_FIRST_SERVER_ERROR, _FIRST_NON_STATUS = 500, 600


def latency_bucket(latency_ms: float) -> int:
    """The histogram bucket of a latency: the first one whose upper bound it does not pass."""
    return bisect_left(LATENCY_BOUNDS_MS, latency_ms)


def percentile_upper_bound_ms(histogram: Sequence[int], percent: float) -> float | None:
    """The upper bound, in ms, of the bucket that holds the request at ``percent`` of the total.

    The request is the ``ceil(percent / 100 * total)``-th, and never earlier than the first. The
    answer is ``math.inf`` when it falls in the open-ended top bucket and ``None`` when the
    histogram holds no request.
    """
    total = sum(histogram)
    if total == 0:
        return None
    rank = max(1, math.ceil(total * percent / 100))
    seen = 0
    for index, count in enumerate(histogram):
        seen += count
        if seen >= rank:
            return LATENCY_BOUNDS_MS[index] if index < OVERFLOW_BUCKET else math.inf
    return math.inf  # a percent over 100 ranks past the last request


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _non_negative(value: float) -> int:
    return max(0, round(value))


@dataclass(slots=True)
class _Bucket:
    """The counters of one app (or of the platform) in one minute."""

    requests: int = 0
    r2xx: int = 0
    r3xx: int = 0
    r4xx: int = 0
    r5xx: int = 0
    upstream_errors: int = 0
    limited: int = 0
    lat_hist: list[int] = field(default_factory=lambda: [0] * LATENCY_BUCKET_COUNT)
    bytes_in: int = 0
    bytes_out: int = 0
    in_flight_max: int = 0
    ws_opened: int = 0
    ws_open: int = 0
    rss_mb_max: int = 0
    restarts: int = 0
    egress_bytes_out: int = 0
    egress_bytes_in: int = 0
    egress_dropped: int = 0
    egress_dropped_by_rule: Counter[str] = field(default_factory=Counter)
    # The platform columns; they stay unset on an app's bucket.
    mem_used_mb: int | None = None
    mem_limit_mb: int | None = None
    children_running: int | None = None
    children_registered: int | None = None
    admission_rejections: int = 0
    registry_errors: int = 0
    registry_ms_max: int = 0
    edge_rss_mb: int | None = None

    def count_request(
        self,
        *,
        status: int,
        latency_ms: float,
        bytes_in: int,
        bytes_out: int,
        upstream_error: bool,
        limited: bool,
    ) -> None:
        self.requests += 1
        if _FIRST_SUCCESS <= status < _FIRST_REDIRECT:
            self.r2xx += 1
        elif _FIRST_REDIRECT <= status < _FIRST_CLIENT_ERROR:
            self.r3xx += 1
        elif _FIRST_CLIENT_ERROR <= status < _FIRST_SERVER_ERROR:
            self.r4xx += 1
        else:
            # 5xx, and anything that is no final status at all: classes must add up to requests.
            self.r5xx += 1
        self.upstream_errors += upstream_error
        self.limited += limited
        self.lat_hist[latency_bucket(latency_ms)] += 1
        self.bytes_in += bytes_in
        self.bytes_out += bytes_out


class MetricsAggregator:
    """Minute buckets per app, filled by the edge and drained by the flusher."""

    def __init__(self, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._buckets: dict[tuple[str, datetime], _Bucket] = {}
        # What is open right now, so a minute that starts with sockets or requests still open
        # begins with them counted.
        self._in_flight: dict[str, int] = {}
        self._in_flight_total = 0
        self._ws_open: dict[str, int] = {}
        self._ws_open_total = 0

    # --- the edge feeds these ---------------------------------------------------------------------

    def record_request(
        self,
        app_slug: str | None,
        *,
        status: int,
        latency_ms: float,
        bytes_in: int = 0,
        bytes_out: int = 0,
        upstream_error: bool = False,
        limited: bool = False,
    ) -> None:
        """Count one finished request. ``app_slug`` is ``None`` when it belongs to no app.

        ``upstream_error`` is for a 502/504 the edge made itself (the child unreachable, a
        timeout), ``limited`` for a refusal by a cap (429, 413): a child can answer the same
        statuses, and those are not the edge's doing.
        """
        for bucket in self._buckets_for(app_slug):
            bucket.count_request(
                status=status,
                latency_ms=latency_ms,
                bytes_in=_non_negative(bytes_in),
                bytes_out=_non_negative(bytes_out),
                upstream_error=upstream_error,
                limited=limited,
            )

    def observe_in_flight(self, app_slug: str, current: int) -> None:
        """Note how many requests of ``app_slug`` are in flight now (call on every change)."""
        current = _non_negative(current)
        self._in_flight_total += current - self._in_flight.get(app_slug, 0)
        if current:
            self._in_flight[app_slug] = current
        else:
            self._in_flight.pop(app_slug, None)
        app_bucket, platform_bucket = self._app_and_platform(app_slug)
        app_bucket.in_flight_max = max(app_bucket.in_flight_max, current)
        platform_bucket.in_flight_max = max(platform_bucket.in_flight_max, self._in_flight_total)

    def websocket_opened(self, app_slug: str) -> None:
        self._ws_open[app_slug] = self._ws_open.get(app_slug, 0) + 1
        self._ws_open_total += 1
        for bucket in self._buckets_for(app_slug):
            bucket.ws_opened += 1
        self._refresh_ws_open(app_slug)

    def websocket_closed(self, app_slug: str) -> None:
        if self._ws_open.get(app_slug, 0) > 0:
            self._ws_open[app_slug] -= 1
            self._ws_open_total -= 1
        self._refresh_ws_open(app_slug)

    def observe_rss(self, app_slug: str, rss_mb: int) -> None:
        """A sample of the resident memory of the child's process tree."""
        bucket = self._bucket(app_slug)
        bucket.rss_mb_max = max(bucket.rss_mb_max, _non_negative(rss_mb))

    def record_restart(self, app_slug: str) -> None:
        for bucket in self._buckets_for(app_slug):
            bucket.restarts += 1

    def record_egress(
        self,
        app_slug: str,
        *,
        bytes_out: int = 0,
        bytes_in: int = 0,
        dropped: int = 0,
        dropped_by_rule: Mapping[str, int] | None = None,
    ) -> None:
        """Add what the pump moved and dropped since its last report (differences, not totals).

        ``dropped`` counts every dropped packet; ``dropped_by_rule`` only those a rule made, by the
        rule's id (``app:2``, ``global:1``).
        """
        for bucket in self._buckets_for(app_slug):
            bucket.egress_bytes_out += _non_negative(bytes_out)
            bucket.egress_bytes_in += _non_negative(bytes_in)
            bucket.egress_dropped += _non_negative(dropped)
            for rule_id, count in (dropped_by_rule or {}).items():
                bucket.egress_dropped_by_rule[rule_id] += _non_negative(count)

    # --- the platform columns ---------------------------------------------------------------------

    def observe_platform(
        self,
        *,
        mem_used_mb: int,
        mem_limit_mb: int,
        children_running: int,
        children_registered: int,
        edge_rss_mb: int,
    ) -> None:
        """A sample of the container: memory keeps its peak of the minute, the rest its latest."""
        platform = self._bucket(PLATFORM_METRICS_SLUG)
        platform.mem_used_mb = max(platform.mem_used_mb or 0, _non_negative(mem_used_mb))
        platform.edge_rss_mb = max(platform.edge_rss_mb or 0, _non_negative(edge_rss_mb))
        platform.mem_limit_mb = _non_negative(mem_limit_mb)
        platform.children_running = _non_negative(children_running)
        platform.children_registered = _non_negative(children_registered)

    def record_admission_rejection(self) -> None:
        self._bucket(PLATFORM_METRICS_SLUG).admission_rejections += 1

    def record_registry_call(self, duration_ms: float, *, failed: bool) -> None:
        platform = self._bucket(PLATFORM_METRICS_SLUG)
        platform.registry_ms_max = max(platform.registry_ms_max, math.ceil(max(0.0, duration_ms)))
        platform.registry_errors += failed

    # --- the flusher takes these ------------------------------------------------------------------

    def drain(self, *, include_open_minute: bool = False) -> list[MetricRow]:
        """Remove the finished minutes and return them, oldest first, then by app.

        ``include_open_minute`` also takes the minute in progress, for the last flush before the
        process stops.
        """
        current = self._current_minute()
        keys = [key for key in self._buckets if include_open_minute or key[1] < current]
        rows: list[MetricRow] = []
        for key in sorted(keys, key=lambda key: (key[1], key[0])):
            bucket = self._buckets.pop(key)
            row = self._to_row(key[0], key[1], bucket)
            if row is not None:
                rows.append(row)
        return rows

    # --- internals --------------------------------------------------------------------------------

    def _current_minute(self) -> datetime:
        return self._clock().astimezone(UTC).replace(second=0, microsecond=0)

    def _bucket(self, slug: str) -> _Bucket:
        key = (slug, self._current_minute())
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket()
            if slug == PLATFORM_METRICS_SLUG:
                bucket.in_flight_max = self._in_flight_total
                bucket.ws_open = self._ws_open_total
            else:
                bucket.in_flight_max = self._in_flight.get(slug, 0)
                bucket.ws_open = self._ws_open.get(slug, 0)
            self._buckets[key] = bucket
        return bucket

    def _app_and_platform(self, app_slug: str) -> tuple[_Bucket, _Bucket]:
        return self._bucket(app_slug), self._bucket(PLATFORM_METRICS_SLUG)

    def _buckets_for(self, app_slug: str | None) -> list[_Bucket]:
        platform = self._bucket(PLATFORM_METRICS_SLUG)
        return [platform] if app_slug is None else [self._bucket(app_slug), platform]

    def _refresh_ws_open(self, app_slug: str) -> None:
        app_bucket, platform_bucket = self._app_and_platform(app_slug)
        app_bucket.ws_open = self._ws_open.get(app_slug, 0)
        platform_bucket.ws_open = self._ws_open_total

    @staticmethod
    def _to_row(slug: str, minute: datetime, bucket: _Bucket) -> MetricRow | None:
        is_platform = slug == PLATFORM_METRICS_SLUG
        if is_platform and bucket.mem_used_mb is None:
            logger.warning(
                "metrics: the platform row of %s is left out: no platform sample in that minute",
                minute.isoformat(),
            )
            return None
        platform_columns: dict[str, int] = {}
        if is_platform:
            platform_columns = {
                "mem_used_mb": bucket.mem_used_mb or 0,
                "mem_limit_mb": bucket.mem_limit_mb or 0,
                "children_running": bucket.children_running or 0,
                "children_registered": bucket.children_registered or 0,
                "admission_rejections": bucket.admission_rejections,
                "registry_errors": bucket.registry_errors,
                "registry_ms_max": bucket.registry_ms_max,
                "edge_rss_mb": bucket.edge_rss_mb or 0,
            }
        return MetricRow(
            app_slug=slug,
            minute=minute,
            requests=bucket.requests,
            r2xx=bucket.r2xx,
            r3xx=bucket.r3xx,
            r4xx=bucket.r4xx,
            r5xx=bucket.r5xx,
            upstream_errors=bucket.upstream_errors,
            limited=bucket.limited,
            lat_hist=list(bucket.lat_hist),
            bytes_in=bucket.bytes_in,
            bytes_out=bucket.bytes_out,
            in_flight_max=bucket.in_flight_max,
            ws_opened=bucket.ws_opened,
            ws_open=bucket.ws_open,
            rss_mb_max=bucket.rss_mb_max,
            restarts=bucket.restarts,
            egress_bytes_out=bucket.egress_bytes_out,
            egress_bytes_in=bucket.egress_bytes_in,
            egress_dropped=bucket.egress_dropped,
            egress_dropped_by_rule=DroppedByRule(dict(bucket.egress_dropped_by_rule)),
            **platform_columns,
        )
