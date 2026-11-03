"""SC-004 as a script: every operating step through the admin API, no SQL typed (T167, FR-053).

The success criterion: "A scripted walkthrough through the UI or API runs with no SQL typed, and the
final database state matches the expectation." The steps, in the order the criterion lists them:

1. register an app directly;
2. request a new app (Grace), approve it (the platform admin);
3. request and approve an OBO delegation, a shared-SP delegation, and a settings change (name and
   memory budget);
4. rename an app by request, and ask for a rename to a slug that is free when asked and taken when
   the admin presses Approve, which closes the request by itself;
5. set an egress rule as a platform admin;
6. open another team's app as a platform admin, and read its snapshot source;
7. share an app with a user and with a Databricks group, and make someone an app admin;
8. attach resources, a secret among them, accepting offers of the release's ``databricks.yml``;
9. update, tag, label, lock, unlock and delete a release, and activate an older one;
10. restart and stop.

"The final database state" is read the only way this test may read it, through the API: the apps,
the requests, the platform settings and the audit log. The audit rows the steps must have written
are compared with the log, in order (the log may hold more rows between them; an expected row that
is missing or out of order fails the test). The apps the test created are deleted afterwards and the
setting it changed is put back; both are best effort and a failure to clean up is a warning that
names what to remove.

The test talks to the edge on the container's own port and plays the Databricks proxy by sending
the identity headers itself, for the platform admin and for the placeholder people (Grace, and the
users she shares with), which only works from inside the container. The platform admin's own token
is sent on the two kinds of call that read the workspace with it (Update and attach) and nowhere
else; no response may contain it. It does not run in the default gate: it is marked ``integration``
and skipped unless ``APPMUX_TEST_CONTAINER=1``; ``scripts/sync-deploy.sh`` puts the code on a
container. Nothing here has run against one, so no claim that the walkthrough passes exists, and a
step whose route is not mounted yet fails with the status it got.

The bodies come from the contract's own request models (``appmux.admin.schemas``). The default
gate does not collect this directory's tests (and ``tests/conftest.py`` skips them), so
``tests/unit/test_sc004_script.py`` loads this file by path and checks, without a container, that
every body still fits its model, that the order check works, and that every audit action the script
expects is a real one.

Set-up the run needs (it fails loudly when one is missing, instead of passing on nothing):

* ``APPMUX_TEST_BASE_URL``: where the edge listens, for example ``http://127.0.0.1:8000``;
* ``APPMUX_TEST_ADMIN_EMAIL``: a platform admin;
* ``APPMUX_TEST_USER_TOKEN``: that person's Databricks token (Update and attach read the workspace
  with it);
* ``APPMUX_TEST_FIXTURES_FOLDER``: a workspace folder starting with ``/Workspace/`` that holds a
  copy of ``tests/fixtures/``;
* ``APPMUX_TEST_GROUP``: the name of a Databricks group to share an app with;
* ``APPMUX_TEST_SECRET``: ``scope/key`` of a secret the admin can read (the secret to attach);
* ``APPMUX_TEST_WAREHOUSE_ID``: a SQL warehouse the admin may use (the resource to attach).
"""

from __future__ import annotations

import os
import secrets
import time
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest

from appmux.admin.schemas import (
    AttachResourceRequest,
    CreatePermissionRequest,
    DecidePermissionRequest,
    GrantRequest,
    OpenModeRequest,
    PutAppEgressRequest,
    PutSettingRequest,
    RegisterAppRequest,
    UpdateConfigRequest,
    UpdateReleaseRequest,
)

API: Final = "/_appmux/api/v1"
REQUEST_TIMEOUT_S: Final = 30.0
POLL_INTERVAL_S: Final = 1.0
RELEASE_TIMEOUT_S: Final = 240.0
STATE_TIMEOUT_S: Final = 60.0
SLUG_SUFFIX_BYTES: Final = 3
A_TEXT_FILE_OF_THE_SNAPSHOT: Final = "app.py"
AUDIT_PAGE_LIMIT: Final = 1000

