"""GET /apps/{app}/requirements and PUT/DELETE /apps/{app}/resources/{key} (T122; FR-034 to FR-039).

The routes run behind the real guard and the real authorizer on the in-memory registry
(``stub_registry``), with a fake group lookup and a fake attach checker. The checker is the seam to
Databricks: what these tests prove is what the routes do with its verdict (what is written, what is
refused, what is audited), not what Databricks answers.

Starting data (all emails are placeholders):

* ``sales``: owner alice, OBO and shared-SP delegated; carol ``CAN_MANAGE``, bob ``CAN_USE``. Its
  target release 1 reads four keys (three from ``app.yaml``, one from the release config) and its
  ``databricks.yml`` offers two resources (one of them read, one not);
* ``plain``: owner erin, no delegation and no release; carol ``CAN_MANAGE``;
* ``selfish``: owner alice, OBO-delegated, with its own service principal held by the secrets
  ``sp-id`` and ``sp-secret``; carol ``CAN_MANAGE``;
* ``platform_admins``: dana; ``attach_check_warehouse_id`` is set unless a test says otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from stub_registry import StubRegistry

from appmux.admin.api import create_admin_app
from appmux.admin.authorizer import RegistryAuthorizer, make_authorizer
from appmux.admin.resources import ResourceRoutes
from appmux.admin.schemas import RequirementsResponse, ResourceView
from appmux.identity.actor import Actor
from appmux.registry.resources import (
    AttachCheckUnavailable,
    AttachProbe,
    CheckVerdict,
)

MOUNT: Final = "/_appmux/api/v1"
ALICE: Final = "alice@example.com"
BOB: Final = "bob@example.com"
CAROL: Final = "carol@example.com"
DANA: Final = "dana@example.com"
ERIN: Final = "erin@example.com"
MALLORY: Final = "mallory@example.com"
VISITOR_TOKEN: Final = "visitor-token-value-for-tests"  # noqa: S105 - a fake value for a test
NOW: Final = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}
CHECK_WAREHOUSE: Final = "wh-check"
RELEASE_DIGEST: Final = "sha256:" + "a" * 64

LEAK_MARKERS: Final = (VISITOR_TOKEN, "Traceback", "SELECT")

WAREHOUSE_BODY: Final = {
    "type": "sql_warehouse",
    "target": "wh-0123",
    "permission": "CAN_USE",
    "access_as": "obo",
}
SECRET_BODY: Final = {"type": "secret", "target": "scope-a/key-a", "permission": "READ"}


class StubChecker:
    """Answers with a fixed verdict (or raises) and remembers every probe it was given."""

    def __init__(
        self, verdict: CheckVerdict = CheckVerdict.OK, *, raises: Exception | None = None
    ) -> None:
        self.verdict = verdict
        self.raises = raises
        self.probes: list[AttachProbe] = []

    async def check(self, probe: AttachProbe) -> CheckVerdict:
        self.probes.append(probe)
        if self.raises is not None:
            raise self.raises
        return self.verdict


class FakeGroups:
    async def groups_for(self, actor: Actor) -> frozenset[str] | None:
        return None if actor.visitor_token is None else frozenset()


def app_row(
    slug: str, *, owner: str, obo: bool = False, shared_sp: bool = False
) -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": slug.title(),
        "owner": owner,
        "source_path": f"/Workspace/Users/{owner}/{slug}",
        "memory_budget_mb": 256,
        "obo_delegated": obo,
        "shared_sp_delegated": shared_sp,
        "created_by": DANA,
        "created_at": NOW,
        "updated_by": DANA,
        "updated_at": NOW,
    }


def config_row(slug: str, **fields: object) -> dict[str, object]:
    return {
        "app_slug": slug,
        "desired_state": "running",
        "updated_by": DANA,
        "updated_at": NOW,
        **fields,
    }


def grant_row(slug: str, principal: str, permission: str) -> dict[str, object]:
    return {
        "app_slug": slug,
        "principal_type": "user",
        "principal": principal,
        "permission": permission,
        "granted_by": ALICE,
        "granted_at": NOW,
    }


def resource_row(slug: str, key: str, *, target: str = "scope-s/key-s") -> dict[str, object]:
    return {
        "app_slug": slug,
        "key": key,
        "type": "secret",
        "target": target,
        "permission": "READ",
        "access_as": None,
        "check_result": "ok",
        "attached_by": ALICE,
        "attached_at": NOW,
    }


def setting_row(key: str, value: object) -> dict[str, object]:
    return {"key": key, "value": value, "updated_by": DANA, "updated_at": NOW}


def release_row(slug: str, release_no: int, **overrides: object) -> dict[str, object]:
    return {
        "app_slug": slug,
        "release_no": release_no,
        "snapshot_digest": RELEASE_DIGEST,
        "detected": {
            "runtime": "python",
            "framework": None,
            "command": ["python", "app.py"],
            "env": [
                {"name": "WAREHOUSE_ID", "value_from": "sql-warehouse"},
                {"name": "API_KEY", "value_from": "api-key"},
                {"name": "SERVING", "value_from": "model-endpoint"},
                {"name": "MODE", "value": "prod"},
            ],
            "bundle_resources": [
                {
                    "key": "sql-warehouse",
                    "type": "sql_warehouse",
                    "target": "wh-0123",
                    "permission": "CAN_USE",
                },
                {
                    "key": "unused-offer",
                    "type": "uc_volume",
                    "target": None,
                    "permission": "READ_VOLUME",
                },
            ],
            "warnings": [],
        },
        "config": {"env": [{"name": "EXTRA", "value_from": "extra-secret"}], "wheel_sources": []},
        "status": "healthy",
        "status_at": NOW,
        "created_by": ALICE,
        "created_at": NOW,
        **overrides,
    }


def seeded_registry(
    *, check_warehouse: bool = True, shared_sp_switch: bool | None = None
) -> StubRegistry:
    registry = StubRegistry(clock=lambda: NOW)
    registry.seed("apps", app_row("sales", owner=ALICE, obo=True, shared_sp=True))
    registry.seed("apps", app_row("plain", owner=ERIN))
    registry.seed("apps", app_row("selfish", owner=ALICE, obo=True))
    registry.seed("app_config", config_row("sales", target_release_no=1))
    registry.seed("app_config", config_row("plain"))
    own_sp_keys = {"own_sp_client_id_key": "sp-id", "own_sp_client_secret_key": "sp-secret"}
    registry.seed("app_config", config_row("selfish", **own_sp_keys))
    registry.seed("releases", release_row("sales", 1))
    registry.seed("app_resources", resource_row("selfish", "sp-id"))
    registry.seed("app_resources", resource_row("selfish", "sp-secret"))
    for slug in ("sales", "plain", "selfish"):
        registry.seed("app_grants", grant_row(slug, CAROL, "CAN_MANAGE"))
        registry.seed("app_grants", grant_row(slug, BOB, "CAN_USE"))
    registry.seed(
        "platform_settings",
        setting_row("platform_admins", [{"type": "user", "name": DANA}]),
    )
    if check_warehouse:
        registry.seed(
            "platform_settings", setting_row("attach_check_warehouse_id", CHECK_WAREHOUSE)
        )
    if shared_sp_switch is not None:
        registry.seed(
            "platform_settings", setting_row("shared_sp_delegation_allowed", shared_sp_switch)
        )
    return registry


class Harness:
    def __init__(
        self, registry: StubRegistry | None = None, checker: StubChecker | None = None
    ) -> None:
        self.registry = registry if registry is not None else seeded_registry()
        self.checker = checker if checker is not None else StubChecker()
        self.authorizer: RegistryAuthorizer = make_authorizer(self.registry, FakeGroups())
        admin = create_admin_app(
            registry=self.registry,
            authorizer=self.authorizer,
            route_providers=[ResourceRoutes(checker=self.checker, clock=lambda: NOW)],
        )
        self.client = TestClient(Starlette(routes=[Mount(MOUNT, app=admin)]))

    def call(
        self,
        method: str,
        path: str,
        email: str | None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        send_token: bool = True,
        json: object = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if email is not None:
            headers["X-Forwarded-Email"] = email
            if send_token:
                headers["X-Forwarded-Access-Token"] = VISITOR_TOKEN
        if method != "GET":
            headers.update(SAME_ORIGIN)
        headers.update(extra_headers or {})
        return self.client.request(method, f"{MOUNT}{path}", headers=headers, json=json)

    def requirements(self, slug: str, email: str | None = CAROL) -> httpx.Response:
        return self.call("GET", f"/apps/{slug}/requirements", email)

    def attach(
        self,
        slug: str,
        key: str,
        body: object,
        email: str | None = CAROL,
        *,
        extra_headers: Mapping[str, str] | None = None,
        send_token: bool = True,
    ) -> httpx.Response:
        return self.call(
            "PUT",
            f"/apps/{slug}/resources/{key}",
            email,
            json=body,
            extra_headers=extra_headers,
            send_token=send_token,
        )

    def detach(self, slug: str, key: str, email: str | None = CAROL) -> httpx.Response:
        return self.call("DELETE", f"/apps/{slug}/resources/{key}", email)

    def stored(self, slug: str) -> dict[str, tuple[str, str, str | None, str]]:
        """``key -> (type, target, access_as, check_result)`` of the app's attached resources."""
        return {
            resource.key: (
                resource.type,
                resource.target,
                resource.access_as,
                resource.check_result,
            )
            for resource in self.registry.snapshot.resources_for(slug)
        }

    def states(self, slug: str) -> dict[str, str]:
        parsed = RequirementsResponse.model_validate_json(self.requirements(slug).text)
        return {item.key: item.state for item in parsed.items}


