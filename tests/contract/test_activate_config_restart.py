"""Activate a release, edit the config, start, stop and restart, rename and give an app away (T102).

``contracts/admin-api.md`` routes, behind the real guard and authorizer on the in-memory registry:

* ``POST /apps/{app}/releases/{id}/activate`` (``release.activate``): any live release, from what
  is stored; a release whose requirement keys are missing is refused with the keys listed;
* ``PUT /apps/{app}/config`` (``config.edit``): needs the ``version`` read; a change of env entries
  or wheel sources makes a release, the rest is written on the app alone;
* ``POST /apps/{app}/start|stop|restart`` (``app.start`` ...): the state the reconciler reports;
* ``PATCH /apps/{app}`` (``app.edit_platform_fields``, also ``app.rename``) and
  ``PUT /apps/{app}/owner`` (``app.set_owner``): a platform admin only, under that admin's token.

The reconciler is replaced by ``FakeControl``, which records what the routes tell it. So what is
pinned here is the routes' side of the ``AppControl`` seam, not the reconciler.

Starting data: see ``edge_harness`` (``sales`` owned by alice, bob ``CAN_USE``, carol
``CAN_MANAGE``, running with no release; ``docs`` owned by frank; dana is the platform admin).
"""

from __future__ import annotations

from typing import Final

import pytest
from edge_harness import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    FRANK,
    NOW,
    VISITOR_TOKEN,
    FakeGroups,
    Reply,
    call,
    seed_standard_rows,
    seeded_registry,
)
from pydantic import SecretStr
from stub_registry import RacedRegistry, StubRegistry

from appmux.admin.api import create_admin_app
from appmux.admin.apps import AppRoutes
from appmux.admin.authorizer import make_authorizer
from appmux.admin.operations import OperationRoutes
from appmux.admin.schemas import AppDetail, AppStateResponse, ConfigChangeResult, ReleaseDetail
from appmux.reconciler.app_control import RunState
from appmux.registry.models import App, AppConfig
from appmux.registry.protocol import Change
from appmux.registry.writers import Identity, WriteKind

pytestmark = pytest.mark.anyio

ERIN: Final = "erin@example.com"
GRACE: Final = "grace@example.com"
DIGEST: Final = "sha256:" + "ab" * 32
OTHER_DIGEST: Final = "sha256:" + "cd" * 32
ENV_VALUE_MARKER: Final = "marker-value-from-the-client"
WHEEL: Final = {"sha256": "c" * 64, "filename": "tool-1.0-py3-none-any.whl", "source": "/w"}

OWN_SP_KEYS: Final = {"own_sp_client_id_key": "sp-id", "own_sp_client_secret_key": "sp-secret"}


class FakeControl:
    """The reconciler as the routes see it: it answers a state and records what it is told."""

    def __init__(self) -> None:
        self.states: dict[str, RunState] = {}
        self.woken: list[str] = []
        self.restarts: list[str] = []
        self.renames: list[tuple[str, str]] = []
        self.stopped: list[str] = []

    def run_state(self, slug: str, desired_state: str) -> RunState:
        if desired_state == "stopped":
            return "stopped"
        return self.states.get(slug, "starting")

    def request_restart(self, slug: str) -> None:
        self.restarts.append(slug)

    def app_renamed(self, old_slug: str, new_slug: str) -> None:
        self.renames.append((old_slug, new_slug))

    async def stop_app(self, slug: str) -> None:
        self.stopped.append(slug)

    def wake(self, slug: str) -> None:
        self.woken.append(slug)


