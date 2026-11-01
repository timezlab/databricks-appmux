"""APPMUX_BOOTSTRAP_ADMINS is honoured only while platform_admins is unset (FR-026, design 9.1).

"Unset" means the registry has no ``platform_admins`` row (``None``). A row holding ``[]`` is set:
the bootstrap list is ignored and nobody is a platform admin, because a deliberately emptied list
must not be refilled from a deploy-time variable (the break-glass path of design 9.1 is SQL).
"""

from __future__ import annotations

from appmux.authz.actions import Action
from appmux.authz.data import effective_platform_admins
from appmux.authz.policy import AdminEntry, AuthzData, Decision, Permission, Reason, authorize
from appmux.identity.actor import Actor
from appmux.settings import PlatformAdmin


def test_bootstrap_list_is_used_while_the_setting_is_unset() -> None:
    admins = effective_platform_admins(None, ["dana@example.com", "erin@example.com"])
    assert admins == (
        AdminEntry("user", "dana@example.com"),
        AdminEntry("user", "erin@example.com"),
    )


def test_bootstrap_list_is_ignored_once_the_setting_exists() -> None:
    configured = [PlatformAdmin(type="user", name="frank@example.com")]
    admins = effective_platform_admins(configured, ["dana@example.com"])
    assert admins == (AdminEntry("user", "frank@example.com"),)


def test_an_empty_setting_is_set_so_the_bootstrap_list_stays_ignored() -> None:
    assert effective_platform_admins([], ["dana@example.com"]) == ()


def test_unset_setting_and_no_bootstrap_list_means_no_platform_admin() -> None:
    assert effective_platform_admins(None, []) == ()


def test_bootstrap_emails_are_case_insensitive_trimmed_and_deduplicated() -> None:
    admins = effective_platform_admins(None, ["Dana@Example.com", " dana@example.com ", "", "  "])
    assert admins == (AdminEntry("user", "dana@example.com"),)


def test_configured_entries_keep_groups_and_lower_case_user_emails() -> None:
    configured = [
        PlatformAdmin(type="user", name="Dana@Example.com"),
        PlatformAdmin(type="group", name="Data-Platform"),
        PlatformAdmin(type="user", name="dana@example.com"),
    ]
    admins = effective_platform_admins(configured, [])
    assert admins == (
        AdminEntry("user", "dana@example.com"),
        AdminEntry("group", "Data-Platform"),
    )


def test_the_bootstrap_admin_holds_can_manage_through_authorize() -> None:
    data = AuthzData(
        apps={},
        platform_admins=effective_platform_admins(None, ["Dana@example.com"]),
        user_authorization=True,
    )
    dana = authorize(Actor(email="dana@example.com"), Action.SETTINGS_EDIT, None, data, groups=None)
    assert dana == Decision(True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE)
    mallory = authorize(
        Actor(email="mallory@example.com"), Action.SETTINGS_EDIT, None, data, groups=None
    )
    assert not mallory.allowed


def test_once_the_setting_exists_the_old_bootstrap_admin_is_no_longer_an_admin() -> None:
    data = AuthzData(
        apps={},
        platform_admins=effective_platform_admins(
            [PlatformAdmin(type="user", name="frank@example.com")], ["dana@example.com"]
        ),
        user_authorization=True,
    )
    dana = authorize(Actor(email="dana@example.com"), Action.SETTINGS_EDIT, None, data, groups=None)
    assert not dana.allowed