@pytest.fixture
def harness() -> Harness:
    return Harness()


def error_code(response: httpx.Response) -> str:
    return str(response.json()["error"]["code"])


def assert_nothing_written(harness: Harness, before: Mapping[str, object]) -> None:
    assert harness.registry.audit_rows == ()
    assert harness.stored("sales") == before


# --- GET requirements -------------------------------------------------------------------------


def test_requirements_list_every_key_of_the_target_release_sorted(harness: Harness) -> None:
    response = harness.requirements("sales")

    assert response.status_code == 200
    parsed = RequirementsResponse.model_validate_json(response.text)
    assert parsed.release_no == 1
    assert [item.key for item in parsed.items] == [
        "api-key",
        "extra-secret",
        "model-endpoint",
        "sql-warehouse",
    ]
    assert {item.state for item in parsed.items} == {"missing"}


def test_a_key_of_the_release_config_counts_as_required(harness: Harness) -> None:
    states = harness.states("sales")

    assert "extra-secret" in states  # the one key that only the release config names
    assert "MODE" not in states  # a literal env entry needs no resource


def test_a_missing_key_with_an_offer_carries_the_offer_and_no_type_hint(harness: Harness) -> None:
    parsed = RequirementsResponse.model_validate_json(harness.requirements("sales").text)
    by_key = {item.key: item for item in parsed.items}

    offered = by_key["sql-warehouse"]
    assert offered.offer is not None
    assert (offered.offer.type, offered.offer.target, offered.offer.permission) == (
        "sql_warehouse",
        "wh-0123",
        "CAN_USE",
    )
    assert offered.suggested_type is None


