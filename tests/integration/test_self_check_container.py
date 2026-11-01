"""``self_check`` on a Databricks container: every check of design 6.4 passes (T068, US3, FR-013).

A sandbox that works on a developer machine proves little: the user namespace, the mounts, the
capability drop, the seccomp filter and the packet pump all depend on what the platform's container
allows. ``self_check`` runs a canary program inside a real sandbox that tries, as a hostile child
would, each thing in ``sandbox.self_check.CHECKS`` (reading appmux's environment, seeing another
process, reaching the loopback or the metadata address, regaining a capability, ...). Two things
are asserted against the deployed appmux:

* **a fresh run** (``POST /platform/self-check``) answers one entry for each check of the table,
  in the table's order, and every one passed, so the run passed as a whole. A missing, repeated or
  unknown entry fails the test: a canary that skipped a check must not read as a pass;
* **the platform page** (``GET /platform``) holds the same verdict, which is what decides whether
  the edge serves apps. The page only reads the last run, so this is also the check that the run
  is kept.

The ``detail`` text of a check is a program's output and is not asserted; it is printed (``pytest
-s``) with the check ids so a run can be compared with spikes 2, 6 and 7.

The test talks to the deployed appmux as the owner of a Databricks token (``ops_session.py``): run
it with ``scripts/run-remote-tests.sh tests/integration/test_self_check_container.py -s``. It is
marked ``integration`` and skipped unless ``APPMUX_TEST_CONTAINER=1``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from ops_session import AdminSession, Target, load_target

from appmux.sandbox.self_check import CHECK_IDS

pytestmark = pytest.mark.integration

# A run starts a sandbox and a canary; a cold container may take longer than a normal request.
SELF_CHECK_TIMEOUT_S = 120.0


@pytest.fixture(scope="module")
def target() -> Target:
    return load_target()


@pytest.fixture
def session(target: Target) -> Iterator[AdminSession]:
    with httpx.Client(base_url=target.base_url, timeout=SELF_CHECK_TIMEOUT_S) as client:
        yield AdminSession(client, target)


def _assert_every_check_passed(result: dict[str, Any]) -> None:
    checks = result["checks"]
    assert [check["name"] for check in checks] == list(CHECK_IDS), (
        "the result must hold each check of the table once, in order"
    )
    failed = [check["name"] for check in checks if not check["passed"]]
    assert not failed, f"checks that failed on the container: {failed}"
    assert result["passed"] is True


def test_a_fresh_self_check_passes_every_check_on_the_container(session: AdminSession) -> None:
    result = session.call("POST", "/platform/self-check").json()

    for check in result["checks"]:
        print(f"self_check {check['name']}: {'ok' if check['passed'] else 'FAILED'}")
    print(f"self_check took {result['duration_ms']} ms")
    _assert_every_check_passed(result)


def test_the_platform_page_holds_the_same_verdict(session: AdminSession) -> None:
    session.call("POST", "/platform/self-check")

    page = session.read("/platform")

    _assert_every_check_passed(page["self_check"])
    # With the pin off the cpu_pin check passes with nothing to prove (sandbox/canary.py).
    assert page["settings"]["child_cpu_pin"] is True, "cpu_pin passed without a pin to prove"