GRACE: Final = "grace.walkthrough@example.com"  # asks for apps and owns the one that is approved
SHARED_USER: Final = "shared.walkthrough@example.com"
APP_ADMIN: Final = "manager.walkthrough@example.com"
BUDGET_MB: Final = 128
RAISED_BUDGET_MB: Final = 192
APPMUX_ACTOR: Final = "appmux"
SHARED_SP_SETTING: Final = "shared_sp_delegation_allowed"
EGRESS_RULE: Final = {
    "action": "deny",
    "cidr": "203.0.113.0/24",
    "port": "any",
    "comment": "walkthrough rule",
}

# The keys that tests/fixtures/bundle_offer's app.yaml asks for, and which two the test attaches.
BUNDLE_KEYS: Final = frozenset({"experiment", "openai-api-key", "sql-warehouse"})
BUNDLE_API_KEY_RESOURCE: Final = "openai-api-key"
BUNDLE_WAREHOUSE_KEY: Final = "sql-warehouse"
BUNDLE_LEFT_MISSING_KEY: Final = "experiment"


# --- the bodies, built from the contract's request models ---------------------------------------


def register_body(*, slug: str, name: str, folder: str, owner: str) -> dict[str, Any]:
    body = RegisterAppRequest(
        slug=slug,
        display_name=name,
        source_path=folder,
        memory_budget_mb=BUDGET_MB,
        owner=owner,
    )
    return body.model_dump(mode="json")


def request_body(kind: str, app: str, payload: dict[str, Any], reason: str) -> dict[str, Any]:
    body = CreatePermissionRequest.model_validate(
        {"kind": kind, "app": app, "payload": payload, "reason": reason}
    )
    return body.model_dump(mode="json")


def register_request_body(*, slug: str, name: str, folder: str) -> dict[str, Any]:
    payload = {
        "slug": slug,
        "display_name": name,
        "source_path": folder,
    }
    return request_body("register", slug, payload, "the walkthrough asks for a new app")


def decision_body(*, reason: str | None = None, note: str | None = None) -> dict[str, Any]:
    return DecidePermissionRequest(reason=reason, note=note).model_dump(mode="json")


def setting_body(value: bool | int | str | None) -> dict[str, Any]:
    return PutSettingRequest(value=value).model_dump(mode="json")


def egress_body(*, version: int) -> dict[str, Any]:
    body = PutAppEgressRequest.model_validate({"version": version, "rules": [EGRESS_RULE]})
    return body.model_dump(mode="json")


def grant_body(kind: str, name: str, permission: str) -> dict[str, Any]:
    body = GrantRequest.model_validate({"type": kind, "name": name, "permission": permission})
    return body.model_dump(mode="json")


def open_mode_body(mode: str) -> dict[str, Any]:
    return OpenModeRequest.model_validate({"mode": mode}).model_dump(mode="json")


def attach_body(offer: dict[str, Any], *, target: str) -> dict[str, Any]:
    """Accept an offer of the bundle: its type and level, with the target the person has."""
    is_secret = offer["type"] == "secret"
    body = AttachResourceRequest(
        type=offer["type"],
        target=target,
        permission=offer["permission"],
        access_as=None if is_secret else "obo",
        from_bundle=True,
    )
    return body.model_dump(mode="json")


def release_body(*, name: str | None, tags: Sequence[str]) -> dict[str, Any]:
    return UpdateReleaseRequest(name=name, tags=list(tags)).model_dump(mode="json")


def config_body(*, config: dict[str, Any], extra_env: dict[str, str]) -> dict[str, Any]:
    env = [*config["env"], *({"name": name, "value": value} for name, value in extra_env.items())]
    body = UpdateConfigRequest.model_validate(
        {
            "version": config["version"],
            "env": env,
            "wheel_sources": config["wheel_sources"],
            "health_path": config["health_path"],
            "health_deadline_s": config["health_deadline_s"],
            "own_sp_client_id_key": config["own_sp_client_id_key"],
            "own_sp_client_secret_key": config["own_sp_client_secret_key"],
        }
    )
    return body.model_dump(mode="json")


