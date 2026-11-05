"""Capacity of the real edge, against a running appmux in a Databricks container (T086, SC-001).

The first integration scenario (quickstart section 3, ADR 0014): 500 requests a second over 10
connections, paced, against a fast app that is registered and running (the ``light_fastapi``
fixture). The load generator (``tests/load/http_load.py``) runs as its own process, started by this
test, and reports the p95 latency, the error rate and the CPU the edge process used.

First run inside the dev App's container, 2026-11-05: 0 failures, p95 3.98 ms, the edge at 62.3% of
one core (see the spike log, "Container runs inside the dev App").

What the test asserts and what it leaves to a person. It asserts the things that need no judgement:
that the app answers before the load starts, that no request fails, and that the generator reached
(nearly) the rate it was asked for, since a paced run that falls behind has not held the rate. It
does not assert a p95 or a CPU limit, because no number for "room to spare" is written down: the
report is printed so the result can be read and recorded under a dated "plan checks" heading in
``specs/001-appmux-v1/spikes/README.md``, and if the edge cannot hold the rate with room to spare
the plan stops and ADR 0014 is revisited (``tasks.md`` T086).

This test talks HTTP to the edge on the container's own port and plays the Databricks proxy by
sending ``X-Forwarded-Email`` itself, which only works from inside the container, where it also can
read the edge process's CPU. It does not run in the default gate: it is marked ``integration`` and
skipped unless ``APPMUX_TEST_CONTAINER=1``; ``scripts/sync-deploy.sh`` is the way to put the code on
a container.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``: where the edge listens, for example ``http://127.0.0.1:8000``;
* ``APPMUX_TEST_EDGE_PID``: the process id of the edge, whose CPU the run reports;
* either ``APPMUX_TEST_LOAD_APP`` (the slug of a registered, running ``light_fastapi`` app) with
  ``APPMUX_TEST_USER_EMAIL`` (a person who may open it), or nothing of the two: the run then
  registers its own ``light_fastapi`` as the platform admin and deletes it afterwards, which needs
  what ``ops_session.load_target`` reads (``APPMUX_TEST_FIXTURES_FOLDER``,
  ``APPMUX_TEST_USER_TOKEN`` and ``APPMUX_TEST_ADMIN_EMAIL``; ``scripts/run-container-tests.sh``
  provides them);
* ``APPMUX_TEST_LOAD_SECONDS``: how long the load lasts (optional, 30 by default).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import httpx
import pytest
from ops_session import REQUEST_TIMEOUT_S, AdminSession, load_target

pytestmark = pytest.mark.integration

LOAD_SCRIPT: Final = Path(__file__).resolve().parents[1] / "load" / "http_load.py"
CONNECTIONS: Final = 10
TARGET_RATE_PER_S: Final = 500.0
# A paced run that cannot keep up finishes fewer requests than fall due; a little slack covers the
# connections that start and stop at the edges of the run.
MIN_RATE_FRACTION: Final = 0.95
DEFAULT_LOAD_SECONDS: Final = 30.0
PREFLIGHT_TIMEOUT_S: Final = 30.0
# How long beyond the load itself the generator process may take to start, finish and print.
GENERATOR_GRACE_S: Final = 60.0
SLUG_SUFFIX_BYTES: Final = 3


@dataclass(frozen=True, slots=True)
class Target:
    host: str
    port: int
    app: str
    email: str
    edge_pid: int
    load_seconds: float


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"set {name} for the capacity run (see the module docstring)")
    return value


def _edge_target(*, app: str, email: str) -> Target:
    base_url = urlsplit(_required("APPMUX_TEST_BASE_URL"))
    if base_url.scheme != "http" or not base_url.hostname or base_url.port is None:
        pytest.fail("APPMUX_TEST_BASE_URL must look like http://127.0.0.1:8000 (host and port)")
    return Target(
        host=base_url.hostname,
        port=base_url.port,
        app=app,
        email=email,
        edge_pid=int(_required("APPMUX_TEST_EDGE_PID")),
        load_seconds=float(os.environ.get("APPMUX_TEST_LOAD_SECONDS", DEFAULT_LOAD_SECONDS)),
    )


@pytest.fixture(scope="module")
def target() -> Iterator[Target]:
    """The app under load: the one named by the environment, or a ``light_fastapi`` of this run.

    Without ``APPMUX_TEST_LOAD_APP`` the run registers its own (the way ``scripts/run-container-
    tests.sh`` runs it, where nothing is registered beforehand), presses Update as the platform
    admin, waits until the app runs, loads it as that admin and deletes it afterwards.
    """
    named_app = os.environ.get("APPMUX_TEST_LOAD_APP", "").strip()
    if named_app:
        yield _edge_target(app=named_app, email=_required("APPMUX_TEST_USER_EMAIL").strip())
        return
    admin_target = load_target()
    with httpx.Client(base_url=admin_target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        session = AdminSession(client, admin_target)
        slug = f"it-edge-load-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
        try:
            session.register(slug, fixture="light_fastapi", name="Edge capacity load")
            release_no = session.press_update(slug)
            session.wait_for_release(slug, release_no, "active")
            session.wait_for_state(slug, "running")
            yield _edge_target(app=slug, email=admin_target.admin_email)
        finally:
            session.delete_created_apps()


def test_the_edge_holds_500_requests_a_second_over_10_connections(target: Target) -> None:
    path = f"/{target.app}/"
    # Before the load: an app that does not answer would fail every request of the run, and "all
    # 503" says less than this does about what to set up.
    answer = httpx.get(
        f"http://{target.host}:{target.port}{path}",
        headers={"X-Forwarded-Email": target.email},
        timeout=PREFLIGHT_TIMEOUT_S,
    )
    assert answer.status_code == 200, (
        f"the app answered {answer.status_code} before the load started; register it, wait for "
        "it to run, and check APPMUX_TEST_USER_EMAIL may open it"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(LOAD_SCRIPT),
            "--host",
            target.host,
            "--port",
            str(target.port),
            "--path",
            path,
            "--email",
            target.email,
            "--connections",
            str(CONNECTIONS),
            "--rate",
            str(TARGET_RATE_PER_S),
            "--duration",
            str(target.load_seconds),
            "--edge-pid",
            str(target.edge_pid),
        ],
        capture_output=True,
        text=True,
        timeout=target.load_seconds + GENERATOR_GRACE_S,
        check=False,
    )
    assert completed.returncode == 0, f"the load generator failed: {completed.stderr.strip()}"
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    print(f"capacity report: {json.dumps(report)}")  # for the record in the plan checks

    assert report["failures"] == 0, (
        f"{report['failures']} of {report['requests']} requests failed "
        f"({report['failure_kinds']}); the edge did not hold the rate"
    )
    assert report["per_second"] >= TARGET_RATE_PER_S * MIN_RATE_FRACTION, (
        f"the run reached {report['per_second']} requests a second of {TARGET_RATE_PER_S}; "
        "the edge fell behind the pace"
    )