def test_a_missing_key_with_no_offer_gets_a_type_hint_from_its_name(harness: Harness) -> None:
    parsed = RequirementsResponse.model_validate_json(harness.requirements("sales").text)
    by_key = {item.key: item for item in parsed.items}

    assert by_key["api-key"].offer is None
    assert by_key["api-key"].suggested_type == "secret"
    assert by_key["model-endpoint"].suggested_type == "serving_endpoint"


def test_an_offer_no_env_entry_asks_for_is_listed_apart(harness: Harness) -> None:
    parsed = RequirementsResponse.model_validate_json(harness.requirements("sales").text)

    assert [(offer.key, offer.type) for offer in parsed.unreferenced_offers] == [
        ("unused-offer", "uc_volume")
    ]


def test_reading_the_requirements_attaches_nothing_and_writes_nothing(harness: Harness) -> None:
    # The offers come from databricks.yml; appmux never attaches on the strength of one (FR-036).
    harness.requirements("sales")

    assert harness.stored("sales") == {}
    assert harness.registry.writes == ()
    assert harness.registry.audit_rows == ()
    assert harness.checker.probes == []


def test_an_app_with_no_target_release_has_no_requirements(harness: Harness) -> None:
    parsed = RequirementsResponse.model_validate_json(harness.requirements("plain").text)

    assert parsed.release_no is None
    assert parsed.items == []
    assert parsed.unreferenced_offers == []


