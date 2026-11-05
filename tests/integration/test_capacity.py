"""Capacity with 10 simulated apps, and the 11th refused, on a real container (T162, SC-003).

The run registers ten fixtures, a mix of the four well-behaved kinds (``light_fastapi``,
``memory_fastapi``, ``streamlit_app``, ``node_app``; see ``tests/fixtures/README.md``), presses
Update on all of them, waits until each one runs, and measures what the slot spends:

* RSS per app and per sandbox, idle and under load (every app gets paced requests for a while);
* the overhead of a sandbox: what its processes hold beyond the app's own (the supervisor with the
  egress pump, ``slirp4netns``, the relay);
* the start time of each release (Update pressed until the release is ``active``);
* CPU: each sandbox's and the edge's, in percent of one core, idle and under load;
* the total resident memory of everything appmux runs against the slot's limit and the memory
  ledger (``GET /platform``), and a linear estimate for 100 apps.

Then the hard condition of SC-003: the platform's ``memory_reserve_mb`` is set so that the slot
holds exactly what is running and an 11th app, a ``light_fastapi`` of the smallest budget, no longer
fits. Its Update must end in a ``failed`` release whose reason says there is not enough memory, its
route must stay closed, and the ten apps must keep answering. The setting is put back afterwards and
every app of the run is deleted.

Everything is printed as ``capacity report: {...}`` JSON lines, to be copied under a dated "plan
checks" heading in ``specs/001-appmux-v1/spikes/README.md``. The numbers are measurements of one
container, not limits: the test asserts only what needs no judgement (every app starts and answers,
nothing fails under load, every figure was measured, the 11th is refused) and leaves "is it enough"
to the person who reads the report. The 100-app figure is a straight multiplication, printed as an
estimate and never asserted.

The apps already registered on the App (a development App hosts demos) run beside these ten and
count in the ledger; the report says how many. "The budget set for 10 apps" therefore means the
ledger holds these ten and whatever else runs, with less free than one more app needs. If the
ledger has no room for the ten when the run starts (the default ``memory_reserve_mb`` of 1024 MB and
a handful of demos leave less than the ten need once each app is charged ``OVERHEAD_PER_CHILD_MB``),
the run lowers the reserve first, says so in the report, and puts the original value back at the
end; it never drops an app that was already there.

It reads ``/proc`` of the container, so it runs only inside it (``scripts/run-container-tests.sh``);
it is marked ``integration`` and skipped unless ``APPMUX_TEST_CONTAINER=1``. It needs what
``ops_session.load_target`` reads and ``APPMUX_TEST_EDGE_PID``, which the container runner sets.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from ops_session import REQUEST_TIMEOUT_S, AdminSession, Target, load_target, register_body
from proc_view import (
    CLOCK_TICKS_PER_S,
    cpu_ticks,
    parent_of,
    processes_of_apps,
    resident_bytes,
    to_mb,
    top_below,
)

from appmux.metrics.sources import read_memory_used_mb
from appmux.reconciler.admission import OVERHEAD_PER_CHILD_MB
from appmux.sandbox.watchdog import process_tree_pids, process_tree_rss_bytes

pytestmark = pytest.mark.integration

LOAD_SCRIPT: Final = Path(__file__).resolve().parents[1] / "load" / "http_load.py"
SLUG_SUFFIX_BYTES: Final = 3


@dataclass(frozen=True, slots=True)
class Kind:
    """One of the four well-behaved kinds, with the budget it is registered with."""

    fixture: str
    budget_mb: int
    copies: int


# Budgets: the platform's floor for a new app (256 MB) for the small Python apps, 512 MB for the
# ones that hold a framework, and the measured minimum of Node.js
# (``runtime_limits.NODE_MIN_MEMORY_MB``).
KINDS: Final = (
    Kind("light_fastapi", 256, 4),
    Kind("memory_fastapi", 512, 2),
    Kind("streamlit_app", 512, 2),
    Kind("node_app", 768, 2),
)
SMALLEST_BUDGET_MB: Final = 256
# What stays free after the ten: below one more small app (256 + OVERHEAD_PER_CHILD_MB).
FREE_AFTER_TEN_MB: Final = 100

UPDATE_WAIT_S: Final = 600.0
RUNNING_WAIT_S: Final = 180.0
IDLE_SETTLE_S: Final = 20.0
IDLE_WINDOW_S: Final = 20.0
LOAD_SECONDS: Final = 20.0
LOAD_RATE_PER_APP: Final = 30.0
LOAD_CONNECTIONS_PER_APP: Final = 2
LOAD_SAMPLE_EVERY_S: Final = 1.0
LOAD_GRACE_S: Final = 90.0
ESTIMATED_APPS: Final = 100
PERCENT: Final = 100
EXPECTED_STARTED: Final = sum(kind.copies for kind in KINDS)


# --- reading /proc ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxReading:
    """One reading of one sandbox: its processes' RSS, the app's share of it, and CPU ticks."""

    sandbox_rss_mb: float
    app_rss_mb: float
    cpu_ticks_by_pid: dict[int, int]


def read_sandboxes(edge_pid: int, slugs: set[str]) -> dict[str, SandboxReading]:
    """For each slug, the sandbox below ``edge_pid`` that runs it.

    A sandbox is the subtree of one direct child of the edge (its supervisor). The app's own
    processes are those whose environment names the slug; the rest of the subtree is the sandbox's
    overhead. A slug no process was found for is left out, and the caller treats that as a failure.
    """
    parents = {pid: parent_of(pid) for pid in process_tree_pids(edge_pid)}
    app_pids = processes_of_apps(edge_pid, slugs)
    readings: dict[str, SandboxReading] = {}
    for slug, pids in app_pids.items():
        supervisor = top_below(pids[0], parents, edge_pid)
        tree = process_tree_pids(supervisor)
        readings[slug] = SandboxReading(
            sandbox_rss_mb=to_mb(process_tree_rss_bytes(supervisor)),
            app_rss_mb=to_mb(sum(resident_bytes(pid) for pid in pids)),
            cpu_ticks_by_pid={pid: ticks for pid in tree if (ticks := cpu_ticks(pid)) is not None},
        )
    return readings


def cpu_percent(before: dict[int, int], after: dict[int, int], seconds: float) -> float:
    """Percent of one core used between two readings, over the processes present in both."""
    ticks = sum(after[pid] - before[pid] for pid in before.keys() & after.keys())
    return round(ticks / CLOCK_TICKS_PER_S / seconds * PERCENT, 1)


def edge_cpu_ticks(edge_pid: int) -> int:
    ticks = cpu_ticks(edge_pid)
    assert ticks is not None, f"the edge process {edge_pid} is gone"
    return ticks


# --- the run ---------------------------------------------------------------------------------


@dataclass(slots=True)
class AppRun:
    slug: str
    kind: str
    budget_mb: int
    start_seconds: float | None = None
    idle: SandboxReading | None = None
    idle_cpu_percent: float | None = None
    loaded_peak_sandbox_mb: float = 0.0
    loaded_peak_app_mb: float = 0.0
    loaded_cpu_percent: float | None = None
    load_report: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CapacityRun:
    target: Target
    session: AdminSession
    edge_pid: int
    apps: list[AppRun]
    edge_rss_mb: float
    edge_idle_cpu_percent: float
    edge_loaded_cpu_percent: float
    other_running_apps: int
    ledger_with_ten: dict[str, int]
    process_tree_rss_mb: float
    container_used_mb: int | None
    original_reserve_mb: int


def _edge_pid() -> int:
    raw = os.environ.get("APPMUX_TEST_EDGE_PID", "").strip()
    if not raw.isdecimal():
        pytest.fail("set APPMUX_TEST_EDGE_PID for the capacity run (see the module docstring)")
    return int(raw)


def _register(session: AdminSession, kind: Kind, index: int) -> AppRun:
    slug = f"it-cap-{kind.fixture.split('_')[0][:5]}-{index}-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
    folder = f"{session.target.fixtures_folder}/{kind.fixture}"
    body = register_body(slug=slug, name=slug, folder=folder, owner=session.admin)
    body["memory_budget_mb"] = kind.budget_mb
    session.call("POST", "/apps", json=body, expect=(200, 201), with_token=True)
    session.created_apps.append(slug)
    return AppRun(slug=slug, kind=kind.fixture, budget_mb=kind.budget_mb)


def _platform(session: AdminSession) -> dict[str, Any]:
    page: dict[str, Any] = session.read("/platform")
    return page


def _running_count(session: AdminSession) -> int:
    apps = session.read("/apps")
    items = apps["items"] if isinstance(apps, dict) else apps
    return sum(1 for app in items if app.get("state") == "running")


def _load(target: Target, apps: list[AppRun], edge_pid: int) -> None:
    """Pace requests at every app at once, sample the sandboxes while it lasts, fill ``apps``."""
    slugs = {app.slug for app in apps}
    processes = {
        app.slug: subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(LOAD_SCRIPT),
                "--host",
                target.host,
                "--port",
                str(target.port),
                "--path",
                f"/{app.slug}/",
                "--email",
                target.admin_email,
                "--connections",
                str(LOAD_CONNECTIONS_PER_APP),
                "--rate",
                str(LOAD_RATE_PER_APP),
                "--duration",
                str(LOAD_SECONDS),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for app in apps
    }
    before = read_sandboxes(edge_pid, slugs)
    started = time.monotonic()
    last = before
    while time.monotonic() - started < LOAD_SECONDS:
        time.sleep(LOAD_SAMPLE_EVERY_S)
        last = read_sandboxes(edge_pid, slugs)
        for app in apps:
            reading = last.get(app.slug)
            if reading is not None:
                app.loaded_peak_sandbox_mb = max(app.loaded_peak_sandbox_mb, reading.sandbox_rss_mb)
                app.loaded_peak_app_mb = max(app.loaded_peak_app_mb, reading.app_rss_mb)
    seconds = time.monotonic() - started
    for app in apps:
        if app.slug in before and app.slug in last:
            app.loaded_cpu_percent = cpu_percent(
                before[app.slug].cpu_ticks_by_pid, last[app.slug].cpu_ticks_by_pid, seconds
            )
    for app in apps:
        output, errors = processes[app.slug].communicate(timeout=LOAD_SECONDS + LOAD_GRACE_S)
        assert processes[app.slug].returncode == 0, f"load on {app.slug} failed: {errors.strip()}"
        app.load_report = json.loads(output.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def capacity() -> Iterator[CapacityRun]:
    target = load_target()
    edge_pid = _edge_pid()
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        session = AdminSession(client, target)
        original_reserve_mb = int(_platform(session)["settings"]["memory_reserve_mb"])
        try:
            yield _run(session, target, edge_pid, original_reserve_mb)
        finally:
            _restore_reserve_and_delete_apps(session, original_reserve_mb)


def _restore_reserve_and_delete_apps(session: AdminSession, original_reserve_mb: int) -> None:
    """Put the reserve back, then delete the ten apps, whether or not the PUT worked.

    The apps go in a ``finally``: an expired token fails the PUT, and ten running apps would
    otherwise keep 5 GB of the slot. What this cannot cover is the run being killed (or the App
    restarted by ``run-container-tests.sh``) before it gets here: the lowered reserve then stays,
    and the ``reserve_lowered_for_the_run`` line of the output names the value to put back.
    """
    try:
        _put_reserve(session, original_reserve_mb)
    finally:
        session.delete_created_apps()


def _reserve_that_leaves_room_for_ten(ledger: dict[str, int], current_reserve_mb: int) -> int:
    """The reserve to set so the ledger can admit all ten, or ``current_reserve_mb`` if it can.

    ``free_mb`` is the limit minus the reserve minus what is committed, so lowering the reserve by
    the shortfall raises ``free_mb`` by the same amount. ``FREE_AFTER_TEN_MB`` is added to the need
    so a small drift of the ledger between this reading and the last registration does not turn
    into a refusal that looks like a product fault.
    """
    needed_mb = (
        sum((kind.budget_mb + OVERHEAD_PER_CHILD_MB) * kind.copies for kind in KINDS)
        + FREE_AFTER_TEN_MB
    )
    shortfall_mb = needed_mb - ledger["free_mb"]
    if shortfall_mb <= 0:
        return current_reserve_mb
    lowered_mb = current_reserve_mb - shortfall_mb
    assert lowered_mb >= 1, (
        f"the ten need {needed_mb} MB but only {ledger['free_mb']} MB are free, and the reserve "
        f"({current_reserve_mb} MB) is too small to make up {shortfall_mb} MB: the apps already on "
        f"this App hold {ledger['committed_mb']} MB of {ledger['limit_mb']} MB; delete some first"
    )
    return lowered_mb


def _put_reserve(session: AdminSession, value_mb: int) -> None:
    # Platform settings are written under the admin's own role, so the token goes with the call.
    session.call(
        "PUT", "/platform/settings/memory_reserve_mb", json={"value": value_mb}, with_token=True
    )


def _run(
    session: AdminSession, target: Target, edge_pid: int, original_reserve_mb: int
) -> CapacityRun:
    other_running = _running_count(session)
    ledger_before = _platform(session)["memory"]
    reserve_for_ten_mb = _reserve_that_leaves_room_for_ten(ledger_before, original_reserve_mb)
    if reserve_for_ten_mb != original_reserve_mb:
        _put_reserve(session, reserve_for_ten_mb)
        _say(
            "reserve_lowered_for_the_run",
            {
                "from_mb": original_reserve_mb,
                "to_mb": reserve_for_ten_mb,
                "ledger_before": ledger_before,
                "charged_per_app_beyond_its_budget_mb": OVERHEAD_PER_CHILD_MB,
            },
        )
    apps = [_register(session, kind, index) for kind in KINDS for index in range(kind.copies)]
    assert len(apps) == EXPECTED_STARTED

    pressed: dict[str, tuple[int, float]] = {}
    for app in apps:
        pressed[app.slug] = (session.press_update(app.slug), time.monotonic())
    for app in apps:
        release_no, pressed_at = pressed[app.slug]
        session.wait_for_release(app.slug, release_no, "active", wait_s=UPDATE_WAIT_S)
        app.start_seconds = round(time.monotonic() - pressed_at, 1)
        session.wait_for_state(app.slug, "running", wait_s=RUNNING_WAIT_S)
        answer = session.open_app(app.slug)
        assert answer.status_code == 200, f"{app.slug} ({app.kind}) answered {answer.status_code}"

    slugs = {app.slug for app in apps}
    time.sleep(IDLE_SETTLE_S)
    idle_before = read_sandboxes(edge_pid, slugs)
    edge_before = edge_cpu_ticks(edge_pid)
    time.sleep(IDLE_WINDOW_S)
    idle_after = read_sandboxes(edge_pid, slugs)
    edge_idle = edge_cpu_ticks(edge_pid)
    missing = slugs - idle_after.keys()
    assert not missing, (
        f"no process of {sorted(missing)} was found below the edge (pid {edge_pid}) by its "
        "DATABRICKS_APP_NAME; the sandbox is not where this test looks"
    )
    for app in apps:
        app.idle = idle_after[app.slug]
        app.idle_cpu_percent = cpu_percent(
            idle_before[app.slug].cpu_ticks_by_pid,
            idle_after[app.slug].cpu_ticks_by_pid,
            IDLE_WINDOW_S,
        )
    edge_idle_cpu = round(
        (edge_idle - edge_before) / CLOCK_TICKS_PER_S / IDLE_WINDOW_S * PERCENT, 1
    )

    ledger = _platform(session)["memory"]
    edge_loaded_start = edge_cpu_ticks(edge_pid)
    load_started = time.monotonic()
    _load(target, apps, edge_pid)
    load_seconds = time.monotonic() - load_started
    edge_loaded_cpu = round(
        (edge_cpu_ticks(edge_pid) - edge_loaded_start) / CLOCK_TICKS_PER_S / load_seconds * PERCENT,
        1,
    )
    return CapacityRun(
        target=target,
        session=session,
        edge_pid=edge_pid,
        apps=apps,
        edge_rss_mb=to_mb(resident_bytes(edge_pid)),
        edge_idle_cpu_percent=edge_idle_cpu,
        edge_loaded_cpu_percent=edge_loaded_cpu,
        other_running_apps=other_running,
        ledger_with_ten=ledger,
        process_tree_rss_mb=to_mb(process_tree_rss_bytes(edge_pid)),
        container_used_mb=read_memory_used_mb(),
        original_reserve_mb=original_reserve_mb,
    )


def _say(label: str, document: dict[str, Any]) -> None:
    print(f"capacity report: {json.dumps({label: document})}")


# --- the report and what needs no judgement ---------------------------------------------------


def test_ten_apps_of_four_kinds_start_answer_and_hold_under_load(capacity: CapacityRun) -> None:
    for app in capacity.apps:
        assert app.load_report["failures"] == 0, (
            f"{app.slug} ({app.kind}): {app.load_report['failures']} of "
            f"{app.load_report['requests']} requests failed under load "
            f"({app.load_report['failure_kinds']})"
        )
        assert app.load_report["requests"] > 0, f"{app.slug} received no load"
    kinds = {app.kind for app in capacity.apps}
    assert kinds == {kind.fixture for kind in KINDS}


def test_the_report_gives_every_figure_of_sc_003(capacity: CapacityRun) -> None:
    per_app = []
    for app in capacity.apps:
        assert app.idle is not None and app.start_seconds is not None
        overhead_mb = round(app.idle.sandbox_rss_mb - app.idle.app_rss_mb, 1)
        per_app.append(
            {
                "kind": app.kind,
                "budget_mb": app.budget_mb,
                "start_seconds": app.start_seconds,
                "idle_app_rss_mb": app.idle.app_rss_mb,
                "idle_sandbox_rss_mb": app.idle.sandbox_rss_mb,
                "sandbox_overhead_mb": overhead_mb,
                "loaded_peak_app_rss_mb": app.loaded_peak_app_mb,
                "loaded_peak_sandbox_rss_mb": app.loaded_peak_sandbox_mb,
                "idle_cpu_percent_of_one_core": app.idle_cpu_percent,
                "loaded_cpu_percent_of_one_core": app.loaded_cpu_percent,
                "loaded_requests_per_second": app.load_report["per_second"],
                "loaded_p95_ms": (app.load_report.get("latency_ms") or {}).get("p95"),
            }
        )
        assert app.idle.app_rss_mb > 0, f"{app.slug}: no resident memory measured for the app"
        assert overhead_mb > 0, f"{app.slug}: the sandbox shows no overhead beyond the app"
        assert app.loaded_peak_sandbox_mb > 0
        assert app.loaded_cpu_percent is not None
    _say("apps", {"per_app": per_app})

    overheads = [item["sandbox_overhead_mb"] for item in per_app]
    mean_overhead_mb = round(sum(overheads) / len(overheads), 1)
    loaded_total_mb = round(sum(app.loaded_peak_sandbox_mb for app in capacity.apps), 1)
    limit_mb = capacity.ledger_with_ten["limit_mb"]
    mean_sandbox_mb = loaded_total_mb / len(capacity.apps)
    estimate_mb = round(capacity.edge_rss_mb + ESTIMATED_APPS * mean_sandbox_mb)
    _say(
        "slot",
        {
            "other_apps_running_beside_the_ten": capacity.other_running_apps,
            "edge_rss_mb": capacity.edge_rss_mb,
            "edge_cpu_percent_of_one_core": {
                "idle": capacity.edge_idle_cpu_percent,
                "loaded": capacity.edge_loaded_cpu_percent,
            },
            "mean_sandbox_overhead_mb": mean_overhead_mb,
            "ten_apps_loaded_peak_sandbox_rss_total_mb": loaded_total_mb,
            "everything_appmux_runs_rss_mb": capacity.process_tree_rss_mb,
            "container_memory_in_use_mb": capacity.container_used_mb,
            "ledger": capacity.ledger_with_ten,
            "overhead_assumed_by_admission_mb": OVERHEAD_PER_CHILD_MB,
            "ESTIMATE_100_apps_rss_mb": {
                "value": estimate_mb,
                "basis": "edge RSS + 100 x the mean loaded sandbox RSS of these ten; linear, "
                "not measured",
                "against_limit_mb": limit_mb,
            },
        },
    )
    assert capacity.process_tree_rss_mb < limit_mb, (
        f"everything appmux runs holds {capacity.process_tree_rss_mb} MB, over the slot's "
        f"{limit_mb} MB"
    )
    assert capacity.ledger_with_ten["free_mb"] >= 0, "ten apps over-commit the slot's ledger"
    committed_by_ten = sum(app.budget_mb + OVERHEAD_PER_CHILD_MB for app in capacity.apps)
    assert capacity.ledger_with_ten["committed_mb"] >= committed_by_ten


def test_the_eleventh_app_is_refused_with_a_clear_reason(capacity: CapacityRun) -> None:
    session = capacity.session
    ledger = _platform(session)["memory"]
    reserve_mb = ledger["limit_mb"] - ledger["committed_mb"] - FREE_AFTER_TEN_MB
    assert reserve_mb >= 1, (
        f"the slot is too small to set a reserve that leaves {FREE_AFTER_TEN_MB} MB free "
        f"(limit {ledger['limit_mb']} MB, committed {ledger['committed_mb']} MB)"
    )
    _put_reserve(session, reserve_mb)
    after = _platform(session)["memory"]
    assert after["free_mb"] == FREE_AFTER_TEN_MB, f"the ledger after the new reserve: {after}"
    _say(
        "eleventh",
        {"ledger_before_it": after, "needs_mb": SMALLEST_BUDGET_MB + OVERHEAD_PER_CHILD_MB},
    )

    first = capacity.apps[0]
    slug = f"it-cap-eleventh-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
    folder = f"{session.target.fixtures_folder}/light_fastapi"
    body = register_body(slug=slug, name=slug, folder=folder, owner=session.admin)
    body["memory_budget_mb"] = SMALLEST_BUDGET_MB
    session.call("POST", "/apps", json=body, expect=(200, 201), with_token=True)
    session.created_apps.append(slug)

    release_no = session.press_update(slug)
    release = session.wait_for_release(slug, release_no, "failed", wait_s=UPDATE_WAIT_S)
    reason = release["status_reason"] or ""
    _say("eleventh", {"release_status": release["status"], "reason": reason})
    assert "not enough memory" in reason, f"the refusal does not say why: {reason!r}"
    assert "MB free" in reason, f"the refusal does not say how much was free: {reason!r}"
    assert session.open_app(slug).status_code == 503, "the refused app has a route"
    assert session.app(slug)["state"] != "running"
    for app in capacity.apps:
        assert session.open_app(app.slug).status_code == 200, (
            f"{app.slug} stopped answering when the 11th was refused"
        )
    assert session.open_app(first.slug).status_code == 200
