"""Credential modes of a child, against a running appmux in a Databricks container (T147, US8).

Two children of the hostile ``secret_registry_snooper`` fixture, which builds its Databricks
credential from the environment with the unmodified SDK, asks the API who it is (``/report``) and
tries other apps' secrets and the registry:

* **an own-SP child** (design 8.5): two ``secret`` resources hold a service principal's client
  id and client secret, ``own_sp_client_id_key`` and ``own_sp_client_secret_key`` name them,
  and the child starts with ``DATABRICKS_CLIENT_ID`` and ``DATABRICKS_CLIENT_SECRET``. Its
  token belongs to that service principal and its reach is exactly what that service principal
  was granted;
* **a shared-SP child**: the platform admin turns the master switch on and delegates ``shared_sp``.
  appmux mints the token and keeps a file in the child's run directory current; the child gets
  ``DATABRICKS_AUTH_TYPE=databricks-cli`` and ``DATABRICKS_CLI_PATH`` and never a static token. The
  token lives an hour, so a child that kept working past that hour proves the file was renewed
  (spike 10: a static ``DATABRICKS_TOKEN`` failed from the first call after the expiry).

What the tests assert, and what they leave to a person:

* each child's token belongs to the identity its mode names (``token_identity``);
* the shared-SP child keeps asking the API successfully for longer than a token lives (the last
  test). A shorter run needs an appmux that advertises a short life (spike 10 used 90 s); this file
  cannot turn that on, so the default waits across the real hour;
* the snooper's containment flags. Every attempt without a token is refused. With a token, reading
  secrets of scopes that the service principal was not granted is refused. For the shared SP the
  only attempt allowed to succeed is the registry login, the accepted residual of design 8.4 (the
  appmux SP's low-privilege registry role, TD-002 in ``docs/tech-debt.md``); any other success, an
  inconclusive attempt or a skipped one fails the test, because a probe that cannot see must not
  pass. Listing secret scopes is not in that residual. The first run (2026-11-04, against the dev
  App on the Free Edition workspace) found that a child's token lists every scope name in both
  modes while reading any value stays refused, so both containment tests fail on ``list_scopes``
  until the owner decides: TD-027 in ``docs/tech-debt.md``. A success there is a finding to record,
  not a number to loosen.

It does not run in the default gate: it is marked ``integration`` and skipped unless
``APPMUX_TEST_CONTAINER=1``. Two ways to run it, both through ``ops_session.py``:
``scripts/run-remote-tests.sh`` runs it from the dev machine against the deployed App as the owner
of the CLI token (the way the 2026-11-04 run went; results in ``specs/001-appmux-v1/spikes/``),
and on a container the test can play the Databricks proxy itself with identity headers. A run
longer than one token life renews the CLI token (``APPMUX_TEST_TOKEN_PROFILE``). No token, client
id or secret value is read, printed or asserted by value; a report that repeated the admin's token
fails.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing). The
platform setting ``attach_check_warehouse_id`` must name a SQL warehouse the admin can use, because
attaching a ``secret`` checks it with ``secret()``:

* ``APPMUX_TEST_BASE_URL``, ``APPMUX_TEST_ADMIN_EMAIL``, ``APPMUX_TEST_USER_TOKEN`` and
  ``APPMUX_TEST_FIXTURES_FOLDER``: as for the other container tests (``ops_session.py``);
* ``APPMUX_TEST_OWN_SP_ID_REF`` and ``APPMUX_TEST_OWN_SP_SECRET_REF``: ``scope/key`` references of a
  throwaway service principal's client id and client secret, readable by the admin;
* ``APPMUX_TEST_OWN_SP_APPLICATION_ID`` and ``APPMUX_TEST_APPMUX_SP_APPLICATION_ID``: the
  application ids those two identities have (not secrets);
* ``APPMUX_TEST_FOREIGN_SECRET_REFS``: comma-separated ``scope/key`` references of secrets that
  neither service principal may read;
* ``APPMUX_TEST_REGISTRY_HOST``: the registry's host; ``APPMUX_TEST_REGISTRY_PORT`` and
  ``APPMUX_TEST_REGISTRY_DATABASE`` are optional. Set ``APPMUX_TEST_REGISTRY_USER`` to the appmux
  SP's application id too: without it the snooper's registry login without a token is *not
  attempted*, and a skipped attempt fails the containment test by design;
* ``APPMUX_TEST_TOKEN_CYCLE_S`` (4200) and ``APPMUX_TEST_TOKEN_POLL_S`` (300): how long the
  shared-SP child is polled, and how often.
"""