# --- the run ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    base_url: str
    admin_email: str
    user_token: str
    fixtures_folder: str
    group: str
    scope_and_key: str
    warehouse_id: str


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} must be set for this test (see the module docstring)")
    return value


@pytest.fixture(scope="module")
def target() -> Target:
    folder = _required("APPMUX_TEST_FIXTURES_FOLDER").rstrip("/")
    if not folder.startswith("/Workspace/"):
        pytest.fail("APPMUX_TEST_FIXTURES_FOLDER must start with /Workspace/")
    return Target(
        base_url=_required("APPMUX_TEST_BASE_URL").rstrip("/"),
        admin_email=_required("APPMUX_TEST_ADMIN_EMAIL").lower(),
        user_token=_required("APPMUX_TEST_USER_TOKEN"),
        fixtures_folder=folder,
        group=_required("APPMUX_TEST_GROUP"),
        scope_and_key=_required("APPMUX_TEST_SECRET"),
        warehouse_id=_required("APPMUX_TEST_WAREHOUSE_ID"),
    )


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return "(no error body)"


class Walkthrough:
    """The calls of the script. Every call is made as somebody and says what it expected."""

    def __init__(self, client: httpx.Client, target: Target) -> None:
        self._client = client
        self.target = target
        self.admin = target.admin_email
        self.created_apps: list[str] = []
        self.expected_actions: list[str] = []
        self.asked: list[tuple[int, str]] = []  # (request id, the status it must end with)

    def call(
        self,
        person: str,
        method: str,
        path: str,
        *,
        json: object | None = None,
        params: dict[str, str] | None = None,
        expect: Sequence[int] = (200,),
        with_token: bool = False,
    ) -> httpx.Response:
        headers = {"X-Forwarded-Email": person}
        if method != "GET":
            headers["Sec-Fetch-Site"] = "same-origin"
        if with_token:
            headers["X-Forwarded-Access-Token"] = self.target.user_token
        response = self._client.request(
            method, f"{API}{path}", json=json, params=params, headers=headers
        )
        # The comparison is made before the assert: pytest's failure text shows both sides of an
        # `in` check, and one side is the token.
        repeats_token = self.target.user_token in response.text
        assert not repeats_token, f"{method} {path}: the answer repeated the access token"
        assert response.status_code in expect, (
            f"{method} {path} as {person}: expected {list(expect)}, got "
            f"{response.status_code} {_error_code(response)}"
        )
        return response

    def read(self, person: str, path: str, **params: str) -> Any:
        return self.call(person, "GET", path, params=params or None).json()

    def expect_audit(self, *actions: str) -> None:
        self.expected_actions.extend(actions)

    # --- building blocks of the steps ------------------------------------------------------

    def register_directly(self, slug: str, *, folder: str, owner: str) -> dict[str, Any]:
        body = register_body(slug=slug, name=f"Walkthrough {slug}", folder=folder, owner=owner)
        response = self.call(self.admin, "POST", "/apps", json=body, expect=(200, 201))
        self.created_apps.append(slug)
        self.expect_audit("app.register")
        detail: dict[str, Any] = response.json()
        return detail

    def ask(self, body: dict[str, Any], *, ends: str) -> dict[str, Any]:
        response = self.call(GRACE, "POST", "/requests", json=body, expect=(200, 201))
        self.expect_audit("request.create")
        row: dict[str, Any] = response.json()
        assert row["status"] == "pending"
        self.asked.append((row["request_id"], ends))
        return row

    def decide(
        self, row: dict[str, Any], *, approve: bool, reason: str | None = None
    ) -> dict[str, Any]:
        verb = "approve" if approve else "deny"
        response = self.call(
            self.admin,
            "POST",
            f"/requests/{row['request_id']}/{verb}",
            json=decision_body(reason=reason, note="walkthrough"),
        )
        answered: dict[str, Any] = response.json()
        return answered

    def app(self, person: str, slug: str) -> dict[str, Any]:
        detail: dict[str, Any] = self.read(person, f"/apps/{slug}")
        return detail

    def release(self, slug: str, release_no: int) -> dict[str, Any]:
        detail: dict[str, Any] = self.read(self.admin, f"/apps/{slug}/releases/{release_no}")
        return detail

    def update(self, slug: str) -> int:
        """Press Update with the person's own token; the number of the new release."""
        response = self.call(
            self.admin, "POST", f"/apps/{slug}/releases", expect=(202,), with_token=True
        )
        self.expect_audit("release.create")
        return int(response.json()["release_no"])

    def wait_for_release(self, slug: str, release_no: int, status: str) -> dict[str, Any]:
        deadline = time.monotonic() + RELEASE_TIMEOUT_S
        while True:
            detail = self.release(slug, release_no)
            if detail["status"] == status:
                return detail
            assert detail["status"] != "failed", (
                f"release {release_no} of {slug} failed: {detail['status_reason']}"
            )
            assert time.monotonic() < deadline, (
                f"release {release_no} of {slug} is still {detail['status']} after "
                f"{RELEASE_TIMEOUT_S:.0f} s, expected {status}"
            )
            time.sleep(POLL_INTERVAL_S)

    def wait_for_failure(self, slug: str, release_no: int) -> dict[str, Any]:
        deadline = time.monotonic() + RELEASE_TIMEOUT_S
        while True:
            detail = self.release(slug, release_no)
            if detail["status"] == "failed":
                return detail
            assert detail["status"] != "active", f"release {release_no} of {slug} must not start"
            assert time.monotonic() < deadline, (
                f"release {release_no} of {slug} is still {detail['status']} after "
                f"{RELEASE_TIMEOUT_S:.0f} s, expected failed"
            )
            time.sleep(POLL_INTERVAL_S)

    def wait_for_state(self, slug: str, state: str) -> dict[str, Any]:
        deadline = time.monotonic() + STATE_TIMEOUT_S
        while True:
            detail = self.app(self.admin, slug)
            if detail["state"] == state:
                return detail
            assert time.monotonic() < deadline, (
                f"{slug} is still {detail['state']} after {STATE_TIMEOUT_S:.0f} s, expected {state}"
            )
            time.sleep(POLL_INTERVAL_S)

    # --- clean-up: best effort, a failure is a warning that names what to remove ------------

    def _admin_write(self, method: str, path: str, *, json: object | None = None) -> int:
        response = self._client.request(
            method,
            f"{API}{path}",
            json=json,
            headers={"X-Forwarded-Email": self.admin, "Sec-Fetch-Site": "same-origin"},
        )
        return response.status_code

    def delete_created_apps(self) -> None:
        for slug in reversed(self.created_apps):
            status = self._admin_write("DELETE", f"/apps/{slug}")
            if status not in {204, 404}:
                warnings.warn(
                    f"could not delete the walkthrough app {slug} (status {status}); "
                    "remove it by hand",
                    stacklevel=2,
                )

    def restore_setting(self, key: str, value: bool | int | str | None) -> None:
        status = self._admin_write("PUT", f"/platform/settings/{key}", json=setting_body(value))
        if status != 200:
            warnings.warn(
                f"could not put the setting {key} back (status {status}); set it by hand",
                stacklevel=2,
            )


