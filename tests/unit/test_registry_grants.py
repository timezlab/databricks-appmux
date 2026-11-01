"""The AuthzData builder and the grant-change helpers of ``appmux.registry.grants`` (T041, T042)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from appmux.authz.policy import AdminEntry, AppAccess, Grant, Permission
from appmux.registry.grants import (
    build_authz_data,
    delete_grant_change,
    find_grant,
    put_grant_change,
)
from appmux.registry.models import App, AppGrant, PlatformSetting
from appmux.registry.snapshot import QuarantinedRow, RegistrySnapshot, build_snapshot
from appmux.registry.writers import WriteKind

NOW: Final = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
LATER: Final = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)


def app(slug: str, *, owner: str = "alice@example.com", obo: bool = False) -> App:
    return App(
        slug=slug,
        display_name=slug.title(),
        owner=owner,
        source_path=f"/Workspace/Users/{owner}/{slug}",
        memory_budget_mb=256,
        obo_delegated=obo,
        created_by="dana@example.com",
        created_at=NOW,
        updated_by="dana@example.com",
        updated_at=NOW,
    )


def grant(slug: str, kind: str, principal: str, permission: str = "CAN_USE") -> AppGrant:
    return AppGrant.model_validate(
        {
            "app_slug": slug,
            "principal_type": kind,
            "principal": principal,
            "permission": permission,
            "granted_by": "alice@example.com",
            "granted_at": NOW,
        }
    )


def admins_setting(value: object) -> PlatformSetting:
    return PlatformSetting.model_validate(
        {
            "key": "platform_admins",
            "value": value,
            "updated_by": "dana@example.com",
            "updated_at": NOW,
        }
    )


def snapshot(
    *,
    apps: tuple[App, ...] = (),
    grants: tuple[AppGrant, ...] = (),
    settings: tuple[PlatformSetting, ...] = (),
    quarantined: tuple[QuarantinedRow, ...] = (),
) -> RegistrySnapshot:
    return build_snapshot(
        {"apps": list(apps), "app_grants": list(grants), "platform_settings": list(settings)},
        loaded_at=NOW,
        quarantined=quarantined,
    )


# --- AuthzData from a snapshot ----------------------------------------------------------------


def test_each_app_carries_its_owner_its_obo_flag_and_its_grants() -> None:
    data = build_authz_data(
        snapshot(
            apps=(app("sales", obo=True), app("docs", owner="frank@example.com")),
            grants=(
                grant("sales", "user", "carol@example.com", "CAN_MANAGE"),
                grant("sales", "group", "sales-team"),
                grant("docs", "everyone", "*"),
            ),
        )
    )

    assert data.apps["sales"] == AppAccess(
        slug="sales",
        owner="alice@example.com",
        grants=(
            Grant("group", "sales-team", Permission.CAN_USE),
            Grant("user", "carol@example.com", Permission.CAN_MANAGE),
        ),
        obo_delegated=True,
    )
    assert data.apps["docs"] == AppAccess(
        slug="docs",
        owner="frank@example.com",
        grants=(Grant("everyone", "*", Permission.CAN_USE),),
        obo_delegated=False,
    )


def test_a_grant_whose_app_is_missing_grants_nothing() -> None:
    data = build_authz_data(
        snapshot(apps=(app("sales"),), grants=(grant("gone", "everyone", "*"),))
    )

    assert set(data.apps) == {"sales"}


def test_user_authorization_is_on_unless_the_caller_says_otherwise() -> None:
    assert build_authz_data(snapshot()).user_authorization is True
    assert build_authz_data(snapshot(), user_authorization=False).user_authorization is False


# --- platform admins and the bootstrap list (FR-026) -------------------------------------------


def test_the_bootstrap_list_applies_while_the_setting_is_unset() -> None:
    data = build_authz_data(snapshot(), bootstrap_admins=["Dana@Example.com"])

    assert data.platform_admins == (AdminEntry("user", "dana@example.com"),)


def test_the_stored_setting_replaces_the_bootstrap_list() -> None:
    stored = admins_setting(
        [{"type": "user", "name": "erin@example.com"}, {"type": "group", "name": "data-platform"}]
    )

    data = build_authz_data(snapshot(settings=(stored,)), bootstrap_admins=["dana@example.com"])

    assert data.platform_admins == (
        AdminEntry("user", "erin@example.com"),
        AdminEntry("group", "data-platform"),
    )


def test_an_empty_stored_setting_is_set_so_nobody_is_admin() -> None:
    data = build_authz_data(
        snapshot(settings=(admins_setting([]),)), bootstrap_admins=["dana@example.com"]
    )

    assert data.platform_admins == ()


def test_a_setting_of_the_wrong_shape_fails_closed_and_does_not_reopen_the_bootstrap() -> None:
    for value in (["dana@example.com"], "dana@example.com", True, 5):
        data = build_authz_data(
            snapshot(settings=(admins_setting(value),)), bootstrap_admins=["dana@example.com"]
        )
        assert data.platform_admins == (), value


def test_a_quarantined_setting_row_is_not_unset() -> None:
    # The row failed its model and is left out of the snapshot; "unset" would hand the slot to
    # the bootstrap list, so the quarantine record is read as "set to nothing usable".
    quarantined = (QuarantinedRow("platform_settings", "platform_admins"),)

    data = build_authz_data(
        snapshot(quarantined=quarantined), bootstrap_admins=["dana@example.com"]
    )

    assert data.platform_admins == ()


def test_a_quarantined_row_of_another_setting_leaves_the_bootstrap_list_alone() -> None:
    quarantined = (QuarantinedRow("platform_settings", "package_mode"),)

    data = build_authz_data(
        snapshot(quarantined=quarantined), bootstrap_admins=["dana@example.com"]
    )

    assert data.platform_admins == (AdminEntry("user", "dana@example.com"),)


# --- grant helpers ----------------------------------------------------------------------------


def test_find_grant_matches_a_user_or_group_without_regard_to_case() -> None:
    snap = snapshot(
        apps=(app("sales"),),
        grants=(grant("sales", "group", "Sales-Team"), grant("sales", "user", "bob@example.com")),
    )

    found = find_grant(snap, "sales", "group", "sales-team")
    assert found is not None and found.principal == "Sales-Team"
    assert find_grant(snap, "sales", "user", "BOB@example.com") is not None
    assert find_grant(snap, "sales", "user", "carol@example.com") is None
    assert find_grant(snap, "docs", "user", "bob@example.com") is None
    assert find_grant(snap, "sales", "everyone", "*") is None


def test_a_new_grant_is_an_insert_with_the_granter() -> None:
    change = put_grant_change(
        None,
        app_slug="sales",
        principal_type="user",
        principal="bob@example.com",
        permission="CAN_USE",
        granted_by="alice@example.com",
        granted_at=LATER,
    )

    assert change is not None
    assert change.kind is WriteKind.APP_GRANT_INSERT
    assert dict(change.values) == {
        "app_slug": "sales",
        "principal_type": "user",
        "principal": "bob@example.com",
        "permission": "CAN_USE",
        "granted_by": "alice@example.com",
        "granted_at": LATER,
    }


def test_a_new_level_is_an_update_of_the_stored_principal() -> None:
    existing = grant("sales", "group", "Sales-Team")

    change = put_grant_change(
        existing,
        app_slug="sales",
        principal_type="group",
        principal="sales-team",
        permission="CAN_MANAGE",
        granted_by="carol@example.com",
        granted_at=LATER,
    )

    assert change is not None
    assert change.kind is WriteKind.APP_GRANT_UPDATE
    assert dict(change.key) == {
        "app_slug": "sales",
        "principal_type": "group",
        "principal": "Sales-Team",
    }
    assert dict(change.values) == {
        "permission": "CAN_MANAGE",
        "granted_by": "carol@example.com",
        "granted_at": LATER,
    }


def test_the_same_level_is_no_change() -> None:
    existing = grant("sales", "user", "bob@example.com", "CAN_USE")

    assert (
        put_grant_change(
            existing,
            app_slug="sales",
            principal_type="user",
            principal="bob@example.com",
            permission="CAN_USE",
            granted_by="carol@example.com",
            granted_at=LATER,
        )
        is None
    )


def test_a_removal_names_the_stored_key() -> None:
    change = delete_grant_change(grant("docs", "everyone", "*"))

    assert change.kind is WriteKind.APP_GRANT_DELETE
    assert dict(change.key) == {"app_slug": "docs", "principal_type": "everyone", "principal": "*"}
    assert not change.values