def test_a_deleted_target_release_leaves_nothing_to_read() -> None:
    registry = seeded_registry()
    registry.seed("apps", app_row("gone", owner=ERIN))
    registry.seed("app_config", config_row("gone", target_release_no=1))
    registry.seed(
        "releases",
        release_row("gone", 1, snapshot_digest=None, deleted_by=CAROL, deleted_at=NOW),
    )
    registry.seed("app_grants", grant_row("gone", CAROL, "CAN_MANAGE"))
    harness = Harness(registry)

    parsed = RequirementsResponse.model_validate_json(harness.requirements("gone").text)

    assert (parsed.release_no, parsed.items) == (None, [])


def test_an_attached_key_reads_as_attached_and_a_detached_one_as_missing_again(
    harness: Harness,
) -> None:
    attached = harness.attach("sales", "api-key", SECRET_BODY)
    assert attached.status_code == 200, attached.text
    assert harness.states("sales")["api-key"] == "attached"

    detached = harness.detach("sales", "api-key")
    assert detached.status_code == 204
    assert harness.states("sales")["api-key"] == "missing"


def test_an_attached_key_has_no_type_hint(harness: Harness) -> None:
    harness.attach("sales", "api-key", SECRET_BODY)
    parsed = RequirementsResponse.model_validate_json(harness.requirements("sales").text)

    assert {item.key: item.suggested_type for item in parsed.items}["api-key"] is None


def test_attaching_an_offered_resource_removes_it_from_the_unreferenced_offers(
    harness: Harness,
) -> None:
    attached = harness.attach(
        "sales",
        "unused-offer",
        {
            "type": "uc_volume",
            "target": "/Volumes/main/default/files",
            "permission": "READ_VOLUME",
            "access_as": "obo",
            "from_bundle": True,
        },
    )
    assert attached.status_code == 200, attached.text

    parsed = RequirementsResponse.model_validate_json(harness.requirements("sales").text)
    assert parsed.unreferenced_offers == []


@pytest.mark.parametrize(
    ("email", "status"),
    [(BOB, 403), (MALLORY, 404), (None, 401)],
    ids=["can-use-only", "stranger", "no-identity"],
)
def test_requirements_are_for_those_who_manage_the_app(
    harness: Harness, email: str | None, status: int
) -> None:
    assert harness.requirements("sales", email).status_code == status


def test_requirements_of_a_missing_app_look_like_those_of_a_stranger(harness: Harness) -> None:
    missing = harness.requirements("nothing", MALLORY)
    stranger = harness.requirements("sales", MALLORY)

    assert missing.status_code == stranger.status_code == 404
    assert missing.json() == stranger.json()


def test_a_platform_admin_with_no_grant_reads_the_requirements(harness: Harness) -> None:
    assert harness.requirements("sales", DANA).status_code == 200


# --- PUT: what an attach stores and audits -----------------------------------------------------


def test_attaching_a_resource_stores_it_checked_and_returns_the_view(harness: Harness) -> None:
    response = harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)

    assert response.status_code == 200, response.text
    view = ResourceView.model_validate_json(response.text)
    assert (view.key, view.type, view.target, view.permission) == (
        "sql-warehouse",
        "sql_warehouse",
        "wh-0123",
        "CAN_USE",
    )
    assert (view.access_as, view.check_result, view.attached_by) == ("obo", "ok", CAROL)
    assert view.attached_at == NOW
    assert harness.stored("sales") == {"sql-warehouse": ("sql_warehouse", "wh-0123", "obo", "ok")}


def test_the_check_runs_with_the_attachers_own_token(harness: Harness) -> None:
    harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)

    (probe,) = harness.checker.probes
    assert probe.resource_type == "sql_warehouse"
    assert probe.target == "wh-0123"
    assert probe.permission == "CAN_USE"
    assert probe.token.get_secret_value() == VISITOR_TOKEN


def test_an_attach_writes_one_audit_row_naming_the_key_and_the_result(harness: Harness) -> None:
    harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)

    (row,) = harness.registry.audit_rows
    assert (row.action, row.app_slug, row.actor) == ("resource.attach", "sales", CAROL)
    assert row.detail == {
        "key": "sql-warehouse",
        "type": "sql_warehouse",
        "access_as": "obo",
        "check": "ok",
    }


