"""Hostile children against appmux in a Databricks container (T069, US3, FR-011).

A child is untrusted code. Each hostile fixture is registered through the normal path (register,
Update, wait for ``active``) and then asked for its ``/report``, which says what the child managed
to do from inside its sandbox. Nothing reaches the sandbox but the release: the owner decided that
the container tests have no entry of their own (T069's "test-only entry that calls the launcher" is
the registry path here).

* ``env_snooper``: no other process's environment and not the platform's ``runtime.cfg`` can be
  opened (``isolated``);
* ``pkg_env_writer``: no file can be created in the package environment (``EROFS``), in
  site-packages and in the overlay. The fixture pins one wheel so its release has an overlay
  (``scripts/run-remote-tests.sh`` builds the wheel into the staged copy);
* ``fork_bomb``: forking stops with ``EAGAIN`` at the platform's ``max_processes_per_child``,
  well before the fixture's own ceiling;
* ``network_scanner``: the metadata address cannot be reached, and a destination that the app's
  own egress rule denies is reached before the rule and not after it. The second needs a control:
  a deny that nothing could reach anyway proves nothing. A rule is read when a child starts (the
  live reload of a running pump is not wired, T066), so the rule goes in and a new release starts
  a new child. Two of its targets are *not* covered here:

  * ``appmux_loopback``: every child listens on port 8000 inside its own network namespace
    (``DEFAULT_CHILD_PORT``) and the platform gives appmux port 8000 as well, so a connect to
    ``127.0.0.1:8000`` reaches the child itself. The first run of this test configured it, it
    connected, and that was the cause, not a hole in the sandbox. The canary of ``self_check``
    has no listener of its own, so its ``loopback_and_container_ip`` check (T068) is the proof;
  * ``container_ip``: the container's address is not known to a caller outside the container
    (T068's check covers its ports from inside a sandbox);
* **a neighbour keeps its latency**: a ``light_fastapi`` app is asked again and again while the
  fork bomb and the scanner run, and every answer is ``200`` with a p95 within a stated tolerance
  of its own idle p95. The tolerance is wide on purpose: the caller is remote, so the number
  carries the proxy's jitter; what the test refuses is errors and a latency that multiplies.

It does not run in the default gate: every test here is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``. Run it with ``scripts/run-remote-tests.sh``; the setup is that of
``ops_session.py``. Everything the run registers is deleted afterwards.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from collections.abc import Iterator
from types import TracebackType
from typing import Any, Final, Self

import httpx
import pytest
from ops_session import (
    REQUEST_TIMEOUT_S,
    AdminSession,
    Target,
    config_body,
    egress_body,
    identity_headers,
    load_target,
)

pytestmark = pytest.mark.integration

SLUG_SUFFIX_BYTES: Final = 3
# The p95 of the neighbour while hostile children run may be this many times its idle p95, plus
# a fixed allowance for the jitter of a remote caller (a proxy hop is tens of milliseconds).
P95_FACTOR: Final = 3.0
P95_ALLOWANCE_S: Final = 0.5
NEIGHBOUR_IDLE_S: Final = 8.0
NEIGHBOUR_PAUSE_S: Final = 0.1
MIN_NEIGHBOUR_SAMPLES: Final = 10
# The hostile reports are asked this many times in turn, so the window the neighbour is judged
# in holds enough requests to have a p95 even through a remote proxy.
HOSTILE_ROUNDS: Final = 3
# What the scanner reports as not configured when a test sets none of its variables: the metadata
# test names it so a target cannot start or stop being tried without this file saying so.
SCANNER_TARGETS_NOT_SET: Final = frozenset(
    {"appmux_loopback", "container_ip", "denied_destination"}
)
# A public address that a container on Free Edition is expected to reach (a TCP service of a
# public resolver); the control step of the deny test fails loudly when it is not reached.
DENIED_HOST: Final = "1.1.1.1"
DENIED_PORT: Final = 443


@pytest.fixture(scope="module")
def target() -> Target:
    return load_target()


@pytest.fixture
def session(target: Target) -> Iterator[AdminSession]:
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        opened = AdminSession(client, target)
        try:
            yield opened
        finally:
            opened.delete_created_apps()


def _suffix() -> str:
    return secrets.token_hex(SLUG_SUFFIX_BYTES)


def _start(session: AdminSession, slug: str, fixture: str) -> None:
    """Register ``fixture`` as ``slug`` and wait until its first release runs."""
    session.register(slug, fixture=fixture)
    session.wait_for_release(slug, session.press_update(slug), "active")
    session.wait_for_state(slug, "running")


def _new_release_with_env(session: AdminSession, slug: str, env: list[dict[str, Any]]) -> None:
    """Set the app's env entries; that makes a release, whose child is started afresh."""
    body = config_body(config=session.app(slug)["config"], env=env)
    release_no = int(session.call("PUT", f"/apps/{slug}/config", json=body).json()["release_no"])
    session.wait_for_release(slug, release_no, "active")
    session.wait_for_state(slug, "running")


