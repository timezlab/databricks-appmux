"""Operations under steady load: the harness of SC-001 (T108). Not part of appmux.

SC-001 says that with steady load on app A, each operation (register B, update B, update A with a
good release, update A with a release that fails its health check, a heavy build in parallel, an
egress rule edit) causes no more failed requests than a no-op control run of the same length, and
that p95 latency before and during is reported (``spec.md``, ``quickstart.md`` section 3).

This is the port of the idea of ``specs/001-appmux-v1/spikes/scripts/spike-07/load_ops.py``: a load
that keeps running while an operation is performed, summarised the way that script's ``summarize``
did (requests, failures and their kinds, p50/p95/p99/max), and compared with a control. Where the
spike put every operation behind a command name inside the sandbox, here an operation is a plain
callable the caller supplies, so the harness knows nothing about appmux: ``Operation("update B",
run=...)``. The load itself is ``http_load.run_load`` (T086), the same paced generator as the
capacity run.

How a run is cut up. The load runs in back-to-back **slices** of ``slice_s`` seconds, each its own
``run_load`` call and so its own ``LoadReport``. A slice is the unit because the generator reports
percentiles per run and a percentile cannot be merged out of two: the harness reports, for a
window of slices, the **worst** slice p95 and the **median** slice p95, and says so in the names.
For each operation:

1. ``before``: ``before_s`` seconds of slices with nothing happening;
2. the operation starts in a worker thread, and slices go on;
3. ``during``: every slice from the start of the operation until it returned, plus ``settle_s``
   more seconds, because what an update does to the old copy (the 30 s drain and its stop) happens
   after the new release is already active.

The control is the same measurement of an operation that does nothing for ``control_s`` seconds.
``allowed_failures`` scales the control's failure count to the length of the window it is compared
with, so a long operation is not held to a short control's count, and a control with no failures
allows none. The judgement (``problems``) is the three things that need no opinion: the operation
itself did not fail, requests were answered while it ran (no answer is not a pass), and the
failures stay within the control's. p95 is reported, never judged: no figure for "room to spare" is
written down.

What this cannot see. A slice boundary closes and reopens the generator's connections, so a
connection that the edge drops exactly at a boundary is not counted; one that breaks inside a slice
is (``interrupted`` in ``http_load``). The generator and the operations share the machine they run
on, so the numbers include their own cost; that is why the control uses the same generator.

Nothing here has run against a container. The offline checks are in ``test_ops_under_load.py``.
"""

from __future__ import annotations

import asyncio
import json
import math
import ssl
import statistics
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Final

from http_load import LoadReport, run_load

DEFAULT_CONNECTIONS: Final = 10
DEFAULT_RATE_PER_S: Final = 500.0
DEFAULT_SLICE_S: Final = 5.0
DEFAULT_BEFORE_S: Final = 10.0
# What follows an update after its release is active: the drain (30 s) and the stop of the old copy.
DEFAULT_SETTLE_S: Final = 35.0
DEFAULT_CONTROL_S: Final = 60.0
# A build can take up to ``build_timeout_s`` (600 s by plan) and then the health wait.
DEFAULT_MAX_OPERATION_S: Final = 900.0
CONTROL_NAME: Final = "control (no-op)"
_ERROR_TEXT_CHARS: Final = 500


@dataclass(frozen=True, slots=True)
class LoadSpec:
    """Where the load goes and how hard.

    Who the generator is: ``email`` when it plays the proxy on the container's own port, or
    ``bearer_token`` (with ``tls``) when it goes through the deployed App's real proxy; exactly one.
    """

    host: str
    port: int
    path: str
    email: str | None = None
    bearer_token: str | None = field(default=None, repr=False)  # a credential: never in a repr
    tls: ssl.SSLContext | None = None
    connections: int = DEFAULT_CONNECTIONS
    rate_per_s: float = DEFAULT_RATE_PER_S
    slice_s: float = DEFAULT_SLICE_S
    before_s: float = DEFAULT_BEFORE_S
    max_operation_s: float = DEFAULT_MAX_OPERATION_S
    edge_pid: int | None = None


