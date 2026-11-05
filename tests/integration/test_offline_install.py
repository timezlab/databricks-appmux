"""Offline installs of the four wheel fixtures, against a running appmux (T137, US7, FR-032).

Each fixture is an app registered on appmux whose package index cannot be reached: the platform
setting ``package_mode`` is ``wheelhouse``, so the build has no index to ask and the wheel pass runs
with an all-deny egress list. The test presses Update on each app, waits for the release to settle
and then checks both what the registry recorded (the release's wheel list) and what the child
really imported (its ``GET /report``):

* ``wheels_only``: the two dependencies exist only as wheel files, so the release is ``active`` and
  both modules come from the overlay;
* ``wheel_reuse``: needs the wheel ``wheels_only`` already stored, so nothing is fetched again: its
  wheel list names the content store as the source;
* ``env_covered``: every requirement is met by the App environment, so no wheel is installed, the
  child has no overlay on its ``PYTHONPATH``, and the decoy wheel on offer is not installed;
* ``newer_wheel``: needs ``appdirs 99.0.0`` while the environment holds 1.4.4, and the child imports
  the wheel's copy.

This test talks HTTP to the edge on the container's own port and plays the Databricks proxy by
sending the identity headers itself, which only works from inside the container. It does not run in
the default gate: every test here is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``; ``scripts/run-container-tests.sh`` puts the code and this test on the
dev App's container and runs it there. It passed there on 2026-11-05 with the platform setting
``package_mode`` at ``wheelhouse`` (Free Edition only; see
``specs/001-appmux-v1/spikes/README.md``).
The fixtures' ``requirements.txt`` files travel in the runner's overlay because the test compares
their pinned hashes with the release's wheel list.

Set-up the run needs (a missing one fails the run loudly instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``: where the edge listens, for example ``http://127.0.0.1:8000``;
* ``APPMUX_TEST_USER_EMAIL`` and ``APPMUX_TEST_USER_TOKEN``: an app admin of the four apps and
  that person's real Databricks access token. Update reads the source folder *as the person*, so a
  made-up token cannot work here. The token is read from the environment, sent only as a header
  and never printed;
* ``APPMUX_TEST_WHEELS_ONLY_APP``, ``APPMUX_TEST_WHEEL_REUSE_APP``, ``APPMUX_TEST_ENV_COVERED_APP``
  and ``APPMUX_TEST_NEWER_WHEEL_APP``: the slugs of four registered apps whose source folders are
  copies of the fixtures, each with its ``wheels/`` folder built by
  ``python tests/fixtures/<name>/make_wheels.py <copy>/wheels``;
* optionally ``APPMUX_TEST_BUILD_WAIT_S``: how long to wait for a release to settle (default 900).
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from appmux.packages.wheels import STORE_SOURCE

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"
API: Final = "/_appmux/api/v1"
REQUEST_TIMEOUT_S: Final = 30.0
POLL_INTERVAL_S: Final = 5.0
DEFAULT_BUILD_WAIT_S: Final = 900.0
SETTLED_STATUSES: Final = frozenset({"active", "failed", "superseded"})
HASH: Final = re.compile(r"--hash=sha256:([0-9a-f]{64})")


@dataclass(frozen=True, slots=True)
class Target:
    base_url: str
    email: str
    token: str = field(default="", repr=False)  # a credential: never in a message or a repr

    def identity(self) -> dict[str, str]:
        return {"X-Forwarded-Email": self.email, "X-Forwarded-Access-Token": self.token}


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"set {name} for the offline-install run (see the module docstring)")
    return value


@pytest.fixture(scope="module")
def target() -> Target:
    return Target(
        base_url=_required("APPMUX_TEST_BASE_URL").rstrip("/"),
        email=_required("APPMUX_TEST_USER_EMAIL").strip().lower(),
        token=_required("APPMUX_TEST_USER_TOKEN"),
    )


@pytest.fixture(scope="module")
def client(target: Target) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as session:
        yield session


def _wait_s() -> float:
    return float(os.environ.get("APPMUX_TEST_BUILD_WAIT_S", DEFAULT_BUILD_WAIT_S))


def _lock_hashes(fixture: str) -> set[str]:
    """The wheel hashes the fixture's own ``requirements.txt`` pins."""
    text = (FIXTURES / fixture / "requirements.txt").read_text(encoding="utf-8")
    return set(HASH.findall(text))


