"""SC-001, no disruption, against a running appmux in a Databricks container (T108, US2).

With steady load on app A (500 requests a second over 10 connections, paced, the load of the
capacity run; ``APPMUX_TEST_RATE_PER_S`` lowers it), each of these operations must cause no more
failed requests than a no-op control run of the same length, and p95 before and during must be
reported:

1. register app B;
2. update B;
3. update A with a good release (A moves from the ``light_fastapi`` fixture to ``slow_start``);
4. update A with a release that fails its health check (``always_500``): A keeps serving, the
   release is ``failed``;
5. a heavy build in parallel (three Updates of the ``wheels_only`` fixture, whose build installs
   wheels into an overlay; the fixtures hold no source distribution with a C extension, which was
   what spike 7 built, so the parallel builds are the heaviest build they have);
6. an egress rule edit on A.

A new release of A needs a different snapshot (an Update of an unchanged folder is "already up to
date" and makes none), and this test cannot write to the workspace folder. So the operations point
A's ``source_path`` at another fixture with ``PATCH /apps/{app}`` (a platform-admin write, which is
why it carries the admin's token) before they press Update, which reads the folder. That makes the
swap of operation 3 a real replacement of one app by a different one, and operation 4 a release of
an app that listens and answers ``500`` everywhere (design 5.5: health is a GET that answers below
500).

The harness is ``tests/load/ops_under_load.py``: slices of load, the operation in a worker thread,
a control that does nothing. Three things are asserted per operation (no operation raised, requests
were answered, failures within the control's); p95 is printed as JSON and not asserted, because no
figure for "room to spare" is written down. Read the printed report (``pytest -s``) and record it
under a dated "plan checks" heading in ``specs/001-appmux-v1/spikes/README.md``.

The load generator runs in this process. Inside the container it goes to the edge's own port and
plays the Databricks proxy by sending ``X-Forwarded-Email`` itself. From another machine
(``scripts/run-remote-tests.sh``, ``APPMUX_TEST_REMOTE=1``) it goes over TLS through the deployed
App's real proxy with the bearer token, so the latencies include the proxy and the network, and a
remote run sets ``APPMUX_TEST_RATE_PER_S`` well below 500: that run is not evidence for SC-001. It
does not run in the default gate: every test that needs appmux is marked
``integration`` and skipped unless ``APPMUX_TEST_CONTAINER=1``; ``scripts/sync-deploy.sh`` puts the
code on a container. It has run only as a reduced remote run on the Free Edition dev App
(2026-11-01, 20 requests a second, spikes README), so no claim that SC-001 holds exists. Update,
the swap and the egress edit must all be mounted for the run to get past its set-up; a step whose
route is missing fails with the status it got.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``: where the edge listens, for example ``http://127.0.0.1:8000`` (a
  remote run: the App's https address);
* ``APPMUX_TEST_ADMIN_EMAIL``: a platform admin, who owns every app the run registers (a remote run
  learns it from the App);
* ``APPMUX_TEST_USER_TOKEN``: that person's Databricks token (Update reads the workspace with it
  and ``PATCH /apps/{app}`` writes as the admin with it); it is read from the environment, sent
  only as a header and never printed;
* ``APPMUX_TEST_FIXTURES_FOLDER``: a workspace folder starting with ``/Workspace/`` that holds a
  copy of ``tests/fixtures/`` (``light_fastapi``, ``slow_start``, ``always_500``) and a copy of
  ``wheels_only`` whose ``wheels/`` folder is built by
  ``python tests/fixtures/wheels_only/make_wheels.py <copy>/wheels``;
* optionally ``APPMUX_TEST_RATE_PER_S`` (requests a second, 500 by default),
  ``APPMUX_TEST_EDGE_PID`` (report the CPU the edge used), ``APPMUX_TEST_CONTROL_S``
  (the control's length, 60 by default), ``APPMUX_TEST_SLICE_S`` (5), ``APPMUX_TEST_BUILD_WAIT_S``
  (how long a release may take to settle, 600) and ``APPMUX_TEST_HEAVY_FIXTURE`` (the heavy build's
  fixture folder, ``wheels_only``). Everything the run registers is deleted afterwards.
"""

from __future__ import annotations

import os
import secrets
import ssl
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

import httpx
import pytest
from ops_session import (
    EGRESS_RULE,
    REQUEST_TIMEOUT_S,
    AdminSession,
    Target,
    config_body,
    egress_body,
    load_target,
    register_body,
    source_path_body,
)

