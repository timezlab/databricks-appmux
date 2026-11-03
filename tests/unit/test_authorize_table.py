"""authorize() as a table (design 9.2, ADR 0010, data model rules 1, 2 and 17).

One row per action, one column per kind of actor. A row lists the kinds that are allowed; every
other kind must be denied. ``test_every_action_has_a_row`` fails when an ``Action`` is missing, so a
new action cannot be added without deciding who may do it. The expected values below were written
by hand from the table of design 9.2, not derived from ``ACTION_REQUIREMENTS``.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

import pytest

from appmux.authz.actions import ACTION_REQUIREMENTS, Action, Requirement
from appmux.authz.data import effective_platform_admins
from appmux.authz.policy import (
    AdminChange,
    AdminEntry,
    AppAccess,
    AuthzData,
    Decision,
    Grant,
    GrantChange,
    Permission,
    Reason,
    authorize,
    check_admin_change,
    check_grant_change,
)
from appmux.identity.actor import Actor
from appmux.settings import PlatformAdmin

SALES = "sales"
DOCS = "docs"
# An App's service principal reaches appmux with its application id as X-Forwarded-Email.
SALES_BOT = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab"


class Kind(StrEnum):
    NO_GRANT = "no_grant"
    CAN_USE = "can_use"
    CAN_MANAGE = "can_manage"
    OWNER = "owner"
    EVERYONE = "everyone"
    GROUP_MEMBER = "group_member"
    GROUP_MANAGER = "group_manager"
    GROUP_UNKNOWN = "group_unknown"
    PLATFORM_ADMIN = "platform_admin"
    SERVICE_PRINCIPAL = "service_principal"


def make_data(*, user_authorization: bool = True) -> AuthzData:
    sales = AppAccess(
        slug=SALES,
        owner="alice@example.com",
        obo_delegated=True,
        grants=(
            Grant("user", "use@example.com", Permission.CAN_USE),
            Grant("user", "carol@example.com", Permission.CAN_MANAGE),
            Grant("group", "sales-team", Permission.CAN_USE),
            Grant("group", "sales-admins", Permission.CAN_MANAGE),
            Grant("service_principal", SALES_BOT, Permission.CAN_USE),
        ),
    )
    docs = AppAccess(
        slug=DOCS,
        owner="frank@example.com",
        grants=(Grant("everyone", "*", Permission.CAN_USE),),
    )
    return AuthzData(
        apps={SALES: sales, DOCS: docs},
        platform_admins=(
            AdminEntry("user", "dana@example.com"),
            AdminEntry("group", "data-platform"),
        ),
        user_authorization=user_authorization,
    )


@dataclass(frozen=True)
class Who:
    email: str
    app: str
    groups: frozenset[str] | None


WHO: dict[Kind, Who] = {
    Kind.NO_GRANT: Who("mallory@example.com", SALES, frozenset()),
    Kind.CAN_USE: Who("use@example.com", SALES, frozenset()),
    Kind.CAN_MANAGE: Who("carol@example.com", SALES, frozenset()),
    Kind.OWNER: Who("alice@example.com", SALES, frozenset()),
    Kind.EVERYONE: Who("grace@example.com", DOCS, frozenset()),
    Kind.GROUP_MEMBER: Who("bob@example.com", SALES, frozenset({"sales-team"})),
    Kind.GROUP_MANAGER: Who("heidi@example.com", SALES, frozenset({"sales-admins"})),
    Kind.GROUP_UNKNOWN: Who("bob@example.com", SALES, None),
    Kind.PLATFORM_ADMIN: Who("dana@example.com", SALES, frozenset()),
    # The group lookup is the visitor's own SCIM `Me`; a service principal has no group to match.
    Kind.SERVICE_PRINCIPAL: Who(SALES_BOT, SALES, None),
}

NOBODY: frozenset[Kind] = frozenset()
EVERY_KIND: frozenset[Kind] = frozenset(Kind)
# Kinds that hold at least CAN_USE on their app.
CAN_OPEN = frozenset(
    {
        Kind.CAN_USE,
        Kind.CAN_MANAGE,
        Kind.OWNER,
        Kind.EVERYONE,
        Kind.GROUP_MEMBER,
        Kind.GROUP_MANAGER,
        Kind.PLATFORM_ADMIN,
        Kind.SERVICE_PRINCIPAL,
    }
)
# Kinds that hold CAN_MANAGE on their app: a CAN_MANAGE user or group grant, the owner, and a
# platform admin implicitly (rule 17).
CAN_MANAGE = frozenset({Kind.CAN_MANAGE, Kind.GROUP_MANAGER, Kind.OWNER, Kind.PLATFORM_ADMIN})
PLATFORM_ONLY = frozenset({Kind.PLATFORM_ADMIN})

# Design 9.2, row by row. The app is the actor's own app from WHO.
ROWS: dict[Action, frozenset[Kind]] = {
    Action.APP_OPEN: CAN_OPEN,
    Action.APP_VIEW: CAN_MANAGE,
    Action.APP_VIEW_AS_USER: PLATFORM_ONLY,  # `sales` is OBO and Dana has no grant on it
    Action.APP_VIEW_LOGS: CAN_MANAGE,
    Action.APP_VIEW_ENVIRONMENT: CAN_MANAGE,
    Action.APP_UPDATE: CAN_MANAGE,
    Action.APP_RESTART: CAN_MANAGE,
    Action.APP_STOP: CAN_MANAGE,
    Action.APP_START: CAN_MANAGE,
    Action.APP_REGISTER: PLATFORM_ONLY,
    Action.APP_DELETE: PLATFORM_ONLY,
    Action.APP_RENAME: PLATFORM_ONLY,
    Action.APP_SET_OWNER: PLATFORM_ONLY,
    Action.APP_EDIT_PLATFORM_FIELDS: PLATFORM_ONLY,
    Action.RELEASE_ACTIVATE: CAN_MANAGE,
    Action.RELEASE_LABEL: CAN_MANAGE,
    Action.RELEASE_TAG: CAN_MANAGE,
    Action.RELEASE_UNTAG: CAN_MANAGE,
    Action.RELEASE_LOCK: CAN_MANAGE,
    Action.RELEASE_UNLOCK: CAN_MANAGE,
    Action.RELEASE_DELETE: CAN_MANAGE,
    Action.RELEASE_VIEW_SOURCE: CAN_MANAGE,
    Action.CONFIG_EDIT: CAN_MANAGE,
    Action.RESOURCE_ATTACH: CAN_MANAGE,
    Action.RESOURCE_DETACH: CAN_MANAGE,
    Action.GRANT_APP: CAN_MANAGE,
    Action.REQUEST_CREATE: CAN_MANAGE,  # with an app named; see test_register_requests
    Action.REQUEST_WITHDRAW: CAN_MANAGE,
    Action.REQUEST_DECIDE: PLATFORM_ONLY,
    Action.DELEGATION_GRANT: PLATFORM_ONLY,
    Action.DELEGATION_REVOKE: PLATFORM_ONLY,
    Action.PLATFORM_VIEW: PLATFORM_ONLY,
    Action.PLATFORM_ADMIN_ADD: PLATFORM_ONLY,
    Action.PLATFORM_ADMIN_REMOVE: PLATFORM_ONLY,
    Action.SETTINGS_EDIT: PLATFORM_ONLY,
    Action.EGRESS_EDIT: PLATFORM_ONLY,
    # Anyone who manages some app, like the suggestions: a child's page script runs with the
    # visitor's identity, so a visitor who manages nothing gets no listing of their folders (T194).
    Action.WORKSPACE_BROWSE: CAN_MANAGE,
    # Anyone who can attach a resource somewhere: CAN_MANAGE on at least one app (T195).
    Action.RESOURCE_SUGGEST: CAN_MANAGE,
    # The admin UI overhaul (contracts/admin-api.md, addendum): CAN_MANAGE on any app will do.
    Action.PRINCIPAL_SEARCH: CAN_MANAGE,
}


def decide(kind: Kind, action: Action | str, *, app: str | None = None) -> Decision:
    who = WHO[kind]
    return authorize(
        Actor(email=who.email),
        action,
        who.app if app is None else app,
        make_data(),
        groups=who.groups,
    )


def test_every_action_has_a_row() -> None:
    assert set(ROWS) == set(Action), "an Action has no row in the authorize table"
    assert set(ACTION_REQUIREMENTS) == set(Action), "an Action has no requirement"


def test_every_requirement_is_used_by_some_action() -> None:
    # A member no action maps to is a branch of authorize() that nothing can reach or test.
    assert set(ACTION_REQUIREMENTS.values()) == set(Requirement)


def test_the_requirement_table_cannot_be_changed_by_an_importer() -> None:
    table = cast("MutableMapping[Action, Requirement]", ACTION_REQUIREMENTS)
    before = dict(ACTION_REQUIREMENTS)
    try:
        with pytest.raises(TypeError):
            table[Action.SETTINGS_EDIT] = Requirement.APP_CAN_USE
        with pytest.raises(TypeError):
            del table[Action.SETTINGS_EDIT]
    finally:
        # If the table were a plain dict the lines above changed it for every later test.
        if isinstance(ACTION_REQUIREMENTS, dict):
            ACTION_REQUIREMENTS.clear()
            ACTION_REQUIREMENTS.update(before)
    assert dict(ACTION_REQUIREMENTS) == before


@pytest.mark.parametrize("kind", list(Kind), ids=lambda kind: kind.value)
@pytest.mark.parametrize("action", list(Action), ids=lambda action: action.value)
def test_authorize_table(action: Action, kind: Kind) -> None:
    decision = decide(kind, action)
    assert decision.allowed is (kind in ROWS[action]), (action, kind, decision)


def test_deny_by_default_for_an_unknown_action() -> None:
    for kind in Kind:
        decision = decide(kind, "app.teleport")
        assert decision == Decision(False, Reason.UNKNOWN_ACTION, None)


def test_unknown_or_missing_app_is_denied_for_app_scoped_actions() -> None:
    for action in (Action.APP_OPEN, Action.APP_VIEW, Action.RELEASE_ACTIVATE, Action.GRANT_APP):
        assert decide(Kind.PLATFORM_ADMIN, action, app="nope").reason is Reason.UNKNOWN_APP
    admin = WHO[Kind.PLATFORM_ADMIN]
    result = authorize(
        Actor(email=admin.email), Action.APP_OPEN, None, make_data(), groups=admin.groups
    )
    assert result.reason is Reason.UNKNOWN_APP


@pytest.mark.parametrize("action", [Action.REQUEST_CREATE, Action.REQUEST_WITHDRAW])
def test_a_request_about_an_unknown_app_is_denied_even_to_a_platform_admin(action: Action) -> None:
    # A request of any kind but `register` names an app; "no app" is any visitor, a slug that is
    # not in the snapshot is not.
    for kind in (Kind.PLATFORM_ADMIN, Kind.OWNER, Kind.NO_GRANT):
        assert decide(kind, action, app="nope") == Decision(False, Reason.UNKNOWN_APP, None)


@pytest.mark.parametrize(
    "action",
    [
        action
        for action, needs in ACTION_REQUIREMENTS.items()
        if needs is Requirement.PLATFORM_CAN_MANAGE
    ],
    ids=lambda action: action.value,
)
def test_platform_actions_decide_on_the_platform_admin_not_on_the_app(action: Action) -> None:
    # These actions never read the app, so a slug that does not exist changes nothing: the admin
    # API answers 404 for it after authorization, and a non-admin is refused whatever the slug.
    assert decide(Kind.PLATFORM_ADMIN, action, app="nope") == Decision(
        True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE
    )
    assert decide(Kind.OWNER, action, app="nope") == Decision(
        False, Reason.NOT_PLATFORM_ADMIN, None
    )


def test_workspace_browse_reads_every_app_not_the_slug_it_is_given() -> None:
    for kind in (Kind.CAN_MANAGE, Kind.GROUP_MANAGER, Kind.OWNER, Kind.PLATFORM_ADMIN):
        assert decide(kind, Action.WORKSPACE_BROWSE, app="nope").allowed, kind


def test_workspace_browse_denies_a_visitor_who_manages_no_app() -> None:
    for kind in (Kind.NO_GRANT, Kind.CAN_USE, Kind.EVERYONE, Kind.GROUP_MEMBER):
        for app in (None, SALES, "nope"):
            assert decide(kind, Action.WORKSPACE_BROWSE, app=app) == Decision(
                False, Reason.NO_MANAGED_APP, None
            ), (kind, app)


def test_resource_suggest_reads_every_app_not_the_slug_it_is_given() -> None:
    # Frank owns `docs` and nothing on `sales`: he manages one app, which is enough.
    result = authorize(
        Actor(email="frank@example.com"),
        Action.RESOURCE_SUGGEST,
        None,
        make_data(),
        groups=frozenset(),
    )
    assert result == Decision(True, Reason.OWNER, Permission.CAN_MANAGE)
    for kind in (Kind.CAN_MANAGE, Kind.GROUP_MANAGER, Kind.OWNER, Kind.PLATFORM_ADMIN):
        assert decide(kind, Action.RESOURCE_SUGGEST, app="nope").allowed, kind


def test_resource_suggest_denies_a_visitor_who_manages_no_app() -> None:
    for kind in (Kind.NO_GRANT, Kind.CAN_USE, Kind.EVERYONE, Kind.GROUP_MEMBER):
        assert decide(kind, Action.RESOURCE_SUGGEST) == Decision(
            False, Reason.NO_MANAGED_APP, None
        ), kind


def test_resource_suggest_does_not_count_a_group_grant_while_groups_are_unknown() -> None:
    who = WHO[Kind.GROUP_MANAGER]
    for groups in (None, frozenset()):
        result = authorize(
            Actor(email=who.email), Action.RESOURCE_SUGGEST, None, make_data(), groups=groups
        )
        assert result == Decision(False, Reason.NO_MANAGED_APP, None), groups


def test_resource_suggest_ignores_group_grants_while_user_authorization_is_off() -> None:
    who = WHO[Kind.GROUP_MANAGER]
    off = make_data(user_authorization=False)
    result = authorize(
        Actor(email=who.email), Action.RESOURCE_SUGGEST, None, off, groups=who.groups
    )
    assert result.allowed is False
    owner = authorize(
        Actor(email="alice@example.com"), Action.RESOURCE_SUGGEST, None, off, groups=None
    )
    assert owner.allowed is True


@pytest.mark.parametrize("app", [None, SALES, DOCS, "nope"])
def test_principal_search_needs_can_manage_on_some_app_whatever_app_is_passed(
    app: str | None,
) -> None:
    # The route names no app: the Sharing form of any app the person manages asks it.
    assert decide(Kind.PLATFORM_ADMIN, Action.PRINCIPAL_SEARCH, app=app) == Decision(
        True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE
    )
    assert decide(Kind.OWNER, Action.PRINCIPAL_SEARCH, app=app) == Decision(
        True, Reason.OWNER, Permission.CAN_MANAGE
    )
    assert decide(Kind.GROUP_MANAGER, Action.PRINCIPAL_SEARCH, app=app) == Decision(
        True, Reason.GROUP_GRANT, Permission.CAN_MANAGE
    )
    for kind in (Kind.CAN_USE, Kind.EVERYONE, Kind.NO_GRANT, Kind.SERVICE_PRINCIPAL):
        assert decide(kind, Action.PRINCIPAL_SEARCH, app=app) == Decision(
            False, Reason.NO_MANAGED_APP, None
        )


def test_principal_search_counts_a_manager_of_any_one_app() -> None:
    # Carol manages `sales` only, Frank owns `docs` only (named in upper case): one app will do.
    carol = WHO[Kind.CAN_MANAGE]
    frank = Actor(email="FRANK@example.com")

    for actor in (Actor(email=carol.email), frank):
        decision = authorize(actor, Action.PRINCIPAL_SEARCH, None, make_data(), groups=None)
        assert decision.allowed is True


def test_an_actor_without_an_email_is_denied() -> None:
    result = authorize(Actor(email="  "), Action.REQUEST_CREATE, None, make_data(), groups=None)
    assert result == Decision(False, Reason.NO_IDENTITY, None)


@pytest.mark.parametrize("kind", list(Kind), ids=lambda kind: kind.value)
@pytest.mark.parametrize("action", [Action.REQUEST_CREATE, Action.REQUEST_WITHDRAW])
def test_register_requests_need_no_app_permission(action: Action, kind: Kind) -> None:
    who = WHO[kind]
    result = authorize(Actor(email=who.email), action, None, make_data(), groups=who.groups)
    assert result == Decision(True, Reason.ANY_VISITOR, None)


# --- Fail closed on an unknown group lookup -------------------------------------------------


def test_unknown_groups_deny_a_group_grant_that_known_groups_allow() -> None:
    assert decide(Kind.GROUP_MEMBER, Action.APP_OPEN) == Decision(
        True, Reason.GROUP_GRANT, Permission.CAN_USE
    )
    assert decide(Kind.GROUP_UNKNOWN, Action.APP_OPEN) == Decision(False, Reason.NO_ACCESS, None)


def test_unknown_groups_still_match_user_owner_and_everyone_grants() -> None:
    data = make_data()
    for email, app, reason in (
        ("use@example.com", SALES, Reason.USER_GRANT),
        ("alice@example.com", SALES, Reason.OWNER),
        ("grace@example.com", DOCS, Reason.EVERYONE_GRANT),
    ):
        result = authorize(Actor(email=email), Action.APP_OPEN, app, data, groups=None)
        assert result.allowed
        assert result.reason is reason


def test_unknown_groups_do_not_match_a_platform_admin_group_entry() -> None:
    data = make_data()
    member = frozenset({"data-platform"})
    allowed = authorize(
        Actor(email="erin@example.com"), Action.SETTINGS_EDIT, None, data, groups=member
    )
    assert allowed == Decision(True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE)
    unknown = authorize(
        Actor(email="erin@example.com"), Action.SETTINGS_EDIT, None, data, groups=None
    )
    assert not unknown.allowed


def test_an_empty_group_set_matches_no_group_grant() -> None:
    assert not decide(Kind.NO_GRANT, Action.APP_OPEN).allowed


# --- User authorization off: only user and everyone grants match -----------------------------


def test_user_authorization_off_ignores_group_grants_even_with_groups_given() -> None:
    data = make_data(user_authorization=False)
    bob = authorize(
        Actor(email="bob@example.com"),
        Action.APP_OPEN,
        SALES,
        data,
        groups=frozenset({"sales-team"}),
    )
    assert not bob.allowed


def test_user_authorization_off_ignores_platform_admin_group_entries() -> None:
    data = make_data(user_authorization=False)
    erin = authorize(
        Actor(email="erin@example.com"),
        Action.SETTINGS_EDIT,
        None,
        data,
        groups=frozenset({"data-platform"}),
    )
    assert not erin.allowed


def test_user_authorization_off_keeps_user_everyone_owner_and_admin_user_entries() -> None:
    data = make_data(user_authorization=False)
    groups = frozenset({"sales-team"})
    for email, app, action in (
        ("use@example.com", SALES, Action.APP_OPEN),
        ("grace@example.com", DOCS, Action.APP_OPEN),
        ("alice@example.com", SALES, Action.APP_VIEW),
        ("dana@example.com", SALES, Action.SETTINGS_EDIT),
    ):
        assert authorize(Actor(email=email), action, app, data, groups=groups).allowed


# --- Levels ----------------------------------------------------------------------------------


def test_the_highest_matching_grant_wins() -> None:
    app = AppAccess(
        slug="x",
        owner="o@example.com",
        grants=(
            Grant("group", "team", Permission.CAN_USE),
            Grant("user", "carol@example.com", Permission.CAN_MANAGE),
        ),
    )
    data = AuthzData(apps={"x": app}, platform_admins=(), user_authorization=True)
    result = authorize(
        Actor(email="carol@example.com"),
        Action.RELEASE_ACTIVATE,
        "x",
        data,
        groups=frozenset({"team"}),
    )
    assert result == Decision(True, Reason.USER_GRANT, Permission.CAN_MANAGE)


def test_a_group_grant_at_can_manage_gives_the_whole_manage_level() -> None:
    expected = Decision(True, Reason.GROUP_GRANT, Permission.CAN_MANAGE)
    for action in (Action.APP_UPDATE, Action.RELEASE_ACTIVATE, Action.GRANT_APP, Action.APP_OPEN):
        assert decide(Kind.GROUP_MANAGER, action) == expected


def test_a_group_grant_at_can_use_stays_below_the_manage_level() -> None:
    assert decide(Kind.GROUP_MEMBER, Action.RELEASE_ACTIVATE) == Decision(
        False, Reason.INSUFFICIENT_LEVEL, Permission.CAN_USE
    )


def test_a_group_manage_grant_is_lost_when_the_group_lookup_failed() -> None:
    who = WHO[Kind.GROUP_MANAGER]
    result = authorize(
        Actor(email=who.email), Action.RELEASE_ACTIVATE, SALES, make_data(), groups=None
    )
    assert result == Decision(False, Reason.NO_ACCESS, None)


def test_can_use_is_denied_a_manage_action_and_reports_the_level_held() -> None:
    assert decide(Kind.CAN_USE, Action.RELEASE_ACTIVATE) == Decision(
        False, Reason.INSUFFICIENT_LEVEL, Permission.CAN_USE
    )


def test_no_grant_is_denied_with_no_level() -> None:
    assert decide(Kind.NO_GRANT, Action.APP_OPEN) == Decision(False, Reason.NO_ACCESS, None)


def test_everyone_is_can_use_at_most_even_if_the_stored_row_says_more() -> None:
    # The shared-SP token can write app_grants (data model section 6); a forged row must not widen.
    app = AppAccess(
        slug="x",
        owner="o@example.com",
        grants=(Grant("everyone", "*", Permission.CAN_MANAGE),),
    )
    data = AuthzData(apps={"x": app}, platform_admins=(), user_authorization=True)
    actor = Actor(email="anyone@example.com")
    assert authorize(actor, Action.APP_OPEN, "x", data, groups=frozenset()).allowed
    assert not authorize(actor, Action.RELEASE_ACTIVATE, "x", data, groups=frozenset()).allowed


# --- A service principal's grant (ADR 0020) ---------------------------------------------------


def test_a_service_principal_grant_matches_the_forwarded_application_id_in_any_case() -> None:
    data = make_data()
    for identity in (SALES_BOT, SALES_BOT.upper()):
        result = authorize(Actor(email=identity), Action.APP_OPEN, SALES, data, groups=None)
        assert result == Decision(True, Reason.SERVICE_PRINCIPAL_GRANT, Permission.CAN_USE)


def test_a_service_principal_grant_does_not_match_another_application_id() -> None:
    other = "0e9d8c7b-6a5f-4e3d-9c2b-1a0f9e8d7c6b"
    result = authorize(Actor(email=other), Action.APP_OPEN, SALES, make_data(), groups=None)
    assert result == Decision(False, Reason.NO_ACCESS, None)


def test_a_service_principal_grant_needs_no_groups_and_no_user_authorization() -> None:
    data = make_data(user_authorization=False)
    result = authorize(Actor(email=SALES_BOT), Action.APP_OPEN, SALES, data, groups=None)
    assert result.allowed


def test_a_service_principal_is_can_use_at_most_even_if_the_stored_row_says_more() -> None:
    # As for everyone: the shared-SP token can write app_grants, so a forged row must not widen.
    app = AppAccess(
        slug="x",
        owner="o@example.com",
        grants=(Grant("service_principal", SALES_BOT, Permission.CAN_MANAGE),),
    )
    data = AuthzData(apps={"x": app}, platform_admins=(), user_authorization=True)
    actor = Actor(email=SALES_BOT)
    assert authorize(actor, Action.APP_OPEN, "x", data, groups=None) == Decision(
        True, Reason.SERVICE_PRINCIPAL_GRANT, Permission.CAN_USE
    )
    assert not authorize(actor, Action.APP_VIEW, "x", data, groups=None).allowed


def test_a_service_principal_cannot_be_granted_can_manage() -> None:
    manage = GrantChange("service_principal", SALES_BOT, Permission.CAN_MANAGE)
    assert check_grant_change("alice@example.com", manage) is Reason.SERVICE_PRINCIPAL_CAN_USE_ONLY
    for allowed in (
        GrantChange("service_principal", SALES_BOT, Permission.CAN_USE),
        GrantChange("service_principal", SALES_BOT, None),
    ):
        assert check_grant_change("alice@example.com", allowed) is None
    result = authorize(
        Actor(email="alice@example.com"),
        Action.GRANT_APP,
        SALES,
        make_data(),
        groups=frozenset(),
        change=manage,
    )
    assert result == Decision(False, Reason.SERVICE_PRINCIPAL_CAN_USE_ONLY, Permission.CAN_MANAGE)


def test_matching_is_case_insensitive_for_emails_and_group_names() -> None:
    app = AppAccess(
        slug="x",
        owner="Owner@Example.com",
        grants=(
            Grant("user", "Use@Example.com", Permission.CAN_USE),
            Grant("group", "Sales-Team", Permission.CAN_USE),
        ),
    )
    data = AuthzData(apps={"x": app}, platform_admins=(), user_authorization=True)
    for email in ("owner@example.com", "USE@example.com"):
        assert authorize(Actor(email=email), Action.APP_OPEN, "x", data, groups=frozenset()).allowed
    in_group = authorize(
        Actor(email="bob@example.com"),
        Action.APP_OPEN,
        "x",
        data,
        groups=frozenset({"sales-team"}),
    )
    assert in_group.allowed


def test_the_groups_argument_is_matched_in_lower_case_too() -> None:
    # identity/groups.py already lower-cases; authorize folds again so a caller that forgets
    # cannot turn a membership into a miss (which would be a silent lockout, not a leak).
    result = authorize(
        Actor(email="bob@example.com"),
        Action.APP_OPEN,
        SALES,
        make_data(),
        groups=frozenset({"Sales-Team"}),
    )
    assert result == Decision(True, Reason.GROUP_GRANT, Permission.CAN_USE)
    admin = authorize(
        Actor(email="erin@example.com"),
        Action.SETTINGS_EDIT,
        None,
        make_data(),
        groups=frozenset({"DATA-PLATFORM"}),
    )
    assert admin.allowed


# --- Rule 17: implicit platform-admin CAN_MANAGE --------------------------------------------


def test_platform_admin_holds_can_manage_on_every_app_without_a_grant_row() -> None:
    data = make_data()
    for slug, app in data.apps.items():
        assert all(grant.name != "dana@example.com" for grant in app.grants)
        result = authorize(
            Actor(email="dana@example.com"), Action.RELEASE_VIEW_SOURCE, slug, data, groups=None
        )
        assert result == Decision(True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE)


def test_platform_admin_through_a_group_entry_holds_can_manage() -> None:
    result = authorize(
        Actor(email="erin@example.com"),
        Action.CONFIG_EDIT,
        SALES,
        make_data(),
        groups=frozenset({"data-platform"}),
    )
    assert result == Decision(True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE)


def test_a_platform_admin_group_entry_stored_with_capitals_is_matched_by_a_lower_case_group() -> (
    None
):
    # The setting keeps a group's case as typed ("Data-Platform"); the actor's groups from the
    # identity module are lower case. Only the fold in is_platform_admin joins the two.
    stored = effective_platform_admins([PlatformAdmin(type="group", name="Data-Platform")], [])
    assert stored == (AdminEntry("group", "Data-Platform"),)
    data = AuthzData(
        apps={SALES: make_data().apps[SALES]}, platform_admins=stored, user_authorization=True
    )
    member = frozenset({"data-platform"})
    result = authorize(
        Actor(email="erin@example.com"), Action.RELEASE_ACTIVATE, SALES, data, groups=member
    )
    assert result == Decision(True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE)
    elsewhere = authorize(
        Actor(email="erin@example.com"),
        Action.SETTINGS_EDIT,
        None,
        data,
        groups=frozenset({"other-group"}),
    )
    assert not elsewhere.allowed


def test_a_platform_admin_listed_by_email_in_any_case_is_matched() -> None:
    result = authorize(
        Actor(email="DANA@example.com"), Action.EGRESS_EDIT, None, make_data(), groups=None
    )
    assert result.allowed


def test_only_a_platform_admin_may_do_platform_actions_even_the_owner_or_app_admin() -> None:
    for kind in (Kind.OWNER, Kind.CAN_MANAGE):
        assert not decide(kind, Action.APP_DELETE).allowed
        assert not decide(kind, Action.APP_SET_OWNER).allowed
        assert not decide(kind, Action.REQUEST_DECIDE).allowed


# --- app.view_as_user ------------------------------------------------------------------------


def test_view_as_user_needs_an_obo_app_and_no_grant() -> None:
    data = make_data()
    dana = Actor(email="dana@example.com")
    assert authorize(dana, Action.APP_VIEW_AS_USER, SALES, data, groups=frozenset()).allowed
    not_obo = authorize(dana, Action.APP_VIEW_AS_USER, DOCS, data, groups=frozenset())
    assert not_obo == Decision(False, Reason.NOT_OBO_APP, Permission.CAN_MANAGE)


@pytest.mark.parametrize(
    "grant",
    [
        Grant("user", "dana@example.com", Permission.CAN_USE),
        Grant("group", "data-platform", Permission.CAN_USE),
        Grant("everyone", "*", Permission.CAN_USE),
    ],
    ids=["user", "group", "everyone"],
)
def test_view_as_user_is_refused_when_the_admin_is_named_by_a_grant(grant: Grant) -> None:
    app = AppAccess(slug="x", owner="o@example.com", obo_delegated=True, grants=(grant,))
    data = AuthzData(
        apps={"x": app},
        platform_admins=(AdminEntry("user", "dana@example.com"),),
        user_authorization=True,
    )
    result = authorize(
        Actor(email="dana@example.com"),
        Action.APP_VIEW_AS_USER,
        "x",
        data,
        groups=frozenset({"data-platform"}),
    )
    assert result == Decision(False, Reason.ALREADY_HAS_ACCESS, Permission.CAN_MANAGE)


def test_view_as_user_is_refused_to_an_owner_who_is_also_a_platform_admin() -> None:
    # The owner already receives the token (design 9.1), so the mode adds nothing and must not
    # write a spurious `app.view_as_user` audit row. No grant row names the owner here, so only the
    # owner test can refuse.
    app = AppAccess(slug="x", owner="Dana@Example.com", obo_delegated=True)
    data = AuthzData(
        apps={"x": app},
        platform_admins=(AdminEntry("user", "dana@example.com"),),
        user_authorization=True,
    )
    result = authorize(
        Actor(email="dana@example.com"),
        Action.APP_VIEW_AS_USER,
        "x",
        data,
        groups=frozenset(),
    )
    assert result == Decision(False, Reason.ALREADY_HAS_ACCESS, Permission.CAN_MANAGE)


def _obo_app_with_a_group_grant(*, user_authorization: bool) -> AuthzData:
    app = AppAccess(
        slug="x",
        owner="o@example.com",
        obo_delegated=True,
        grants=(Grant("group", "data-platform", Permission.CAN_USE),),
    )
    return AuthzData(
        apps={"x": app},
        platform_admins=(AdminEntry("user", "dana@example.com"),),
        user_authorization=user_authorization,
    )


def test_view_as_user_fails_closed_when_the_group_lookup_failed() -> None:
    # With unknown groups a group grant cannot be seen, so "named by no grant" cannot be shown
    # (FR-023: a failed group lookup fails closed). The same admin is refused with known groups
    # (test above), so allowing here would flip the answer on a SCIM outage.
    result = authorize(
        Actor(email="dana@example.com"),
        Action.APP_VIEW_AS_USER,
        "x",
        _obo_app_with_a_group_grant(user_authorization=True),
        groups=None,
    )
    assert result == Decision(False, Reason.GROUPS_UNKNOWN, Permission.CAN_MANAGE)


def test_view_as_user_with_user_authorization_off_does_not_need_groups() -> None:
    # Group grants never match while user authorization is off, so unknown groups hide nothing.
    result = authorize(
        Actor(email="dana@example.com"),
        Action.APP_VIEW_AS_USER,
        "x",
        _obo_app_with_a_group_grant(user_authorization=False),
        groups=None,
    )
    assert result == Decision(True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE)


def test_view_as_user_is_refused_to_the_owner_and_to_non_admins() -> None:
    data = make_data()
    owner = authorize(
        Actor(email="alice@example.com"), Action.APP_VIEW_AS_USER, SALES, data, groups=frozenset()
    )
    assert owner.reason is Reason.NOT_PLATFORM_ADMIN
    assert not decide(Kind.CAN_MANAGE, Action.APP_VIEW_AS_USER).allowed


# --- Rule 1: the owner cannot be lowered or removed ------------------------------------------


@pytest.mark.parametrize("level", [None, Permission.CAN_USE], ids=["remove", "lower"])
def test_rule_1_owner_cannot_be_removed_or_lowered(level: Permission | None) -> None:
    change = GrantChange("user", "Alice@example.com", level)
    assert check_grant_change("alice@example.com", change) is Reason.OWNER_PROTECTED
    result = authorize(
        Actor(email="alice@example.com"),
        Action.GRANT_APP,
        SALES,
        make_data(),
        groups=frozenset(),
        change=change,
    )
    assert result == Decision(False, Reason.OWNER_PROTECTED, Permission.CAN_MANAGE)


def test_rule_1_owner_may_be_named_at_can_manage_and_others_may_be_changed() -> None:
    owner_manage = GrantChange("user", "alice@example.com", Permission.CAN_MANAGE)
    assert check_grant_change("alice@example.com", owner_manage) is None
    for change in (
        GrantChange("user", "bob@example.com", None),
        GrantChange("group", "alice@example.com", Permission.CAN_USE),
        GrantChange("everyone", "*", Permission.CAN_USE),
    ):
        assert check_grant_change("alice@example.com", change) is None


def test_everyone_cannot_be_granted_can_manage() -> None:
    change = GrantChange("everyone", "*", Permission.CAN_MANAGE)
    assert check_grant_change("alice@example.com", change) is Reason.EVERYONE_CAN_USE_ONLY


def test_a_grant_change_does_not_widen_a_denied_actor() -> None:
    harmless = GrantChange("user", "bob@example.com", Permission.CAN_USE)
    result = authorize(
        Actor(email="use@example.com"),
        Action.GRANT_APP,
        SALES,
        make_data(),
        groups=frozenset(),
        change=harmless,
    )
    assert result.reason is Reason.INSUFFICIENT_LEVEL


# --- Rule 2: the last user entry of platform_admins ------------------------------------------


def test_rule_2_a_list_with_no_user_entry_is_refused() -> None:
    only_group = AdminChange((AdminEntry("group", "data-platform"),))
    assert check_admin_change(only_group) is Reason.LAST_USER_ADMIN
    assert check_admin_change(AdminChange(())) is Reason.LAST_USER_ADMIN
    keeps_a_user = AdminChange((AdminEntry("group", "g"), AdminEntry("user", "dana@example.com")))
    assert check_admin_change(keeps_a_user) is None


@pytest.mark.parametrize("name", ["", "   "])
def test_rule_2_a_blank_user_entry_is_not_a_user_entry(name: str) -> None:
    blank = AdminChange((AdminEntry("group", "g"), AdminEntry("user", name)))
    assert check_admin_change(blank) is Reason.LAST_USER_ADMIN


@pytest.mark.parametrize("action", [Action.PLATFORM_ADMIN_REMOVE, Action.PLATFORM_ADMIN_ADD])
def test_rule_2_through_authorize(action: Action) -> None:
    dana = Actor(email="dana@example.com")
    data = make_data()
    refused = authorize(
        dana,
        action,
        None,
        data,
        groups=None,
        change=AdminChange((AdminEntry("group", "data-platform"),)),
    )
    assert refused == Decision(False, Reason.LAST_USER_ADMIN, Permission.CAN_MANAGE)
    kept = authorize(
        dana,
        action,
        None,
        data,
        groups=None,
        change=AdminChange((AdminEntry("user", "dana@example.com"),)),
    )
    assert kept.allowed


def test_a_change_of_the_wrong_kind_for_the_action_is_denied() -> None:
    dana = Actor(email="dana@example.com")
    data = make_data()
    wrong = authorize(
        dana,
        Action.GRANT_APP,
        SALES,
        data,
        groups=None,
        change=AdminChange((AdminEntry("user", "dana@example.com"),)),
    )
    assert wrong.reason is Reason.UNEXPECTED_CHANGE
    stray = authorize(
        dana,
        Action.APP_OPEN,
        SALES,
        data,
        groups=None,
        change=GrantChange("user", "bob@example.com", None),
    )
    assert stray.reason is Reason.UNEXPECTED_CHANGE


# --- The decision never carries user-controlled text ------------------------------------------


def test_a_decision_holds_only_codes_never_the_actor_text() -> None:
    hostile = "x@example.com\nINJECT <script>"
    result = authorize(
        Actor(email=hostile), Action.APP_OPEN, "sales\nINJECT", make_data(), groups=frozenset()
    )
    assert isinstance(result.reason, Reason)
    assert "INJECT" not in repr(result)