def _press_update(client: httpx.Client, target: Target, app: str) -> int:
    response = client.post(
        f"{API}/apps/{app}/releases",
        headers={**target.identity(), "Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code in {200, 202}, (
        f"Update of '{app}' answered {response.status_code}: {response.text[:300]}"
    )
    return int(response.json()["release_no"])


def _settled_release(client: httpx.Client, target: Target, app: str, release_no: int) -> Any:
    deadline = time.monotonic() + _wait_s()
    while True:
        response = client.get(f"{API}/apps/{app}/releases/{release_no}", headers=target.identity())
        assert response.status_code == 200, f"the release read answered {response.status_code}"
        release = response.json()
        if release["status"] in SETTLED_STATUSES:
            return release
        assert time.monotonic() < deadline, (
            f"release {release_no} of '{app}' is still {release['status']} after {_wait_s():g} s"
        )
        time.sleep(POLL_INTERVAL_S)


def _update_and_settle(client: httpx.Client, target: Target, env_name: str) -> tuple[str, Any]:
    app = _required(env_name)
    release = _settled_release(client, target, app, _press_update(client, target, app))
    assert release["status"] == "active", (
        f"release {release['release_no']} of '{app}' ended {release['status']}: "
        f"{release['status_reason']}\n{release['build_log_tail']}"
    )
    return app, release


def _report(client: httpx.Client, target: Target, app: str) -> Any:
    response = client.get(f"/{app}/report", headers=target.identity())
    assert response.status_code == 200, f"the child '{app}' answered {response.status_code}"
    return response.json()


@pytest.fixture(scope="module")
def wheels_only(client: httpx.Client, target: Target) -> tuple[str, Any]:
    return _update_and_settle(client, target, "APPMUX_TEST_WHEELS_ONLY_APP")


# --- the four fixtures ------------------------------------------------------------------------


def test_wheels_only_installs_both_wheels_into_its_overlay(
    client: httpx.Client, target: Target, wheels_only: tuple[str, Any]
) -> None:
    app, release = wheels_only
    assert {wheel["sha256"] for wheel in release["wheels"]} == _lock_hashes("wheels_only")

    report = _report(client, target, app)
    alpha = report["packages"]["appmux-fixture-alpha"]
    beta = report["packages"]["appmux-fixture-beta"]
    assert alpha["value"] == "appmux-fixture-alpha 1.0.0"
    assert beta["value"] == "appmux-fixture-beta 1.0.0 on appmux-fixture-alpha 1.0.0"
    assert report["pythonpath"], "the child should start with an overlay on its PYTHONPATH"
    assert alpha["file"].startswith(report["pythonpath"].split(":")[0])


def test_wheel_reuse_takes_the_stored_wheel_and_fetches_nothing(
    client: httpx.Client, target: Target, wheels_only: tuple[str, Any]
) -> None:
    # Depends on ``wheels_only``: that Update is what puts the shared wheel in the content store.
    app, release = _update_and_settle(client, target, "APPMUX_TEST_WHEEL_REUSE_APP")

    assert {wheel["sha256"] for wheel in release["wheels"]} == _lock_hashes("wheel_reuse")
    assert {wheel["source"] for wheel in release["wheels"]} == {STORE_SOURCE}
    package = _report(client, target, app)["packages"]["appmux-fixture-alpha"]
    assert package["value"] == "appmux-fixture-alpha 1.0.0"


def test_env_covered_installs_no_wheel_and_builds_no_overlay(
    client: httpx.Client, target: Target
) -> None:
    app, release = _update_and_settle(client, target, "APPMUX_TEST_ENV_COVERED_APP")

    assert release["wheels"] == []
    report = _report(client, target, app)
    assert report["pythonpath"] == "", "no overlay means nothing in front of the environment"
    assert report["decoy"] == {"error": "ModuleNotFoundError"}, (
        "the wheel on offer was not asked for"
    )


def test_newer_wheel_wins_over_the_environments_copy_for_this_release_only(
    client: httpx.Client, target: Target
) -> None:
    app, release = _update_and_settle(client, target, "APPMUX_TEST_NEWER_WHEEL_APP")

    assert {wheel["sha256"] for wheel in release["wheels"]} == _lock_hashes("newer_wheel")
    report = _report(client, target, app)
    assert report["imported"]["version"] == "99.0.0"
    assert report["imported"]["marker"] == "appmux-fixture-newer-wheel"
    overlay = report["pythonpath"].split(":")[0]
    assert report["imported"]["file"].startswith(overlay)
    assert [copy["version"] for copy in report["copies"]][:2] == ["99.0.0", "1.4.4"]