class Harness:
    def __init__(self, registry: StubRegistry | None = None) -> None:
        self.registry: StubRegistry = registry if registry is not None else seeded_registry()
        self.control = FakeControl()
        self.app = create_admin_app(
            registry=self.registry,
            authorizer=make_authorizer(self.registry, FakeGroups()),
            route_providers=[
                AppRoutes(app_state=self.control.run_state, on_renamed=self.control.app_renamed),
                OperationRoutes(self.control),
            ],
        )

    async def request(
        self, method: str, path: str, email: str | None, json_body: object = None
    ) -> Reply:
        return await call(self.app, method, path, email=email, json_body=json_body)

    async def activate(self, email: str | None, release_no: object, slug: str = "sales") -> Reply:
        return await self.request("POST", f"/apps/{slug}/releases/{release_no}/activate", email)

    async def put_config(self, email: str | None, **overrides: object) -> Reply:
        body = {**self.config_body(), **overrides}
        return await self.request("PUT", "/apps/sales/config", email, body)

    def config_body(self) -> dict[str, object]:
        config = config_of(self.registry, "sales")
        target = self.registry.snapshot.release("sales", config.target_release_no or 0)
        return {
            "version": config.version,
            "env": []
            if target is None
            else [e.model_dump(exclude_none=True) for e in target.config.env],
            "wheel_sources": [],
            "health_path": config.health_path,
            "health_deadline_s": config.health_deadline_s,
            "own_sp_client_id_key": config.own_sp_client_id_key,
            "own_sp_client_secret_key": config.own_sp_client_secret_key,
        }

    async def operate(self, operation: str, email: str | None, slug: str = "sales") -> Reply:
        return await self.request("POST", f"/apps/{slug}/{operation}", email)

    async def patch(self, email: str | None, body: object, slug: str = "sales") -> Reply:
        return await self.request("PATCH", f"/apps/{slug}", email, body)

    def is_untouched(self) -> bool:
        return (
            self.registry.writes == ()
            and self.registry.audit_rows == ()
            and self.control.woken == []
            and self.control.restarts == []
            and self.control.renames == []
        )


def seed_release(
    registry: StubRegistry,
    release_no: int,
    status: str = "superseded",
    *,
    needs: tuple[str, ...] = (),
    env: tuple[tuple[str, str], ...] = (),
    deleted: bool = False,
) -> None:
    fields: dict[str, object] = {
        "app_slug": "sales",
        "release_no": release_no,
        "snapshot_digest": None if deleted else DIGEST,
        "detected": {
            "runtime": "python",
            "command": ["python", "app.py"],
            "env": [{"name": key.upper(), "value_from": key} for key in needs],
        },
        "config": {"env": [{"name": name, "value": value} for name, value in env]},
        "wheels": [WHEEL],
        "status": status,
        "status_at": NOW,
        "created_by": CAROL,
        "created_at": NOW,
    }
    if deleted:
        fields.update(deleted_by=CAROL, deleted_at=NOW)
    registry.seed("releases", fields)


def seed_app_config(registry: StubRegistry, **columns: object) -> None:
    registry.seed(
        "app_config",
        {
            "app_slug": "sales",
            "desired_state": "running",
            "updated_by": DANA,
            "updated_at": NOW,
            **columns,
        },
    )


def attach(registry: StubRegistry, key: str, resource_type: str = "secret") -> None:
    registry.seed(
        "app_resources",
        {
            "app_slug": "sales",
            "key": key,
            "type": resource_type,
            "target": "scope/token-name" if resource_type == "secret" else "warehouse-0001",
            "permission": "READ" if resource_type == "secret" else "CAN_USE",
            "access_as": None if resource_type == "secret" else "obo",
            "check_result": "ok",
            "attached_by": ALICE,
            "attached_at": NOW,
        },
    )


def config_of(registry: StubRegistry, slug: str) -> AppConfig:
    config = registry.snapshot.app_config(slug)
    assert config is not None
    return config


def app_of(registry: StubRegistry, slug: str) -> App:
    app = registry.snapshot.app(slug)
    assert app is not None
    return app


def error_of(reply: Reply) -> tuple[str, str]:
    error = reply.json()["error"]
    return error["code"], error["message"]


def harness_with_releases() -> Harness:
    """Releases 1 and 2 are older, 3 is active and the target."""
    harness = Harness()
    seed_release(harness.registry, 1)
    seed_release(harness.registry, 2)
    seed_release(harness.registry, 3, "active", env=(("MODE", "slow"),))
    seed_app_config(harness.registry, target_release_no=3)
    return harness


# --- POST /apps/{app}/releases/{id}/activate ---------------------------------------------------


async def test_an_app_admin_activates_an_older_release_and_gets_its_detail() -> None:
    harness = harness_with_releases()

    reply = await harness.activate(CAROL, 1)

    assert reply.status == 200
    detail = ReleaseDetail.model_validate_json(reply.body)
    assert (detail.release_no, detail.status, detail.is_target) == (1, "pending", True)
    config = config_of(harness.registry, "sales")
    assert (config.target_release_no, config.desired_state) == (1, "running")
    assert harness.control.woken == ["sales"]
    (row,) = harness.registry.audit_rows
    assert (row.actor, row.action, row.app_slug) == (CAROL, "release.activate", "sales")
    assert row.detail == {"release": 1}