@pytest.fixture
def walk(target: Target) -> Iterator[Walkthrough]:
    with httpx.Client(base_url=target.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        session = Walkthrough(client, target)
        try:
            yield session
        finally:
            session.delete_created_apps()


def assert_in_order(actual: Sequence[str], expected: Sequence[str]) -> None:
    """``expected`` appears in ``actual`` in that order; rows in between are allowed."""
    position = 0
    for index, action in enumerate(expected):
        try:
            position = actual.index(action, position) + 1
        except ValueError:
            raise AssertionError(
                f"audit row {index + 1} of {len(expected)} ({action}) is missing or out of "
                f"order; the log holds {list(actual)}"
            ) from None


@dataclass(frozen=True, slots=True)
class Slugs:
    """The apps of one run. The suffix keeps two runs, and the leftovers of a failed one, apart."""

    direct: str
    asked: str
    renamed: str
    taken: str
    bundle: str

    @classmethod
    def fresh(cls) -> Slugs:
        suffix = secrets.token_hex(SLUG_SUFFIX_BYTES)
        return cls(
            direct=f"walk-direct-{suffix}",
            asked=f"walk-asked-{suffix}",
            renamed=f"walk-renamed-{suffix}",
            taken=f"walk-taken-{suffix}",
            bundle=f"walk-bundle-{suffix}",
        )


def step_requests_and_registration(walk: Walkthrough, slugs: Slugs) -> None:
    """Steps 1 to 4: register, ask, approve, delegate, change settings, rename."""
    admin, light = walk.admin, f"{walk.target.fixtures_folder}/light_fastapi"

    detail = walk.register_directly(slugs.direct, folder=light, owner=admin)
    assert (detail["state"], detail["releases"], detail["owner"]) == ("stopped", [], admin)

    row = walk.ask(
        register_request_body(slug=slugs.asked, name="Walkthrough asked", folder=light),
        ends="approved",
    )
    approved = walk.decide(row, approve=True, reason="the walkthrough approves the new app")
    walk.expect_audit("request.approve")
    assert approved["status"] == "approved" and approved["app_version"] is not None
    walk.created_apps.append(slugs.asked)
    detail = walk.app(GRACE, slugs.asked)
    assert (detail["owner"], detail["state"], detail["releases"]) == (GRACE, "stopped", [])
    # A request names no budget: the app starts with the platform's default.
    default_budget = walk.read(admin, "/platform")["settings"]["default_memory_budget_mb"]
    assert detail["memory_budget_mb"] == default_budget

    # The master switch for the shared SP is a platform setting; it is put back at the end.
    walk.call(admin, "PUT", f"/platform/settings/{SHARED_SP_SETTING}", json=setting_body(True))
    walk.expect_audit("settings.edit")
    for flag, why in (("obo", "needs the visitor's data"), ("shared_sp", "runs a nightly job")):
        row = walk.ask(request_body("delegation", slugs.asked, {flag: True}, why), ends="approved")
        walk.decide(row, approve=True, reason=f"the walkthrough allows {flag}")
        walk.expect_audit("request.approve")
    row = walk.ask(
        request_body(
            "settings",
            slugs.asked,
            {"display_name": "Walkthrough reports", "memory_budget_mb": RAISED_BUDGET_MB},
            "a better name and room to grow",
        ),
        ends="approved",
    )
    walk.decide(row, approve=True)
    walk.expect_audit("request.approve")
    detail = walk.app(GRACE, slugs.asked)
    assert detail["delegation"]["obo_delegated"] is True
    assert detail["delegation"]["shared_sp_delegated"] is True
    assert (detail["display_name"], detail["memory_budget_mb"]) == (
        "Walkthrough reports",
        RAISED_BUDGET_MB,
    )

    row = walk.ask(
        request_body("settings", slugs.asked, {"slug": slugs.renamed}, "a better address"),
        ends="approved",
    )
    walk.decide(row, approve=True)
    walk.expect_audit("request.approve", "app.rename")
    walk.created_apps.remove(slugs.asked)
    walk.created_apps.append(slugs.renamed)
    walk.call(GRACE, "GET", f"/apps/{slugs.asked}", expect=(404,))
    assert walk.app(GRACE, slugs.renamed)["slug"] == slugs.renamed

    # A slug that is free when Grace asks and taken when the admin presses Approve: the request is
    # closed by itself, as denied, under the admin who pressed the button (spec US8, scenario 4).
    row = walk.ask(
        request_body("settings", slugs.renamed, {"slug": slugs.taken}, "a shorter address"),
        ends="denied",
    )
    walk.register_directly(slugs.taken, folder=light, owner=admin)
    closed = walk.decide(row, approve=True)
    walk.expect_audit("request.deny")
    assert closed["status"] == "denied" and closed["closed_by"] == admin
    assert "taken" in (closed["closed_note"] or "")
    assert walk.app(GRACE, slugs.renamed)["slug"] == slugs.renamed


def step_egress_and_open(walk: Walkthrough, slugs: Slugs) -> int:
    """Steps 5 and 6: an egress rule, then open the app and read its source as a platform admin.

    Returns the number of the release the Update made.
    """
    admin = walk.admin
    version = walk.app(admin, slugs.renamed)["version"]
    walk.call(admin, "PUT", f"/apps/{slugs.renamed}/egress", json=egress_body(version=version))
    walk.expect_audit("egress.edit")
    assert [rule["cidr"] for rule in walk.app(admin, slugs.renamed)["egress"]] == [
        EGRESS_RULE["cidr"]
    ]

    first = walk.update(slugs.renamed)
    walk.wait_for_release(slugs.renamed, first, "active")

    # The admin has no grant on Grace's app: it is listed with both ways to open it, and opening
    # it as the visitor writes an audit row.
    listed = {item["slug"]: item for item in walk.read(admin, "/apps")["items"]}
    assert set(listed[slugs.renamed]["open_modes"]) == {"as_user", "without_token"}
    walk.call(
        admin,
        "POST",
        f"/apps/{slugs.renamed}/open-mode",
        json=open_mode_body("as_user"),
        expect=(204,),
    )
    walk.expect_audit("app.view_as_user")

    source_path = f"/apps/{slugs.renamed}/releases/{first}/source"
    files = {item["path"] for item in walk.read(admin, source_path)["items"]}
    assert A_TEXT_FILE_OF_THE_SNAPSHOT in files
    source = walk.read(admin, f"{source_path}/{A_TEXT_FILE_OF_THE_SNAPSHOT}")
    assert source["content"]
    walk.expect_audit("release.view_source", "release.view_source")
    return first


def step_sharing(walk: Walkthrough, slugs: Slugs) -> None:
    """Step 7: a user, a Databricks group and an app admin, shared by the app's owner."""
    shared = (
        ("user", SHARED_USER, "CAN_USE"),
        ("group", walk.target.group, "CAN_USE"),
        ("user", APP_ADMIN, "CAN_MANAGE"),
    )
    for kind, name, permission in shared:
        path = f"/apps/{slugs.renamed}/grants"
        walk.call(GRACE, "PUT", path, json=grant_body(kind, name, permission))
        walk.expect_audit("grant.app")
    stored = walk.app(GRACE, slugs.renamed)["grants"]
    assert {(g["type"], g["name"], g["permission"]) for g in stored} == set(shared)


def step_resources(walk: Walkthrough, slugs: Slugs) -> None:
    """Step 8: the bundle's release fails for its missing keys; accept two of the three offers."""
    admin, bundle = walk.admin, slugs.bundle
    folder = f"{walk.target.fixtures_folder}/bundle_offer"
    walk.register_directly(bundle, folder=folder, owner=admin)
    failed = walk.wait_for_failure(bundle, walk.update(bundle))
    assert "missing resources" in (failed["status_reason"] or "")

    panel = walk.read(admin, f"/apps/{bundle}/requirements")
    offers = {item["key"]: item["offer"] for item in panel["items"]}
    assert set(offers) == BUNDLE_KEYS
    assert all(offer is not None for offer in offers.values())
    targets = {
        BUNDLE_API_KEY_RESOURCE: walk.target.scope_and_key,
        BUNDLE_WAREHOUSE_KEY: walk.target.warehouse_id,
    }
    for key, resource_target in targets.items():
        attached = walk.call(
            admin,
            "PUT",
            f"/apps/{bundle}/resources/{key}",
            json=attach_body(offers[key], target=resource_target),
            with_token=True,
        ).json()
        walk.expect_audit("resource.attach")
        assert attached["check_result"] in {"ok", "unverified"}
    panel = walk.read(admin, f"/apps/{bundle}/requirements")
    assert {item["key"]: item["state"] for item in panel["items"]} == {
        BUNDLE_API_KEY_RESOURCE: "attached",
        BUNDLE_WAREHOUSE_KEY: "attached",
        BUNDLE_LEFT_MISSING_KEY: "missing",
    }


def step_releases_and_lifecycle(walk: Walkthrough, slugs: Slugs, first: int) -> None:
    """Steps 9 and 10: a second release, tag, label, lock, activate the older, delete, stop."""
    admin, app = walk.admin, slugs.renamed
    config = walk.app(admin, app)["config"]
    changed = walk.call(
        admin,
        "PUT",
        f"/apps/{app}/config",
        json=config_body(config=config, extra_env={"WALKTHROUGH_MARK": "two"}),
    ).json()
    walk.expect_audit("config.edit")
    assert changed["release_no"] is not None, "a config change on a running app makes a release"
    second = int(changed["release_no"])
    walk.wait_for_release(app, second, "active")

    release = f"/apps/{app}/releases/{first}"
    walk.call(admin, "PUT", release, json=release_body(name=None, tags=["stable"]))
    walk.expect_audit("release.tag")
    walk.call(admin, "PUT", release, json=release_body(name="first", tags=["stable"]))
    walk.expect_audit("release.label")
    walk.call(admin, "PUT", f"{release}/lock")
    walk.expect_audit("release.lock")
    assert walk.release(app, first)["locked_by"] == admin
    walk.call(admin, "DELETE", f"{release}/lock")
    walk.expect_audit("release.unlock")
    assert walk.release(app, first)["locked_by"] is None
    walk.call(admin, "POST", f"{release}/activate")
    walk.expect_audit("release.activate")
    walk.wait_for_release(app, first, "active")
    walk.call(admin, "DELETE", f"/apps/{app}/releases/{second}", expect=(204,))
    walk.expect_audit("release.delete")
    walk.call(admin, "GET", f"/apps/{app}/releases/{second}", expect=(404,))

    walk.call(admin, "POST", f"/apps/{app}/restart")
    walk.expect_audit("app.restart")
    walk.wait_for_state(app, "running")
    stopped = walk.call(admin, "POST", f"/apps/{app}/stop").json()
    walk.expect_audit("app.stop")
    assert stopped["desired_state"] == "stopped"
    walk.wait_for_state(app, "stopped")


def check_final_state(walk: Walkthrough, slugs: Slugs, first: int) -> None:
    """What the database must hold now, read through the API."""
    admin = walk.admin
    final = walk.app(admin, slugs.renamed)
    assert [release["release_no"] for release in final["releases"]] == [first]
    (kept,) = final["releases"]
    assert (kept["name"], kept["tags"], kept["locked_by"]) == ("first", ["stable"], None)
    assert (final["owner"], final["memory_budget_mb"]) == (GRACE, RAISED_BUDGET_MB)
    assert (final["config"]["desired_state"], final["config"]["target_release_no"]) == (
        "stopped",
        first,
    )
    direct = walk.app(admin, slugs.direct)
    assert (direct["state"], direct["releases"]) == ("stopped", [])
    assert walk.app(admin, slugs.taken)["slug"] == slugs.taken

    rows = {row["request_id"]: row for row in walk.read(admin, "/requests")["items"]}
    assert {request_id: rows[request_id]["status"] for request_id, _ in walk.asked} == dict(
        walk.asked
    )


@pytest.mark.integration
def test_the_sc004_walkthrough_runs_through_the_api_and_the_final_state_is_the_expected_one(
    walk: Walkthrough,
) -> None:
    admin = walk.admin
    slugs = Slugs.fresh()
    log_before = walk.read(admin, "/audit", limit="1")["items"]  # newest first
    last_audit_id = log_before[0]["id"] if log_before else 0
    shared_sp_before = walk.read(admin, "/platform")["settings"][SHARED_SP_SETTING]
    try:
        step_requests_and_registration(walk, slugs)
        first = step_egress_and_open(walk, slugs)
        step_sharing(walk, slugs)
        step_resources(walk, slugs)
        step_releases_and_lifecycle(walk, slugs, first)
        check_final_state(walk, slugs, first)

        people = {admin, GRACE, SHARED_USER, APP_ADMIN, APPMUX_ACTOR}
        rows = walk.read(admin, "/audit", limit=str(AUDIT_PAGE_LIMIT))["items"]
        ours = [
            row for row in reversed(rows) if row["id"] > last_audit_id and row["actor"] in people
        ]
        assert_in_order([row["action"] for row in ours], walk.expected_actions)
    finally:
        if shared_sp_before is not True:
            walk.restore_setting(SHARED_SP_SETTING, shared_sp_before)