from __future__ import annotations

import os
import secrets
import time
import warnings
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest
from ops_session import (
    REQUEST_TIMEOUT_S,
    AdminSession,
    config_body,
    error_code,
    identity_headers,
    load_target,
)

from appmux.admin.schemas import AttachResourceRequest, DelegationReasonRequest, PutSettingRequest

pytestmark = pytest.mark.integration

SNOOPER_FIXTURE: Final = "secret_registry_snooper"
SHARED_SP_SETTING: Final = "shared_sp_delegation_allowed"
OWN_ID_RESOURCE: Final = "sp-client-id"
OWN_SECRET_RESOURCE: Final = "sp-client-secret"  # noqa: S105 - a resource key, not a credential
# The snooper tries the registry and secrets with and without a token; it takes a few seconds each.
REPORT_TIMEOUT_S: Final = 120.0
REPORT_READY_WAIT_S: Final = 240.0
POLL_PAUSE_S: Final = 2.0
DEFAULT_CYCLE_S: Final = 4200.0  # past the 3600 s a minted token lives
DEFAULT_POLL_S: Final = 300.0
SLUG_SUFFIX_BYTES: Final = 3
# Design 8.4, exposure (1): the appmux SP's registry role. Nothing else may succeed with its token.
ACCEPTED_SHARED_SP_RESIDUAL: Final = frozenset({"with token: registry login"})


@dataclass(frozen=True, slots=True)
class Setup:
    own_id_ref: str
    own_secret_ref: str
    own_application_id: str
    appmux_application_id: str
    foreign_refs: str
    registry_host: str
    registry_port: str | None
    registry_database: str | None
    registry_user: str | None
    cycle_s: float
    poll_s: float


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} must be set for this test (see the module docstring)")
    return value