async def test_activation_is_written_by_the_service_principal_without_a_token() -> None:
    harness = harness_with_releases()

    await harness.activate(CAROL, 1)

    (write,) = harness.registry.writes
    assert write.identity is Identity.APPMUX_SP
    assert write.admin_token is None
    assert set(write.kinds) == {WriteKind.RELEASE_UPDATE, WriteKind.APP_CONFIG_UPDATE}


async def test_a_release_that_failed_its_health_check_can_be_activated() -> None:
    harness = harness_with_releases()
    seed_release(harness.registry, 4, "failed")

    reply = await harness.activate(CAROL, 4)

    assert reply.status == 200
    assert ReleaseDetail.model_validate_json(reply.body).status == "pending"


async def test_missing_requirement_keys_are_listed_and_nothing_is_written() -> None:
    harness = harness_with_releases()
    seed_release(harness.registry, 4, needs=("warehouse-id", "api-secret"))

    reply = await harness.activate(CAROL, 4)

    assert reply.status == 409
    code, message = error_of(reply)
    assert code == "requirements_missing"
    assert "api-secret, warehouse-id" in message
    assert harness.is_untouched()


async def test_the_keys_attached_since_make_the_same_release_activate() -> None:
    harness = harness_with_releases()
    seed_release(harness.registry, 4, needs=("warehouse-id",))
    assert (await harness.activate(CAROL, 4)).status == 409
    attach(harness.registry, "warehouse-id", "sql_warehouse")

    assert (await harness.activate(CAROL, 4)).status == 200


async def test_an_app_deleted_right_after_the_activation_is_404_and_it_stays_recorded() -> None:
    # The activation committed and was audited; its app is gone, so there is no release to show,
    # and a 404 is what the same request a moment later gets. A 500 would say it failed.
    registry = seed_standard_rows(RacedRegistry(clock=lambda: NOW))
    harness = Harness(registry)
    seed_release(registry, 1)
    seed_release(registry, 2, "active")
    seed_app_config(registry, target_release_no=2)
    registry.after_next_write(
        Change(WriteKind.APP_DELETE, key={"slug": "sales"}),
        admin_token=SecretStr("another-admins-token"),
    )

    reply = await harness.activate(CAROL, 1)

    assert (reply.status, error_of(reply)) == (404, ("not_found", "no such app"))
    assert [row.action for row in registry.audit_rows] == ["release.activate"]


@pytest.mark.parametrize("release_no", ["99", "0", "-1", "abc", "1.5"])
async def test_an_unknown_release_is_not_found(release_no: str) -> None:
    harness = harness_with_releases()

    reply = await harness.activate(CAROL, release_no)

    assert reply.status == 404
    assert harness.is_untouched()


async def test_a_deleted_release_is_not_found() -> None:
    harness = harness_with_releases()
    seed_release(harness.registry, 4, deleted=True)

    assert (await harness.activate(CAROL, 4)).status == 404
    assert harness.is_untouched()


@pytest.mark.parametrize("status", ["building", "starting", "healthy"])
async def test_a_release_on_its_way_up_cannot_be_activated(status: str) -> None:
    harness = harness_with_releases()
    seed_release(harness.registry, 4, status)

    reply = await harness.activate(CAROL, 4)

    assert (reply.status, error_of(reply)[0]) == (409, "release_in_progress")
    assert harness.is_untouched()


async def test_activating_the_serving_release_changes_no_status() -> None:
    harness = harness_with_releases()

    reply = await harness.activate(CAROL, 3)

    assert reply.status == 200
    assert ReleaseDetail.model_validate_json(reply.body).status == "active"
    (write,) = harness.registry.writes
    assert write.kinds == (WriteKind.APP_CONFIG_UPDATE,)
    assert harness.control.restarts == []


async def test_activating_the_release_of_a_crashed_app_asks_for_a_restart() -> None:
    harness = harness_with_releases()
    harness.control.states["sales"] = "crashed"

    await harness.activate(CAROL, 3)

    assert harness.control.restarts == ["sales"]


