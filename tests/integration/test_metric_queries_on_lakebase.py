"""The step and breakdown queries of ``GET /metrics`` on the real registry database (T193).

The unit tests check this SQL as text (``tests/unit/test_registry_client.py``) and the contract
tests run a Python copy of its rules (``tests/contract/stub_registry.py``); neither shows what
PostgreSQL makes of it. Here the real ``RegistryClient`` runs ``read_metric_steps`` and
``read_metric_breakdown`` on Lakebase, over rows the test writes, and checks that:

* a step starts at a whole multiple of its length from ``STEP_ORIGIN`` (2000-01-01Z), so a window
  that starts inside a step gets that step with the step's own start, and each column of a step
  follows its rule (sum, max, last, histogram, drops by rule);
* the longest window the routes admit, ``MAX_METRIC_STEPS`` steps, comes back whole;
* the breakdown ranks the apps by their requests over the window, a tie by slug, adds the others
  into one series (``app_slug`` null, their peaks summed) and leaves the platform row out of both.

**Nothing real is read or written.** Every connection the client opens first creates a temporary
``metrics`` table from the baseline DDL (``migrations/0001_baseline.sql``). PostgreSQL looks in
the session's temporary schema first, so the client's unqualified ``metrics`` names that table; the
connection is refused if it does not, before any statement of the client runs. The table goes when
the connection closes.

It logs in as the owner of the token ``scripts/run-remote-tests.sh`` passes (their own Lakebase
role, the token as the password: how a platform admin's writes log in), so it needs what a platform
admin has, plus the right to create temporary tables, which PostgreSQL gives every role by default
(the platform admin's role on the Free Edition dev App's Lakebase had it, 2026-11-04). Marked
``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``. Set-up, local and never committed (the test fails loudly when one is
missing):

* what ``ops_session.load_target`` reads, which ``scripts/run-remote-tests.sh`` sets;
* ``APPMUX_TEST_REGISTRY_HOST``: the Lakebase host of the App's database (the App's ``PGHOST``);
* ``APPMUX_TEST_REGISTRY_DATABASE``: its database name (the App's ``APPMUX_LAKEBASE_DATABASE``).

On 2026-11-04 all four passed on the Free Edition dev App's Lakebase (27 s, from the dev machine
with the host and database of the App in the environment), each on its own temporary table as the
test is built to do. On 2026-11-03 the first three had passed against a local PostgreSQL 17 with
SSL, with these variables pointed at it, beside a permanent ``metrics`` table that they left as it
was. Two mutations of the SQL each failed one of them: steps counted from the window's start, and
ties ranked by slug in descending order.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import psycopg
import pytest
from ops_session import load_target
from pydantic import SecretStr

from appmux.__main__ import LAKEBASE_PORT
from appmux.admin.metrics import MAX_METRIC_STEPS, MAX_METRIC_WINDOW, METRIC_STEPS
from appmux.registry.client import (
    ConnectionParameters,
    DatabaseConnection,
    RegistryClient,
    connect_with_psycopg,
)
from appmux.registry.metric_steps import step_start
from appmux.registry.models import LATENCY_BUCKET_COUNT, PLATFORM_METRICS_SLUG, MetricRow

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

OPERATION_TIMEOUT_S: Final = 120.0  # a connection here also creates and checks the table
BASELINE: Final = Path(__file__).resolve().parents[2] / "migrations" / "0001_baseline.sql"
_METRICS_DDL: Final = re.compile(r"^CREATE TABLE metrics \((.*?)^\);", re.MULTILINE | re.DOTALL)
_IS_PRIVATE_TABLE: Final = (
    "SELECT (SELECT relnamespace FROM pg_class WHERE oid = 'metrics'::regclass) "
    "= pg_my_temp_schema() AS is_private"
)

QUARTER: Final = METRIC_STEPS["15m"]
HOUR: Final = METRIC_STEPS["1h"]
FIVE_MINUTES: Final = METRIC_STEPS["5m"]
# The longest window GET /metrics admits for a 5-minute step: MAX_METRIC_STEPS steps.
LONGEST_START: Final = datetime(2001, 4, 1, tzinfo=UTC)
LONGEST_END: Final = LONGEST_START + MAX_METRIC_STEPS * FIVE_MINUTES


def at(day: int, hour: int, minute: int) -> datetime:
    """A minute of March 2001: years from any row the App itself writes."""
    return datetime(2001, 3, day, hour, minute, tzinfo=UTC)


def row(slug: str, minute: datetime, requests: int, **columns: object) -> MetricRow:
    values: dict[str, object] = {"lat_hist": [0] * LATENCY_BUCKET_COUNT, **columns}
    if slug == PLATFORM_METRICS_SLUG:
        values.setdefault("mem_used_mb", 1)
    return MetricRow.model_validate(
        {"app_slug": slug, "minute": minute, "requests": requests, **values}
    )


def first_buckets(*counts: int) -> list[int]:
    return [*counts, *[0] * (LATENCY_BUCKET_COUNT - len(counts))]


# Steps of 15 minutes for alpha, asked from 10:07 to 10:52 on 4 March.
STEP_ROWS: Final = (
    row("alpha", at(4, 10, 6), 100),  # before the window
    row(
        "alpha",
        at(4, 10, 7),
        1,
        rss_mb_max=70,
        ws_open=4,
        lat_hist=first_buckets(1),
        egress_dropped_by_rule={"app:1": 1},
    ),
    row(
        "alpha",
        at(4, 10, 14),
        2,
        rss_mb_max=60,
        ws_open=2,
        lat_hist=first_buckets(0, 1),
        egress_dropped_by_rule={"app:1": 2, "global:1": 5},
    ),
    row("alpha", at(4, 10, 15), 3),
    row("alpha", at(4, 10, 29), 4),
    row("alpha", at(4, 10, 44), 5),
    row("alpha", at(4, 10, 45), 6),
    row("alpha", at(4, 10, 52), 100),  # the end of the window is not in it
    row("bravo", at(4, 10, 20), 100),  # another app
)
# The breakdown of 10:00 to 12:00 on 5 March by the hour, two apps of their own.
BREAKDOWN_ROWS: Final = (
    row("bravo", at(5, 10, 10), 30, rss_mb_max=10),
    row("alpha", at(5, 10, 5), 10, rss_mb_max=20),
    row("alpha", at(5, 11, 5), 20, rss_mb_max=25),  # alpha ties bravo at 30 and wins by slug
    row("charlie", at(5, 11, 20), 20, rss_mb_max=100),
    row("delta", at(5, 10, 30), 3, rss_mb_max=40),
    row("delta", at(5, 11, 30), 2, rss_mb_max=50),
    row(PLATFORM_METRICS_SLUG, at(5, 10, 0), 1000, rss_mb_max=900),  # never an app
)
# One row two minutes into each 5-minute step of the longest window.
LONGEST_ROWS: Final = tuple(
    row("golf", LONGEST_START + index * FIVE_MINUTES + timedelta(minutes=2), 1)
    for index in range(MAX_METRIC_STEPS)
)
# A row in the step just after the longest window, which no window of 1440 whole steps reaches.
PAST_THE_LONGEST_ROW: Final = row("golf", LONGEST_END, 1)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"set {name} for the metric queries run (see the module docstring)")
    return value


def _temporary_metrics_ddl() -> str:
    found = _METRICS_DDL.search(BASELINE.read_text(encoding="utf-8"))
    if found is None:
        pytest.fail("migrations/0001_baseline.sql has no CREATE TABLE metrics to copy")
    return f"CREATE TEMPORARY TABLE metrics ({found.group(1)})"


async def _connect_with_a_private_metrics_table(
    parameters: ConnectionParameters,
) -> DatabaseConnection:
    """A connection on which ``metrics`` is a temporary table of this session, or a failure."""
    connection = await connect_with_psycopg(parameters)
    try:
        await connection.execute(_temporary_metrics_ddl())
        (answer,) = await (await connection.execute(_IS_PRIVATE_TABLE)).fetchall()
    except psycopg.Error as exc:
        await connection.close()
        # The SQLSTATE says enough (42501: no right to create temporary tables); the text may not
        # be fit to print.
        pytest.fail(f"the temporary metrics table could not be made (SQLSTATE {exc.sqlstate})")
    if answer["is_private"] is not True:
        await connection.close()
        pytest.fail("metrics does not name the temporary table; nothing was written")
    return connection


@pytest.fixture
async def registry() -> AsyncIterator[RegistryClient]:
    target = load_target()
    token = SecretStr(target.user_token)

    async def owner_token() -> SecretStr:
        return token

    client = RegistryClient(
        host=_required("APPMUX_TEST_REGISTRY_HOST"),
        port=LAKEBASE_PORT,
        database=_required("APPMUX_TEST_REGISTRY_DATABASE"),
        user=target.admin_email,
        token_provider=owner_token,
        connection_factory=_connect_with_a_private_metrics_table,
        operation_timeout_s=OPERATION_TIMEOUT_S,
    )
    try:
        await client.append_metrics(
            [*STEP_ROWS, *BREAKDOWN_ROWS, *LONGEST_ROWS, PAST_THE_LONGEST_ROW]
        )
        yield client
    finally:
        await client.close()


async def test_steps_start_at_multiples_of_their_length_from_the_origin(
    registry: RegistryClient,
) -> None:
    start, end = at(4, 10, 7), at(4, 10, 52)

    steps = await registry.read_metric_steps(
        app_slug="alpha", start=start, end=end, step=QUARTER, limit=100
    )

    # step_start counts from STEP_ORIGIN; a step counted from the window's start would be 10:07.
    in_window = [r.minute for r in STEP_ROWS if r.app_slug == "alpha" and start <= r.minute < end]
    assert [s.minute for s in steps] == sorted({step_start(m, QUARTER) for m in in_window})
    assert [(s.minute, s.requests) for s in steps] == [
        (at(4, 10, 0), 3),  # 10:07 and 10:14: the window cuts the step, the step keeps its start
        (at(4, 10, 15), 7),
        (at(4, 10, 30), 5),
        (at(4, 10, 45), 6),
    ]
    first = steps[0]
    assert (first.rss_mb_max, first.ws_open) == (70, 2)  # the peak, and the latest minute's gauge
    assert first.lat_hist == first_buckets(1, 1)
    assert first.egress_dropped_by_rule.root == {"app:1": 3, "global:1": 5}


async def test_the_longest_window_the_routes_admit_comes_back_whole(
    registry: RegistryClient,
) -> None:
    assert LONGEST_END - LONGEST_START <= MAX_METRIC_WINDOW
    expected = [LONGEST_START + index * FIVE_MINUTES for index in range(MAX_METRIC_STEPS)]

    steps = await registry.read_metric_steps(
        app_slug="golf",
        start=LONGEST_START,
        end=LONGEST_END,
        step=FIVE_MINUTES,
        limit=MAX_METRIC_STEPS + 1,
    )
    breakdown = await registry.read_metric_breakdown(
        start=LONGEST_START, end=LONGEST_END, step=FIVE_MINUTES, top=8
    )

    assert [s.minute for s in steps] == expected
    assert [(point.app_slug, point.minute) for point in breakdown] == [
        ("golf", minute) for minute in expected
    ]


async def test_a_window_as_long_that_starts_inside_a_step_holds_one_step_more(
    registry: RegistryClient,
) -> None:
    # Why the routes count the steps a window touches (review T205 R6): 1440 x 5 minutes from two
    # minutes into a step hold the step it starts in, cut, and the one after the longest window.
    start, end = LONGEST_START + timedelta(minutes=2), LONGEST_END + timedelta(minutes=2)
    expected = [LONGEST_START + index * FIVE_MINUTES for index in range(MAX_METRIC_STEPS + 1)]

    steps = await registry.read_metric_steps(
        app_slug="golf", start=start, end=end, step=FIVE_MINUTES, limit=MAX_METRIC_STEPS + 2
    )
    breakdown = await registry.read_metric_breakdown(start=start, end=end, step=FIVE_MINUTES, top=8)

    assert end - start == MAX_METRIC_STEPS * FIVE_MINUTES
    assert [s.minute for s in steps] == expected
    assert [(point.app_slug, point.minute) for point in breakdown] == [
        ("golf", minute) for minute in expected
    ]


async def test_the_breakdown_ranks_by_requests_then_slug_and_adds_up_the_rest(
    registry: RegistryClient,
) -> None:
    points = await registry.read_metric_breakdown(
        start=at(5, 10, 0), end=at(5, 12, 0), step=HOUR, top=2
    )

    assert [(p.app_slug, p.minute, p.requests, p.rss_mb_max) for p in points] == [
        ("alpha", at(5, 10, 0), 10, 20),
        ("alpha", at(5, 11, 0), 20, 25),
        ("bravo", at(5, 10, 0), 30, 10),
        (None, at(5, 10, 0), 3, 40),  # delta alone; the platform row is in no series
        (None, at(5, 11, 0), 22, 150),  # charlie and delta, their peaks added
    ]