def _optional(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


@pytest.fixture(scope="module")
def run_setup() -> Setup:
    return Setup(
        own_id_ref=_required("APPMUX_TEST_OWN_SP_ID_REF"),
        own_secret_ref=_required("APPMUX_TEST_OWN_SP_SECRET_REF"),
        own_application_id=_required("APPMUX_TEST_OWN_SP_APPLICATION_ID"),
        appmux_application_id=_required("APPMUX_TEST_APPMUX_SP_APPLICATION_ID"),
        foreign_refs=_required("APPMUX_TEST_FOREIGN_SECRET_REFS"),
        registry_host=_required("APPMUX_TEST_REGISTRY_HOST"),
        registry_port=_optional("APPMUX_TEST_REGISTRY_PORT"),
        registry_database=_optional("APPMUX_TEST_REGISTRY_DATABASE"),
        registry_user=_optional("APPMUX_TEST_REGISTRY_USER"),
        cycle_s=float(os.environ.get("APPMUX_TEST_TOKEN_CYCLE_S", DEFAULT_CYCLE_S)),
        poll_s=float(os.environ.get("APPMUX_TEST_TOKEN_POLL_S", DEFAULT_POLL_S)),
    )


def snooper_env(setup: Setup) -> list[dict[str, Any]]:
    """The env entries that aim the snooper (all non-secret; see its docstring)."""
    entries = {
        "SNOOP_SECRET_REFS": setup.foreign_refs,
        "SNOOP_REGISTRY_HOST": setup.registry_host,
        "SNOOP_REGISTRY_PORT": setup.registry_port,
        "SNOOP_REGISTRY_DATABASE": setup.registry_database,
        "SNOOP_REGISTRY_USER": setup.registry_user,
    }
    return [{"name": name, "value": value} for name, value in entries.items() if value]


def attempt_names(entries: Iterable[str]) -> list[str]:
    """The attempt of each ``"<case>: <attempt>"`` entry of the snooper's report.

    The assertion already says which case it judges, and a message that repeats ``with token: ...``
    is blanked by the remote runner's masking (it hides whatever follows ``token:``), which hid
    exactly the names the failure needed.
    """
    return sorted(entry.partition(": ")[2] for entry in entries)


def attempts_without_token(report: dict[str, Any]) -> str:
    """What each attempt made without a credential did, for a failure message (no values)."""
    attempts: dict[str, Any] = report["without_token"] or {}
    return "; ".join(
        f"{name}: {result.get('outcome')} ({result.get('detail')})"
        for name, result in attempts.items()
    )


class Children:
    """The two children, built through the admin API and read through the edge."""

    def __init__(self, client: httpx.Client, session: AdminSession, setup: Setup) -> None:
        self._client = client
        self.session = session
        self.setup = setup
        self.suffix = secrets.token_hex(SLUG_SUFFIX_BYTES)
        self.shared_sp_before: bool | int | str | None = None

    def start_snooper(self, slug: str) -> dict[str, Any]:
        """Register, Update and wait until the snooper runs; returns the app's detail."""
        session = self.session
        session.register(slug, fixture=SNOOPER_FIXTURE)
        release_no = session.press_update(slug)
        session.wait_for_release(slug, release_no, "active")
        session.wait_for_state(slug, "running")
        return session.app(slug)

    def configure(self, slug: str, *, own_sp: bool) -> int | None:
        """Save the env entries and, for ``own_sp``, the two key names; returns the new release."""
        detail = self.session.app(slug)
        config = dict(detail["config"])
        if own_sp:
            config["own_sp_client_id_key"] = OWN_ID_RESOURCE
            config["own_sp_client_secret_key"] = OWN_SECRET_RESOURCE
        body = config_body(config=config, env=snooper_env(self.setup))
        response = self.session.call("PUT", f"/apps/{slug}/config", json=body)
        release_no = response.json()["release_no"]
        if release_no is not None:
            self.session.wait_for_release(slug, release_no, "active")
        return release_no if isinstance(release_no, int) else None

    def attach_secret(self, slug: str, key: str, reference: str) -> None:
        body = AttachResourceRequest(type="secret", target=reference, permission="READ")
        self.session.call(
            "PUT",
            f"/apps/{slug}/resources/{key}",
            json=body.model_dump(mode="json", exclude_none=True),
            with_token=True,
        )

    def delegate_shared_sp(self, slug: str) -> None:
        detail = self.session.app(slug)
        body = DelegationReasonRequest(
            version=detail["version"], reason="the credential-modes test lends the shared SP"
        )
        self.session.call(
            "PUT",
            f"/apps/{slug}/delegation/shared_sp",
            json=body.model_dump(mode="json"),
            with_token=True,
        )

    def set_shared_sp_switch(self, value: bool) -> None:
        self.session.call(
            "PUT",
            f"/platform/settings/{SHARED_SP_SETTING}",
            json=PutSettingRequest(value=value).model_dump(mode="json"),
        )

    def report(self, slug: str) -> dict[str, Any]:
        """The snooper's ``/report`` through the edge, as the admin; it never holds a token."""
        response = self._client.get(
            f"/{slug}/report",
            headers=identity_headers(self.session.target),
            timeout=REPORT_TIMEOUT_S,
        )
        assert response.status_code == 200, (
            f"/{slug}/report answered {response.status_code} {error_code(response)}"
        )
        repeated = self.session.target.bearer() in response.text
        assert not repeated, f"/{slug}/report repeated the admin's access token"
        body: dict[str, Any] = response.json()
        return body

    def wait_for_credential(self, slug: str) -> dict[str, Any]:
        """Poll ``/report`` until the child reports a credential (a restart takes time)."""
        deadline = time.monotonic() + REPORT_READY_WAIT_S
        last = "no answer yet"
        while time.monotonic() < deadline:
            try:
                report = self.report(slug)
            except (AssertionError, httpx.HTTPError) as problem:
                last = type(problem).__name__
            else:
                if report["token_delivered"] is True:
                    return report
                last = "the child reports no credential"
            time.sleep(POLL_PAUSE_S)
        pytest.fail(f"/{slug}/report: {last} after {REPORT_READY_WAIT_S:g} s")

    def restore(self) -> None:
        if self.shared_sp_before is True:
            return
        response = self.session.call(
            "PUT",
            f"/platform/settings/{SHARED_SP_SETTING}",
            json=PutSettingRequest(value=self.shared_sp_before).model_dump(mode="json"),
            expect=(200, 400, 409),
        )
        if response.status_code != 200:
            warnings.warn(
                f"could not put {SHARED_SP_SETTING} back (status {response.status_code}); "
                "set it by hand",
                stacklevel=2,
            )


@pytest.fixture(scope="module")
def children(run_setup: Setup) -> Iterator[Children]:
    target = load_target()
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        session = AdminSession(client, target)
        scene = Children(client, session, run_setup)
        scene.shared_sp_before = session.read("/platform")["settings"][SHARED_SP_SETTING]
        try:
            yield scene
        finally:
            session.delete_created_apps()
            scene.restore()


@pytest.fixture(scope="module")
def own_sp_child(children: Children, run_setup: Setup) -> str:
    slug = f"cred-own-{children.suffix}"
    children.start_snooper(slug)
    children.attach_secret(slug, OWN_ID_RESOURCE, run_setup.own_id_ref)
    children.attach_secret(slug, OWN_SECRET_RESOURCE, run_setup.own_secret_ref)
    children.configure(slug, own_sp=True)
    children.session.wait_for_state(slug, "running")
    return slug


@pytest.fixture(scope="module")
def shared_sp_child(children: Children) -> str:
    slug = f"cred-shared-{children.suffix}"
    children.start_snooper(slug)
    children.configure(slug, own_sp=False)
    children.set_shared_sp_switch(True)
    children.delegate_shared_sp(slug)
    # The credential is built when the child starts, so the delegation reaches it on a restart.
    children.session.call("POST", f"/apps/{slug}/restart")
    children.wait_for_credential(slug)
    return slug


def test_an_own_sp_child_calls_the_api_as_its_own_service_principal(
    children: Children, own_sp_child: str, run_setup: Setup
) -> None:
    report = children.wait_for_credential(own_sp_child)

    assert report["token_identity"] == run_setup.own_application_id


def test_an_own_sp_child_reaches_neither_other_apps_secrets_nor_the_registry(
    children: Children, own_sp_child: str
) -> None:
    report = children.report(own_sp_child)

    assert report["without_token_contained"] is True, attempts_without_token(report)
    assert report["with_token_contained"] is True, (
        f"succeeded {attempt_names(report['succeeded'])}, "
        f"inconclusive {attempt_names(report['inconclusive'])}, "
        f"not attempted {attempt_names(report['not_attempted'])}"
    )


def test_a_shared_sp_child_calls_the_api_as_the_appmux_service_principal(
    children: Children, shared_sp_child: str, run_setup: Setup
) -> None:
    report = children.report(shared_sp_child)

    assert report["token_delivered"] is True
    assert report["token_identity"] == run_setup.appmux_application_id


def test_a_shared_sp_child_reaches_neither_other_apps_secrets_nor_the_registry_beyond_the_residual(
    children: Children, shared_sp_child: str
) -> None:
    report = children.report(shared_sp_child)

    assert report["without_token_contained"] is True, attempts_without_token(report)
    assert report["inconclusive"] == [] and report["not_attempted"] == []
    reads = {
        name: result for name, result in report["with_token"].items() if name.startswith("read ")
    }
    assert reads, (
        "the snooper tried no secret read; APPMUX_TEST_FOREIGN_SECRET_REFS was not applied"
    )
    assert {result["outcome"] for result in reads.values()} == {"refused"}
    beyond_the_residual = set(report["succeeded"]) - ACCEPTED_SHARED_SP_RESIDUAL
    assert not beyond_the_residual, (
        f"succeeded beyond the residual: {attempt_names(beyond_the_residual)}; "
        f"only {attempt_names(ACCEPTED_SHARED_SP_RESIDUAL)} is accepted"
    )


def test_a_shared_sp_child_keeps_calling_the_api_across_a_token_expiry(
    children: Children, shared_sp_child: str, run_setup: Setup
) -> None:
    deadline = time.monotonic() + run_setup.cycle_s
    calls = 0
    while True:
        report = children.report(shared_sp_child)
        calls += 1
        assert report["token_delivered"] is True, f"call {calls}: no credential"
        assert report["token_identity"] == run_setup.appmux_application_id, f"call {calls}"
        if time.monotonic() >= deadline:
            break
        time.sleep(run_setup.poll_s)
    assert calls >= 2, "the cycle was too short to see more than one call"