@pytest.mark.parametrize(("email", "status"), [(BOB, 403), (ERIN, 404), (None, 401), (FRANK, 404)])
async def test_only_someone_who_manages_the_app_activates(email: str | None, status: int) -> None:
    harness = harness_with_releases()

    reply = await harness.activate(email, 1)

    assert reply.status == status
    assert harness.is_untouched()


async def test_activating_in_an_app_that_does_not_exist_is_not_found() -> None:
    harness = harness_with_releases()

    assert (await harness.activate(DANA, 1, slug="nothing")).status == 404


# --- PUT /apps/{app}/config --------------------------------------------------------------------


async def test_an_env_change_creates_a_release_and_answers_its_number() -> None:
    harness = harness_with_releases()
    before = config_of(harness.registry, "sales").version

    reply = await harness.put_config(CAROL, env=[{"name": "MODE", "value": "fast"}])

    assert reply.status == 200
    result = ConfigChangeResult.model_validate_json(reply.body)
    assert (result.config_version, result.release_no) == (before + 1, 4)
    created = harness.registry.snapshot.release("sales", 4)
    assert created is not None
    assert (created.snapshot_digest, created.status, created.created_by) == (
        DIGEST,
        "pending",
        CAROL,
    )
    assert [(e.name, e.value) for e in created.config.env] == [("MODE", "fast")]
    assert [w.sha256 for w in created.wheels] == [WHEEL["sha256"]]
    assert config_of(harness.registry, "sales").target_release_no == 4
    assert harness.control.woken == ["sales"]
    (row,) = harness.registry.audit_rows
    assert (row.actor, row.action, row.app_slug) == (CAROL, "config.edit", "sales")
    assert row.detail["release"] == 4


async def test_the_config_edit_is_written_by_the_service_principal() -> None:
    harness = harness_with_releases()

    await harness.put_config(CAROL, env=[{"name": "MODE", "value": "fast"}])

    (write,) = harness.registry.writes
    assert write.identity is Identity.APPMUX_SP
    assert write.admin_token is None
    assert write.kinds == (WriteKind.RELEASE_INSERT, WriteKind.APP_CONFIG_UPDATE)


async def test_a_platform_admin_edits_another_teams_config_with_no_grant_of_their_own() -> None:
    """Implicit access (FR-028, T152): audited under the admin's name, no ``app_grants`` row."""
    harness = harness_with_releases()
    grants_before = list(harness.registry.snapshot.app_grants)

    reply = await harness.put_config(DANA, env=[{"name": "MODE", "value": "fast"}])

    assert reply.status == 200, reply.body
    (row,) = harness.registry.audit_rows
    assert (row.actor, row.action, row.app_slug) == (DANA, "config.edit", "sales")
    assert list(harness.registry.snapshot.app_grants) == grants_before
    assert not [g for g in harness.registry.snapshot.app_grants if g.principal.lower() == DANA]


async def test_a_health_check_change_makes_no_release() -> None:
    harness = harness_with_releases()
    before = config_of(harness.registry, "sales").version

    reply = await harness.put_config(CAROL, health_path="/ready", health_deadline_s=90)

    assert reply.status == 200
    result = ConfigChangeResult.model_validate_json(reply.body)
    assert (result.config_version, result.release_no) == (before + 1, None)
    config = config_of(harness.registry, "sales")
    assert (config.health_path, config.health_deadline_s) == ("/ready", 90)
    assert len(harness.registry.snapshot.releases_for("sales")) == 3
    assert harness.control.woken == []
    (row,) = harness.registry.audit_rows
    assert row.action == "config.edit"


async def test_own_sp_keys_are_stored_on_the_app_once_they_are_attached_secrets() -> None:
    harness = harness_with_releases()
    attach(harness.registry, "sp-id")
    attach(harness.registry, "sp-secret")

    reply = await harness.put_config(CAROL, **OWN_SP_KEYS)

    assert reply.status == 200
    assert ConfigChangeResult.model_validate_json(reply.body).release_no is None
    config = config_of(harness.registry, "sales")
    assert (config.own_sp_client_id_key, config.own_sp_client_secret_key) == ("sp-id", "sp-secret")


