"""GET /me, GET /apps and the grant routes (T035; contracts/admin-api.md, FR-022 to FR-026).

The routes run behind the real guard and the real authorizer on the in-memory registry
(``stub_registry``), with a fake group lookup. Three kinds of actor are used throughout: a plain
user (bob), a user with ``CAN_MANAGE`` through a grant (carol) and a platform admin (dana) who is
named by no grant.

Starting data (all emails are placeholders):

* ``sales``: owner alice, OBO-delegated; bob ``CAN_USE`` and carol ``CAN_MANAGE`` by user grants,
  the group ``sales-team`` ``CAN_USE``;
* ``docs``: owner frank, shared with ``everyone``;
* ``ops``: owner erin, not OBO, shared with the group ``ops-team`` only;
* ``private``: owner erin, no grant;
* ``platform_admins``: dana as a user, ``data-platform`` as a group.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from stub_registry import StubRegistry

from appmux.admin.api import create_admin_app
from appmux.admin.authorizer import RegistryAuthorizer, make_authorizer
from appmux.admin.grants import GrantRoutes
from appmux.admin.identity import IdentityRoutes
from appmux.admin.schemas import AppGrant as AppGrantBody
from appmux.admin.schemas import AppList, MeResponse
from appmux.identity.actor import Actor
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.snapshot import RegistrySnapshot

MOUNT: Final = "/_appmux/api/v1"
ALICE: Final = "alice@example.com"
BOB: Final = "bob@example.com"
CAROL: Final = "carol@example.com"
DANA: Final = "dana@example.com"
ERIN: Final = "erin@example.com"
FRANK: Final = "frank@example.com"
GRACE: Final = "grace@example.com"
VISITOR_TOKEN: Final = "visitor-token-value-for-tests"  # noqa: S105 - a fake value for a test
NOW: Final = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
SAME_ORIGIN: Final = {"Sec-Fetch-Site": "same-origin"}

LEAK_MARKERS: Final = (VISITOR_TOKEN, "Traceback", "SELECT")


class FakeGroups:
    """A group lookup that answers from a table and can be told to fail or to break."""

    def __init__(self, table: Mapping[str, frozenset[str]]) -> None:
        self.table = dict(table)
        self.is_failing = False
        self.is_broken = False
        self.calls: list[str] = []

    async def groups_for(self, actor: Actor) -> frozenset[str] | None:
        self.calls.append(actor.email)
        if self.is_broken:
            raise RuntimeError("the group lookup broke")
        if self.is_failing or actor.visitor_token is None:
            return None  # what the real lookup answers: unknown, never "no groups"
        return self.table.get(actor.email.lower(), frozenset())


def app_row(
    slug: str, *, owner: str, obo: bool = False, display_name: str | None = None
) -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": display_name or slug.title(),
        "owner": owner,
        "source_path": f"/Workspace/Users/{owner}/{slug}",
        "memory_budget_mb": 256,
        "obo_delegated": obo,
        "created_by": DANA,
        "created_at": NOW,
        "updated_by": DANA,
        "updated_at": NOW,
    }


def grant_row(slug: str, kind: str, principal: str, permission: str) -> dict[str, object]:
    return {
        "app_slug": slug,
        "principal_type": kind,
        "principal": principal,
        "permission": permission,
        "granted_by": ALICE,
        "granted_at": NOW,
    }


def seeded_registry() -> StubRegistry:
    registry = StubRegistry(clock=lambda: NOW)
    for row in (
        app_row("sales", owner=ALICE, obo=True, display_name="Sales"),
        app_row("docs", owner=FRANK, display_name="Documents"),
        app_row("ops", owner=ERIN, display_name="Operations"),
        app_row("private", owner=ERIN, display_name="Private"),
    ):
        registry.seed("apps", row)
        registry.seed(
            "app_config",
            {
                "app_slug": row["slug"],
                "desired_state": "running",
                "updated_by": DANA,
                "updated_at": NOW,
            },
        )
    for row in (
        grant_row("sales", "user", BOB, "CAN_USE"),
        grant_row("sales", "user", CAROL, "CAN_MANAGE"),
        grant_row("sales", "group", "sales-team", "CAN_USE"),
        grant_row("docs", "everyone", "*", "CAN_USE"),
        grant_row("ops", "group", "ops-team", "CAN_USE"),
    ):
        registry.seed("app_grants", row)
    registry.seed(
        "platform_settings",
        {
            "key": "platform_admins",
            "value": [
                {"type": "user", "name": DANA},
                {"type": "group", "name": "data-platform"},
            ],
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )
    return registry


class Harness:
    def __init__(
        self,
        registry: StubRegistry | None = None,
        *,
        groups: Mapping[str, frozenset[str]] | None = None,
        bootstrap_admins: tuple[str, ...] = (),
    ) -> None:
        self.registry = registry if registry is not None else seeded_registry()
        self.groups = FakeGroups(
            groups
            if groups is not None
            else {
                BOB: frozenset({"sales-team"}),
                GRACE: frozenset({"ops-team"}),
                "heidi@example.com": frozenset({"data-platform"}),
            }
        )
        self.authorizer: RegistryAuthorizer = make_authorizer(
            self.registry, self.groups, bootstrap_admins=bootstrap_admins
        )
        admin = create_admin_app(
            registry=self.registry,
            authorizer=self.authorizer,
            route_providers=[
                IdentityRoutes(self.authorizer),
                GrantRoutes(clock=lambda: NOW),
            ],
        )
        self.client = TestClient(Starlette(routes=[Mount(MOUNT, app=admin)]))

    def call(
        self,
        method: str,
        path: str,
        email: str | None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        json: object = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if email is not None:
            headers["X-Forwarded-Email"] = email
            headers["X-Forwarded-Access-Token"] = VISITOR_TOKEN
        if method != "GET":
            headers.update(SAME_ORIGIN)
        headers.update(extra_headers or {})
        return self.client.request(
            method, f"{MOUNT}{path}", headers=headers, content=content, json=json
        )

    def put_grant(self, slug: str, email: str, body: object) -> httpx.Response:
        return self.call("PUT", f"/apps/{slug}/grants", email, json=body)

    def delete_grant(self, slug: str, email: str, kind: str, name: str) -> httpx.Response:
        return self.call("DELETE", f"/apps/{slug}/grants/{kind}/{name}", email)

    def grants_of(self, slug: str) -> dict[tuple[str, str], str]:
        return {
            (grant.principal_type, grant.principal): grant.permission
            for grant in self.registry.snapshot.grants_for(slug)
        }

    def listed(self, email: str) -> dict[str, dict[str, object]]:
        response = self.call("GET", "/apps", email)
        assert response.status_code == 200, response.text
        return {item["slug"]: item for item in response.json()["items"]}


@pytest.fixture
def harness() -> Harness:
    return Harness()


# --- GET /me ----------------------------------------------------------------------------------


def test_me_of_a_plain_user_lists_the_groups_and_no_admin_rights(harness: Harness) -> None:
    response = harness.call("GET", "/me", BOB)

    assert response.status_code == 200
    me = MeResponse.model_validate_json(response.text)
    assert me == MeResponse(email=BOB, groups=["sales-team"], is_platform_admin=False)


def test_me_of_a_platform_admin_named_as_a_user(harness: Harness) -> None:
    me = MeResponse.model_validate_json(harness.call("GET", "/me", DANA).text)

    assert me.is_platform_admin is True
    assert me.groups == []


def test_me_of_a_member_of_an_admin_group_is_a_platform_admin(harness: Harness) -> None:
    me = MeResponse.model_validate_json(harness.call("GET", "/me", "heidi@example.com").text)

    assert me.is_platform_admin is True
    assert me.groups == ["data-platform"]


def test_me_with_a_failed_group_lookup_has_no_groups_and_no_group_admin_rights(
    harness: Harness,
) -> None:
    harness.groups.is_failing = True

    heidi = MeResponse.model_validate_json(harness.call("GET", "/me", "heidi@example.com").text)
    dana = MeResponse.model_validate_json(harness.call("GET", "/me", DANA).text)

    assert heidi.groups == [] and heidi.is_platform_admin is False
    assert dana.is_platform_admin is True  # a user entry needs no group


def test_me_sorts_the_groups() -> None:
    harness = Harness(groups={BOB: frozenset({"b-team", "a-team", "c-team"})})

    me = MeResponse.model_validate_json(harness.call("GET", "/me", BOB).text)

    assert me.groups == ["a-team", "b-team", "c-team"]


def test_the_bootstrap_admin_is_a_platform_admin_while_the_setting_is_unset() -> None:
    registry = StubRegistry(clock=lambda: NOW)
    harness = Harness(registry, bootstrap_admins=(DANA,))

    assert MeResponse.model_validate_json(harness.call("GET", "/me", DANA).text).is_platform_admin
    assert not MeResponse.model_validate_json(
        harness.call("GET", "/me", BOB).text
    ).is_platform_admin


def test_the_bootstrap_admin_is_ignored_once_the_setting_exists() -> None:
    harness = Harness(bootstrap_admins=(BOB,))

    assert not MeResponse.model_validate_json(
        harness.call("GET", "/me", BOB).text
    ).is_platform_admin


def test_me_and_apps_need_an_identity(harness: Harness) -> None:
    assert harness.call("GET", "/me", None).status_code == 401
    assert harness.call("GET", "/apps", None).status_code == 401


# --- GET /apps --------------------------------------------------------------------------------


def test_a_plain_user_sees_only_the_apps_they_can_open(harness: Harness) -> None:
    response = harness.call("GET", "/apps", BOB)

    listing = AppList.model_validate_json(response.text)
    assert [(item.slug, item.permission) for item in listing.items] == [
        ("docs", "CAN_USE"),  # everyone
        ("sales", "CAN_USE"),  # a user grant and the group, both CAN_USE
    ]
    assert all(item.open_modes == [] for item in listing.items)


def test_a_can_manage_user_sees_can_manage_on_that_app_only(harness: Harness) -> None:
    listed = harness.listed(CAROL)

    assert set(listed) == {"docs", "sales"}
    assert listed["sales"]["permission"] == "CAN_MANAGE"
    assert listed["docs"]["permission"] == "CAN_USE"


def test_a_platform_admin_sees_every_app_with_can_manage(harness: Harness) -> None:
    listing = AppList.model_validate_json(harness.call("GET", "/apps", DANA).text)

    assert [item.slug for item in listing.items] == ["docs", "ops", "private", "sales"]
    assert {item.permission for item in listing.items} == {"CAN_MANAGE"}


def test_the_list_is_ordered_by_display_name_then_slug() -> None:
    registry = seeded_registry()
    registry.seed("apps", app_row("zeta", owner=ERIN, display_name="Documents"))
    harness = Harness(registry)

    slugs = [item["slug"] for item in harness.call("GET", "/apps", DANA).json()["items"]]

    assert slugs == ["docs", "zeta", "ops", "private", "sales"]


def test_an_admin_who_is_neither_owner_nor_named_gets_the_two_open_modes_on_an_obo_app(
    harness: Harness,
) -> None:
    listed = harness.listed(DANA)

    assert listed["sales"]["open_modes"] == ["without_token", "as_user"]
    assert listed["docs"]["open_modes"] == []  # not OBO-delegated: one Open
    assert listed["ops"]["open_modes"] == []


def test_an_owner_a_grant_holder_and_a_named_admin_get_one_open(harness: Harness) -> None:
    harness.registry.seed("app_grants", grant_row("sales", "user", DANA, "CAN_USE"))

    assert harness.listed(ALICE)["sales"]["open_modes"] == []  # the owner
    assert harness.listed(BOB)["sales"]["open_modes"] == []  # a visitor
    assert harness.listed(DANA)["sales"]["open_modes"] == []  # an admin named by a grant


def test_everyone_sees_the_everyone_apps_and_a_stranger_sees_nothing_else(harness: Harness) -> None:
    assert set(harness.listed("mallory@example.com")) == {"docs"}


def test_a_group_grant_lists_the_app_for_a_member(harness: Harness) -> None:
    assert set(harness.listed(GRACE)) == {"docs", "ops"}


def test_a_failed_group_lookup_hides_group_shared_apps_but_not_user_or_everyone_ones(
    harness: Harness,
) -> None:
    harness.groups.is_failing = True

    assert set(harness.listed(GRACE)) == {"docs"}  # ops was shared with a group only
    assert set(harness.listed(BOB)) == {"docs", "sales"}  # bob also holds a user grant
    assert set(harness.listed("heidi@example.com")) == {"docs"}  # no admin rights without groups


def test_a_broken_group_lookup_fails_closed_too(harness: Harness) -> None:
    harness.groups.is_broken = True

    assert set(harness.listed(GRACE)) == {"docs"}


def test_a_request_without_a_visitor_token_matches_no_group() -> None:
    harness = Harness()

    response = harness.client.get(f"{MOUNT}/apps", headers={"X-Forwarded-Email": GRACE})

    assert [item["slug"] for item in response.json()["items"]] == ["docs"]


def test_the_state_comes_from_the_desired_state_until_the_reconciler_reports() -> None:
    registry = seeded_registry()
    registry.seed(
        "app_config",
        {"app_slug": "docs", "desired_state": "stopped", "updated_by": DANA, "updated_at": NOW},
    )
    harness = Harness(registry)

    listed = harness.listed(DANA)

    assert listed["docs"]["state"] == "stopped"
    assert listed["sales"]["state"] == "starting"  # wanted running; nothing yet says it is


def test_the_list_is_served_from_the_last_snapshot_while_the_registry_is_down(
    harness: Harness,
) -> None:
    harness.registry.go_down()

    assert set(harness.listed(BOB)) == {"docs", "sales"}


def test_a_removed_grant_denies_on_the_next_snapshot(harness: Harness) -> None:
    assert asyncio.run(harness.authorizer(Actor(email=BOB), "app.open", "sales")) is True
    assert "sales" in harness.listed(BOB)

    removed = harness.delete_grant("sales", CAROL, "user", BOB)
    assert removed.status_code == 204
    harness.groups.table[BOB] = frozenset()  # and bob's group no longer shares it either

    assert asyncio.run(harness.authorizer(Actor(email=BOB), "app.open", "sales")) is False
    assert "sales" not in harness.listed(BOB)


# --- PUT /apps/{app}/grants -------------------------------------------------------------------


def test_an_app_admin_adds_a_user_grant(harness: Harness) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": GRACE, "permission": "CAN_USE"}
    )

    assert response.status_code == 200, response.text
    body = AppGrantBody.model_validate_json(response.text)
    assert (body.type, body.name, body.permission, body.granted_by) == (
        "user",
        GRACE,
        "CAN_USE",
        CAROL,
    )
    assert body.granted_at == NOW
    assert harness.grants_of("sales")[("user", GRACE)] == "CAN_USE"


def test_a_group_and_an_everyone_grant_can_be_added(harness: Harness) -> None:
    group = harness.put_grant(
        "ops", ERIN, {"type": "group", "name": "finance", "permission": "CAN_MANAGE"}
    )
    everyone = harness.put_grant(
        "ops", ERIN, {"type": "everyone", "name": "*", "permission": "CAN_USE"}
    )

    assert group.status_code == everyone.status_code == 200
    assert harness.grants_of("ops") == {
        ("group", "ops-team"): "CAN_USE",
        ("group", "finance"): "CAN_MANAGE",
        ("everyone", "*"): "CAN_USE",
    }


def test_a_new_grant_takes_effect_on_the_next_request(harness: Harness) -> None:
    assert "private" not in harness.listed(GRACE)

    harness.put_grant("private", ERIN, {"type": "user", "name": GRACE, "permission": "CAN_USE"})

    assert "private" in harness.listed(GRACE)


def test_changing_the_level_updates_the_row_and_the_audit_row_says_from_and_to(
    harness: Harness,
) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": BOB, "permission": "CAN_MANAGE"}
    )

    assert response.status_code == 200
    assert harness.grants_of("sales")[("user", BOB)] == "CAN_MANAGE"
    row = harness.registry.audit_rows[-1]
    assert (row.action, row.app_slug, row.actor) == ("grant.app", "sales", CAROL)
    assert row.detail == {"principal": f"user:{BOB}", "permission": "CAN_MANAGE", "from": "CAN_USE"}


def test_the_audit_row_of_a_new_grant_names_the_principal_and_level(harness: Harness) -> None:
    harness.put_grant("sales", CAROL, {"type": "group", "name": "finance", "permission": "CAN_USE"})

    assert len(harness.registry.audit_rows) == 1
    row = harness.registry.audit_rows[0]
    assert row.action == "grant.app"
    assert row.actor == CAROL
    assert row.app_slug == "sales"
    assert row.detail == {"principal": "group:finance", "permission": "CAN_USE"}


def test_putting_the_level_a_principal_already_has_writes_nothing(harness: Harness) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": BOB, "permission": "CAN_USE"}
    )

    assert response.status_code == 200
    body = AppGrantBody.model_validate_json(response.text)
    assert (body.name, body.permission, body.granted_by) == (BOB, "CAN_USE", ALICE)
    assert harness.registry.audit_rows == ()


def test_a_group_is_found_under_its_stored_spelling() -> None:
    registry = seeded_registry()
    registry.seed("app_grants", grant_row("private", "group", "Finance-Team", "CAN_USE"))
    harness = Harness(registry)

    response = harness.put_grant(
        "private", ERIN, {"type": "group", "name": "finance-team", "permission": "CAN_MANAGE"}
    )

    assert response.status_code == 200
    assert harness.grants_of("private") == {("group", "Finance-Team"): "CAN_MANAGE"}


def test_a_platform_admin_with_no_grant_may_share_an_app(harness: Harness) -> None:
    response = harness.put_grant(
        "ops", DANA, {"type": "user", "name": BOB, "permission": "CAN_USE"}
    )

    assert response.status_code == 200
    assert response.json()["granted_by"] == DANA
    assert harness.registry.audit_rows[-1].actor == DANA


def test_a_can_use_holder_may_not_share(harness: Harness) -> None:
    response = harness.put_grant(
        "sales", BOB, {"type": "user", "name": GRACE, "permission": "CAN_USE"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert (("user", GRACE)) not in harness.grants_of("sales")
    assert harness.registry.audit_rows == ()


def test_a_stranger_and_a_missing_app_get_the_same_404(harness: Harness) -> None:
    stranger = harness.put_grant(
        "private", "mallory@example.com", {"type": "user", "name": BOB, "permission": "CAN_USE"}
    )
    missing = harness.put_grant(
        "nothing", "mallory@example.com", {"type": "user", "name": BOB, "permission": "CAN_USE"}
    )

    assert stranger.status_code == missing.status_code == 404
    assert stranger.json() == missing.json()
    assert harness.grants_of("private") == {}


def test_a_group_manager_whose_lookup_failed_is_treated_as_having_no_access() -> None:
    registry = seeded_registry()
    registry.seed("app_grants", grant_row("ops", "group", "ops-team", "CAN_MANAGE"))
    harness = Harness(registry)
    harness.groups.is_failing = True

    response = harness.put_grant(
        "ops", GRACE, {"type": "user", "name": BOB, "permission": "CAN_USE"}
    )

    assert response.status_code == 404
    assert ("user", BOB) not in harness.grants_of("ops")


def test_a_group_manager_with_a_working_lookup_may_share() -> None:
    registry = seeded_registry()
    registry.seed("app_grants", grant_row("ops", "group", "ops-team", "CAN_MANAGE"))
    harness = Harness(registry)

    response = harness.put_grant(
        "ops", GRACE, {"type": "user", "name": BOB, "permission": "CAN_USE"}
    )

    assert response.status_code == 200


def test_the_owner_cannot_be_named_with_a_lower_level(harness: Harness) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": ALICE, "permission": "CAN_USE"}
    )

    assert response.status_code == 400
    assert harness.grants_of("sales") == {
        ("user", BOB): "CAN_USE",
        ("user", CAROL): "CAN_MANAGE",
        ("group", "sales-team"): "CAN_USE",
    }
    assert harness.registry.audit_rows == ()


def test_naming_the_owner_with_can_manage_is_accepted(harness: Harness) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": ALICE, "permission": "CAN_MANAGE"}
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"type": "everyone", "name": "*", "permission": "CAN_MANAGE"},
        {"type": "everyone", "name": "sales-team", "permission": "CAN_USE"},
        {"type": "user", "name": "Carol", "permission": "CAN_USE"},
        {"type": "user", "name": "Grace@Example.com", "permission": "CAN_USE"},
        {"type": "team", "name": "x", "permission": "CAN_USE"},
        {"type": "group", "name": "*", "permission": "CAN_USE"},
        {"type": "user", "name": GRACE, "permission": "OWNER"},
        {"type": "user", "name": GRACE},
        {"type": "user", "name": GRACE, "permission": "CAN_USE", "granted_by": ALICE},
        {},
    ],
)
def test_a_body_the_schema_refuses_is_a_400_and_writes_nothing(
    harness: Harness, body: object
) -> None:
    response = harness.put_grant("sales", CAROL, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert len(harness.grants_of("sales")) == 3
    assert harness.registry.audit_rows == ()


def test_a_body_that_is_not_json_is_a_400(harness: Harness) -> None:
    response = harness.call(
        "PUT",
        "/apps/sales/grants",
        CAROL,
        content=b"not json",
        extra_headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_a_change_must_come_from_the_admin_page_itself(harness: Harness) -> None:
    response = harness.client.put(
        f"{MOUNT}/apps/sales/grants",
        headers={"X-Forwarded-Email": CAROL},
        json={"type": "user", "name": GRACE, "permission": "CAN_USE"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cross_origin"
    assert ("user", GRACE) not in harness.grants_of("sales")


def test_a_grant_write_needs_the_registry(harness: Harness) -> None:
    harness.registry.go_down()

    response = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": GRACE, "permission": "CAN_USE"}
    )

    assert response.status_code == 503
    assert harness.registry.audit_rows == ()


def test_neither_the_response_nor_the_audit_rows_hold_the_visitor_token(harness: Harness) -> None:
    put = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": GRACE, "permission": "CAN_USE"}
    )
    delete = harness.delete_grant("sales", CAROL, "user", GRACE)
    bad = harness.put_grant("sales", CAROL, {"type": "user", "name": "x", "permission": "CAN_USE"})

    texts = [put.text, delete.text, bad.text]
    texts += [json.dumps(row.model_dump(mode="json")) for row in harness.registry.audit_rows]
    for text in texts:
        for marker in LEAK_MARKERS:
            assert marker not in text


def test_a_principal_the_audit_log_cannot_hold_is_refused_before_anything_is_written(
    harness: Harness,
) -> None:
    # A group name shaped like a credential is refused by the audit entry (data model rule 15).
    name = f"token={VISITOR_TOKEN}"

    response = harness.put_grant(
        "sales", CAROL, {"type": "group", "name": name, "permission": "CAN_USE"}
    )

    assert response.status_code == 400
    assert VISITOR_TOKEN not in response.text
    assert len(harness.grants_of("sales")) == 3


# --- DELETE /apps/{app}/grants/{type}/{name} --------------------------------------------------


def test_an_app_admin_removes_a_user_grant(harness: Harness) -> None:
    response = harness.delete_grant("sales", CAROL, "user", BOB)

    assert response.status_code == 204
    assert response.content == b""
    assert ("user", BOB) not in harness.grants_of("sales")
    row = harness.registry.audit_rows[-1]
    assert (row.action, row.app_slug, row.actor) == ("grant.app", "sales", CAROL)
    assert row.detail == {"principal": f"user:{BOB}", "permission": "CAN_USE", "to": "removed"}


def test_a_group_and_the_everyone_grant_can_be_removed(harness: Harness) -> None:
    group = harness.delete_grant("sales", CAROL, "group", "sales-team")
    everyone = harness.delete_grant("docs", FRANK, "everyone", "*")

    assert group.status_code == everyone.status_code == 204
    assert ("group", "sales-team") not in harness.grants_of("sales")
    assert harness.grants_of("docs") == {}


def test_the_owner_cannot_be_removed(harness: Harness) -> None:
    response = harness.delete_grant("sales", CAROL, "user", ALICE)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "owner_protected"
    assert harness.registry.audit_rows == ()


def test_the_owner_cannot_be_removed_even_with_no_grant_row_and_a_platform_admin_asking(
    harness: Harness,
) -> None:
    response = harness.delete_grant("ops", DANA, "user", ERIN)

    assert response.status_code == 400


def test_removing_a_grant_that_is_not_there_is_a_404_with_its_own_code(harness: Harness) -> None:
    response = harness.delete_grant("sales", CAROL, "user", GRACE)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "no_such_grant"
    assert harness.registry.audit_rows == ()


def test_a_can_use_holder_may_not_remove_a_grant(harness: Harness) -> None:
    response = harness.delete_grant("sales", BOB, "user", CAROL)

    assert response.status_code == 403
    assert ("user", CAROL) in harness.grants_of("sales")


def test_a_stranger_removing_a_grant_gets_the_no_such_app_404(harness: Harness) -> None:
    response = harness.delete_grant("private", "mallory@example.com", "user", BOB)

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "no such app"}}


@pytest.mark.parametrize(
    ("kind", "name"),
    [("team", "x"), ("user", "Carol"), ("everyone", "sales-team"), ("group", "*")],
)
def test_a_path_the_schema_refuses_is_a_400(harness: Harness, kind: str, name: str) -> None:
    response = harness.delete_grant("sales", CAROL, kind, name)

    assert response.status_code == 400
    assert len(harness.grants_of("sales")) == 3


def test_a_removal_needs_the_registry(harness: Harness) -> None:
    harness.registry.go_down()

    assert harness.delete_grant("sales", CAROL, "user", BOB).status_code == 503


# --- service principal grants (T188, ADR 0020) ------------------------------------------------

# A made-up application id: an App's service principal reaches a child under this value in
# X-Forwarded-Email (docs/reference/mcp-and-a2a.md).
SALES_BOT: Final = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab"


def test_an_app_admin_shares_an_app_with_a_service_principal(harness: Harness) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "service_principal", "name": SALES_BOT, "permission": "CAN_USE"}
    )

    assert response.status_code == 200, response.text
    body = AppGrantBody.model_validate_json(response.text)
    assert (body.type, body.name, body.permission, body.granted_by) == (
        "service_principal",
        SALES_BOT,
        "CAN_USE",
        CAROL,
    )
    assert harness.grants_of("sales")[("service_principal", SALES_BOT)] == "CAN_USE"
    row = harness.registry.audit_rows[-1]
    assert row.detail == {"principal": f"service_principal:{SALES_BOT}", "permission": "CAN_USE"}


def test_a_service_principal_grant_matches_the_application_id_in_any_case(
    harness: Harness,
) -> None:
    harness.put_grant(
        "sales", CAROL, {"type": "service_principal", "name": SALES_BOT, "permission": "CAN_USE"}
    )

    for forwarded in (SALES_BOT, SALES_BOT.upper()):
        listed = harness.listed(forwarded)
        assert listed["sales"]["permission"] == "CAN_USE"
        assert set(listed) == {"sales", "docs"}  # docs is shared with everyone
        assert ask(harness, forwarded, "app.open", "sales") is True
        assert ask(harness, forwarded, "app.view", "sales") is False


def test_a_service_principal_cannot_be_given_can_manage(harness: Harness) -> None:
    response = harness.put_grant(
        "sales",
        CAROL,
        {"type": "service_principal", "name": SALES_BOT, "permission": "CAN_MANAGE"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert ("service_principal", SALES_BOT) not in harness.grants_of("sales")
    assert harness.registry.audit_rows == ()


@pytest.mark.parametrize(
    "name",
    ["sales-bot", SALES_BOT.upper(), SALES_BOT.replace("-", ""), f"{{{SALES_BOT}}}", "*", ""],
    ids=["a-name", "upper-case", "no-dashes", "braces", "star", "empty"],
)
def test_a_service_principal_not_named_by_a_lower_case_application_id_is_refused(
    harness: Harness, name: str
) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "service_principal", "name": name, "permission": "CAN_USE"}
    )

    assert response.status_code == 400
    assert len(harness.grants_of("sales")) == 3
    assert harness.registry.audit_rows == ()


def test_an_app_admin_removes_a_service_principal_grant(harness: Harness) -> None:
    harness.put_grant(
        "sales", CAROL, {"type": "service_principal", "name": SALES_BOT, "permission": "CAN_USE"}
    )

    response = harness.delete_grant("sales", CAROL, "service_principal", SALES_BOT)

    assert response.status_code == 204
    assert ("service_principal", SALES_BOT) not in harness.grants_of("sales")
    row = harness.registry.audit_rows[-1]
    assert row.detail == {
        "principal": f"service_principal:{SALES_BOT}",
        "permission": "CAN_USE",
        "to": "removed",
    }
    assert "sales" not in harness.listed(SALES_BOT)


@pytest.mark.parametrize("name", ["sales-bot", SALES_BOT.upper()], ids=["a-name", "upper-case"])
def test_a_service_principal_path_that_is_not_a_lower_case_application_id_is_a_400(
    harness: Harness, name: str
) -> None:
    response = harness.delete_grant("sales", CAROL, "service_principal", name)

    assert response.status_code == 400
    assert "name" in response.json()["error"]["message"]


def test_an_application_id_with_no_grant_sees_only_what_everyone_sees(harness: Harness) -> None:
    assert set(harness.listed(SALES_BOT)) == {"docs"}
    assert ask(harness, SALES_BOT, "app.open", "sales") is False


def test_a_manager_who_loses_the_grant_loses_the_right_to_share(harness: Harness) -> None:
    harness.put_grant("ops", ERIN, {"type": "user", "name": GRACE, "permission": "CAN_MANAGE"})
    assert (
        harness.put_grant(
            "ops", GRACE, {"type": "user", "name": BOB, "permission": "CAN_USE"}
        ).status_code
        == 200
    )

    harness.delete_grant("ops", ERIN, "user", GRACE)
    harness.groups.table[GRACE] = frozenset()  # and the group no longer shares it either

    again = harness.put_grant(
        "ops", GRACE, {"type": "user", "name": CAROL, "permission": "CAN_USE"}
    )
    assert again.status_code == 404
    assert ("user", CAROL) not in harness.grants_of("ops")


# --- the authorizer ---------------------------------------------------------------------------


def ask(harness: Harness, email: str, action: str, app: str | None) -> bool:
    return asyncio.run(
        harness.authorizer(Actor(email=email, visitor_token=SecretStr("t")), action, app)
    )


def test_the_authorizer_answers_with_a_plain_bool(harness: Harness) -> None:
    assert ask(harness, BOB, "app.open", "sales") is True
    assert ask(harness, BOB, "grant.app", "sales") is False
    assert ask(harness, CAROL, "grant.app", "sales") is True
    assert ask(harness, DANA, "platform.view", None) is True
    assert ask(harness, BOB, "platform.view", None) is False


def test_the_authorizer_denies_an_unknown_action_and_an_unknown_app(harness: Harness) -> None:
    assert ask(harness, DANA, "app.fly", "sales") is False
    assert ask(harness, DANA, "app.open", "nothing") is False


def test_the_authorizer_reads_the_current_snapshot_on_every_call(harness: Harness) -> None:
    assert ask(harness, GRACE, "app.open", "private") is False

    harness.put_grant("private", ERIN, {"type": "user", "name": GRACE, "permission": "CAN_USE"})

    assert ask(harness, GRACE, "app.open", "private") is True


class SnapshotlessRegistry(StubRegistry):
    """A registry whose snapshot cannot be read, for a defect the authorizer must survive."""

    @property
    def snapshot(self) -> RegistrySnapshot:
        raise RuntimeError("the snapshot broke")


def authorizer_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "appmux.admin.authorizer"]


def test_a_defect_inside_the_authorizer_fails_closed_and_is_logged_once_without_the_actor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = Harness(SnapshotlessRegistry())

    with caplog.at_level(logging.DEBUG):
        answer = ask(harness, DANA, "app.open", "sales")

    assert answer is False
    records = authorizer_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert VISITOR_TOKEN not in caplog.text
    assert DANA not in caplog.text


def test_a_group_lookup_that_raises_counts_as_unknown_groups(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.groups.is_broken = True

    with caplog.at_level(logging.DEBUG):
        group_only = ask(harness, GRACE, "app.open", "ops")
        named_admin = ask(harness, DANA, "app.open", "ops")

    assert group_only is False  # ops is shared with a group only
    assert named_admin is True  # a user entry of platform_admins needs no group
    assert len(authorizer_records(caplog)) == 2  # once per question
    assert VISITOR_TOKEN not in caplog.text


# --- group names: spaces, slashes, control characters (D3, D4) --------------------------------


def path_of(name: str) -> str:
    return quote(name, safe="")


def test_a_group_whose_name_holds_a_space_is_granted_shown_and_revoked() -> None:
    harness = Harness(groups={GRACE: frozenset({"data platform"})})

    added = harness.put_grant(
        "private", ERIN, {"type": "group", "name": "Data Platform", "permission": "CAN_USE"}
    )

    assert added.status_code == 200, added.text
    assert added.json()["name"] == "Data Platform"
    assert harness.grants_of("private") == {("group", "Data Platform"): "CAN_USE"}
    row = harness.registry.audit_rows[-1]
    assert row.detail == {"principal": "group:Data Platform", "permission": "CAN_USE"}
    assert "private" in harness.listed(GRACE)  # a member (groups are lower-case) can open it

    removed = harness.call("DELETE", f"/apps/private/grants/group/{path_of('Data Platform')}", ERIN)

    assert removed.status_code == 204
    assert harness.grants_of("private") == {}
    assert harness.registry.audit_rows[-1].detail == {
        "principal": "group:Data Platform",
        "permission": "CAN_USE",
        "to": "removed",
    }
    assert "private" not in harness.listed(GRACE)


def test_a_group_whose_name_holds_a_slash_can_be_revoked_through_the_route(
    harness: Harness,
) -> None:
    added = harness.put_grant(
        "sales", CAROL, {"type": "group", "name": "eu/finance", "permission": "CAN_USE"}
    )
    assert added.status_code == 200, added.text
    assert ("group", "eu/finance") in harness.grants_of("sales")

    for spelling in (path_of("eu/finance"), "eu/finance"):
        if ("group", "eu/finance") not in harness.grants_of("sales"):
            harness.put_grant(
                "sales", CAROL, {"type": "group", "name": "eu/finance", "permission": "CAN_USE"}
            )
        removed = harness.call("DELETE", f"/apps/sales/grants/group/{spelling}", CAROL)

        assert removed.status_code == 204, (spelling, removed.text)
        assert ("group", "eu/finance") not in harness.grants_of("sales")
    assert harness.registry.audit_rows[-1].detail["to"] == "removed"


@pytest.mark.parametrize("name", ["bad\nname", "bad\x00name", "bad\x1fname", "bad\u2028name"])
def test_a_group_name_with_a_control_character_is_refused_naming_the_name_field(
    harness: Harness, name: str
) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "group", "name": name, "permission": "CAN_USE"}
    )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "name" in message
    assert "principal" not in message  # a field the caller never sent
    assert len(harness.grants_of("sales")) == 3
    assert harness.registry.audit_rows == ()


def test_a_group_name_shaped_like_a_credential_is_refused_naming_the_name_field(
    harness: Harness,
) -> None:
    response = harness.put_grant(
        "sales",
        CAROL,
        {"type": "group", "name": f"token={VISITOR_TOKEN}", "permission": "CAN_USE"},
    )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "name" in message and "principal" not in message
    assert VISITOR_TOKEN not in response.text


def test_a_group_name_over_the_length_limit_is_refused_naming_the_name_field(
    harness: Harness,
) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "group", "name": "g" * 256, "permission": "CAN_USE"}
    )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "name" in message and "principal" not in message
    assert len(harness.grants_of("sales")) == 3


@pytest.mark.parametrize(
    ("kind", "name", "field"),
    [("team", "x", "type"), ("user", "Carol", "name"), ("group", "*", "name")],
)
def test_a_refused_delete_path_names_the_path_parameter_not_a_body(
    harness: Harness, kind: str, name: str, field: str
) -> None:
    response = harness.delete_grant("sales", CAROL, kind, name)

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert field in message
    assert "body" not in message and "principal" not in message


def test_a_delete_path_with_a_control_character_in_the_group_name_is_a_400_naming_the_name(
    harness: Harness,
) -> None:
    response = harness.call("DELETE", "/apps/sales/grants/group/bad%1Fname", CAROL)

    assert response.status_code == 400
    assert "name" in response.json()["error"]["message"]


def test_a_delete_path_with_a_newline_in_the_name_matches_no_route(harness: Harness) -> None:
    # Starlette's path convertor does not match a newline; no grant can hold such a name, so the
    # standard 404 is the right answer and nothing is removed.
    response = harness.call("DELETE", "/apps/sales/grants/group/bad%0Aname", CAROL)

    assert response.status_code == 404
    assert len(harness.grants_of("sales")) == 3


# --- audit detail details ---------------------------------------------------------------------


def test_removing_a_can_manage_grant_records_can_manage_in_the_audit_row(harness: Harness) -> None:
    response = harness.delete_grant("sales", DANA, "user", CAROL)

    assert response.status_code == 204
    assert harness.registry.audit_rows[-1].detail == {
        "principal": f"user:{CAROL}",
        "permission": "CAN_MANAGE",
        "to": "removed",
    }


def test_removing_a_grant_found_under_another_spelling_records_the_stored_spelling() -> None:
    registry = seeded_registry()
    registry.seed("app_grants", grant_row("private", "group", "Finance-Team", "CAN_MANAGE"))
    harness = Harness(registry)

    response = harness.delete_grant("private", ERIN, "group", "finance-team")

    assert response.status_code == 204
    assert harness.registry.audit_rows[-1].detail == {
        "principal": "group:Finance-Team",
        "permission": "CAN_MANAGE",
        "to": "removed",
    }


def test_a_level_change_found_under_another_spelling_records_the_stored_spelling() -> None:
    registry = seeded_registry()
    registry.seed("app_grants", grant_row("private", "group", "Finance-Team", "CAN_USE"))
    harness = Harness(registry)

    harness.put_grant(
        "private", ERIN, {"type": "group", "name": "finance-team", "permission": "CAN_MANAGE"}
    )

    assert harness.registry.audit_rows[-1].detail == {
        "principal": "group:Finance-Team",
        "permission": "CAN_MANAGE",
        "from": "CAN_USE",
    }


# --- the owner rule is checked twice, and each check holds alone ------------------------------


def test_lowering_the_owner_is_refused_with_the_owner_protected_code(harness: Harness) -> None:
    response = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": ALICE, "permission": "CAN_USE"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "owner_protected"


def test_the_schema_check_alone_refuses_lowering_the_owner(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("appmux.admin.grants.check_grant_change", lambda owner, change: None)

    response = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": ALICE, "permission": "CAN_USE"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "owner_protected"


def test_the_policy_check_alone_refuses_lowering_the_owner(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("appmux.admin.grants.refuse_lowering_owner", lambda grant, owner: None)

    response = harness.put_grant(
        "sales", CAROL, {"type": "user", "name": ALICE, "permission": "CAN_USE"}
    )

    assert response.status_code == 400
    assert harness.grants_of("sales")[("user", BOB)] == "CAN_USE"
    assert ("user", ALICE) not in harness.grants_of("sales")


def test_the_schema_check_alone_refuses_removing_the_owner(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("appmux.admin.grants.check_grant_change", lambda owner, change: None)

    response = harness.delete_grant("sales", CAROL, "user", ALICE)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "owner_protected"


def test_the_policy_check_alone_refuses_removing_the_owner(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "appmux.admin.grants.refuse_removing_owner", lambda principal_type, name, owner: None
    )

    response = harness.delete_grant("sales", CAROL, "user", ALICE)

    assert response.status_code == 400


# --- no snapshot, no access, a trailing slash -------------------------------------------------


class NeverLoadedRegistry(StubRegistry):
    """A registry that has never loaded a snapshot: reading it raises RegistryUnavailable."""

    @property
    def snapshot(self) -> RegistrySnapshot:
        raise RegistryUnavailable("not loaded yet")


def test_me_and_apps_are_503_when_no_snapshot_has_ever_loaded() -> None:
    harness = Harness(NeverLoadedRegistry())

    assert harness.call("GET", "/me", BOB).status_code == 503
    assert harness.call("GET", "/apps", BOB).status_code == 503
    assert (
        harness.put_grant(
            "sales", CAROL, {"type": "user", "name": GRACE, "permission": "CAN_USE"}
        ).status_code
        == 503
    )


def test_get_apps_for_an_actor_with_no_access_lists_nothing_of_the_other_apps(
    harness: Harness,
) -> None:
    registry = StubRegistry(clock=lambda: NOW)  # no `everyone` grant anywhere
    for slug, display_name in (("sales", "Sales"), ("private", "Private"), ("ops", "Operations")):
        registry.seed("apps", app_row(slug, owner=ERIN, display_name=display_name))
    registry.seed("app_grants", grant_row("sales", "user", BOB, "CAN_USE"))
    harness = Harness(registry)

    response = harness.call("GET", "/apps", "mallory@example.com")

    assert response.status_code == 200
    assert response.json() == {"items": []}
    for secret in ("sales", "Sales", "ops", "private", "Private", ERIN, BOB, "Operations"):
        assert secret not in response.text


def test_get_apps_and_me_with_a_trailing_slash_are_a_plain_404(harness: Harness) -> None:
    for path in ("/apps/", "/me/"):
        response = harness.call("GET", path, BOB, extra_headers={})

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert "location" not in response.headers


def test_the_authorizer_denies_when_the_answer_is_not_a_plain_true(harness: Harness) -> None:
    for action in ("", "APP.OPEN", "app.open ", "grant.app\n"):
        assert ask(harness, DANA, action, "sales") is False
    assert ask(harness, DANA, "app.open", "../sales") is False


def test_an_app_with_no_config_row_is_listed_as_stopped() -> None:
    registry = StubRegistry(clock=lambda: NOW)
    registry.seed("apps", app_row("bare", owner=ERIN))
    harness = Harness(registry)

    assert harness.listed(ERIN)["bare"]["state"] == "stopped"
