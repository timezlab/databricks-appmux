"""The calls the SC-001 and crash/rollback container tests share (T108, T109). Not a test file.

A small session over the admin API, as a platform admin. It runs in one of two modes:

* **inside the container** (the default): it plays the Databricks proxy by sending
  ``X-Forwarded-Email`` itself, and sends the admin's access token (``X-Forwarded-Access-Token``)
  only on the calls that need it (register does not; Update reads the workspace with it and
  ``PATCH /apps/{app}`` writes as the admin with it);
* **against the deployed App** (``APPMUX_TEST_REMOTE=1``, ``APPMUX_TEST_BASE_URL`` an ``https``
  address): the real proxy is in front of appmux, so the session sends the owner's OAuth token as
  ``Authorization: Bearer`` and the proxy puts the identity headers on the request. Nothing is
  forged, so a test can act only as the token's owner (``identity_headers`` skips a test that wants
  another person), and the admin's address is read from ``GET /me`` instead of a variable.

The session refuses to pass if an answer ever repeats the token. Request bodies are built from the
contract's own models (``appmux.admin.schemas``), so a body that stops fitting the contract fails
``test_every_body_fits_its_contract_model`` in ``test_zero_downtime.py`` without a container.

Pytest puts this folder on ``sys.path`` when it collects the tests next to it, which is how the
two test files import it. The set-up (environment variables) is described by those files;
``load_target`` fails the run loudly when one is missing, instead of passing on nothing.
"""

from __future__ import annotations

import os
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
import pytest

from appmux.admin.schemas import (
    PutAppEgressRequest,
    RegisterAppRequest,
    UpdateAppRequest,
    UpdateConfigRequest,
)

API: Final = "/_appmux/api/v1"
REMOTE_VARIABLE: Final = "APPMUX_TEST_REMOTE"
HTTPS_PORT: Final = 443
REQUEST_TIMEOUT_S: Final = 30.0
POLL_INTERVAL_S: Final = 1.0
BUDGET_MB: Final = 128
DEFAULT_RELEASE_WAIT_S: Final = 600.0
DEFAULT_STATE_WAIT_S: Final = 120.0
EGRESS_RULE: Final = {
    "action": "deny",
    "cidr": "203.0.113.0/24",
    "port": "any",
    "comment": "zero-downtime run",
}
_ERROR_TEXT_CHARS: Final = 300


@dataclass(frozen=True, slots=True)
class Target:
    base_url: str
    host: str
    port: int
    admin_email: str
    fixtures_folder: str
    user_token: str = field(repr=False)  # a credential: never in a message or a repr
    is_remote: bool = False


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} must be set for this test (see the module docstring)")
    return value


def is_remote_run() -> bool:
    return os.environ.get(REMOTE_VARIABLE) == "1"


def load_target(*, transport: httpx.BaseTransport | None = None) -> Target:
    """The target of this run; ``transport`` replaces the network for a remote run's ``GET /me``."""
    base_url = _required("APPMUX_TEST_BASE_URL").rstrip("/")
    parts = urlsplit(base_url)
    remote = is_remote_run()
    if remote:
        if parts.scheme != "https" or not parts.hostname:
            pytest.fail("APPMUX_TEST_BASE_URL of a remote run must be the App's https address")
        port = parts.port or HTTPS_PORT
    else:
        if parts.scheme != "http" or not parts.hostname or parts.port is None:
            pytest.fail("APPMUX_TEST_BASE_URL must look like http://127.0.0.1:8000 (host and port)")
        port = parts.port
    folder = _required("APPMUX_TEST_FIXTURES_FOLDER").rstrip("/")
    if not folder.startswith("/Workspace/"):
        pytest.fail("APPMUX_TEST_FIXTURES_FOLDER must start with /Workspace/")
    token = _required("APPMUX_TEST_USER_TOKEN")
    admin_email = (
        _learn_admin(base_url, token, transport)
        if remote
        else _required("APPMUX_TEST_ADMIN_EMAIL").lower()
    )
    return Target(
        base_url=base_url,
        host=parts.hostname,
        port=port,
        admin_email=admin_email,
        fixtures_folder=folder,
        user_token=token,
        is_remote=remote,
    )