@pytest.mark.parametrize(
    "attached",
    [{}, {"sp-id": "sql_warehouse", "sp-secret": "secret"}, {"sp-id": "secret"}],
    ids=["none attached", "one is not a secret", "one is missing"],
)
async def test_own_sp_keys_that_are_not_attached_secrets_are_refused(
    attached: dict[str, str],
) -> None:
    harness = harness_with_releases()
    for key, resource_type in attached.items():
        attach(harness.registry, key, resource_type)

    reply = await harness.put_config(CAROL, **OWN_SP_KEYS)

    assert reply.status == 400
    assert config_of(harness.registry, "sales").own_sp_client_id_key is None
    assert harness.registry.writes == ()


async def test_one_own_sp_key_without_the_other_is_refused() -> None:
    harness = harness_with_releases()
    attach(harness.registry, "sp-id")

    reply = await harness.put_config(CAROL, own_sp_client_id_key="sp-id")

    assert reply.status == 400
    assert harness.is_untouched()


async def test_a_stale_version_is_a_conflict_and_nothing_changes() -> None:
    harness = harness_with_releases()
    stale = config_of(harness.registry, "sales").version - 1

    reply = await harness.put_config(CAROL, version=stale, env=[{"name": "MODE", "value": "x"}])

    assert (reply.status, error_of(reply)[0]) == (409, "changed_by_others")
    assert harness.is_untouched()


async def test_a_body_without_version_is_refused() -> None:
    harness = harness_with_releases()
    body = harness.config_body()
    del body["version"]

    reply = await harness.request("PUT", "/apps/sales/config", CAROL, body)

    assert reply.status == 400
    assert harness.is_untouched()


async def test_a_health_path_without_a_leading_slash_is_refused() -> None:
    harness = harness_with_releases()

    reply = await harness.put_config(CAROL, health_path="ready")

    assert reply.status == 400
    assert harness.is_untouched()


async def test_env_entries_for_an_app_with_no_release_are_refused_with_a_next_step() -> None:
    harness = Harness()

    reply = await harness.put_config(CAROL, env=[{"name": "MODE", "value": "fast"}])

    assert (reply.status, error_of(reply)[0]) == (409, "no_release")
    assert "Update" in error_of(reply)[1]
    assert harness.is_untouched()


async def test_an_env_value_never_reaches_the_audit_row_or_the_answer() -> None:
    harness = harness_with_releases()

    reply = await harness.put_config(CAROL, env=[{"name": "MODE", "value": ENV_VALUE_MARKER}])

    assert reply.status == 200
    assert ENV_VALUE_MARKER not in reply.text
    assert ENV_VALUE_MARKER not in repr(harness.registry.audit_rows)


@pytest.mark.parametrize(("email", "status"), [(BOB, 403), (ERIN, 404), (None, 401)])
async def test_only_someone_who_manages_the_app_edits_its_config(
    email: str | None, status: int
) -> None:
    harness = harness_with_releases()

    reply = await harness.put_config(email, health_path="/ready")

    assert reply.status == status
    assert harness.is_untouched()


# --- POST /apps/{app}/start, stop, restart -----------------------------------------------------


async def test_stop_writes_the_wish_and_answers_stopped() -> None:
    harness = harness_with_releases()

    reply = await harness.operate("stop", CAROL)

    assert reply.status == 200
    state = AppStateResponse.model_validate_json(reply.body)
    assert (state.state, state.desired_state) == ("stopped", "stopped")
    assert config_of(harness.registry, "sales").desired_state == "stopped"
    assert harness.control.woken == ["sales"]
    (row,) = harness.registry.audit_rows
    assert (row.actor, row.action, row.app_slug) == (CAROL, "app.stop", "sales")


async def test_start_asks_a_stopped_app_to_run_and_answers_the_reconcilers_state() -> None:
    harness = harness_with_releases()
    seed_app_config(harness.registry, desired_state="stopped", target_release_no=3)

    reply = await harness.operate("start", CAROL)

    assert reply.status == 200
    state = AppStateResponse.model_validate_json(reply.body)
    assert (state.state, state.desired_state) == ("starting", "running")
    assert config_of(harness.registry, "sales").desired_state == "running"
    assert harness.control.woken == ["sales"]
    assert [row.action for row in harness.registry.audit_rows] == ["app.start"]