def test_an_attach_from_the_bundle_offer_says_so_in_the_audit_row(harness: Harness) -> None:
    harness.attach("sales", "sql-warehouse", {**WAREHOUSE_BODY, "from_bundle": True})

    (row,) = harness.registry.audit_rows
    assert row.detail["from"] == "databricks.yml"


def test_a_secret_is_stored_as_a_reference_and_checked_on_the_check_warehouse(
    harness: Harness,
) -> None:
    response = harness.attach("sales", "api-key", SECRET_BODY)

    assert response.status_code == 200, response.text
    view = ResourceView.model_validate_json(response.text)
    assert (view.type, view.target, view.permission, view.access_as) == (
        "secret",
        "scope-a/key-a",
        "READ",
        None,
    )
    assert harness.stored("sales") == {"api-key": ("secret", "scope-a/key-a", None, "ok")}
    (probe,) = harness.checker.probes
    assert probe.warehouse_id == CHECK_WAREHOUSE


def test_a_secrets_audit_row_names_scope_and_key_and_no_access_as(harness: Harness) -> None:
    harness.attach("sales", "api-key", SECRET_BODY)

    (row,) = harness.registry.audit_rows
    assert row.detail == {
        "key": "api-key",
        "type": "secret",
        "target": "scope-a/key-a",
        "check": "ok",
    }


def test_attaching_again_replaces_the_resource_and_audits_again(harness: Harness) -> None:
    harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)
    changed = harness.attach(
        "sales",
        "sql-warehouse",
        {**WAREHOUSE_BODY, "target": "wh-4567", "permission": "CAN_MANAGE"},
    )

    assert changed.status_code == 200, changed.text
    assert harness.stored("sales") == {"sql-warehouse": ("sql_warehouse", "wh-4567", "obo", "ok")}
    assert [row.action for row in harness.registry.audit_rows] == ["resource.attach"] * 2


def test_a_type_with_no_check_is_stored_unverified_and_the_checker_is_not_asked(
    harness: Harness,
) -> None:
    response = harness.attach(
        "sales",
        "nightly",
        {"type": "job", "target": "1234", "permission": "CAN_MANAGE_RUN", "access_as": "obo"},
    )

    assert response.status_code == 200, response.text
    assert ResourceView.model_validate_json(response.text).check_result == "unverified"
    assert harness.stored("sales") == {"nightly": ("job", "1234", "obo", "unverified")}
    assert harness.checker.probes == []
    (row,) = harness.registry.audit_rows
    assert row.detail["check"] == "unverified"


def test_a_checker_that_cannot_tell_leaves_an_obo_resource_unverified() -> None:
    harness = Harness(checker=StubChecker(CheckVerdict.UNVERIFIED))

    response = harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)

    assert response.status_code == 200, response.text
    assert ResourceView.model_validate_json(response.text).check_result == "unverified"


def test_an_own_service_principal_resource_is_checked_and_stored() -> None:
    harness = Harness()

    response = harness.attach("selfish", "sql-warehouse", {**WAREHOUSE_BODY, "access_as": "own_sp"})

    assert response.status_code == 200, response.text
    assert harness.stored("selfish")["sql-warehouse"][2] == "own_sp"


def test_shared_sp_is_stored_once_the_platform_switch_is_on_and_the_check_passes() -> None:
    harness = Harness(seeded_registry(shared_sp_switch=True))

    response = harness.attach(
        "sales", "sql-warehouse", {**WAREHOUSE_BODY, "access_as": "shared_sp"}
    )

    assert response.status_code == 200, response.text
    assert harness.stored("sales") == {
        "sql-warehouse": ("sql_warehouse", "wh-0123", "shared_sp", "ok")
    }


def test_a_secret_may_replace_a_secret_that_holds_the_own_service_principal() -> None:
    harness = Harness()

    response = harness.attach("selfish", "sp-secret", {**SECRET_BODY, "target": "scope-b/key-b"})

    assert response.status_code == 200, response.text
    assert harness.stored("selfish")["sp-secret"][1] == "scope-b/key-b"


# --- PUT: what is refused, with nothing written ------------------------------------------------


