"""The rules that sum minute rows into longer steps (T193, ``registry/metric_steps.py``).

The registry client's SQL and the in-memory stub are both built from ``STEP_RULES``; these tests
pin that every column has a rule, that each column has the rule the contract gives it, and that a
step starts at the same instant in UTC whatever window asks for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from appmux.registry.metric_steps import (
    BREAKDOWN_COLUMNS,
    STEP_RULES,
    MetricBreakdownRow,
    step_start,
)
from appmux.registry.models import MetricRow

KEY_COLUMNS = {"app_slug", "minute"}


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_every_column_of_a_minute_row_has_one_rule_and_the_key_has_none() -> None:
    assert set(STEP_RULES) == set(MetricRow.model_fields) - KEY_COLUMNS


def test_each_column_is_summed_maximised_or_read_last_as_the_contract_says() -> None:
    by_rule: dict[str, set[str]] = {}
    for column, rule in STEP_RULES.items():
        by_rule.setdefault(rule, set()).add(column)

    assert by_rule["max"] == {
        "in_flight_max",
        "rss_mb_max",
        "registry_ms_max",
        "mem_used_mb",
        "edge_rss_mb",
    }
    assert by_rule["last"] == {"ws_open", "mem_limit_mb", "children_running", "children_registered"}
    assert by_rule["histogram"] == {"lat_hist"}
    assert by_rule["by_rule"] == {"egress_dropped_by_rule"}
    assert "requests" in by_rule["sum"] and "admission_rejections" in by_rule["sum"]


def test_the_breakdown_columns_are_columns_of_a_minute_row() -> None:
    assert set(BREAKDOWN_COLUMNS) <= set(STEP_RULES)
    assert set(MetricBreakdownRow.model_fields) == {*BREAKDOWN_COLUMNS, *KEY_COLUMNS}


@pytest.mark.parametrize(
    ("minute", "step", "start"),
    [
        ("2026-10-30T11:07:00", timedelta(minutes=1), "2026-10-30T11:07:00"),
        ("2026-10-30T11:04:00", timedelta(minutes=5), "2026-10-30T11:00:00"),
        ("2026-10-30T11:05:00", timedelta(minutes=5), "2026-10-30T11:05:00"),
        ("2026-10-30T11:14:00", timedelta(minutes=15), "2026-10-30T11:00:00"),
        ("2026-10-30T11:15:00", timedelta(minutes=15), "2026-10-30T11:15:00"),
        ("2026-10-30T11:59:00", timedelta(hours=1), "2026-10-30T11:00:00"),
        ("2026-10-30T00:00:00", timedelta(hours=1), "2026-10-30T00:00:00"),
    ],
)
def test_a_step_starts_on_a_whole_multiple_of_its_length_in_utc(
    minute: str, step: timedelta, start: str
) -> None:
    assert step_start(utc(minute), step) == utc(start)


def test_the_start_of_a_step_does_not_depend_on_the_zone_the_minute_is_written_in() -> None:
    # 13:14 at +02:00 is 11:14 UTC: its quarter starts at 11:00 UTC, not at 13:00 local.
    local = datetime.fromisoformat("2026-10-30T13:14:00+02:00")

    assert step_start(local, timedelta(minutes=15)) == utc("2026-10-30T11:00:00")