def _learn_admin(base_url: str, token: str, transport: httpx.BaseTransport | None) -> str:
    """The token owner's address, from the App itself: no e-mail is typed into a command or file."""
    try:
        with httpx.Client(
            base_url=base_url, transport=transport, timeout=REQUEST_TIMEOUT_S
        ) as client:
            response = client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        # The exception text can hold the address of the App; only its type is shown.
        pytest.fail(f"GET /me could not be made ({type(exc).__name__})", pytrace=False)
    if response.status_code != 200:
        pytest.fail(
            f"GET /me answered {response.status_code}: the token was not accepted by the App "
            "(is it a fresh OAuth token of a user who can open the App?)",
            pytrace=False,
        )
    me = response.json()
    if not me.get("is_platform_admin"):
        pytest.fail(
            "the owner of the token is not a platform admin of this appmux; deploy with "
            "APPMUX_DEPLOY_BOOTSTRAP_ADMIN set to that person (scripts/sync-deploy.sh)",
            pytrace=False,
        )
    return str(me["email"]).lower()


def identity_headers(
    target: Target,
    *,
    person: str | None = None,
    with_token: bool = False,
    mutating: bool = False,
) -> dict[str, str]:
    """The headers that say who is calling, for the mode of this run.

    Inside the container the test plays the proxy: ``X-Forwarded-Email`` is ``person`` (default the
    admin) and ``X-Forwarded-Access-Token`` is added when ``with_token``. Against the deployed App
    the real proxy does that from the bearer token, which is always sent and always the admin's, so
    a call as anyone else cannot be made and the test is skipped. ``mutating`` adds the same-origin
    mark every write needs.
    """
    headers: dict[str, str] = {}
    if mutating:
        headers["Sec-Fetch-Site"] = "same-origin"
    if target.is_remote:
        if person is not None and person.lower() != target.admin_email:
            pytest.skip(
                "a remote run acts only as the owner of the bearer token, so it cannot act as "
                "another person; run this test inside the container"
            )
        headers["Authorization"] = f"Bearer {target.user_token}"
        return headers
    headers["X-Forwarded-Email"] = person or target.admin_email
    if with_token:
        headers["X-Forwarded-Access-Token"] = target.user_token
    return headers


def register_body(*, slug: str, name: str, folder: str, owner: str) -> dict[str, Any]:
    return RegisterAppRequest(
        slug=slug, display_name=name, source_path=folder, memory_budget_mb=BUDGET_MB, owner=owner
    ).model_dump(mode="json")


def source_path_body(*, version: int, folder: str) -> dict[str, Any]:
    return UpdateAppRequest(version=version, source_path=folder).model_dump(mode="json")


def egress_body(*, version: int, rules: list[dict[str, str]]) -> dict[str, Any]:
    return PutAppEgressRequest.model_validate({"version": version, "rules": rules}).model_dump(
        mode="json"
    )


def config_body(*, config: dict[str, Any], env: list[dict[str, Any]]) -> dict[str, Any]:
    return UpdateConfigRequest.model_validate(
        {
            "version": config["version"],
            "env": env,
            "wheel_sources": config["wheel_sources"],
            "health_path": config["health_path"],
            "health_deadline_s": config["health_deadline_s"],
            "own_sp_client_id_key": config["own_sp_client_id_key"],
            "own_sp_client_secret_key": config["own_sp_client_secret_key"],
        }
    ).model_dump(mode="json")