def refused(
    harness: Harness,
    response: httpx.Response,
    status: int,
    code: str,
    *,
    kept: Mapping[str, object],
) -> None:
    assert response.status_code == status, response.text
    assert error_code(response) == code
    assert not any(marker in response.text for marker in LEAK_MARKERS)
    assert_nothing_written(harness, kept)


def test_no_access_refuses_the_attach_and_writes_nothing() -> None:
    harness = Harness(checker=StubChecker(CheckVerdict.NO_ACCESS))

    response = harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)

    refused(harness, response, 400, "no_access", kept={})
    assert "wh-0123" not in response.text


def test_shared_sp_is_refused_when_the_check_could_not_verify() -> None:
    harness = Harness(
        seeded_registry(shared_sp_switch=True), checker=StubChecker(CheckVerdict.UNVERIFIED)
    )

    response = harness.attach(
        "sales", "sql-warehouse", {**WAREHOUSE_BODY, "access_as": "shared_sp"}
    )

    refused(harness, response, 400, "unverified_shared_sp", kept={})


def test_shared_sp_is_refused_for_a_type_that_has_no_check() -> None:
    harness = Harness(seeded_registry(shared_sp_switch=True))

    response = harness.attach(
        "sales",
        "nightly",
        {"type": "job", "target": "1234", "permission": "CAN_MANAGE", "access_as": "shared_sp"},
    )

    refused(harness, response, 400, "unverified_shared_sp", kept={})
    assert harness.checker.probes == []


def test_obo_is_refused_for_an_app_without_the_obo_delegation(harness: Harness) -> None:
    response = harness.attach("plain", "sql-warehouse", WAREHOUSE_BODY, ERIN)

    assert response.status_code == 400
    assert error_code(response) == "delegation_missing"
    assert harness.stored("plain") == {}
    assert harness.registry.audit_rows == ()
    assert harness.checker.probes == []


def test_shared_sp_is_refused_while_the_platform_switch_is_off(harness: Harness) -> None:
    # `sales` is shared_sp-delegated; the platform admin has not allowed shared_sp anywhere.
    response = harness.attach(
        "sales", "sql-warehouse", {**WAREHOUSE_BODY, "access_as": "shared_sp"}
    )

    refused(harness, response, 400, "delegation_missing", kept={})


def test_shared_sp_is_refused_for_an_app_without_its_delegation_even_with_the_switch_on() -> None:
    harness = Harness(seeded_registry(shared_sp_switch=True))

    response = harness.attach(
        "plain", "sql-warehouse", {**WAREHOUSE_BODY, "access_as": "shared_sp"}, ERIN
    )

    assert response.status_code == 400
    assert error_code(response) == "delegation_missing"
    assert harness.stored("plain") == {}


def test_own_sp_is_refused_for_an_app_with_no_own_service_principal_keys(harness: Harness) -> None:
    response = harness.attach("sales", "sql-warehouse", {**WAREHOUSE_BODY, "access_as": "own_sp"})

    refused(harness, response, 400, "own_sp_keys_missing", kept={})


def test_a_level_the_type_does_not_have_is_refused(harness: Harness) -> None:
    response = harness.attach("sales", "sql-warehouse", {**WAREHOUSE_BODY, "permission": "SELECT"})

    refused(harness, response, 400, "bad_permission", kept={})
    assert harness.checker.probes == []


def test_an_unknown_type_is_refused(harness: Harness) -> None:
    response = harness.attach("sales", "thing", {**WAREHOUSE_BODY, "type": "spaceship"})

    refused(harness, response, 400, "unknown_type", kept={})


def test_a_target_the_type_cannot_have_is_refused_before_any_call(harness: Harness) -> None:
    response = harness.attach("sales", "sql-warehouse", {**WAREHOUSE_BODY, "target": "wh 0'; --"})

    refused(harness, response, 400, "bad_target", kept={})
    assert harness.checker.probes == []
    assert "wh 0" not in response.text


def test_a_secret_needs_the_check_warehouse_setting() -> None:
    harness = Harness(seeded_registry(check_warehouse=False))

    response = harness.attach("sales", "api-key", SECRET_BODY)

    refused(harness, response, 400, "check_warehouse_unset", kept={})
    assert "attach_check_warehouse_id" in response.json()["error"]["message"]


