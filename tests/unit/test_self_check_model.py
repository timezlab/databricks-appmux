"""The self-check result model and ``self_check()`` (T045, T064; design 6.4, FR-013).

The canary is a short program that runs in a real sandbox and prints ONE JSON object,
``{"checks": [{"id": ..., "passed": ..., "detail": ...}, ...]}``, with one entry per check of
design 6.4. ``self_check()`` takes the text from an injected runner and builds the result. What is
pinned here is the part that decides whether children may run:

* the result lists one entry per check, and any failed check fails the whole result;
* a missing, duplicated, unknown or malformed entry, an empty list and text that is not one JSON
  object all fail the result (an unreadable answer is never a pass);
* a canary that cannot run, times out or raises fails the result, without its message reaching
  the result or the log;
* the failed checks are named by fixed ids, for the status page.

The sandbox itself (namespaces, seccomp, the pump) only runs in a container; what the canary
sees there is proven by the integration tests, not here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

import pytest

from appmux.sandbox.self_check import (
    CANARY_CHECK_NAME,
    CANARY_OUTPUT_MAX_BYTES,
    CHECK_IDS,
    DETAIL_MAX_LENGTH,
    CanaryOutputRefused,
    CanaryUnavailable,
    SelfCheckReport,
    SelfCheckState,
    parse_canary_output,
    self_check,
)

pytestmark = pytest.mark.anyio

LEAKY_MESSAGE: Final = "failed with token=fake-token-value-0123 at /databricks/secrets"
EXAMPLE_RESULT: Final = (
    Path(__file__).resolve().parents[2]
    / "specs"
    / "001-appmux-v1"
    / "contracts"
    / "examples"
    / "SelfCheckResult.json"
)
START: Final = datetime(2026, 10, 30, 8, 0, 3, tzinfo=UTC)

Runner = Callable[[], Awaitable[str]]


def canary_text(
    *,
    failing: Mapping[str, str] | None = None,
    omit: Sequence[str] = (),
    extra: Sequence[object] = (),
    details: Mapping[str, str] | None = None,
) -> str:
    """What a canary prints: every check passing unless ``failing`` names it (with its detail)."""
    failing = failing or {}
    details = details or {}
    checks: list[object] = [
        {
            "id": check_id,
            "passed": check_id not in failing,
            "detail": failing.get(check_id, details.get(check_id, "")),
        }
        for check_id in CHECK_IDS
        if check_id not in omit
    ]
    checks.extend(extra)
    return json.dumps({"checks": checks})


def runner_of(text: str) -> Runner:
    async def run_canary() -> str:
        return text

    return run_canary


def runner_raising(error: BaseException) -> Runner:
    async def run_canary() -> str:
        raise error

    return run_canary


class Clock:
    """A wall clock and a monotonic timer that move only when told to."""

    def __init__(self) -> None:
        self.elapsed_s = 0.0

    def now(self) -> datetime:
        return START

    def monotonic(self) -> float:
        return self.elapsed_s


async def run(runner: Runner, *, timeout_s: float = 5.0) -> SelfCheckReport:
    clock = Clock()
    return await self_check(runner, timeout_s=timeout_s, clock=clock.now, monotonic=clock.monotonic)


# --- parsing what the canary printed ------------------------------------------------------------


def test_a_complete_output_gives_one_result_per_check_in_the_order_printed() -> None:
    results = parse_canary_output(canary_text())

    assert [result.name for result in results] == list(CHECK_IDS)
    assert all(result.passed for result in results)
    assert all(result.detail is None for result in results)  # an empty detail is no detail


def test_the_order_is_the_canarys_run_order() -> None:
    reversed_checks = [
        {"id": check_id, "passed": True, "detail": ""} for check_id in reversed(CHECK_IDS)
    ]

    results = parse_canary_output(json.dumps({"checks": reversed_checks}))

    assert [result.name for result in results] == list(reversed(CHECK_IDS))


def test_a_failed_check_keeps_its_detail_and_a_passed_one_may_have_one() -> None:
    text = canary_text(
        failing={"sysv_ipc": "shmget on key 4242 succeeded"},
        details={"capabilities": "mount, ip link and AF_PACKET all refused"},
    )

    by_name = {result.name: result for result in parse_canary_output(text)}

    assert by_name["sysv_ipc"].passed is False
    assert by_name["sysv_ipc"].detail == "shmget on key 4242 succeeded"
    assert by_name["capabilities"].passed is True
    assert by_name["capabilities"].detail == "mount, ip link and AF_PACKET all refused"


def test_a_detail_is_cut_short_and_stripped_of_control_characters() -> None:
    long_detail = "x" * (DETAIL_MAX_LENGTH * 3)
    text = canary_text(
        failing={"sysv_ipc": long_detail, "capabilities": "line one\nline two\x1b[31m\x00"},
    )

    by_name = {result.name: result for result in parse_canary_output(text)}

    assert by_name["sysv_ipc"].detail is not None
    assert len(by_name["sysv_ipc"].detail or "") <= DETAIL_MAX_LENGTH
    capabilities = by_name["capabilities"].detail or ""
    assert capabilities.isprintable()
    assert "line one" in capabilities and "line two" in capabilities


def test_a_blank_detail_of_a_failed_check_is_no_detail() -> None:
    text = canary_text(failing={"sysv_ipc": "   "})

    by_name = {result.name: result for result in parse_canary_output(text)}

    assert by_name["sysv_ipc"].passed is False
    assert by_name["sysv_ipc"].detail is None


def _entry(check_id: object = "sysv_ipc", **fields: object) -> dict[str, object]:
    return {"id": check_id, "passed": True, "detail": "", **fields}


def _all_but(check_id: str, *entries: object) -> str:
    kept = [entry for entry in json.loads(canary_text())["checks"] if entry["id"] != check_id]
    return json.dumps({"checks": [*kept, *entries]})


REFUSED_OUTPUTS: Final[dict[str, str]] = {
    "empty text": "",
    "plain text": "all checks passed",
    "two objects": canary_text() + canary_text(),
    "an object and a trailing line": canary_text() + "\nok",
    "a list": "[]",
    "a string": '"ok"',
    "no checks key": "{}",
    "an empty list": json.dumps({"checks": []}),
    "checks that is not a list": json.dumps({"checks": "all"}),
    "an unknown top-level key": json.dumps(
        {"checks": json.loads(canary_text())["checks"], "ok": 1}
    ),
    "a missing check": canary_text(omit=["sysv_ipc"]),
    "every check missing but one": json.dumps({"checks": [_entry()]}),
    "a duplicated check": canary_text(extra=[_entry("sysv_ipc")]),
    "an unknown check": canary_text(extra=[_entry("made_up_check")]),
    "an unknown check in place of a known one": _all_but("sysv_ipc", _entry("made_up_check")),
    "an id that is not a string": _all_but("sysv_ipc", _entry(7)),
    "no id": _all_but("sysv_ipc", {"passed": True, "detail": ""}),
    "no passed": _all_but("sysv_ipc", {"id": "sysv_ipc", "detail": ""}),
    "passed as a string": _all_but("sysv_ipc", _entry(passed="true")),
    "passed as a number": _all_but("sysv_ipc", _entry(passed=1)),
    "passed as null": _all_but("sysv_ipc", _entry(passed=None)),
    "a detail that is not a string": _all_but("sysv_ipc", _entry(detail=5)),
    "an unknown key in an entry": _all_but("sysv_ipc", _entry(extra="x")),
    "an entry that is not an object": _all_but("sysv_ipc", "sysv_ipc"),
    "NaN": '{"checks": NaN}',
}


@pytest.mark.parametrize("text", list(REFUSED_OUTPUTS.values()), ids=list(REFUSED_OUTPUTS))
def test_output_that_is_not_exactly_the_full_list_is_refused(text: str) -> None:
    with pytest.raises(CanaryOutputRefused):
        parse_canary_output(text)


def test_output_larger_than_the_cap_is_refused_before_it_is_parsed() -> None:
    padding = " " * CANARY_OUTPUT_MAX_BYTES

    with pytest.raises(CanaryOutputRefused, match="more than"):
        parse_canary_output(canary_text() + padding)


def test_a_refusal_names_the_missing_check_and_never_echoes_an_unknown_one() -> None:
    with pytest.raises(CanaryOutputRefused) as missing:
        parse_canary_output(canary_text(omit=["sysv_ipc"]))
    with pytest.raises(CanaryOutputRefused) as unknown:
        parse_canary_output(canary_text(extra=[_entry("made_up_check_with_a_secret")]))

    assert "sysv_ipc" in str(missing.value)
    assert "made_up_check_with_a_secret" not in str(unknown.value)


def test_the_contract_example_lists_known_checks_in_run_order() -> None:
    # The example is what the admin UI is built against; a check renamed in one place only would
    # show the UI names the page never gets.
    names = [check["name"] for check in json.loads(EXAMPLE_RESULT.read_text())["checks"]]

    assert set(names) <= set(CHECK_IDS)
    assert names == [check_id for check_id in CHECK_IDS if check_id in names]


# --- the result ------------------------------------------------------------------------------


@pytest.mark.parametrize("check_id", CHECK_IDS)
async def test_any_one_failed_check_fails_the_whole_result(check_id: str) -> None:
    report = await run(runner_of(canary_text(failing={check_id: "it succeeded"})))

    assert report.passed is False
    assert report.failed_names == (check_id,)
    assert [check.name for check in report.checks] == list(CHECK_IDS)
    assert sum(check.passed for check in report.checks) == len(CHECK_IDS) - 1


async def test_every_check_passing_passes_the_result_with_the_time_and_duration() -> None:
    clock = Clock()

    async def run_canary() -> str:
        clock.elapsed_s += 0.84
        return canary_text()

    report = await self_check(run_canary, timeout_s=5.0, clock=clock.now, monotonic=clock.monotonic)

    assert report.passed is True
    assert report.failed_names == ()
    assert [check.name for check in report.checks] == list(CHECK_IDS)
    assert report.ran_at == START
    assert report.duration_ms == 840


async def test_several_failed_checks_are_all_named() -> None:
    report = await run(
        runner_of(canary_text(failing={"sysv_ipc": "x", "metadata_address": "connected"}))
    )

    assert report.passed is False
    assert set(report.failed_names) == {"sysv_ipc", "metadata_address"}


def test_a_report_with_no_check_is_not_a_pass() -> None:
    report = SelfCheckReport(ran_at=START, duration_ms=0, checks=())

    assert report.passed is False


@pytest.mark.parametrize("text", list(REFUSED_OUTPUTS.values()), ids=list(REFUSED_OUTPUTS))
async def test_output_that_cannot_be_read_fails_the_result_and_names_the_canary(text: str) -> None:
    report = await run(runner_of(text))

    assert report.passed is False
    assert report.failed_names == (CANARY_CHECK_NAME,)
    (only,) = report.checks
    assert only.passed is False
    assert only.detail  # the reason, for the platform page


async def test_a_canary_that_cannot_run_fails_the_result_with_the_reason_it_gave() -> None:
    reason = "the sandbox could not start: user namespaces are not available"

    report = await run(runner_raising(CanaryUnavailable(reason)))

    assert report.passed is False
    assert report.failed_names == (CANARY_CHECK_NAME,)
    assert report.checks[0].detail == reason


async def test_a_canary_that_raises_fails_the_result_without_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        report = await run(runner_raising(OSError(LEAKY_MESSAGE)))

    assert report.passed is False
    assert report.failed_names == (CANARY_CHECK_NAME,)
    assert "fake-token-value-0123" not in repr(report)
    assert "/databricks/secrets" not in repr(report)
    assert "fake-token-value-0123" not in caplog.text
    assert "OSError" in caplog.text  # the class is enough to find the cause


async def test_a_canary_that_does_not_finish_in_time_fails_the_result() -> None:
    never = asyncio.Event()

    async def run_canary() -> str:
        await never.wait()
        return canary_text()

    report = await run(run_canary, timeout_s=0.05)

    assert report.passed is False
    assert report.failed_names == (CANARY_CHECK_NAME,)
    assert "did not finish" in (report.checks[0].detail or "")


@pytest.mark.parametrize("answer", [None, b"{}", 0, ["checks"]], ids=repr)
async def test_a_runner_that_returns_no_text_fails_the_result(answer: object) -> None:
    async def run_canary() -> str:
        return cast("str", answer)  # the point: a runner that breaks its declared type

    report = await run(run_canary)

    assert report.passed is False
    assert report.failed_names == (CANARY_CHECK_NAME,)


async def test_cancelling_the_run_is_not_a_failed_check() -> None:
    started = asyncio.Event()
    never = asyncio.Event()

    async def run_canary() -> str:
        started.set()
        await never.wait()
        return canary_text()

    task = asyncio.create_task(run(run_canary, timeout_s=60.0))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- the state the edge and the platform page share ----------------------------------------------


async def test_before_the_first_run_nothing_is_passing_and_the_canary_is_named() -> None:
    state = SelfCheckState(runner_of(canary_text()))

    assert state.latest is None
    assert state.is_passing is False
    assert state.failed_names == (CANARY_CHECK_NAME,)


async def test_a_run_is_kept_and_a_later_failed_run_replaces_it() -> None:
    texts = [canary_text(), canary_text(failing={"sysv_ipc": "it succeeded"})]

    async def run_canary() -> str:
        return texts.pop(0)

    state = SelfCheckState(run_canary)

    first = await state.run()
    assert state.latest is first
    assert state.is_passing is True
    assert state.failed_names == ()

    second = await state.run()
    assert state.latest is second
    assert state.is_passing is False
    assert state.failed_names == ("sysv_ipc",)


async def test_runs_do_not_overlap() -> None:
    running = 0
    most_at_once = 0

    async def run_canary() -> str:
        nonlocal running, most_at_once
        running += 1
        most_at_once = max(most_at_once, running)
        await asyncio.sleep(0.01)
        running -= 1
        return canary_text()

    state = SelfCheckState(run_canary)

    await asyncio.gather(state.run(), state.run(), state.run())

    assert most_at_once == 1  # the canary is a whole sandbox; two at once would race each other


async def test_a_cancelled_run_leaves_the_last_result_in_place() -> None:
    first = canary_text()
    never = asyncio.Event()
    calls = 0

    async def run_canary() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        await never.wait()
        return first

    state = SelfCheckState(run_canary, timeout_s=60.0)
    kept = await state.run()
    task = asyncio.create_task(state.run())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.latest is kept


async def test_the_state_fails_closed_when_the_runner_raises() -> None:
    state = SelfCheckState(runner_raising(CanaryUnavailable("no sandbox here")))

    report = await state.run()

    assert report.passed is False
    assert state.is_passing is False
    assert state.failed_names == (CANARY_CHECK_NAME,)
