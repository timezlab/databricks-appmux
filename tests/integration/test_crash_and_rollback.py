"""A crashing app and a rollback, against a running appmux in a Databricks container (T109, US2).

**A crash loop ends ``crashed``, and only its own route answers ``503``** (FR-007, design 5.6). The
``crash_loop`` fixture passes its health check, opens its route, and then exits with status 7
about three seconds after every start. The reconciler restarts the same release with backoff (1, 2,
4, 8 and 16 seconds); after the fifth restart the app is ``crashed``. The test checks:

* the app reaches ``crashed`` and stays there (nothing restarts it by itself);
* its route answers ``503``, and **not** the answer of an older release: the app had a healthy
  release 1 (``light_fastapi``) before the crashing release 2 was swapped in, and the crash must
  not bring release 1 back (the same release is restarted, never an older one);
* release 2 is still the ``active`` one and release 1 is ``superseded``;
* a neighbour app keeps answering ``200`` throughout the crash: the route that goes to ``503``
  is only the crashed app's.

**A rollback starts from the stored snapshot** (FR-009, design 5.2). The app gets five releases
from five different fixtures, so each has its own snapshot; then its ``source_path`` is pointed at
a folder that does not exist, and release 3 is activated. Release 3 must run, and answer as its own
fixture does, from what appmux stored, while release 5 becomes ``superseded``. This test cannot
write to the workspace, so a source folder that is gone is made by pointing ``source_path`` at a
folder that never existed; what it proves is that activation does not read the app's source folder.

The first test reads ``crashed`` from ``AppDetail.state``, which the admin routes report only when
the service passes the reconciler's ``run_state`` to them (the wiring ``Service`` gives
``AppControl``, ``assembly.py``).

The caller is the owner of a Databricks token (``ops_session.py``): ``scripts/sync-deploy.sh`` puts
the code on a container, and ``scripts/run-remote-tests.sh`` runs this file against it. It does not
run in the default gate: every test here is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``. It passed 2 of 2 on 2026-10-31 against the Free Edition dev App.
Everything the run registers is deleted afterwards.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``: where the edge listens, for example ``http://127.0.0.1:8000``;
* ``APPMUX_TEST_ADMIN_EMAIL``: a platform admin, who owns every app the run registers;
* ``APPMUX_TEST_USER_TOKEN``: that person's Databricks token (Update reads the workspace with it
  and ``PATCH /apps/{app}`` writes as the admin with it); read from the environment, sent only as a
  header and never printed;
* ``APPMUX_TEST_FIXTURES_FOLDER``: a workspace folder starting with ``/Workspace/`` that holds a
  copy of ``tests/fixtures/`` (``light_fastapi``, ``crash_loop``, ``slow_start``,
  ``memory_fastapi``, ``absolute_links``, ``base_path_aware``);
* optionally ``APPMUX_TEST_BUILD_WAIT_S`` (how long a release may take to settle, 600) and
  ``APPMUX_TEST_CRASH_WAIT_S`` (how long the crash loop may take to end, 240).
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator
from typing import Final

import httpx
import pytest
from ops_session import REQUEST_TIMEOUT_S, AdminSession, Target, load_target

pytestmark = pytest.mark.integration

SLUG_SUFFIX_BYTES: Final = 3
DEFAULT_CRASH_WAIT_S: Final = 240.0
# After the last restart nothing may start the app again: wait longer than the longest backoff.
STAYS_CRASHED_FOR_S: Final = 30.0
CHECK_INTERVAL_S: Final = 5.0
CRASHED_STATUS: Final = 503
# Five fixtures with five different snapshots, each of which says which one it is on this path.
ROLLBACK_FIXTURES: Final = (
    ("slow_start", "/report"),
    ("memory_fastapi", "/report"),
    ("light_fastapi", "/"),
    ("absolute_links", "/report"),
    ("base_path_aware", "/report"),
)
ROLLBACK_TO: Final = 3


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


def _fixture_that_answers(session: AdminSession, slug: str, path: str) -> str:
    answer = session.open_app(slug, path)
    assert answer.status_code == 200, f"{slug}{path} answered {answer.status_code}"
    return str(answer.json()["fixture"])


def _release_statuses(session: AdminSession, slug: str) -> dict[int, str]:
    return {r["release_no"]: r["status"] for r in session.app(slug)["releases"]}


def test_a_crash_loop_ends_crashed_with_only_its_own_route_at_503(session: AdminSession) -> None:
    suffix = _suffix()
    neighbour, crashing = f"zc-ok-{suffix}", f"zc-loop-{suffix}"
    session.register(neighbour, fixture="light_fastapi")
    session.wait_for_release(neighbour, session.press_update(neighbour), "active")
    session.wait_for_state(neighbour, "running")

    # Release 1 is healthy, so there is an older release the crash must not bring back.
    session.register(crashing, fixture="light_fastapi")
    session.wait_for_release(crashing, session.press_update(crashing), "active")
    session.point_at(crashing, "crash_loop")
    crashing_release = session.press_update(crashing)
    session.wait_for_release(crashing, crashing_release, "active")

    crash_wait_s = float(os.environ.get("APPMUX_TEST_CRASH_WAIT_S", DEFAULT_CRASH_WAIT_S))
    session.wait_for_state(crashing, "crashed", wait_s=crash_wait_s)

    assert session.open_app(crashing).status_code == CRASHED_STATUS
    assert session.open_app(neighbour).status_code == 200, "a crash must not touch other routes"
    statuses = _release_statuses(session, crashing)
    assert statuses == {1: "superseded", crashing_release: "active"}, (
        f"the crashed app's releases are {statuses}; the crashing release stays active and the "
        "older one is not brought back"
    )

    # It stays crashed: nothing restarts it once the backoff is used up.
    deadline = time.monotonic() + STAYS_CRASHED_FOR_S
    while time.monotonic() < deadline:
        time.sleep(CHECK_INTERVAL_S)
        assert session.app(crashing)["state"] == "crashed"
        assert session.open_app(crashing).status_code == CRASHED_STATUS
        assert session.open_app(neighbour).status_code == 200


def test_a_rollback_to_release_3_of_5_runs_from_the_stored_snapshot(session: AdminSession) -> None:
    slug = f"zr-{_suffix()}"
    session.register(slug, fixture=ROLLBACK_FIXTURES[0][0])
    for number, (fixture, _) in enumerate(ROLLBACK_FIXTURES, start=1):
        if number > 1:
            session.point_at(slug, fixture)
        release_no = session.press_update(slug)
        assert release_no == number, f"release {release_no} was made where {number} was expected"
        session.wait_for_release(slug, release_no, "active")
    last_fixture, last_path = ROLLBACK_FIXTURES[-1]
    assert _fixture_that_answers(session, slug, last_path) == last_fixture

    # The source folder is gone: the app now points at a folder that does not exist.
    session.point_at(slug, f"gone-{_suffix()}")

    activated = session.call("POST", f"/apps/{slug}/releases/{ROLLBACK_TO}/activate").json()
    assert activated["release_no"] == ROLLBACK_TO
    session.wait_for_release(slug, ROLLBACK_TO, "active")
    session.wait_for_state(slug, "running")

    wanted_fixture, wanted_path = ROLLBACK_FIXTURES[ROLLBACK_TO - 1]
    assert _fixture_that_answers(session, slug, wanted_path) == wanted_fixture
    statuses = _release_statuses(session, slug)
    assert statuses[ROLLBACK_TO] == "active" and statuses[len(ROLLBACK_FIXTURES)] == "superseded"
    assert set(statuses) == set(range(1, len(ROLLBACK_FIXTURES) + 1)), "no release may be lost"