# tests/load holds scripts, not a package; pytest only puts it on sys.path when it collects the
# tests there, and this file must also run alone.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "load"))
from ops_under_load import (
    LoadSpec,
    Operation,
    OperationReport,
    SuiteReport,
    problems,
    run_suite,
)

CONNECTIONS: Final = 10
DEFAULT_RATE_PER_S: Final = 500.0
DEFAULT_CONTROL_S: Final = 60.0
DEFAULT_SLICE_S: Final = 5.0
DEFAULT_HEAVY_FIXTURE: Final = "wheels_only"
HEAVY_BUILDS: Final = 3
# Nothing is drained after these: no running copy is replaced.
QUIET_SETTLE_S: Final = 5.0
SLUG_SUFFIX_BYTES: Final = 3

REGISTER_B: Final = "register B"
UPDATE_B: Final = "update B"
UPDATE_A_GOOD: Final = "update A with a good release"
UPDATE_A_UNHEALTHY: Final = "update A with a release that fails its health check"
HEAVY_BUILD: Final = "a heavy build in parallel"
EGRESS_EDIT: Final = "an egress rule edit"
OPERATION_NAMES: Final = (
    REGISTER_B,
    UPDATE_B,
    UPDATE_A_GOOD,
    UPDATE_A_UNHEALTHY,
    HEAVY_BUILD,
    EGRESS_EDIT,
)


def test_every_body_fits_its_contract_model() -> None:
    """The one test of this file that runs without a container: the bodies are valid."""
    folder = "/Workspace/Shared/fixtures/light_fastapi"
    config = {
        "version": 1,
        "wheel_sources": [],
        "health_path": "/",
        "health_deadline_s": 60,
        "own_sp_client_id_key": None,
        "own_sp_client_secret_key": None,
    }
    bodies = [
        register_body(slug="zd-a-0a1b2c", name="A", folder=folder, owner="dana@example.com"),
        source_path_body(version=2, folder=folder),
        egress_body(version=3, rules=[EGRESS_RULE]),
        egress_body(version=4, rules=[]),
        config_body(
            config=config, env=[{"name": "LOG_LEVEL", "value": "info", "value_from": None}]
        ),
    ]

    assert all(isinstance(body, dict) and body for body in bodies)