@dataclass(frozen=True, slots=True)
class Operation:
    """Something to do while the load runs. ``run`` blocks until the operation has taken effect."""

    name: str
    run: Callable[[], None]
    settle_s: float = DEFAULT_SETTLE_S


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Several slices added up. The p95 figures are of single slices (see the module docstring)."""

    slices: int
    seconds: float
    requests: int
    failures: int
    failure_kinds: list[str]
    worst_slice_p95_ms: float | None
    median_slice_p95_ms: float | None
    edge_cpu_seconds: float | None


@dataclass(frozen=True, slots=True)
class OperationReport:
    name: str
    before: WindowStats
    during: WindowStats
    operation_seconds: float
    operation_error: str | None


@dataclass(frozen=True, slots=True)
class SuiteReport:
    control: OperationReport
    operations: list[OperationReport]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def window_stats(reports: Sequence[LoadReport]) -> WindowStats:
    p95s = [report.latency_ms.p95 for report in reports if report.latency_ms is not None]
    edge_cpu = [r.edge_cpu_seconds for r in reports if r.edge_cpu_seconds is not None]
    return WindowStats(
        slices=len(reports),
        seconds=round(sum(report.seconds for report in reports), 2),
        requests=sum(report.requests for report in reports),
        failures=sum(report.failures for report in reports),
        failure_kinds=sorted({kind for report in reports for kind in report.failure_kinds}),
        worst_slice_p95_ms=max(p95s) if p95s else None,
        median_slice_p95_ms=round(statistics.median(p95s), 2) if p95s else None,
        edge_cpu_seconds=round(sum(edge_cpu), 3) if edge_cpu else None,
    )


def allowed_failures(control: WindowStats, window_seconds: float) -> int:
    """The failures a window of ``window_seconds`` may show: the control's, scaled to its length."""
    if control.failures == 0:
        return 0
    if control.seconds <= 0:
        return control.failures
    return math.ceil(control.failures * window_seconds / control.seconds)


def problems(control: OperationReport, report: OperationReport) -> list[str]:
    """What is wrong with ``report`` against the control; empty when SC-001 holds for it."""
    found: list[str] = []
    if report.operation_error is not None:
        found.append(f"the operation itself failed: {report.operation_error}")
    if report.during.requests == 0:
        found.append("no request was answered while the operation ran")
    allowed = allowed_failures(control.during, report.during.seconds)
    if report.during.failures > allowed:
        found.append(
            f"{report.during.failures} failed requests while the operation ran against {allowed} "
            f"allowed by the control for the same length ({report.during.seconds:g} s); kinds: "
            f"{', '.join(report.during.failure_kinds) or 'none recorded'}"
        )
    return found


class _BackgroundCall:
    """``run`` in a daemon thread: a hung operation must not keep the process from exiting."""

    def __init__(self, run: Callable[[], None]) -> None:
        self._run = run
        self._finished = threading.Event()
        self.error: str | None = None
        self.seconds = 0.0
        self._thread = threading.Thread(target=self._main, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def is_finished(self) -> bool:
        return self._finished.is_set()

    def _main(self) -> None:
        started = time.monotonic()
        try:
            self._run()
        except Exception as exc:
            # The operation's failure is reported in its row and judged by ``problems``; it must
            # not stop the load that other operations are still to be measured against.
            self.error = f"{type(exc).__name__}: {str(exc)[:_ERROR_TEXT_CHARS]}"
        finally:
            self.seconds = time.monotonic() - started
            self._finished.set()


async def _slice(spec: LoadSpec) -> LoadReport:
    return await run_load(
        host=spec.host,
        port=spec.port,
        path=spec.path,
        email=spec.email,
        bearer_token=spec.bearer_token,
        tls=spec.tls,
        connections=spec.connections,
        rate_per_s=spec.rate_per_s,
        duration_s=spec.slice_s,
        edge_pid=spec.edge_pid,
    )


async def measure(spec: LoadSpec, operation: Operation) -> OperationReport:
    """Load before, then load while ``operation`` runs and settles; its failure is reported."""
    before = [await _slice(spec) for _ in range(max(1, math.ceil(spec.before_s / spec.slice_s)))]

    call = _BackgroundCall(operation.run)
    started = time.monotonic()
    call.start()
    during: list[LoadReport] = []
    settle_slices_left = math.ceil(operation.settle_s / spec.slice_s)
    timeout_error: str | None = None
    while True:
        during.append(await _slice(spec))
        if not call.is_finished():
            if time.monotonic() - started > spec.max_operation_s:
                timeout_error = f"still running after {spec.max_operation_s:g} s; the run went on"
                break
            continue
        if settle_slices_left <= 0:
            break
        settle_slices_left -= 1

    return OperationReport(
        name=operation.name,
        before=window_stats(before),
        during=window_stats(during),
        operation_seconds=round(
            call.seconds if call.is_finished() else time.monotonic() - started, 2
        ),
        operation_error=call.error or timeout_error,
    )


async def run_suite_async(
    spec: LoadSpec, operations: Sequence[Operation], *, control_s: float = DEFAULT_CONTROL_S
) -> SuiteReport:
    """The control first, then each operation in the given order, one after the other."""
    control_operation = Operation(CONTROL_NAME, run=lambda: time.sleep(control_s), settle_s=0.0)
    control = await measure(spec, control_operation)
    return SuiteReport(
        control=control, operations=[await measure(spec, operation) for operation in operations]
    )


def run_suite(
    spec: LoadSpec, operations: Sequence[Operation], *, control_s: float = DEFAULT_CONTROL_S
) -> SuiteReport:
    """``run_suite_async`` for a caller that is not inside an event loop (a pytest fixture)."""
    return asyncio.run(run_suite_async(spec, operations, control_s=control_s))