async def test_start_with_nothing_to_run_is_refused_and_says_what_to_do() -> None:
    harness = Harness()
    seed_app_config(harness.registry, desired_state="stopped")

    reply = await harness.operate("start", CAROL)

    assert (reply.status, error_of(reply)[0]) == (409, "no_release")
    assert "Update" in error_of(reply)[1]
    assert harness.is_untouched()


async def test_start_of_a_crashed_app_is_a_restart() -> None:
    harness = harness_with_releases()
    harness.control.states["sales"] = "crashed"

    reply = await harness.operate("start", CAROL)

    assert reply.status == 200
    assert harness.control.restarts == ["sales"]


async def test_restart_asks_the_reconciler_and_changes_no_row() -> None:
    harness = harness_with_releases()
    before = config_of(harness.registry, "sales").version
    harness.control.states["sales"] = "running"

    reply = await harness.operate("restart", CAROL)

    assert reply.status == 200
    state = AppStateResponse.model_validate_json(reply.body)
    assert (state.state, state.desired_state) == ("running", "running")
    assert harness.control.restarts == ["sales"]
    assert config_of(harness.registry, "sales").version == before
    assert [row.action for row in harness.registry.audit_rows] == ["app.restart"]


async def test_restart_of_a_stopped_app_starts_it() -> None:
    harness = harness_with_releases()
    seed_app_config(harness.registry, desired_state="stopped", target_release_no=3)

    reply = await harness.operate("restart", CAROL)

    assert reply.status == 200
    assert config_of(harness.registry, "sales").desired_state == "running"
    assert harness.control.restarts == []
    assert harness.control.woken == ["sales"]


@pytest.mark.parametrize("operation", ["start", "stop", "restart"])
@pytest.mark.parametrize(("email", "status"), [(BOB, 403), (ERIN, 404), (None, 401)])
async def test_only_someone_who_manages_the_app_operates_it(
    operation: str, email: str | None, status: int
) -> None:
    harness = harness_with_releases()

    reply = await harness.operate(operation, email)

    assert reply.status == status
    assert harness.is_untouched()


async def test_operating_an_app_that_does_not_exist_is_not_found() -> None:
    harness = harness_with_releases()

    assert (await harness.operate("stop", DANA, slug="nothing")).status == 404


# --- PATCH /apps/{app} -------------------------------------------------------------------------


async def test_a_platform_admin_renames_an_app_and_the_reconciler_is_told() -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    reply = await harness.patch(DANA, {"version": version, "slug": "billing"})

    assert reply.status == 200
    detail = AppDetail.model_validate_json(reply.body)
    assert (detail.slug, detail.version) == ("billing", version + 1)
    assert harness.registry.snapshot.app("sales") is None
    assert len(harness.registry.snapshot.releases_for("billing")) == 3
    assert len(harness.registry.snapshot.grants_for("billing")) == 2
    assert harness.control.renames == [("sales", "billing")]


async def test_a_rename_writes_both_audit_rows_under_the_new_slug() -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    await harness.patch(DANA, {"version": version, "slug": "billing"})

    rows = harness.registry.audit_rows
    assert [(row.actor, row.action, row.app_slug) for row in rows] == [
        (DANA, "app.edit_platform_fields", "billing"),
        (DANA, "app.rename", "billing"),
    ]
    assert rows[1].detail == {"from": "sales", "to": "billing"}


async def test_the_platform_fields_are_written_under_the_admins_own_token() -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    await harness.patch(DANA, {"version": version, "slug": "billing"})

    (write,) = harness.registry.writes
    assert write.admin_token is not None
    assert write.admin_token.get_secret_value() == VISITOR_TOKEN
    assert write.kinds == (WriteKind.APP_UPDATE,)


async def test_a_taken_slug_is_a_conflict_and_nothing_changes() -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    reply = await harness.patch(DANA, {"version": version, "slug": "docs"})

    assert (reply.status, error_of(reply)[0]) == (409, "slug_taken")
    assert harness.is_untouched()
    assert harness.registry.snapshot.app("sales") is not None


