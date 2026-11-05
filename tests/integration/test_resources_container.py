"""Resources and secrets against a running appmux in a Databricks container (T129, FR-034..FR-039).

The scenario, in order, on the ``appyaml_missing_resource`` fixture (two ``valueFrom`` keys,
``openai-api-key`` and ``sql-warehouse``, and no resource for either):

1. The first Update of the app fails the release in the reconcile **before any build**, and its
   ``status_reason`` lists both keys at once (``missing resources: openai-api-key, sql-warehouse``).
   The child never gets a route.
2. The person who manages the app attaches the two resources with their own token: a scratch
   secret (read with the appmux service principal at start) and a SQL warehouse (checked with the
   person's token). The requirements panel then reads both as attached.
3. Activating the release starts the child, and the child's ``/report`` says both variables are set
   (the fixture never returns a resolved value).
4. The secret reaches only that child: a second app (the ``env_snooper`` fixture, with no such
   resource) has neither variable in its environment, and the secret's value shows up in no
   response of the admin API.

This test talks HTTP to the edge on the container's own port and plays the Databricks proxy by
sending the identity headers itself, which only works from inside the container. It does not run in
the default gate: every test here is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``; ``scripts/run-container-tests.sh`` puts the code and this test on the
dev App's container and runs it there. It passed there on 2026-11-05 (Free Edition only; see
``specs/001-appmux-v1/spikes/README.md``).

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing). Use the
Free Edition scratch workspace only, a scratch secret scope and a disposable secret:

* ``APPMUX_TEST_BASE_URL``: where the edge listens, for example ``http://127.0.0.1:8000``;
* ``APPMUX_TEST_RESOURCES_APP``: the slug of a registered app whose source folder holds a copy of
  ``tests/fixtures/appyaml_missing_resource``, OBO-delegated, with **no release yet** (a second run
  needs a fresh app: an Update of an unchanged folder creates no release);
* ``APPMUX_TEST_OTHER_APP``: the slug of a registered, active app running ``env_snooper``, with
  no resources;
* ``APPMUX_TEST_MANAGER_EMAIL`` and ``APPMUX_TEST_MANAGER_TOKEN``: a person with ``CAN_MANAGE`` on
  both apps and that person's own user token with the ``sql`` scope. The token is read from the
  environment and goes only into the identity header; the test never prints it;
* ``APPMUX_TEST_WAREHOUSE_ID``: a SQL warehouse the person can use;
* ``APPMUX_TEST_SECRET_REF``: ``scope/key`` of the scratch secret. The person needs ``READ`` on the
  scope (the attach check) and so does the appmux service principal (the read at start);
* ``APPMUX_TEST_SECRET_VALUE``: that secret's value, only to prove it appears nowhere. A failed
  assertion names the place and never repeats the value.

The platform setting ``attach_check_warehouse_id`` must be set, because the secret's attach check
runs ``secret()`` on that warehouse.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

import httpx
import pytest

pytestmark = pytest.mark.integration

API: Final = "/_appmux/api/v1"
REQUEST_TIMEOUT_S: Final = 60.0  # an attach check runs a statement on a warehouse that may start
POLL_INTERVAL_S: Final = 3.0
POLL_TIMEOUT_S: Final = 240.0
API_KEY_RESOURCE: Final = "openai-api-key"
WAREHOUSE_KEY: Final = "sql-warehouse"
EXPECTED_REASON: Final = f"missing resources: {API_KEY_RESOURCE}, {WAREHOUSE_KEY}"
ENVIRONMENT_NAMES: Final = ("OPENAI_API_KEY", "WAREHOUSE_ID")
SETTLED: Final = frozenset({"failed", "active"})


@dataclass(frozen=True, slots=True)
class Target:
    base_url: str
    app: str
    other_app: str
    email: str
    token: str
    warehouse_id: str
    secret_ref: str
    secret_value: str


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"set {name} for the resources run (see the module docstring)")
    return value


@pytest.fixture(scope="module")
def target() -> Target:
    return Target(
        base_url=_required("APPMUX_TEST_BASE_URL").rstrip("/"),
        app=_required("APPMUX_TEST_RESOURCES_APP"),
        other_app=_required("APPMUX_TEST_OTHER_APP"),
        email=_required("APPMUX_TEST_MANAGER_EMAIL").strip().lower(),
        token=_required("APPMUX_TEST_MANAGER_TOKEN"),
        warehouse_id=_required("APPMUX_TEST_WAREHOUSE_ID"),
        secret_ref=_required("APPMUX_TEST_SECRET_REF"),
        secret_value=_required("APPMUX_TEST_SECRET_VALUE"),
    )


@pytest.fixture(scope="module")
def client(target: Target) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as session:
        yield session


def _identity(target: Target, *, write: bool = False) -> dict[str, str]:
    headers = {"X-Forwarded-Email": target.email, "X-Forwarded-Access-Token": target.token}
    if write:
        headers["Sec-Fetch-Site"] = "same-origin"
    return headers


def _expect(response: httpx.Response, status: int, what: str) -> httpx.Response:
    # The body is not part of the message: a failed call must not print anything it echoes.
    if response.status_code != status:
        pytest.fail(f"{what} answered {response.status_code}, expected {status}")
    return response


def _assert_value_absent(target: Target, text: str, where: str) -> None:
    # Not `assert value not in text`: pytest would print both sides when it fails.
    if target.secret_value in text:
        pytest.fail(f"the secret value appears in {where}")


def _wait_until(
    what: str,
    read: Callable[[], dict[str, object]],
    is_settled: Callable[[dict[str, object]], bool],
) -> dict[str, object]:
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while True:
        state = read()
        if is_settled(state):
            return state
        if time.monotonic() > deadline:
            pytest.fail(f"{what} did not settle within {POLL_TIMEOUT_S:.0f} s")
        time.sleep(POLL_INTERVAL_S)


def _release(client: httpx.Client, target: Target, release_no: int) -> dict[str, object]:
    response = client.get(
        f"{API}/apps/{target.app}/releases/{release_no}", headers=_identity(target)
    )
    return dict(_expect(response, 200, "reading the release").json())


def _requirements(client: httpx.Client, target: Target) -> dict[str, object]:
    response = client.get(f"{API}/apps/{target.app}/requirements", headers=_identity(target))
    return dict(_expect(response, 200, "reading the requirements").json())


def _states(requirements: dict[str, object]) -> dict[str, str]:
    items = requirements["items"]
    assert isinstance(items, list)
    return {str(item["key"]): str(item["state"]) for item in items}


@pytest.fixture(scope="module")
def first_release(client: httpx.Client, target: Target) -> int:
    """Press Update on the fresh app and return the release it created."""
    response = client.post(
        f"{API}/apps/{target.app}/releases", headers=_identity(target, write=True)
    )
    if response.status_code == 200:
        pytest.fail(
            "the app already has a live release of this folder; register a fresh app for this run"
        )
    return int(_expect(response, 202, "the Update").json()["release_no"])


def test_a_release_with_missing_resources_fails_before_any_build_naming_every_key(
    client: httpx.Client, target: Target, first_release: int
) -> None:
    release = _wait_until(
        "the first release",
        lambda: _release(client, target, first_release),
        lambda state: state["status"] in SETTLED,
    )

    assert release["status"] == "failed"
    assert release["status_reason"] == EXPECTED_REASON
    assert release["wheels"] == [], "a build ran although the check had already failed"
    states = _states(_requirements(client, target))
    assert states == {API_KEY_RESOURCE: "missing", WAREHOUSE_KEY: "missing"}
    child = client.get(f"/{target.app}/report", headers=_identity(target))
    assert child.status_code != 200, "a child with a failed release has a route"


def test_attaching_the_resources_and_activating_the_release_starts_the_child(
    client: httpx.Client, target: Target, first_release: int
) -> None:
    warehouse = client.put(
        f"{API}/apps/{target.app}/resources/{WAREHOUSE_KEY}",
        headers=_identity(target, write=True),
        json={
            "type": "sql_warehouse",
            "target": target.warehouse_id,
            "permission": "CAN_USE",
            "access_as": "obo",
        },
    )
    _expect(warehouse, 200, "attaching the warehouse")
    secret = client.put(
        f"{API}/apps/{target.app}/resources/{API_KEY_RESOURCE}",
        headers=_identity(target, write=True),
        json={"type": "secret", "target": target.secret_ref, "permission": "READ"},
    )
    _expect(secret, 200, "attaching the secret")
    for where, response in (("the warehouse attach", warehouse), ("the secret attach", secret)):
        _assert_value_absent(target, response.text, where)
    assert warehouse.json()["check_result"] == "ok"
    assert secret.json()["check_result"] == "ok"
    assert secret.json()["target"] == target.secret_ref, "a secret is shown as its reference"
    assert _states(_requirements(client, target)) == {
        API_KEY_RESOURCE: "attached",
        WAREHOUSE_KEY: "attached",
    }

    activate = client.post(
        f"{API}/apps/{target.app}/releases/{first_release}/activate",
        headers=_identity(target, write=True),
    )
    _expect(activate, 200, "activating the release")
    release = _wait_until(
        "the activated release",
        lambda: _release(client, target, first_release),
        lambda state: state["status"] in SETTLED,
    )
    assert release["status"] == "active", f"the release ended as {release['status']}"

    report = client.get(f"/{target.app}/report", headers=_identity(target))
    _expect(report, 200, "the child's report")
    assert report.json()["env"] == {
        "LOG_LEVEL": "info",
        "OPENAI_API_KEY": "set",
        "WAREHOUSE_ID": "set",
    }
    _assert_value_absent(target, report.text, "the child's report")


def test_the_secret_value_is_in_no_response_of_the_admin_api(
    client: httpx.Client, target: Target, first_release: int
) -> None:
    release = client.get(
        f"{API}/apps/{target.app}/releases/{first_release}", headers=_identity(target)
    )
    audit = client.get(
        f"{API}/audit", params={"app": target.app, "limit": 1000}, headers=_identity(target)
    )
    requirements = client.get(f"{API}/apps/{target.app}/requirements", headers=_identity(target))
    detail = client.get(f"{API}/apps/{target.app}", headers=_identity(target))

    for where, response in (
        ("the release detail", release),
        ("the audit log", audit),
        ("the requirements", requirements),
        ("the app detail", detail),
    ):
        _expect(response, 200, where)
        _assert_value_absent(target, response.text, where)
    actions = {row["action"] for row in audit.json()["items"]}
    assert "resource.attach" in actions


def test_the_secret_reaches_only_the_child_that_declared_it(
    client: httpx.Client, target: Target, first_release: int
) -> None:
    other = client.get(f"/{target.other_app}/report", headers=_identity(target))

    _expect(other, 200, "the other child's report")
    report = other.json()
    names = report["own_environment_names"]
    assert not set(ENVIRONMENT_NAMES) & set(names), "a variable of another app reached this child"
    assert report["isolated"] is True
    _assert_value_absent(target, other.text, "the other child's report")