def _seconds(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _load_spec(target: Target, path: str) -> LoadSpec:
    """The load against ``path``: as the proxy inside the container, through the proxy remotely."""
    return LoadSpec(
        host=target.host,
        port=target.port,
        path=path,
        email=None if target.is_remote else target.admin_email,
        bearer_token=target.user_token if target.is_remote else None,
        tls=ssl.create_default_context() if target.is_remote else None,
        connections=CONNECTIONS,
        rate_per_s=_seconds("APPMUX_TEST_RATE_PER_S", DEFAULT_RATE_PER_S),
        slice_s=_seconds("APPMUX_TEST_SLICE_S", DEFAULT_SLICE_S),
        edge_pid=_edge_pid(),
    )


def _edge_pid() -> int | None:
    value = os.environ.get("APPMUX_TEST_EDGE_PID", "").strip()
    return int(value) if value else None


@pytest.fixture(scope="module")
def target() -> Target:
    return load_target()


class Scenario:
    """The apps of the run and the six operations, each of which waits for its own effect."""

    def __init__(self, session: AdminSession, suffix: str) -> None:
        self.session = session
        self.a = f"zd-a-{suffix}"
        self.b = f"zd-b-{suffix}"
        self.heavy = [f"zd-h{index}-{suffix}" for index in range(1, HEAVY_BUILDS + 1)]
        self.heavy_fixture = os.environ.get("APPMUX_TEST_HEAVY_FIXTURE", DEFAULT_HEAVY_FIXTURE)
        self.good_release_of_a: int | None = None

    def set_up(self) -> None:
        """App A running on release 1, and the apps the heavy build will update."""
        session = self.session
        session.register(self.a, fixture="light_fastapi")
        first = session.press_update(self.a)
        session.wait_for_release(self.a, first, "active")
        session.wait_for_state(self.a, "running")
        answer = session.open_app(self.a)
        assert answer.status_code == 200, (
            f"app {self.a} answers {answer.status_code} before the run starts; a load against it "
            "would fail every request"
        )
        for slug in self.heavy:
            session.register(slug, fixture=self.heavy_fixture)

    def register_b(self) -> None:
        detail = self.session.register(self.b, fixture="light_fastapi")
        assert detail["state"] == "stopped" and detail["releases"] == []

    def update_b(self) -> None:
        release_no = self.session.press_update(self.b)
        self.session.wait_for_release(self.b, release_no, "active")
        self.session.wait_for_state(self.b, "running")

    def update_a_with_a_good_release(self) -> None:
        session = self.session
        session.point_at(self.a, "slow_start")
        release_no = session.press_update(self.a)
        session.wait_for_release(self.a, release_no, "active")
        self.good_release_of_a = release_no

    def update_a_with_a_release_that_fails_health(self) -> None:
        session = self.session
        session.point_at(self.a, "always_500")
        release_no = session.press_update(self.a)
        failed = session.wait_for_release(self.a, release_no, "failed")
        assert failed["status_reason"], "a failed release must say why"
        # A keeps serving the good release the whole time (spec US1 scenario 4).
        detail = session.app(self.a)
        active = [r["release_no"] for r in detail["releases"] if r["status"] == "active"]
        assert detail["state"] == "running" and active == [self.good_release_of_a], (
            f"after the failed release {release_no}, A is {detail['state']} with active releases "
            f"{active}; the good release was {self.good_release_of_a}"
        )
        assert session.open_app(self.a).status_code == 200

    def heavy_build(self) -> None:
        def update(slug: str) -> None:
            release_no = self.session.press_update(slug)
            self.session.wait_for_release(slug, release_no, "active")

        with ThreadPoolExecutor(max_workers=len(self.heavy)) as pool:
            for outcome in [pool.submit(update, slug) for slug in self.heavy]:
                outcome.result()  # re-raises what the build's wait asserted

    def edit_egress_of_a(self) -> None:
        session = self.session
        version = session.app(self.a)["version"]
        session.call(
            "PUT", f"/apps/{self.a}/egress", json=egress_body(version=version, rules=[EGRESS_RULE])
        )
        rules = session.app(self.a)["egress"]
        assert [rule["cidr"] for rule in rules] == [EGRESS_RULE["cidr"]]

    def operations(self) -> list[Operation]:
        return [
            Operation(REGISTER_B, self.register_b, settle_s=QUIET_SETTLE_S),
            Operation(UPDATE_B, self.update_b, settle_s=QUIET_SETTLE_S),
            Operation(UPDATE_A_GOOD, self.update_a_with_a_good_release),
            Operation(
                UPDATE_A_UNHEALTHY,
                self.update_a_with_a_release_that_fails_health,
                settle_s=QUIET_SETTLE_S,
            ),
            Operation(HEAVY_BUILD, self.heavy_build, settle_s=QUIET_SETTLE_S),
            Operation(EGRESS_EDIT, self.edit_egress_of_a, settle_s=QUIET_SETTLE_S),
        ]


@pytest.fixture(scope="module")
def suite(target: Target) -> Iterator[SuiteReport]:
    suffix = secrets.token_hex(SLUG_SUFFIX_BYTES)
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        session = AdminSession(client, target)
        scenario = Scenario(session, suffix)
        try:
            scenario.set_up()
            result = run_suite(
                _load_spec(target, f"/{scenario.a}/"),
                scenario.operations(),
                control_s=_seconds("APPMUX_TEST_CONTROL_S", DEFAULT_CONTROL_S),
            )
            print(result.to_json())  # the figures to record; the report holds no person or token
            yield result
        finally:
            session.delete_created_apps()


def _operation(suite: SuiteReport, name: str) -> OperationReport:
    (found,) = [item for item in suite.operations if item.name == name]
    return found


@pytest.mark.integration
def test_the_control_run_was_made_under_load(suite: SuiteReport) -> None:
    control = suite.control
    # A control that answered nothing would let every operation pass: it needs requests to count.
    assert control.during.requests > 0 and control.before.requests > 0
    p95_before, p95_during = control.before.worst_slice_p95_ms, control.during.worst_slice_p95_ms
    print(f"control: worst slice p95 {p95_before} ms before, {p95_during} ms during")


@pytest.mark.integration
@pytest.mark.parametrize("name", OPERATION_NAMES)
def test_the_operation_causes_no_more_failed_requests_than_the_control(
    suite: SuiteReport, name: str
) -> None:
    report = _operation(suite, name)

    print(
        f"{name}: worst slice p95 {report.before.worst_slice_p95_ms} ms before, "
        f"{report.during.worst_slice_p95_ms} ms during; failed requests {report.before.failures} "
        f"before, {report.during.failures} during (control {suite.control.during.failures})"
    )
    assert problems(suite.control, report) == []