async def test_other_platform_fields_change_without_a_rename() -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    reply = await harness.patch(
        DANA, {"version": version, "display_name": "Sales app", "memory_budget_mb": 512}
    )

    assert reply.status == 200
    app = app_of(harness.registry, "sales")
    assert (app.display_name, app.memory_budget_mb, app.version) == ("Sales app", 512, version + 1)
    assert harness.control.renames == []
    (row,) = harness.registry.audit_rows
    assert row.action == "app.edit_platform_fields"
    assert row.detail["before"]["memory_budget_mb"] == 256
    assert row.detail["after"]["memory_budget_mb"] == 512


async def test_a_patch_that_changes_nothing_writes_and_signals_nothing() -> None:
    harness = harness_with_releases()
    app = app_of(harness.registry, "sales")

    reply = await harness.patch(DANA, {"version": app.version, "slug": "sales"})

    assert reply.status == 200
    assert AppDetail.model_validate_json(reply.body).version == app.version
    assert harness.is_untouched()


@pytest.mark.parametrize("email", [ALICE, CAROL, BOB])
async def test_only_a_platform_admin_edits_the_platform_fields(email: str) -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    reply = await harness.patch(email, {"version": version, "slug": "billing"})

    assert reply.status == 403
    assert harness.is_untouched()


async def test_a_stranger_cannot_tell_the_app_exists() -> None:
    harness = harness_with_releases()

    assert (await harness.patch(ERIN, {"version": 1, "slug": "billing"})).status == 404


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "billing"},
        {"version": 1},
        {"version": 1, "slug": "Not A Slug"},
        {"version": 1, "memory_budget_mb": 0},
        {"version": 1, "unknown": "x"},
    ],
    ids=["no version", "no field", "bad slug", "bad budget", "unknown field"],
)
async def test_a_body_that_fails_its_schema_is_refused(body: dict[str, object]) -> None:
    harness = harness_with_releases()

    reply = await harness.patch(DANA, body)

    assert reply.status == 400
    assert harness.is_untouched()


async def test_a_stale_version_on_a_patch_is_a_conflict() -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    reply = await harness.patch(DANA, {"version": version + 5, "slug": "billing"})

    assert (reply.status, error_of(reply)[0]) == (409, "changed_by_others")
    assert harness.is_untouched()


# --- PUT /apps/{app}/owner ---------------------------------------------------------------------


async def test_a_platform_admin_gives_an_app_to_someone_else() -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    reply = await harness.request(
        "PUT", "/apps/sales/owner", DANA, {"version": version, "owner": GRACE}
    )

    assert reply.status == 200
    detail = AppDetail.model_validate_json(reply.body)
    assert (detail.owner, detail.version) == (GRACE, version + 1)
    (row,) = harness.registry.audit_rows
    assert (row.actor, row.action, row.app_slug) == (DANA, "app.set_owner", "sales")
    assert row.detail["owner"] == GRACE
    (write,) = harness.registry.writes
    assert write.admin_token is not None


@pytest.mark.parametrize("email", [ALICE, CAROL])
async def test_the_owner_cannot_give_the_app_away_without_a_platform_admin(email: str) -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    reply = await harness.request(
        "PUT", "/apps/sales/owner", email, {"version": version, "owner": GRACE}
    )

    assert reply.status == 403
    assert harness.is_untouched()


async def test_naming_the_current_owner_again_changes_nothing() -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    reply = await harness.request(
        "PUT", "/apps/sales/owner", DANA, {"version": version, "owner": ALICE}
    )

    assert reply.status == 200
    assert harness.is_untouched()


@pytest.mark.parametrize(
    "body",
    [{"owner": GRACE}, {"version": 1}, {"version": 1, "owner": "not an email"}],
    ids=["no version", "no owner", "bad owner"],
)
async def test_an_owner_body_that_fails_its_schema_is_refused(body: dict[str, object]) -> None:
    harness = harness_with_releases()

    reply = await harness.request("PUT", "/apps/sales/owner", DANA, body)

    assert reply.status == 400
    assert harness.is_untouched()


async def test_a_stale_version_on_the_owner_is_a_conflict() -> None:
    harness = harness_with_releases()
    version = app_of(harness.registry, "sales").version

    reply = await harness.request(
        "PUT", "/apps/sales/owner", DANA, {"version": version + 3, "owner": GRACE}
    )

    assert (reply.status, error_of(reply)[0]) == (409, "changed_by_others")
    assert harness.is_untouched()
