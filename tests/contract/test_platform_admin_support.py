"""Platform admin support: implicit access, Open as a user, ``PUT /platform/admins`` (T152).

The routes run behind the real guard and authorizer on the in-memory registry, with a fake group
lookup. What is pinned:

* a platform admin who is neither owner nor named by a grant changes another team's configuration;
  the change is audited under the admin's own name, and no ``app_grants`` row appears for them
  (the access is implicit, FR-028, rule 17);
* ``POST /apps/{app}/open-mode``: ``as_user`` writes ``app.view_as_user`` and answers the edge
  cookie, switching back writes nothing and answers a deletion; only a platform admin without a
  grant on an OBO-delegated app may choose; a registry that cannot record the choice sets no cookie;
* ``PUT /platform/admins`` replaces the list, audits each entry added or removed with the stated
  reason, and refuses a list with no ``user`` entry (the last-admin guard, FR-026).

Starting data (``edge_harness`` plus one flag): ``sales`` is OBO-delegated (owner alice, bob
``CAN_USE``, carol ``CAN_MANAGE``), ``docs`` is not (owner frank), ``hr`` is OBO-delegated (owner
frank, nobody else); dana is the platform admin, a user entry. All emails are placeholders.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import httpx
import pytest
from edge_harness import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    FRANK,
    NOW,
    VISITOR_TOKEN,
    AdminApi,
    FakeGroups,
    app_row,
    seeded_registry,
)
from stub_registry import StubRegistry

from appmux.admin.api import RouteProvider
from appmux.admin.authorizer import RegistryAuthorizer
from appmux.admin.grants import GrantRoutes
from appmux.admin.identity import IdentityRoutes
from appmux.admin.platform import PlatformRoutes
from appmux.admin.schemas import AdminList
from appmux.authz.actions import Action
from appmux.edge.open_mode import OpenModeCookies, OpenModeTokenPolicy
from appmux.identity.actor import Actor
from appmux.registry.writers import Identity, WriteKind
from appmux.rewrite.response import OPEN_MODE_COOKIE_NAME
from appmux.settings import PlatformAdmin

ERIN: Final = "erin@example.com"
OPEN_MODE_KEY: Final = b"c" * 32
ADMINS_PATH: Final = "/platform/admins"
REASON: Final = "adding the on-call engineer for the quarter"
SECRET_MARKERS: Final = (VISITOR_TOKEN, "Traceback", "SELECT")


def registry_with_obo_apps() -> StubRegistry:
    registry = seeded_registry()
    registry.seed("apps", {**app_row("sales", owner=ALICE), "obo_delegated": True})
    registry.seed("apps", {**app_row("hr", owner=FRANK), "obo_delegated": True})
    registry.seed(
        "app_config",
        {"app_slug": "hr", "desired_state": "running", "updated_by": DANA, "updated_at": NOW},
    )
    return registry


class Support:
    """The admin API with every provider this story touches, over one registry."""

    def __init__(
        self,
        registry: StubRegistry | None = None,
        *,
        bootstrap_admins: Sequence[str] = (),
        wire_cookies: bool = True,
    ) -> None:
        self.cookies = OpenModeCookies(key=OPEN_MODE_KEY)

        def providers(authorizer: RegistryAuthorizer) -> list[RouteProvider]:
            return [
                IdentityRoutes(
                    authorizer, open_mode_cookies=self.cookies if wire_cookies else None
                ),
                GrantRoutes(clock=lambda: NOW),
                PlatformRoutes(bootstrap_admins=bootstrap_admins),
            ]

        self.api = AdminApi(
            registry if registry is not None else registry_with_obo_apps(),
            providers,
            groups=FakeGroups(),
            bootstrap_admins=bootstrap_admins,
        )
        self.registry = self.api.registry
        self.call = self.api.call

    def audit(self) -> list[tuple[str, str, str | None]]:
        """(actor, action, app) of every audit row so far, oldest first."""
        return [(row.actor, row.action, row.app_slug) for row in self.registry.audit_rows]

    def admins(self) -> list[tuple[str, str]]:
        setting = self.registry.snapshot.setting("platform_admins")
        assert setting is not None and isinstance(setting.value, list)
        return [(e.type, e.name) for e in setting.value if isinstance(e, PlatformAdmin)]


@pytest.fixture
def support() -> Support:
    return Support()


def cookie_pair(response: httpx.Response) -> str:
    return response.headers["set-cookie"].split(";")[0]


def visitor(email: str) -> Actor:
    return Actor(email=email)


# --- a platform admin edits another team's configuration ----------------------------------------


def test_a_platform_admin_changes_the_sharing_of_another_teams_app_and_it_is_audited(
    support: Support,
) -> None:
    grants_before = list(support.registry.snapshot.app_grants)

    response = support.call(
        "PUT",
        "/apps/sales/grants",
        DANA,
        json={"type": "group", "name": "data-science", "permission": "CAN_USE"},
    )

    assert response.status_code == 200, response.text
    assert support.audit() == [(DANA, "grant.app", "sales")]
    added = [g for g in support.registry.snapshot.app_grants if g not in grants_before]
    assert [(g.principal_type, g.principal) for g in added] == [("group", "data-science")]


def test_no_grant_row_ever_names_the_admin_for_that_access(support: Support) -> None:
    support.call(
        "PUT",
        "/apps/sales/grants",
        DANA,
        json={"type": "user", "name": ERIN, "permission": "CAN_USE"},
    )

    named = [g for g in support.registry.snapshot.app_grants if g.principal.lower() == DANA]
    assert named == []
    listing = support.call("GET", "/apps", DANA).json()["items"]
    assert {item["slug"]: item["permission"] for item in listing} == {
        "docs": "CAN_MANAGE",
        "hr": "CAN_MANAGE",
        "sales": "CAN_MANAGE",
    }


async def authorizer_allows(authorizer: RegistryAuthorizer, email: str, action: Action) -> bool:
    return await authorizer(visitor(email), action.value, "sales")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "action",
    [
        Action.APP_OPEN,
        Action.APP_START,
        Action.APP_STOP,
        Action.APP_RESTART,
        Action.CONFIG_EDIT,
        Action.RESOURCE_ATTACH,
        Action.RESOURCE_DETACH,
        Action.GRANT_APP,
        Action.RELEASE_ACTIVATE,
        Action.RELEASE_VIEW_SOURCE,
    ],
)
async def test_a_platform_admin_may_manage_an_app_they_have_no_grant_on(
    support: Support, action: Action
) -> None:
    assert await authorizer_allows(support.api.authorizer, DANA, action) is True
    assert await authorizer_allows(support.api.authorizer, BOB, action) is (
        action is Action.APP_OPEN
    )


# --- POST /apps/{app}/open-mode -----------------------------------------------------------------


def test_open_as_a_user_answers_the_edge_cookie_and_writes_the_audit_row(support: Support) -> None:
    response = support.call("POST", "/apps/sales/open-mode", DANA, json={"mode": "as_user"})

    assert response.status_code == 204 and response.content == b""
    name_and_value, *attributes = [p.strip() for p in response.headers["set-cookie"].split(";")]
    assert name_and_value.startswith(f"{OPEN_MODE_COOKIE_NAME}=")
    assert sorted(attributes) == ["HttpOnly", "Path=/", "SameSite=Lax", "Secure"]
    assert support.audit() == [(DANA, "app.view_as_user", "sales")]
    write = support.registry.writes[-1]
    assert write.kinds == () and write.identity is Identity.APPMUX_SP  # nothing but the audit row
    assert VISITOR_TOKEN not in str(dict(response.headers))


def test_the_choice_changes_what_the_launcher_offers_nothing_but_the_cookie(
    support: Support,
) -> None:
    before = support.call("GET", "/apps", DANA).json()
    support.call("POST", "/apps/sales/open-mode", DANA, json={"mode": "as_user"})

    assert support.call("GET", "/apps", DANA).json() == before
    modes = {item["slug"]: item["open_modes"] for item in before["items"]}
    assert modes == {
        "docs": [],  # not OBO-delegated: one Open
        "hr": ["without_token", "as_user"],
        "sales": ["without_token", "as_user"],
    }


def test_the_cookie_the_route_sets_is_the_choice_the_edge_reads(support: Support) -> None:
    response = support.call("POST", "/apps/sales/open-mode", DANA, json={"mode": "as_user"})
    sent = [("Cookie", cookie_pair(response))]

    assert support.cookies.chosen_as_user(sent, visitor(DANA)) == {"sales"}
    assert support.cookies.chosen_as_user(sent, visitor(ERIN)) == set()  # not another person's


@pytest.mark.anyio
async def test_from_the_route_to_the_edge_decision_and_back(support: Support) -> None:
    dana = visitor(DANA)
    policy = OpenModeTokenPolicy(support.api.authorizer, support.cookies)

    async def passes(*pairs: str, slug: str = "sales") -> bool:
        return await policy(dana, slug, [("Cookie", pair) for pair in pairs])

    assert await passes() is False  # the default
    chosen = cookie_pair(
        support.call("POST", "/apps/sales/open-mode", DANA, json={"mode": "as_user"})
    )
    assert await passes(chosen) is True
    assert await passes(chosen, slug="hr") is False  # one app only
    back = support.call(
        "POST",
        "/apps/sales/open-mode",
        DANA,
        json={"mode": "without_token"},
        headers={"Cookie": chosen},
    )
    assert await passes(cookie_pair(back)) is False


def test_a_second_choice_keeps_the_first(support: Support) -> None:
    first = cookie_pair(
        support.call("POST", "/apps/sales/open-mode", DANA, json={"mode": "as_user"})
    )

    second = support.call(
        "POST", "/apps/hr/open-mode", DANA, json={"mode": "as_user"}, headers={"Cookie": first}
    )

    assert support.cookies.chosen_as_user([("Cookie", cookie_pair(second))], visitor(DANA)) == {
        "sales",
        "hr",
    }
    assert support.audit() == [
        (DANA, "app.view_as_user", "sales"),
        (DANA, "app.view_as_user", "hr"),
    ]


def test_switching_back_deletes_the_cookie_and_writes_no_audit_row(support: Support) -> None:
    chosen = cookie_pair(
        support.call("POST", "/apps/sales/open-mode", DANA, json={"mode": "as_user"})
    )
    audited = len(support.audit())

    response = support.call(
        "POST",
        "/apps/sales/open-mode",
        DANA,
        json={"mode": "without_token"},
        headers={"Cookie": chosen},
    )

    assert response.status_code == 204
    assert response.headers["set-cookie"].startswith(f"{OPEN_MODE_COOKIE_NAME}=;")
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert len(support.audit()) == audited


@pytest.mark.parametrize(
    ("email", "slug", "status"),
    [
        (ALICE, "sales", 403),  # the owner is a visitor already
        (CAROL, "sales", 403),  # CAN_MANAGE by a grant
        (BOB, "sales", 403),  # CAN_USE only
        (DANA, "docs", 403),  # not OBO-delegated: one Open
        (BOB, "docs", 404),  # an app a stranger cannot open
        (DANA, "nothing", 404),
    ],
)
def test_only_a_platform_admin_without_a_grant_on_an_obo_app_may_choose(
    support: Support, email: str, slug: str, status: int
) -> None:
    response = support.call("POST", f"/apps/{slug}/open-mode", email, json={"mode": "as_user"})

    assert response.status_code == status
    assert "set-cookie" not in response.headers
    assert support.audit() == []
    assert not any(marker in response.text for marker in SECRET_MARKERS)


def test_an_admin_who_is_named_by_a_grant_has_nothing_to_choose() -> None:
    registry = registry_with_obo_apps()
    registry.seed(
        "app_grants",
        {
            "app_slug": "hr",
            "principal_type": "user",
            "principal": DANA,
            "permission": "CAN_USE",
            "granted_by": FRANK,
            "granted_at": NOW,
        },
    )
    support = Support(registry)

    response = support.call("POST", "/apps/hr/open-mode", DANA, json={"mode": "as_user"})

    assert response.status_code == 403 and "set-cookie" not in response.headers
    modes = {i["slug"]: i["open_modes"] for i in support.call("GET", "/apps", DANA).json()["items"]}
    assert modes["hr"] == [] and modes["sales"] == ["without_token", "as_user"]


@pytest.mark.parametrize(
    "body", [{"mode": "admin"}, {"mode": None}, {}, {"mode": "as_user", "x": 1}]
)
def test_a_body_that_is_not_a_known_mode_is_a_400_that_repeats_nothing(
    support: Support, body: object
) -> None:
    response = support.call("POST", "/apps/sales/open-mode", DANA, json=body)

    assert response.status_code == 400
    assert "admin" not in response.text
    assert "set-cookie" not in response.headers and support.audit() == []


def test_a_choice_needs_the_same_origin_like_every_change(support: Support) -> None:
    response = support.call(
        "POST", "/apps/sales/open-mode", DANA, json={"mode": "as_user"}, same_origin=False
    )

    assert response.status_code == 403 and "set-cookie" not in response.headers
    assert support.audit() == []


def test_a_registry_that_cannot_record_the_choice_sets_no_cookie(support: Support) -> None:
    support.registry.go_down()

    response = support.call("POST", "/apps/sales/open-mode", DANA, json={"mode": "as_user"})

    assert response.status_code == 503 and "set-cookie" not in response.headers
    support.registry.come_back()
    assert support.audit() == []


def test_without_the_edge_cookie_writer_the_route_says_so_and_records_nothing() -> None:
    support = Support(wire_cookies=False)

    response = support.call("POST", "/apps/sales/open-mode", DANA, json={"mode": "as_user"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "open_mode_unavailable"
    assert "set-cookie" not in response.headers and support.audit() == []


# --- PUT /platform/admins -----------------------------------------------------------------------


def put_admins(
    support: Support, email: str | None, admins: object, reason: object = REASON
) -> httpx.Response:
    return support.call("PUT", ADMINS_PATH, email, json={"admins": admins, "reason": reason})


def user(email: str) -> dict[str, str]:
    return {"type": "user", "name": email}


def group(name: str) -> dict[str, str]:
    return {"type": "group", "name": name}


def test_adding_an_admin_replaces_the_list_and_audits_each_entry_added(support: Support) -> None:
    response = put_admins(support, DANA, [user(DANA), user(ERIN), group("Data Platform")])

    assert response.status_code == 200, response.text
    assert AdminList.model_validate_json(response.text).model_dump(mode="json") == {
        "items": [
            {"type": "user", "name": DANA},
            {"type": "user", "name": ERIN},
            {"type": "group", "name": "Data Platform"},
        ]
    }
    assert support.admins() == [("user", DANA), ("user", ERIN), ("group", "Data Platform")]
    rows = support.registry.audit_rows
    assert [(r.actor, r.action, r.detail, r.reason) for r in rows] == [
        (DANA, "platform_admin.add", {"principal": f"user:{ERIN}"}, REASON),
        (DANA, "platform_admin.add", {"principal": "group:Data Platform"}, REASON),
    ]
    write = support.registry.writes[-1]
    assert write.kinds == (WriteKind.PLATFORM_SETTING_UPDATE,)
    # Platform data is written as the admin's own role, with their token.
    assert write.identity is Identity.PLATFORM_ADMIN and write.admin_token is not None


def test_removing_an_entry_audits_it_and_it_stops_being_an_admin(support: Support) -> None:
    put_admins(support, DANA, [user(DANA), user(ERIN)])

    response = put_admins(support, DANA, [user(ERIN)], reason="dana leaves the platform team")

    assert response.status_code == 200
    assert support.admins() == [("user", ERIN)]
    last = support.registry.audit_rows[-1]
    assert (last.actor, last.action, last.detail) == (
        DANA,
        "platform_admin.remove",
        {"principal": f"user:{DANA}"},
    )
    assert last.reason == "dana leaves the platform team"
    assert support.call("GET", "/me", DANA).json()["is_platform_admin"] is False
    assert support.call("GET", "/me", ERIN).json()["is_platform_admin"] is True


def test_a_replacement_audits_the_removed_and_the_added_entries_one_by_one(
    support: Support,
) -> None:
    put_admins(support, DANA, [user(DANA), group("old-team")])
    before = len(support.registry.audit_rows)

    put_admins(support, DANA, [user(DANA), user(ERIN)], reason="the team changed")

    new_rows = support.registry.audit_rows[before:]
    assert sorted((r.action, r.detail["principal"]) for r in new_rows) == [
        ("platform_admin.add", f"user:{ERIN}"),
        ("platform_admin.remove", "group:old-team"),
    ]
    assert {r.reason for r in new_rows} == {"the team changed"}


def test_the_answer_keeps_the_order_that_was_sent(support: Support) -> None:
    response = put_admins(support, DANA, [user(ERIN), group("zeta"), user(DANA)])

    assert [item["name"] for item in response.json()["items"]] == [ERIN, "zeta", DANA]


@pytest.mark.parametrize(
    "admins",
    [
        [],
        [group("data-platform")],
        [group("data-platform"), group("ops")],
    ],
)
def test_a_list_that_would_leave_no_user_entry_is_refused_and_changes_nothing(
    support: Support, admins: list[dict[str, str]]
) -> None:
    before = support.admins()

    response = put_admins(support, DANA, admins)

    assert response.status_code == 400
    # The body model refuses it (rule 2); the generic 400 names the body, never the entries.
    assert response.json()["error"]["code"] == "bad_request"
    assert support.admins() == before
    assert support.audit() == [] and support.registry.writes == ()


def test_a_list_naming_one_entry_twice_is_refused(support: Support) -> None:
    response = put_admins(support, DANA, [user(DANA), user(DANA)])

    assert response.status_code == 400 and support.audit() == []


@pytest.mark.parametrize("reason", ["", "   ", None, "password=hunter2"])
def test_a_missing_or_credential_shaped_reason_is_refused_without_echo(
    support: Support, reason: object
) -> None:
    response = put_admins(support, DANA, [user(DANA), user(ERIN)], reason=reason)

    assert response.status_code == 400
    assert "hunter2" not in response.text
    assert support.admins() == [("user", DANA)] and support.audit() == []


def test_the_same_list_is_a_no_op_with_no_audit_row(support: Support) -> None:
    response = put_admins(support, DANA, [user(DANA)])

    assert response.status_code == 200
    assert response.json() == {"items": [{"type": "user", "name": DANA}]}
    assert support.audit() == [] and support.registry.writes == ()


@pytest.mark.parametrize("email", [ALICE, BOB, CAROL, FRANK, ERIN])
def test_only_a_platform_admin_changes_the_list(support: Support, email: str) -> None:
    response = put_admins(support, email, [user(email)])

    assert response.status_code == 403
    assert support.admins() == [("user", DANA)] and support.audit() == []


def test_a_person_who_may_not_change_the_list_is_refused_before_the_body_is_read(
    support: Support,
) -> None:
    response = support.call("PUT", ADMINS_PATH, BOB, json={"admins": "not a list"})

    assert response.status_code == 403


def test_the_change_needs_the_same_origin_like_every_change(support: Support) -> None:
    response = support.call(
        "PUT",
        ADMINS_PATH,
        DANA,
        json={"admins": [user(DANA), user(ERIN)], "reason": REASON},
        same_origin=False,
    )

    assert response.status_code == 403 and support.audit() == []


def test_an_admin_whose_role_cannot_write_platform_data_changes_nothing() -> None:
    registry = seeded_registry(platform_writer_tokens={"tok-test-another-admin"})
    support = Support(registry)

    response = put_admins(support, DANA, [user(DANA), user(ERIN)])

    assert response.status_code == 403
    assert support.admins() == [("user", DANA)] and support.audit() == []


def test_while_the_setting_is_unset_the_bootstrap_admins_are_the_old_list() -> None:
    registry = StubRegistry(clock=lambda: NOW)
    for row in (app_row("sales", owner=ALICE),):
        registry.seed("apps", row)
    support = Support(registry, bootstrap_admins=(DANA,))

    response = put_admins(support, DANA, [user(DANA), user(ERIN)])

    assert response.status_code == 200, response.text
    assert support.admins() == [("user", DANA), ("user", ERIN)]
    write = support.registry.writes[-1]
    assert write.kinds == (WriteKind.PLATFORM_SETTING_INSERT,)
    # dana was already an admin through the bootstrap list: only erin is new.
    assert [(r.action, r.detail["principal"]) for r in support.registry.audit_rows] == [
        ("platform_admin.add", f"user:{ERIN}")
    ]


def test_dropping_a_bootstrap_admin_is_audited_as_a_removal() -> None:
    registry = StubRegistry(clock=lambda: NOW)
    registry.seed("apps", app_row("sales", owner=ALICE))
    support = Support(registry, bootstrap_admins=(DANA, ERIN))

    response = put_admins(support, DANA, [user(DANA)], reason="erin moved to another team")

    assert response.status_code == 200
    assert support.admins() == [("user", DANA)]
    assert [(r.action, r.detail["principal"]) for r in support.registry.audit_rows] == [
        ("platform_admin.remove", f"user:{ERIN}")
    ]
