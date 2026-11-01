"""The SC-001 harness (T108), against a fake HTTP server on a local port.

These tests check the harness, not appmux: that it cuts a run into slices, keeps the load going
while an operation runs, puts the slices after the operation's start in ``during``, scales the
control's failures to the length of the window it is compared with, reports an operation that
raises instead of losing it, and gives up on one that never returns. What the edge does under load
while appmux updates an app is ``tests/integration/test_zero_downtime.py`` on a container.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

import pytest
from http_load import HEAD_END, LatencyMs, LoadReport
from ops_under_load import (
    CONTROL_NAME,
    LoadSpec,
    Operation,
    OperationReport,
    WindowStats,
    allowed_failures,
    measure,
    problems,
    run_suite_async,
    window_stats,
)

pytestmark = pytest.mark.anyio

HOST: Final = "127.0.0.1"
EMAIL: Final = "dana@example.com"
TOKEN: Final = "fake-token-for-tests-0123456789"  # noqa: S105 - a placeholder, not a credential
OK_RESPONSE: Final = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
ERROR_RESPONSE: Final = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 2\r\n\r\nno"


@dataclass
class FakeEdge:
    port: int
    is_failing: bool = False


@asynccontextmanager
async def serve() -> AsyncIterator[FakeEdge]:
    edge = FakeEdge(port=0)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                await reader.readuntil(HEAD_END)
                writer.write(ERROR_RESPONSE if edge.is_failing else OK_RESPONSE)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass  # the generator closed its end: nothing more to answer on this connection
        finally:
            writer.close()

    server = await asyncio.start_server(handle, HOST, 0)
    edge.port = server.sockets[0].getsockname()[1]
    try:
        yield edge
    finally:
        server.close()
        await server.wait_closed()


def spec_for(edge: FakeEdge, *, max_operation_s: float = 30.0) -> LoadSpec:
    return LoadSpec(
        host=HOST,
        port=edge.port,
        path="/sales/",
        email=EMAIL,
        connections=2,
        rate_per_s=100.0,
        slice_s=0.2,
        before_s=0.4,
        max_operation_s=max_operation_s,
    )


# --- the sums ----------------------------------------------------------------------------------


def load_report(
    *, requests: int, failures: int, p95_ms: float | None, kinds: list[str] | None = None
) -> LoadReport:
    return LoadReport(
        connections=2,
        target_rate_per_s=100.0,
        seconds=5.0,
        requests=requests,
        per_second=requests / 5.0,
        failures=failures,
        failure_kinds=kinds or [],
        error_rate=0.0,
        cpu_seconds=0.0,
        latency_ms=None
        if p95_ms is None
        else LatencyMs(p50=p95_ms / 2, p95=p95_ms, p99=p95_ms * 2, max=p95_ms * 3),
    )


def test_slices_add_up_and_the_p95_figures_are_those_of_single_slices() -> None:
    stats = window_stats(
        [
            load_report(requests=500, failures=0, p95_ms=2.0),
            load_report(requests=480, failures=3, p95_ms=9.0, kinds=["status 503"]),
            load_report(requests=500, failures=1, p95_ms=4.0, kinds=["status 502", "status 503"]),
        ]
    )

    assert (stats.slices, stats.seconds, stats.requests, stats.failures) == (3, 15.0, 1480, 4)
    assert stats.failure_kinds == ["status 502", "status 503"]
    assert (stats.worst_slice_p95_ms, stats.median_slice_p95_ms) == (9.0, 4.0)
    assert stats.edge_cpu_seconds is None


def test_a_window_in_which_nothing_was_answered_has_no_p95() -> None:
    stats = window_stats([load_report(requests=0, failures=2, p95_ms=None)])

    assert (stats.requests, stats.failures) == (0, 2)
    assert stats.worst_slice_p95_ms is None and stats.median_slice_p95_ms is None


def window(*, seconds: float, requests: int = 1000, failures: int = 0) -> WindowStats:
    return WindowStats(
        slices=1,
        seconds=seconds,
        requests=requests,
        failures=failures,
        failure_kinds=["status 503"] if failures else [],
        worst_slice_p95_ms=3.0,
        median_slice_p95_ms=3.0,
        edge_cpu_seconds=None,
    )


def report(name: str, *, during: WindowStats, error: str | None = None) -> OperationReport:
    return OperationReport(
        name=name,
        before=window(seconds=10.0),
        during=during,
        operation_seconds=during.seconds,
        operation_error=error,
    )


def test_a_clean_control_allows_no_failure_whatever_the_length() -> None:
    control = window(seconds=60.0, failures=0)

    assert allowed_failures(control, 5.0) == 0
    assert allowed_failures(control, 600.0) == 0


def test_the_controls_failures_are_scaled_to_the_length_of_the_window_and_rounded_up() -> None:
    control = window(seconds=60.0, failures=3)

    assert allowed_failures(control, 60.0) == 3
    assert allowed_failures(control, 120.0) == 6
    assert allowed_failures(control, 10.0) == 1  # half a failure is still one: round up


# --- the judgement -------------------------------------------------------------------------------


CLEAN_CONTROL = report(CONTROL_NAME, during=window(seconds=60.0))


def test_an_operation_with_no_more_failures_than_the_control_passes() -> None:
    assert problems(CLEAN_CONTROL, report("update B", during=window(seconds=70.0))) == []
    noisy_control = report(CONTROL_NAME, during=window(seconds=60.0, failures=2))
    assert (
        problems(noisy_control, report("update B", during=window(seconds=60.0, failures=2))) == []
    )


def test_one_failed_request_more_than_a_clean_control_is_a_problem_naming_the_kind() -> None:
    (message,) = problems(
        CLEAN_CONTROL, report("update A", during=window(seconds=60.0, failures=1))
    )

    assert "1 failed requests" in message and "0 allowed" in message and "status 503" in message


def test_an_operation_that_raised_is_a_problem_even_when_the_load_was_fine() -> None:
    found = problems(
        CLEAN_CONTROL,
        report("update A", during=window(seconds=60.0), error="AssertionError: release failed"),
    )

    assert found == ["the operation itself failed: AssertionError: release failed"]


def test_no_answer_at_all_is_not_a_pass() -> None:
    found = problems(CLEAN_CONTROL, report("update A", during=window(seconds=60.0, requests=0)))

    assert found == ["no request was answered while the operation ran"]


# --- the run ------------------------------------------------------------------------------------


async def test_the_load_goes_on_while_the_operation_runs_and_before_it_has_its_own_slices() -> None:
    async with serve() as edge:
        operation = Operation("slow thing", run=lambda: time.sleep(0.5), settle_s=0.2)

        result = await measure(spec_for(edge), operation)

    assert result.before.slices == 2  # 0.4 s of 0.2 s slices
    # The operation lasts 0.5 s: three slices run before it returns, one more settles after it.
    assert result.during.slices >= 4
    assert result.operation_seconds >= 0.5 and result.operation_error is None
    assert result.during.requests > 0 and result.during.failures == 0
    assert result.during.worst_slice_p95_ms is not None
    assert problems(CLEAN_CONTROL, result) == []


async def test_a_failure_that_starts_with_the_operation_shows_in_during_and_not_in_before() -> None:
    async with serve() as edge:

        def break_the_edge_for_a_moment() -> None:
            edge.is_failing = True
            time.sleep(0.3)
            edge.is_failing = False

        operation = Operation("bad update", break_the_edge_for_a_moment, settle_s=0.0)
        result = await measure(spec_for(edge), operation)

    assert result.before.failures == 0
    assert result.during.failures > 0 and result.during.failure_kinds == ["status 503"]
    assert any("failed requests" in message for message in problems(CLEAN_CONTROL, result))


async def test_an_operation_that_raises_is_reported_and_the_run_carries_on() -> None:
    def explode() -> None:
        raise RuntimeError("the release did not become active")

    async with serve() as edge:
        result = await measure(spec_for(edge), Operation("update A", run=explode, settle_s=0.0))

    assert result.operation_error == "RuntimeError: the release did not become active"
    assert result.during.requests > 0


async def test_an_operation_that_never_returns_ends_the_run_with_an_error() -> None:
    forever = Operation("hung", run=lambda: time.sleep(5), settle_s=0.0)
    async with serve() as edge:
        result = await measure(spec_for(edge, max_operation_s=0.5), forever)

    assert (
        result.operation_error is not None and "still running after 0.5 s" in result.operation_error
    )
    assert result.operation_seconds < 5


async def test_a_suite_runs_the_control_first_and_then_each_operation_in_order() -> None:
    order: list[str] = []

    def named(name: str) -> Operation:
        return Operation(name, run=lambda: order.append(name), settle_s=0.0)

    async with serve() as edge:
        suite = await run_suite_async(
            spec_for(edge), [named("register B"), named("update B")], control_s=0.2
        )

    assert suite.control.name == CONTROL_NAME
    assert [item.name for item in suite.operations] == ["register B", "update B"]
    assert order == ["register B", "update B"]
    assert all(problems(suite.control, item) == [] for item in suite.operations)
    assert suite.control.during.seconds >= 0.2


async def test_the_printed_report_holds_numbers_and_names_and_not_the_person() -> None:
    async with serve() as edge:
        suite = await run_suite_async(
            spec_for(edge), [Operation("noop", run=lambda: None, settle_s=0.0)], control_s=0.2
        )

    text = suite.to_json()
    assert json.loads(text)["operations"][0]["name"] == "noop"
    assert EMAIL not in text


async def test_a_spec_with_a_bearer_token_and_no_email_drives_the_load_and_hides_the_token() -> (
    None
):
    async with serve() as edge:
        spec = LoadSpec(
            host=HOST,
            port=edge.port,
            path="/sales/",
            bearer_token=TOKEN,
            connections=2,
            rate_per_s=100.0,
            slice_s=0.2,
            before_s=0.2,
        )
        report = await measure(spec, Operation("noop", run=lambda: None, settle_s=0.0))

    assert report.before.requests > 0 and report.before.failures == 0
    assert TOKEN not in repr(spec)  # a spec ends up in assertion messages and tracebacks