def error_code(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return "(no error body)"


class AdminSession:
    """Every call is made as the platform admin and says which statuses it expected."""

    def __init__(self, client: httpx.Client, target: Target) -> None:
        self._client = client
        self.target = target
        self.admin = target.admin_email
        self.created_apps: list[str] = []

    def call(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        expect: tuple[int, ...] = (200,),
        with_token: bool = False,
    ) -> httpx.Response:
        headers = identity_headers(self.target, with_token=with_token, mutating=method != "GET")
        response = self._client.request(method, f"{API}{path}", json=json, headers=headers)
        assert self.target.user_token not in response.text, (
            f"{method} {path}: the answer repeated the access token"
        )
        assert response.status_code in expect, (
            f"{method} {path}: expected {list(expect)}, got {response.status_code} "
            f"{error_code(response)}: {response.text[:_ERROR_TEXT_CHARS]}"
        )
        return response

    def read(self, path: str) -> Any:
        return self.call("GET", path).json()

    def app(self, slug: str) -> dict[str, Any]:
        detail: dict[str, Any] = self.read(f"/apps/{slug}")
        return detail

    def release(self, slug: str, release_no: int) -> dict[str, Any]:
        detail: dict[str, Any] = self.read(f"/apps/{slug}/releases/{release_no}")
        return detail

    # --- steps --------------------------------------------------------------------------------

    def register(self, slug: str, *, fixture: str, name: str | None = None) -> dict[str, Any]:
        folder = f"{self.target.fixtures_folder}/{fixture}"
        body = register_body(slug=slug, name=name or slug, folder=folder, owner=self.admin)
        response = self.call("POST", "/apps", json=body, expect=(200, 201))
        self.created_apps.append(slug)
        detail: dict[str, Any] = response.json()
        return detail

    def point_at(self, slug: str, fixture: str) -> None:
        """Make the next Update read the folder of ``fixture`` (the ``PATCH`` sends the token)."""
        version = self.app(slug)["version"]
        folder = f"{self.target.fixtures_folder}/{fixture}"
        self.call(
            "PATCH",
            f"/apps/{slug}",
            json=source_path_body(version=version, folder=folder),
            with_token=True,
        )

    def press_update(self, slug: str) -> int:
        response = self.call("POST", f"/apps/{slug}/releases", expect=(200, 202), with_token=True)
        body = response.json()
        assert body["status"] != "up_to_date", (
            f"Update of {slug} found its folder unchanged, so no release was made"
        )
        return int(body["release_no"])

    def wait_for_release(
        self, slug: str, release_no: int, status: str, *, wait_s: float | None = None
    ) -> dict[str, Any]:
        limit = wait_s if wait_s is not None else _wait_s("APPMUX_TEST_BUILD_WAIT_S")
        deadline = time.monotonic() + limit
        while True:
            detail = self.release(slug, release_no)
            if detail["status"] == status:
                return detail
            assert detail["status"] != "failed" or status == "failed", (
                f"release {release_no} of {slug} failed: {detail['status_reason']}"
            )
            assert time.monotonic() < deadline, (
                f"release {release_no} of {slug} is still {detail['status']} after "
                f"{limit:g} s, expected {status}"
            )
            time.sleep(POLL_INTERVAL_S)

    def wait_for_state(
        self, slug: str, state: str, *, wait_s: float | None = None
    ) -> dict[str, Any]:
        limit = (
            wait_s
            if wait_s is not None
            else _wait_s("APPMUX_TEST_STATE_WAIT_S", DEFAULT_STATE_WAIT_S)
        )
        deadline = time.monotonic() + limit
        while True:
            detail = self.app(slug)
            if detail["state"] == state:
                return detail
            assert time.monotonic() < deadline, (
                f"{slug} is still {detail['state']} after {limit:g} s, expected {state}"
            )
            time.sleep(POLL_INTERVAL_S)

    def open_app(self, slug: str, path: str = "/") -> httpx.Response:
        """``GET /<slug><path>`` through the edge, as the admin: what a visitor would get."""
        return self._client.get(f"/{slug}{path}", headers=identity_headers(self.target))

    def delete_created_apps(self) -> None:
        for slug in reversed(self.created_apps):
            response = self._client.request(
                "DELETE",
                f"{API}/apps/{slug}",
                headers=identity_headers(self.target, mutating=True),
            )
            if response.status_code not in {204, 404}:
                warnings.warn(
                    f"could not delete the test app {slug} (status {response.status_code}); "
                    "remove it by hand",
                    stacklevel=2,
                )


def _wait_s(name: str, default: float = DEFAULT_RELEASE_WAIT_S) -> float:
    return float(os.environ.get(name, default))
