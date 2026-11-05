"""The App permissions the ADR 0020 run changes are put back exactly (tests/integration/app_acl.py).

``test_service_principal_grant.py`` gives a service principal ``CAN_USE`` on the appmux App so that
the Apps proxy lets its call in. ``databricks apps set-permissions`` replaces the whole *direct*
list and has no way to delete one entry, so the run puts the list back from a snapshot. A snapshot
that kept an inherited entry would turn it into a direct one; one that dropped a direct entry would
remove somebody's access to the App. Both are pure functions of the CLI's JSON, so they are checked
here, with made-up principals, and the integration test only has to call them. The module is loaded
by path because the integration directory is not a package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

MODULE = Path(__file__).resolve().parents[1] / "integration" / "app_acl.py"


@pytest.fixture(scope="module")
def app_acl() -> ModuleType:
    spec = importlib.util.spec_from_file_location("app_acl_under_test", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["app_acl_under_test"] = module
    spec.loader.exec_module(module)
    return module


def permission(level: str, *, inherited: bool) -> dict[str, Any]:
    return {"permission_level": level, "inherited": inherited}


def entry(key: str, name: str, *permissions: dict[str, Any]) -> dict[str, Any]:
    return {key: name, "all_permissions": list(permissions)}


LISTING = {
    "object_id": "/apps/example",
    "access_control_list": [
        entry("user_name", "owner@example.test", permission("CAN_MANAGE", inherited=False)),
        entry("group_name", "admins", permission("CAN_MANAGE", inherited=True)),
        entry("service_principal_name", "sp-one", permission("CAN_USE", inherited=False)),
        entry(
            "user_name",
            "mixed@example.test",
            permission("CAN_MANAGE", inherited=True),
            permission("CAN_USE", inherited=False),
        ),
    ],
}


def test_the_direct_entries_leave_out_what_is_inherited(app_acl: ModuleType) -> None:
    kept = app_acl.direct_entries(LISTING)

    assert {"group_name": "admins", "permission_level": "CAN_MANAGE"} not in kept
    assert len(kept) == 3


def test_an_entry_that_is_partly_inherited_keeps_only_its_direct_level(
    app_acl: ModuleType,
) -> None:
    kept = app_acl.direct_entries(LISTING)

    assert {"user_name": "mixed@example.test", "permission_level": "CAN_USE"} in kept
    assert {"user_name": "mixed@example.test", "permission_level": "CAN_MANAGE"} not in kept


def test_each_direct_entry_names_its_principal_with_the_key_the_cli_expects(
    app_acl: ModuleType,
) -> None:
    kept = app_acl.direct_entries(LISTING)

    assert {"user_name": "owner@example.test", "permission_level": "CAN_MANAGE"} in kept
    assert {"service_principal_name": "sp-one", "permission_level": "CAN_USE"} in kept


def test_a_listing_with_no_entries_has_no_direct_entries(app_acl: ModuleType) -> None:
    assert app_acl.direct_entries({"object_id": "/apps/example"}) == []


def test_two_lists_are_the_same_whatever_their_order(app_acl: ModuleType) -> None:
    first = app_acl.direct_entries(LISTING)

    assert app_acl.same_entries(first, list(reversed(first)))


def test_a_changed_level_or_an_extra_entry_is_a_difference(app_acl: ModuleType) -> None:
    first = app_acl.direct_entries(LISTING)
    raised = [
        {**item, "permission_level": "CAN_MANAGE"} if item.get("service_principal_name") else item
        for item in first
    ]
    extra = [*first, {"service_principal_name": "sp-two", "permission_level": "CAN_USE"}]

    assert not app_acl.same_entries(first, raised)
    assert not app_acl.same_entries(first, extra)
    assert not app_acl.same_entries(first, first[:-1])


def test_the_body_to_put_back_wraps_the_entries_in_an_access_control_list(
    app_acl: ModuleType,
) -> None:
    kept = app_acl.direct_entries(LISTING)

    assert app_acl.permissions_body(kept) == {"access_control_list": kept}
