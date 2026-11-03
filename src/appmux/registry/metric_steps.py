"""How the minute rows of ``metrics`` add up to longer steps (T193, ``GET /metrics?step=``).

``GET /metrics?step=`` and ``GET /metrics/breakdown`` answer per step of 5 minutes, 15 minutes or
an hour as well as per minute. What each column becomes over a step is defined once, here; the
registry client builds its SQL from ``STEP_RULES`` and the in-memory stub of the tests folds rows
with the same table:

* ``sum``: the counts, which add up across minutes;
* ``max``: the peaks, which are the ``*_max`` columns and the platform's two memory figures (each
  minute already holds the peak of its samples, ``MetricsAggregator.observe_platform``);
* ``last``: the gauges read at one moment (WebSockets open, the memory limit, the children
  running and registered): the value of the latest minute of the step;
* ``histogram``: ``lat_hist``, bucket by bucket;
* ``by_rule``: ``egress_dropped_by_rule``, rule id by rule id.

A column that is null in a minute (the platform columns of an app's row) stays null over a step.

A step starts at a whole multiple of its length counted from ``STEP_ORIGIN``, so the steps fall on
the same UTC instants whatever window asks for them: a quarter at :00, :15, :30 or :45, an hour on
the hour. A window that starts or ends inside a step cuts it: the step holds only the minutes in
the window and still carries its own start as its ``minute``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, Field

from appmux.registry.models import LATENCY_BUCKET_COUNT, RowModel

StepRule = Literal["sum", "max", "last", "histogram", "by_rule"]

STEP_ORIGIN: Final = datetime(2000, 1, 1, tzinfo=UTC)

STEP_RULES: Final[Mapping[str, StepRule]] = MappingProxyType(
    {
        "requests": "sum",
        "r2xx": "sum",
        "r3xx": "sum",
        "r4xx": "sum",
        "r5xx": "sum",
        "upstream_errors": "sum",
        "limited": "sum",
        "lat_hist": "histogram",
        "bytes_in": "sum",
        "bytes_out": "sum",
        "in_flight_max": "max",
        "ws_opened": "sum",
        "ws_open": "last",
        "rss_mb_max": "max",
        "restarts": "sum",
        "egress_bytes_out": "sum",
        "egress_bytes_in": "sum",
        "egress_dropped": "sum",
        "egress_dropped_by_rule": "by_rule",
        "mem_used_mb": "max",
        "mem_limit_mb": "last",
        "children_running": "last",
        "children_registered": "last",
        "admission_rejections": "sum",
        "registry_errors": "sum",
        "registry_ms_max": "max",
        "edge_rss_mb": "max",
    }
)

# The columns of one point of ``GET /metrics/breakdown`` (contract addendum, ``MetricsBreakdown``).
BREAKDOWN_COLUMNS: Final = (
    "requests",
    "r4xx",
    "r5xx",
    "rss_mb_max",
    "bytes_out",
    "egress_dropped",
    "lat_hist",
)


def step_start(minute: datetime, step: timedelta) -> datetime:
    """The start of the step that holds ``minute``, in UTC."""
    return STEP_ORIGIN + ((minute - STEP_ORIGIN) // step) * step


class MetricBreakdownRow(RowModel):
    """One step of one series of the breakdown.

    ``app_slug`` is an app among the top ones, or ``None`` for the apps below the cut, added
    together: their counts and histograms, and the sum of their peaks of the step.
    """

    app_slug: str | None
    minute: AwareDatetime
    requests: int
    r4xx: int
    r5xx: int
    rss_mb_max: int
    bytes_out: int
    egress_dropped: int
    lat_hist: Annotated[
        list[int], Field(min_length=LATENCY_BUCKET_COUNT, max_length=LATENCY_BUCKET_COUNT)
    ]