def test_an_attach_with_no_visitor_token_cannot_be_checked(harness: Harness) -> None:
    response = harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY, send_token=False)

    # No token also means no group lookup; carol still manages `sales` through her own grant.
    assert response.status_code == 400, response.text
    assert error_code(response) == "token_missing"
    assert harness.stored("sales") == {}
    assert harness.registry.audit_rows == ()
    assert harness.checker.probes == []


@pytest.mark.parametrize(
    "body",
    [
        {**SECRET_BODY, "access_as": "obo"},
        {**SECRET_BODY, "permission": "CAN_USE"},
        {**SECRET_BODY, "target": "no-slash"},
        {"type": "sql_warehouse", "target": "wh-0123", "permission": "CAN_USE"},
        {**WAREHOUSE_BODY, "access_as": "root"},
        {**WAREHOUSE_BODY, "surplus": 1},
        {**WAREHOUSE_BODY, "target": ""},
    ],
    ids=[
        "secret-with-access-as",
        "secret-with-another-level",
        "secret-without-scope",
        "no-access-as",
        "access-as-unknown",
        "unknown-field",
        "blank-target",
    ],
)
def test_a_body_that_breaks_the_schema_is_a_400_that_repeats_no_value(
    harness: Harness, body: dict[str, object]
) -> None:
    response = harness.attach("sales", "api-key", body)

    refused(harness, response, 400, error_code(response), kept={})
    assert harness.checker.probes == []
    assert "scope-a" not in response.text
    assert "no-slash" not in response.text


def test_a_key_that_is_not_a_resource_key_is_a_400(harness: Harness) -> None:
    response = harness.attach("sales", "Not_A_Key", WAREHOUSE_BODY)

    refused(harness, response, 400, error_code(response), kept={})
    assert harness.checker.probes == []


def test_a_key_shaped_like_a_credential_is_refused_and_never_echoed(harness: Harness) -> None:
    key = "dapi0123456789abcdef0123"

    response = harness.attach("sales", key, WAREHOUSE_BODY)

    refused(harness, response, 400, error_code(response), kept={})
    assert key not in response.text
    assert harness.checker.probes == []


def test_a_check_that_could_not_run_is_a_503_and_writes_nothing() -> None:
    harness = Harness(
        checker=StubChecker(raises=AttachCheckUnavailable("the check could not run; try again"))
    )

    response = harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)

    refused(harness, response, 503, "check_unavailable", kept={})


def test_a_registry_that_went_down_during_the_attach_is_a_503_and_writes_nothing(
    harness: Harness,
) -> None:
    harness.registry.go_down()

    response = harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)

    assert response.status_code == 503, response.text
    assert harness.stored("sales") == {}
    assert harness.registry.audit_rows == ()


def test_the_own_service_principal_secret_may_not_become_another_type(harness: Harness) -> None:
    response = harness.attach("selfish", "sp-secret", WAREHOUSE_BODY)

    assert response.status_code == 409, response.text
    assert error_code(response) == "in_use"
    assert harness.stored("selfish")["sp-secret"] == ("secret", "scope-s/key-s", None, "ok")
    assert harness.registry.audit_rows == ()
    assert harness.checker.probes == []


# --- PUT / DELETE: who may ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "status"),
    [(BOB, 403), (MALLORY, 404), (None, 401)],
    ids=["can-use-only", "stranger", "no-identity"],
)
def test_only_those_who_manage_the_app_may_attach(
    harness: Harness, email: str | None, status: int
) -> None:
    response = harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY, email)

    assert response.status_code == status
    assert harness.stored("sales") == {}
    assert harness.registry.audit_rows == ()
    assert harness.checker.probes == []


@pytest.mark.parametrize(
    ("email", "status"),
    [(BOB, 403), (MALLORY, 404), (None, 401)],
    ids=["can-use-only", "stranger", "no-identity"],
)
def test_only_those_who_manage_the_app_may_detach(
    harness: Harness, email: str | None, status: int
) -> None:
    harness.registry.seed("app_resources", resource_row("sales", "api-key"))

    response = harness.detach("sales", "api-key", email)

    assert response.status_code == status
    assert "api-key" in harness.stored("sales")
    assert harness.registry.audit_rows == ()


def test_a_platform_admin_with_no_grant_may_attach(harness: Harness) -> None:
    response = harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY, DANA)

    assert response.status_code == 200, response.text
    assert harness.registry.audit_rows[-1].actor == DANA


