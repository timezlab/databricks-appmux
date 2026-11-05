"""A service principal calls a child through the Apps proxy, with and without a grant (ADR 0020).

[ADR 0020](docs/decisions/0020-let-a-service-principal-hold-an-app-grant.md) adds the
``service_principal`` grant: another App's service principal arrives with its application id in
``X-Forwarded-Email`` and no token, and a grant that names that id admits it, alone. The unit and
contract tests prove the policy; this file measures the whole path, through the real proxy, with a
throwaway service principal that is not an App:

1. with no ``CAN_USE`` on the appmux App the **Apps proxy** refuses the call (layer one);
2. with ``CAN_USE`` the call reaches appmux and a child that holds no grant for the principal
   answers ``404``, the same answer as for an app that does not exist (layer two);
3. a ``service_principal`` grant for a *different* application id changes nothing;
4. a grant for this principal opens the child: ``200``, and the child sees the application id in
   ``X-Forwarded-Email`` and no ``X-Forwarded-Access-Token``;
5. removing the grant closes the child again;
6. taking ``CAN_USE`` back (the direct permission list is put back exactly as it was, and the
   test checks it entry by entry) makes the proxy refuse the principal again, and the refusal must
   last ``REFUSAL_STABLE_FOR_S``. **That takes about an hour**: on 2026-11-04 the proxy kept
   admitting the principal for 59 to 61 minutes after the revoke, through an App restart, with no
   flapping (a 600 s limit, the first guess, failed after 10 minutes). So the wait is opt-in
   (``APPMUX_TEST_WAIT_FOR_REVOKE=1``); without it the test still takes the permission back and
   checks the list, and skips the wait. What closes a child to a principal at once is appmux's
   own grant (test 5), not this permission.

It runs only from the dev machine (``scripts/run-remote-tests.sh``): the real proxy is the thing
under test, so a run inside the container, which plays the proxy, is skipped. The child is the
``light_fastapi`` fixture, whose ``GET /headers`` lists the identity headers it received.

It changes one thing outside appmux's registry: ``CAN_USE`` for the throwaway service principal on
the appmux App. A snapshot of the App's direct permission list is taken first and put back with
``set-permissions`` at the end, whatever happened, and checked entry by entry
(``app_acl.py``). It prints no principal, id, token or address.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``, ``APPMUX_TEST_USER_TOKEN`` and ``APPMUX_TEST_FIXTURES_FOLDER``: as for
  the other container tests (``ops_session.py``); the token's owner must be a platform admin and
  must manage the appmux App;
* ``APPMUX_TEST_TOKEN_PROFILE``: that person's Databricks CLI profile, used for the permissions of
  the App and to read the service principal's client id and secret;
* ``APPMUX_TEST_OWN_SP_ID_REF`` and ``APPMUX_TEST_OWN_SP_SECRET_REF``: ``scope/key`` references of
  the throwaway service principal's client id and client secret, readable by that person;
* ``APPMUX_TEST_OWN_SP_APPLICATION_ID``: its application id (not a secret);
* ``APPMUX_APP_NAME``: the appmux App, default ``appmux``.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import time
import uuid
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

import httpx
import pytest
from app_acl import AclError, AppAcl, Entry
from ops_session import (
    REQUEST_TIMEOUT_S,
    TOKEN_PROFILE_VARIABLE,
    AdminSession,
    load_target,
)

pytestmark = pytest.mark.integration

FIXTURE: Final = "light_fastapi"
SLUG_SUFFIX_BYTES: Final = 3
CLI_TIMEOUT_S: Final = 60.0
OIDC_TIMEOUT_S: Final = 60.0
POLL_PAUSE_S: Final = 3.0
# What the Apps proxy answers a caller it does not let in. Anything else came from appmux.
PROXY_REFUSALS: Final = frozenset({302, 401, 403})
# How long a permission of the Apps proxy may take to be seen. Measured, not assumed: the run prints
# the seconds it took, and this limit only stops a run that waits for ever.
PROXY_OPENS_WITHIN_S: Final = 300.0
# appmux's own grants are read from its registry snapshot, a moment after the write.
ROUTE_CHANGES_WITHIN_S: Final = 30.0
# A refusal after a revoke counts once it has lasted this long (see test_credential_modes.py).
REFUSAL_STABLE_FOR_S: Final = 60.0
# Measured on 2026-11-04 (Free Edition): 59 to 61 minutes. The limit leaves room for a slower day.
REVOKE_SETTLES_WITHIN_S: Final = 5400.0
REVOKE_POLL_PAUSE_S: Final = 30.0
WAIT_FOR_REVOKE_VARIABLE: Final = "APPMUX_TEST_WAIT_FOR_REVOKE"
# How long a grant for somebody else is watched to see that it did not admit this principal.
NEGATIVE_WATCH_S: Final = 12.0


@dataclass(frozen=True, slots=True)
class Setup:
    profile: str
    app_name: str
    id_reference: str
    secret_reference: str
    application_id: str


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} must be set for this test (see the module docstring)")
    return value


def _cli_json(profile: str, *arguments: str) -> dict[str, object]:
    done = subprocess.run(
        ["databricks", *arguments, "--profile", profile, "-o", "json"],
        capture_output=True,
        check=False,
        timeout=CLI_TIMEOUT_S,
    )
    if done.returncode != 0:
        # The output can carry the workspace address; only the command and the code are shown.
        pytest.fail(f"`databricks {' '.join(arguments[:2])}` failed (exit code {done.returncode})")
    answer: dict[str, object] = json.loads(done.stdout)
    return answer


def _secret_value(profile: str, reference: str) -> str:
    scope, _, key = reference.partition("/")
    answer = _cli_json(profile, "secrets", "get-secret", scope, key)
    return base64.b64decode(str(answer["value"])).decode()


def _service_principal_token(setup: Setup) -> str:
    """An OAuth token of the throwaway service principal, from the workspace's token endpoint."""
    details = _cli_json(setup.profile, "auth", "describe")["details"]
    assert isinstance(details, dict)
    host = str(details["host"]).rstrip("/")
    client_id = _secret_value(setup.profile, setup.id_reference)
    client_secret = _secret_value(setup.profile, setup.secret_reference)
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        response = httpx.post(
            f"{host}/oidc/v1/token",
            data={"grant_type": "client_credentials", "scope": "all-apis"},
            headers={"Authorization": f"Basic {basic}"},
            timeout=OIDC_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        pytest.fail(f"the token endpoint could not be reached ({type(exc).__name__})")
    if response.status_code != 200:
        pytest.fail(f"the token endpoint answered {response.status_code} for the service principal")
    return str(response.json()["access_token"])


class Scene:
    """One child, one throwaway service principal, and the appmux App's permissions around them."""

    def __init__(
        self,
        *,
        session: AdminSession,
        caller: httpx.Client,
        token: str,
        acl: AppAcl,
        snapshot: list[Entry],
        setup: Setup,
        slug: str,
    ) -> None:
        self.session = session
        self.setup = setup
        self.slug = slug
        self._caller = caller
        self._token = token
        self._acl = acl
        self._snapshot = snapshot
        self.acl_is_back = False

    def call(self, path: str) -> httpx.Response:
        """``GET path`` through the Apps proxy as the throwaway service principal."""
        response = self._caller.get(path, headers={"Authorization": f"Bearer {self._token}"})
        assert self._token not in response.text, f"GET {path}: the answer repeated the token"
        return response

    def child(self, path: str = "/headers") -> str:
        return f"/{self.slug}{path}"

    def wait_for(
        self, path: str, wanted: Callable[[int], bool], *, within_s: float, what: str
    ) -> tuple[httpx.Response, float]:
        """Call until the status satisfies ``wanted``; returns that answer and the seconds taken."""
        started = time.monotonic()
        seen: set[int] = set()
        while True:
            response = self.call(path)
            seen.add(response.status_code)
            elapsed_s = time.monotonic() - started
            if wanted(response.status_code):
                return response, elapsed_s
            assert elapsed_s < within_s, (
                f"{what}: still not there after {within_s:g} s; statuses seen: {sorted(seen)}"
            )
            time.sleep(POLL_PAUSE_S)

    def grant(self, application_id: str) -> None:
        body = {"type": "service_principal", "name": application_id, "permission": "CAN_USE"}
        self.session.call("PUT", f"/apps/{self.slug}/grants", json=body)

    def remove_grant(self, application_id: str) -> None:
        path = f"/apps/{self.slug}/grants/service_principal/{application_id}"
        self.session.call("DELETE", path, expect=(204,))

    def give_can_use(self) -> None:
        self._acl.give_can_use(self.setup.application_id)
        self.acl_is_back = False

    def put_the_permissions_back(self) -> None:
        self._acl.put_back(self._snapshot)
        self.acl_is_back = True

    def wait_for_a_settled_refusal(self, path: str) -> float:
        """Seconds until the proxy has refused the principal ``REFUSAL_STABLE_FOR_S`` in a row."""
        started = time.monotonic()
        refused_since: float | None = None
        while True:
            now = time.monotonic()
            if self.call(path).status_code not in PROXY_REFUSALS:
                refused_since = None
            elif refused_since is None:
                refused_since = now
            if refused_since is not None and now - refused_since >= REFUSAL_STABLE_FOR_S:
                return now - started
            assert now - started < REVOKE_SETTLES_WITHIN_S, (
                f"the proxy still lets the principal in {REVOKE_SETTLES_WITHIN_S:g} s after "
                "CAN_USE was taken back"
            )
            time.sleep(REVOKE_POLL_PAUSE_S)


@pytest.fixture(scope="module")
def scene() -> Iterator[Scene]:
    target = load_target()
    if not target.is_remote:
        pytest.skip("the Apps proxy is the thing under test: run this from the dev machine")
    setup = Setup(
        profile=_required(TOKEN_PROFILE_VARIABLE),
        app_name=os.environ.get("APPMUX_APP_NAME", "appmux").strip() or "appmux",
        id_reference=_required("APPMUX_TEST_OWN_SP_ID_REF"),
        secret_reference=_required("APPMUX_TEST_OWN_SP_SECRET_REF"),
        application_id=_required("APPMUX_TEST_OWN_SP_APPLICATION_ID").lower(),
    )
    token = _service_principal_token(setup)
    acl = AppAcl(setup.profile, setup.app_name)
    snapshot = acl.read()
    with (
        httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as admin_client,
        httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as caller,
    ):
        session = AdminSession(admin_client, target)
        slug = f"spgrant-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"
        current = Scene(
            session=session,
            caller=caller,
            token=token,
            acl=acl,
            snapshot=snapshot,
            setup=setup,
            slug=slug,
        )
        try:
            session.register(slug, fixture=FIXTURE)
            release_no = session.press_update(slug)
            session.wait_for_release(slug, release_no, "active")
            session.wait_for_state(slug, "running")
            assert session.open_app(slug, "/headers").status_code == 200, (
                "the child does not answer the platform admin, so nothing below could be read"
            )
            yield current
        finally:
            session.delete_created_apps()
            if not current.acl_is_back:
                _put_back_or_warn(current)


@pytest.fixture(scope="module")
def can_use(scene: Scene) -> Iterator[None]:
    """``CAN_USE`` on the appmux App for the throwaway principal; taken back at the end."""
    scene.give_can_use()
    yield
    if not scene.acl_is_back:
        _put_back_or_warn(scene)


def _put_back_or_warn(scene: Scene) -> None:
    try:
        scene.put_the_permissions_back()
    except AclError as exc:
        warnings.warn(
            f"the appmux App's permissions were not put back: {exc}; read them with "
            "`databricks apps get-permissions` and put them right by hand",
            stacklevel=2,
        )


def test_the_proxy_refuses_a_service_principal_that_has_no_can_use_on_appmux(scene: Scene) -> None:
    response = scene.call(scene.child())

    assert response.status_code in PROXY_REFUSALS, (
        f"expected a refusal by the proxy ({sorted(PROXY_REFUSALS)}), got {response.status_code}"
    )


def test_with_can_use_the_call_reaches_appmux_and_a_child_without_a_grant_is_not_found(
    scene: Scene, can_use: None
) -> None:
    response, opened_after_s = scene.wait_for(
        scene.child(),
        lambda status: status not in PROXY_REFUSALS,
        within_s=PROXY_OPENS_WITHIN_S,
        what="the proxy letting the principal in after CAN_USE",
    )
    print(f"measured: the proxy let the principal in {opened_after_s:.0f} s after CAN_USE")

    assert response.status_code == 404
    unknown = scene.call(f"/nobody-{scene.slug}/headers")
    assert unknown.status_code == 404
    assert response.text == unknown.text, "a child without a grant must look like a missing one"


def test_a_grant_for_a_different_application_id_does_not_admit_the_principal(
    scene: Scene, can_use: None
) -> None:
    other = str(uuid.uuid4())
    scene.grant(other)
    try:
        deadline = time.monotonic() + NEGATIVE_WATCH_S
        while time.monotonic() < deadline:
            assert scene.call(scene.child()).status_code == 404
            time.sleep(POLL_PAUSE_S)
    finally:
        scene.remove_grant(other)


def test_a_grant_for_the_principal_opens_the_child_to_it_with_its_application_id(
    scene: Scene, can_use: None
) -> None:
    scene.grant(scene.setup.application_id)

    response, opened_after_s = scene.wait_for(
        scene.child(),
        lambda status: status != 404,
        within_s=ROUTE_CHANGES_WITHIN_S,
        what="the child opening after the grant",
    )
    print(f"measured: the child opened {opened_after_s:.0f} s after the grant")

    assert response.status_code == 200
    seen = response.json()
    assert str(seen["values"]["x-forwarded-email"]).lower() == scene.setup.application_id
    assert "x-forwarded-access-token" not in seen["names"], "an SP call carries no visitor token"


def test_removing_the_grant_closes_the_child_again(scene: Scene, can_use: None) -> None:
    scene.remove_grant(scene.setup.application_id)

    _, closed_after_s = scene.wait_for(
        scene.child(),
        lambda status: status == 404,
        within_s=ROUTE_CHANGES_WITHIN_S,
        what="the child closing after the grant was removed",
    )
    print(f"measured: the child closed {closed_after_s:.0f} s after the grant was removed")

    for _ in range(3):
        time.sleep(1.0)
        assert scene.call(scene.child()).status_code == 404


def test_taking_can_use_back_makes_the_proxy_refuse_the_principal_again(
    scene: Scene, can_use: None
) -> None:
    scene.put_the_permissions_back()
    if os.environ.get(WAIT_FOR_REVOKE_VARIABLE) != "1":
        pytest.skip(
            "the permissions are back as they were; the proxy takes about an hour to stop "
            f"admitting the principal (measured 2026-11-04), so set {WAIT_FOR_REVOKE_VARIABLE}=1 "
            "to wait for it"
        )

    settled_after_s = scene.wait_for_a_settled_refusal(scene.child())

    print(
        f"measured: the proxy refused the principal for good {settled_after_s:.0f} s after revoke"
    )