def _report(session: AdminSession, slug: str, fixture: str) -> dict[str, Any]:
    answer = session.open_app(slug, "/report")
    assert answer.status_code == 200, f"{fixture}/report answered {answer.status_code}"
    report: dict[str, Any] = answer.json()
    assert report["fixture"] == fixture
    return report


def _p95(durations_s: list[float]) -> float:
    ordered = sorted(durations_s)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


class NeighbourProbe:
    """Asks a neighbour's route again and again from a thread; keeps each status and duration.

    A transport failure is recorded as status 0 rather than raised in the thread: the test then
    fails on it where it can name the neighbour.
    """

    def __init__(self, target: Target, slug: str) -> None:
        self._target = target
        self._slug = slug
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.statuses: list[int] = []
        self.durations_s: list[float] = []

    def _run(self) -> None:
        with httpx.Client(base_url=self._target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
            while not self._stopped.is_set():
                started = time.monotonic()
                try:
                    status = client.get(
                        f"/{self._slug}/", headers=identity_headers(self._target)
                    ).status_code
                except httpx.HTTPError:
                    status = 0
                self.durations_s.append(time.monotonic() - started)
                self.statuses.append(status)
                self._stopped.wait(NEIGHBOUR_PAUSE_S)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stopped.set()
        self._thread.join(timeout=REQUEST_TIMEOUT_S)


def test_env_snooper_opens_no_foreign_environment_and_no_platform_secret(
    session: AdminSession,
) -> None:
    slug = f"zh-env-{_suffix()}"
    _start(session, slug, "env_snooper")

    report = _report(session, slug, "env_snooper")

    print(
        f"env_snooper: proc {report['proc_listing']}, pid 1 {report['pid1_environ_open']}, "
        f"runtime.cfg {report['runtime_cfg_open']}, {len(report['visible_pids'])} pids visible"
    )
    assert report["foreign_environ_opened"] == []
    assert report["pid1_environ_open"] != "opened"
    assert report["runtime_cfg_open"] != "opened"
    assert report["isolated"] is True


def test_pkg_env_writer_cannot_create_a_file_in_the_package_environment(
    session: AdminSession,
) -> None:
    slug = f"zh-pkg-{_suffix()}"
    _start(session, slug, "pkg_env_writer")

    report = _report(session, slug, "pkg_env_writer")

    results = sorted({attempt["result"] for attempt in report["attempts"]})
    print(f"pkg_env_writer: {len(report['attempts'])} attempts, results {results}")
    assert report["created"] == []
    assert report["site_packages_all_erofs"] is True
    assert report["overlay_probed"] is True, "the release has no overlay, so half is unproven"
    assert report["overlay_all_erofs"] is True


def test_fork_bomb_stops_at_the_process_limit_of_the_platform(session: AdminSession) -> None:
    limit = session.read("/platform")["settings"]["max_processes_per_child"]
    slug = f"zh-fork-{_suffix()}"
    _start(session, slug, "fork_bomb")

    report = _report(session, slug, "fork_bomb")

    print(
        f"fork_bomb: {report['forks_succeeded']} forks of {limit}, refused with "
        f"{report['refused_with']} after {report['elapsed_s']} s"
    )
    assert report["stopped_by_ceiling"] is False, "no process limit was enforced"
    assert report["refused_with"] == "EAGAIN"
    assert report["rlimit_nproc_soft"] == limit
    assert 0 < report["forks_succeeded"] < limit
    assert session.app(slug)["state"] == "running", "the child must survive its own fork bomb"


def test_network_scanner_cannot_reach_the_metadata_address(session: AdminSession) -> None:
    slug = f"zh-net-{_suffix()}"
    _start(session, slug, "network_scanner")

    report = _report(session, slug, "network_scanner")

    outcomes = {attempt["label"]: attempt["result"] for attempt in report["attempts"]}
    print(f"network_scanner: {outcomes}")
    assert report["connected"] == []
    assert outcomes.get("metadata") not in {None, "connected"}, "the metadata address was not tried"
    assert set(report["not_configured"]) == SCANNER_TARGETS_NOT_SET


def test_a_deny_rule_of_the_app_stops_a_destination_that_the_control_reaches(
    session: AdminSession,
) -> None:
    slug = f"zh-deny-{_suffix()}"
    destination = f"{DENIED_HOST}:{DENIED_PORT}"
    env = [{"name": "SCANNER_DENIED_DESTINATION", "value": destination, "value_from": None}]
    _start(session, slug, "network_scanner")
    _new_release_with_env(session, slug, env)

    control = _report(session, slug, "network_scanner")

    assert destination in control["connected"], (
        f"the control did not reach {destination} ({control['attempts']}), so a deny rule on it "
        "would prove nothing: egress from this container is closed or the address is not served"
    )

    rule = {"action": "deny", "cidr": f"{DENIED_HOST}/32", "port": str(DENIED_PORT), "comment": ""}
    version = session.app(slug)["version"]
    session.call("PUT", f"/apps/{slug}/egress", json=egress_body(version=version, rules=[rule]))
    # The same env makes no release, so one value changes: the new release starts a new child,
    # which reads the rule (the scanner ignores the extra variable).
    changed_env = [*env, {"name": "SCANNER_RUN", "value": "with-rule", "value_from": None}]
    _new_release_with_env(session, slug, changed_env)

    ruled = _report(session, slug, "network_scanner")

    outcomes = {attempt["label"]: attempt["result"] for attempt in ruled["attempts"]}
    print(f"deny rule: before {control['connected']}, after {ruled['connected']}, {outcomes}")
    assert ruled["connected"] == []
    assert outcomes.get("denied_destination") not in {None, "connected"}


def test_a_neighbour_keeps_its_latency_while_hostile_children_run(
    session: AdminSession,
) -> None:
    suffix = _suffix()
    neighbour, forker, scanner = f"zh-ok-{suffix}", f"zh-fork-{suffix}", f"zh-net-{suffix}"
    _start(session, neighbour, "light_fastapi")
    _start(session, forker, "fork_bomb")
    _start(session, scanner, "network_scanner")

    with NeighbourProbe(session.target, neighbour) as probe:
        time.sleep(NEIGHBOUR_IDLE_S)
        idle_samples = len(probe.durations_s)
        for _ in range(HOSTILE_ROUNDS):
            _report(session, forker, "fork_bomb")
            _report(session, scanner, "network_scanner")
    idle = probe.durations_s[:idle_samples], probe.statuses[:idle_samples]
    busy = probe.durations_s[idle_samples:], probe.statuses[idle_samples:]

    assert len(idle[0]) >= MIN_NEIGHBOUR_SAMPLES and len(busy[0]) >= MIN_NEIGHBOUR_SAMPLES, (
        f"too few samples to judge a p95 ({len(idle[0])} idle, {len(busy[0])} busy)"
    )
    idle_p95, busy_p95 = _p95(idle[0]), _p95(busy[0])
    print(
        f"neighbour p95: idle {idle_p95 * 1000:.0f} ms ({len(idle[0])} requests), "
        f"with hostile children {busy_p95 * 1000:.0f} ms ({len(busy[0])} requests)"
    )
    assert set(idle[1]) == {200}, f"the idle neighbour answered {sorted(set(idle[1]))}"
    assert set(busy[1]) == {200}, f"the neighbour answered {sorted(set(busy[1]))} while hostile"
    assert busy_p95 <= idle_p95 * P95_FACTOR + P95_ALLOWANCE_S