def test_a_call_that_does_not_come_from_the_page_is_refused(harness: Harness) -> None:
    attach = harness.attach(
        "sales",
        "sql-warehouse",
        WAREHOUSE_BODY,
        extra_headers={"Sec-Fetch-Site": "cross-site"},
    )
    detach = harness.call(
        "DELETE",
        "/apps/selfish/resources/sp-id",
        CAROL,
        extra_headers={"Sec-Fetch-Site": "cross-site"},
    )

    for response in (attach, detach):
        assert response.status_code == 403
        assert error_code(response) == "cross_origin"
    assert harness.stored("sales") == {}
    assert "sp-id" in harness.stored("selfish")


# --- DELETE ------------------------------------------------------------------------------------


def test_detaching_removes_the_resource_and_audits_it(harness: Harness) -> None:
    harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)

    response = harness.detach("sales", "sql-warehouse")

    assert response.status_code == 204
    assert response.content == b""
    assert harness.stored("sales") == {}
    attach_row, detach_row = harness.registry.audit_rows
    assert attach_row.action == "resource.attach"
    assert (detach_row.action, detach_row.app_slug, detach_row.actor) == (
        "resource.detach",
        "sales",
        CAROL,
    )
    assert detach_row.detail == {"key": "sql-warehouse", "type": "sql_warehouse"}


def test_detaching_a_secret_audits_no_target(harness: Harness) -> None:
    harness.attach("sales", "api-key", SECRET_BODY)

    harness.detach("sales", "api-key")

    assert harness.registry.audit_rows[-1].detail == {"key": "api-key", "type": "secret"}


@pytest.mark.parametrize("key", ["never-attached", "Not_A_Key"], ids=["unknown", "bad-form"])
def test_detaching_a_resource_the_app_does_not_have_is_a_404(harness: Harness, key: str) -> None:
    response = harness.detach("sales", key)

    assert response.status_code == 404
    assert error_code(response) == "no_such_resource"
    assert harness.registry.audit_rows == ()


def test_detaching_a_resource_of_another_app_is_a_404(harness: Harness) -> None:
    response = harness.detach("sales", "sp-id")  # `selfish` has it, `sales` does not

    assert response.status_code == 404
    assert "sp-id" in harness.stored("selfish")


@pytest.mark.parametrize("key", ["sp-id", "sp-secret"])
def test_the_resources_of_the_own_service_principal_cannot_be_detached(
    harness: Harness, key: str
) -> None:
    response = harness.detach("selfish", key)

    assert response.status_code == 409, response.text
    assert error_code(response) == "in_use"
    assert key in harness.stored("selfish")
    assert harness.registry.audit_rows == ()


def test_detaching_while_the_registry_is_down_is_a_503(harness: Harness) -> None:
    harness.registry.seed("app_resources", resource_row("sales", "api-key"))
    harness.registry.go_down()

    response = harness.detach("sales", "api-key")

    assert response.status_code == 503
    assert "api-key" in harness.stored("sales")


# --- the token and the logs --------------------------------------------------------------------


def test_the_visitor_token_reaches_no_response_audit_row_or_log(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        responses = [
            harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY),
            harness.attach("sales", "api-key", SECRET_BODY),
            harness.attach("sales", "bad", {**WAREHOUSE_BODY, "permission": "SELECT"}),
            harness.requirements("sales"),
            harness.detach("sales", "sql-warehouse"),
        ]

    assert VISITOR_TOKEN not in caplog.text
    for response in responses:
        assert VISITOR_TOKEN not in response.text
    for row in harness.registry.audit_rows:
        assert VISITOR_TOKEN not in repr(row)


def test_a_failing_check_leaves_neither_the_token_nor_its_error_in_the_response_or_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness(checker=StubChecker(raises=RuntimeError(f"boom {VISITOR_TOKEN}")))

    with caplog.at_level(logging.DEBUG):
        response = harness.attach("sales", "sql-warehouse", WAREHOUSE_BODY)

    assert response.status_code == 500
    assert error_code(response) == "internal_error"
    assert VISITOR_TOKEN not in response.text
    assert harness.stored("sales") == {}
    assert harness.registry.audit_rows == ()
