"""Registry data for authorization: the ``AuthzData`` of a snapshot, and the grant writes.

``authz`` is pure and reads plain values (``authz.policy.AuthzData``); this module builds them from
a ``RegistrySnapshot`` so that ``authz`` never imports the registry (design 3.4). It also builds the
``Change`` values the grant routes write, so that the table, the key and the columns of
``app_grants`` are named in one place.

Platform admins (design 9.1, FR-026): the ``platform_admins`` setting when it exists, else the
bootstrap list of ``APPMUX_BOOTSTRAP_ADMINS``. A setting that exists but cannot be used, because
its value has the wrong shape or its row failed its model and was quarantined, counts as set to
nobody: the bootstrap list must not come back to life through a damaged row. The shared-SP token
cannot write a setting (data model 6), so this is defence in depth, not an expected state.

``user_authorization`` is not a registry value. Whether the visitor token reaches appmux depends on
the App's deploy configuration; a request without a token has no groups, and the group lookup then
answers ``None``, which ``authorize`` already treats as "no group matches". The parameter exists
for a caller that knows the feature is off for the whole App.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Final

from appmux.authz.data import effective_platform_admins
from appmux.authz.policy import AdminEntry, AppAccess, AuthzData, Grant, Permission
from appmux.registry.models import App, AppGrant
from appmux.registry.protocol import Change
from appmux.registry.snapshot import QuarantinedRow, RegistrySnapshot
from appmux.registry.writers import WriteKind
from appmux.settings import PlatformAdmin

PLATFORM_ADMINS_KEY: Final = "platform_admins"
_SETTINGS_TABLE: Final = "platform_settings"


def build_authz_data(
    snapshot: RegistrySnapshot,
    *,
    bootstrap_admins: Iterable[str] = (),
    user_authorization: bool = True,
) -> AuthzData:
    """What ``authz.authorize`` reads, taken from one snapshot.

    An app is listed with its grants (a grant whose app is missing from the snapshot grants
    nothing, because it has no app to belong to).
    """
    return AuthzData(
        apps={app.slug: _access_of(snapshot, app) for app in snapshot.apps},
        platform_admins=_platform_admins(snapshot, bootstrap_admins),
        user_authorization=user_authorization,
    )


def _access_of(snapshot: RegistrySnapshot, app: App) -> AppAccess:
    return AppAccess(
        slug=app.slug,
        owner=app.owner,
        grants=tuple(
            Grant(grant.principal_type, grant.principal, Permission(grant.permission))
            for grant in snapshot.grants_for(app.slug)
        ),
        obo_delegated=app.obo_delegated,
    )


def _platform_admins(
    snapshot: RegistrySnapshot, bootstrap_admins: Iterable[str]
) -> tuple[AdminEntry, ...]:
    setting = snapshot.setting(PLATFORM_ADMINS_KEY)
    configured: list[PlatformAdmin] | None
    if setting is None:
        # Unset, unless the row exists and was left out of the snapshot for failing its model.
        configured = [] if _is_quarantined(snapshot) else None
    elif isinstance(setting.value, list) and all(
        isinstance(entry, PlatformAdmin) for entry in setting.value
    ):
        configured = [entry for entry in setting.value if isinstance(entry, PlatformAdmin)]
    else:
        configured = []
    return effective_platform_admins(configured, bootstrap_admins)


def _is_quarantined(snapshot: RegistrySnapshot) -> bool:
    return QuarantinedRow(_SETTINGS_TABLE, PLATFORM_ADMINS_KEY) in snapshot.quarantined


def find_grant(
    snapshot: RegistrySnapshot, app_slug: str, principal_type: str, principal: str
) -> AppGrant | None:
    """The stored grant of a principal on an app, or ``None``.

    A user's email and a group's name are compared without regard to case (the policy matches them
    that way), so a second spelling finds the row that is there instead of adding another.
    """
    wanted = principal.strip().lower()
    for grant in snapshot.grants_for(app_slug):
        if grant.principal_type == principal_type and grant.principal.strip().lower() == wanted:
            return grant
    return None


def put_grant_change(
    existing: AppGrant | None,
    *,
    app_slug: str,
    principal_type: str,
    principal: str,
    permission: str,
    granted_by: str,
    granted_at: datetime,
) -> Change | None:
    """The write that gives a principal ``permission`` on an app, or ``None`` when it has it.

    ``existing`` is ``find_grant``'s answer. A principal has one level per app, so a different
    level updates the stored row (under its stored spelling) and records who set it and when.
    """
    if existing is None:
        return Change(
            WriteKind.APP_GRANT_INSERT,
            values={
                "app_slug": app_slug,
                "principal_type": principal_type,
                "principal": principal,
                "permission": permission,
                "granted_by": granted_by,
                "granted_at": granted_at,
            },
        )
    if existing.permission == permission:
        return None
    return Change(
        WriteKind.APP_GRANT_UPDATE,
        key=_key_of(existing),
        values={"permission": permission, "granted_by": granted_by, "granted_at": granted_at},
    )


def delete_grant_change(existing: AppGrant) -> Change:
    """The write that removes a stored grant."""
    return Change(WriteKind.APP_GRANT_DELETE, key=_key_of(existing))


def _key_of(grant: AppGrant) -> dict[str, object]:
    return {
        "app_slug": grant.app_slug,
        "principal_type": grant.principal_type,
        "principal": grant.principal,
    }
