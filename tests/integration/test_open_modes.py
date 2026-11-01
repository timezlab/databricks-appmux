"""Open as a user, against a running appmux in a Databricks container (T155, FR-028, rule 17).

A platform admin who is neither the owner nor named by a grant opens an OBO-delegated app. By
default the child receives no ``X-Forwarded-Access-Token``; after ``POST /apps/{app}/open-mode``
with ``as_user`` it receives one until the admin switches back, and the choice is audited as
``app.view_as_user``. The child is the ``light_fastapi`` fixture, whose ``GET /headers`` lists the
names of the request headers it got (never a value of a credential).

This test talks HTTP to the edge on the container's own port and plays the Databricks proxy by
sending the identity headers itself, which only works from inside the container. It does not run in
the default gate: every test here is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``; ``scripts/sync-deploy.sh`` is the way to put the code on a container.
Nothing here has been run against one yet.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``: where the edge listens, for example ``http://127.0.0.1:8000``;
* ``APPMUX_TEST_OBO_APP``: the slug of a registered, active, OBO-delegated ``light_fastapi`` app;
* ``APPMUX_TEST_ADMIN_EMAIL``: a platform admin who is not the app's owner and holds no grant on
  it.

The token the test sends is a made-up value: appmux only passes it on, and the fixture only reports
that a header of that name arrived.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import httpx
import pytest

pytestmark = pytest.mark.integration

FAKE_TOKEN: Final = "tok-test-open-mode"  # noqa: S105 - a placeholder, never a real credential
COOKIE_NAME: Final = "__Host-appmux_open"
USER_ACCESS_HEADER: Final = "x-forwarded-access-token"
REQUEST_TIMEOUT_S: Final = 30.0
API: Final = "/_appmux/api/v1"


@dataclass(frozen=True, slots=True)
class Target:
    base_url: str
    app: str
    admin_email: str


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"set {name} for the open-mode run (see the module docstring)")
    return value


@pytest.fixture(scope="module")
def target() -> Target:
    return Target(
        base_url=_required("APPMUX_TEST_BASE_URL").rstrip("/"),
        app=_required("APPMUX_TEST_OBO_APP"),
        admin_email=_required("APPMUX_TEST_ADMIN_EMAIL").strip().lower(),
    )


@pytest.fixture
def client(target: Target) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as session:
        yield session


def _identity(target: Target) -> dict[str, str]:
    return {"X-Forwarded-Email": target.admin_email, "X-Forwarded-Access-Token": FAKE_TOKEN}


def _choose(
    client: httpx.Client, target: Target, mode: str, *, cookie: str | None = None
) -> httpx.Response:
    headers = {**_identity(target), "Sec-Fetch-Site": "same-origin"}
    if cookie is not None:
        headers["Cookie"] = cookie
    return client.post(f"{API}/apps/{target.app}/open-mode", json={"mode": mode}, headers=headers)


def _cookie_pair(response: httpx.Response) -> str:
    """``name=value`` of the cookie the response sets (a jar would drop a Secure one over http)."""
    set_cookie = response.headers.get("set-cookie", "")
    assert set_cookie.startswith(f"{COOKIE_NAME}="), "the response did not set the open-mode cookie"
    return set_cookie.split(";", 1)[0]


@dataclass(frozen=True, slots=True)
class ChildView:
    """What the ``light_fastapi`` fixture reports of the request it received."""

    names: list[str]
    email: str | None


def _child_headers(client: httpx.Client, target: Target, *, cookie: str | None = None) -> ChildView:
    headers = _identity(target)
    if cookie is not None:
        headers["Cookie"] = cookie
    response = client.get(f"/{target.app}/headers", headers=headers)
    assert response.status_code == 200, f"the child answered {response.status_code}"
    body = response.json()
    return ChildView(
        names=[str(name) for name in body["names"]],
        email=body["values"].get("x-forwarded-email"),
    )


def _view_as_user_rows(client: httpx.Client, target: Target) -> int:
    response = client.get(
        f"{API}/audit", params={"app": target.app, "limit": 1000}, headers=_identity(target)
    )
    assert response.status_code == 200, f"the audit read answered {response.status_code}"
    return sum(
        1
        for row in response.json()["items"]
        if row["action"] == "app.view_as_user" and row["actor"] == target.admin_email
    )


def test_the_default_open_gives_the_child_no_token_but_still_the_email(
    client: httpx.Client, target: Target
) -> None:
    seen = _child_headers(client, target)

    assert USER_ACCESS_HEADER not in seen.names
    assert seen.email == target.admin_email


def test_open_as_a_user_passes_the_token_until_the_admin_switches_back(
    client: httpx.Client, target: Target
) -> None:
    rows_before = _view_as_user_rows(client, target)

    chosen = _choose(client, target, "as_user")
    assert chosen.status_code == 204
    cookie = _cookie_pair(chosen)

    as_user = _child_headers(client, target, cookie=cookie)
    assert USER_ACCESS_HEADER in as_user.names
    assert "cookie" not in as_user.names, "the edge's cookie must not reach the child"
    assert _view_as_user_rows(client, target) == rows_before + 1

    switched = _choose(client, target, "without_token", cookie=cookie)
    assert switched.status_code == 204
    assert "Max-Age=0" in switched.headers["set-cookie"]

    back = _child_headers(client, target)
    assert USER_ACCESS_HEADER not in back.names
    assert _view_as_user_rows(client, target) == rows_before + 1, "switching back writes no row"


def test_a_cookie_made_up_by_a_page_script_does_not_open_as_a_user(
    client: httpx.Client, target: Target
) -> None:
    forged = f"{COOKIE_NAME}={target.app}~AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    seen = _child_headers(client, target, cookie=forged)

    assert USER_ACCESS_HEADER not in seen.names
    assert "cookie" not in seen.names
