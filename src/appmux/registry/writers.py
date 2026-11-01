"""The write-identity rule of data model section 6, as a table from write kind to identity.

Two database identities write the registry:

* ``PLATFORM_ADMIN``: a platform admin's own role, a member of the group role
  ``appmux_platform_writers``. The login is that person's OBO token. It writes what decides what
  runs in the slot: ``apps``, ``platform_settings``, the answer to a request, and the first
  ``app_config`` row of an app.
* ``APPMUX_SP``: the appmux service principal. It writes what an app admin does and the results:
  ``app_config`` updates, grants, releases, resources, metrics, and asking or withdrawing a request.

``audit_log`` takes an INSERT from either, in the transaction of the change it records.

The table here and the ``GRANT`` statements of ``migrations/0002_grants.sql`` state the same rule
twice, once for the code and once for the database. ``tests/unit/test_write_identity.py`` compares
them. This module opens no connection: ``registry/client.py`` asks ``require_identity`` before it
runs any write.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal


class Identity(StrEnum):
    """The two database identities; each value is the name of its Postgres role."""

    APPMUX_SP = "appmux_sp"
    PLATFORM_ADMIN = "appmux_platform_writers"


class WriteKind(StrEnum):
    """One kind of write to one table. The value is what an error message shows."""

    PLATFORM_SETTING_INSERT = "platform_settings.insert"
    PLATFORM_SETTING_UPDATE = "platform_settings.update"
    PLATFORM_SETTING_DELETE = "platform_settings.delete"
    APP_INSERT = "apps.insert"
    APP_UPDATE = "apps.update"
    APP_DELETE = "apps.delete"
    REQUEST_CREATE = "permission_requests.create"
    REQUEST_WITHDRAW = "permission_requests.withdraw"
    REQUEST_ANSWER = "permission_requests.answer"
    APP_CONFIG_INSERT = "app_config.insert"
    APP_CONFIG_UPDATE = "app_config.update"
    APP_GRANT_INSERT = "app_grants.insert"
    APP_GRANT_UPDATE = "app_grants.update"
    APP_GRANT_DELETE = "app_grants.delete"
    RELEASE_INSERT = "releases.insert"
    RELEASE_UPDATE = "releases.update"
    APP_RESOURCE_INSERT = "app_resources.insert"
    APP_RESOURCE_UPDATE = "app_resources.update"
    APP_RESOURCE_DELETE = "app_resources.delete"
    METRIC_INSERT = "metrics.insert"
    METRIC_DELETE = "metrics.delete"
    AUDIT_INSERT = "audit_log.insert"


class WriteIdentityError(Exception):
    """A write was attempted with an identity that data model section 6 does not give it."""


@dataclass(frozen=True, slots=True)
class WriteRule:
    """The SQL privilege a write kind needs, and the identities that hold it."""

    table: str
    privilege: Literal["INSERT", "UPDATE", "DELETE"]
    identities: frozenset[Identity]


_SP: Final = frozenset({Identity.APPMUX_SP})
_ADMIN: Final = frozenset({Identity.PLATFORM_ADMIN})
_EITHER: Final = _SP | _ADMIN

WRITE_RULES: Final[dict[WriteKind, WriteRule]] = {
    WriteKind.PLATFORM_SETTING_INSERT: WriteRule("platform_settings", "INSERT", _ADMIN),
    WriteKind.PLATFORM_SETTING_UPDATE: WriteRule("platform_settings", "UPDATE", _ADMIN),
    WriteKind.PLATFORM_SETTING_DELETE: WriteRule("platform_settings", "DELETE", _ADMIN),
    WriteKind.APP_INSERT: WriteRule("apps", "INSERT", _ADMIN),
    WriteKind.APP_UPDATE: WriteRule("apps", "UPDATE", _ADMIN),
    WriteKind.APP_DELETE: WriteRule("apps", "DELETE", _ADMIN),
    # The one table both classes write, safe because a request grants nothing (data model 4.3).
    WriteKind.REQUEST_CREATE: WriteRule("permission_requests", "INSERT", _SP),
    WriteKind.REQUEST_WITHDRAW: WriteRule("permission_requests", "UPDATE", _SP),
    WriteKind.REQUEST_ANSWER: WriteRule("permission_requests", "UPDATE", _ADMIN),
    # A platform admin writes the first app_config row at registration. Later edits, a platform
    # admin's included, go through the service principal after authorize().
    WriteKind.APP_CONFIG_INSERT: WriteRule("app_config", "INSERT", _ADMIN),
    WriteKind.APP_CONFIG_UPDATE: WriteRule("app_config", "UPDATE", _SP),
    WriteKind.APP_GRANT_INSERT: WriteRule("app_grants", "INSERT", _SP),
    WriteKind.APP_GRANT_UPDATE: WriteRule("app_grants", "UPDATE", _SP),
    WriteKind.APP_GRANT_DELETE: WriteRule("app_grants", "DELETE", _SP),
    WriteKind.RELEASE_INSERT: WriteRule("releases", "INSERT", _SP),
    WriteKind.RELEASE_UPDATE: WriteRule("releases", "UPDATE", _SP),
    WriteKind.APP_RESOURCE_INSERT: WriteRule("app_resources", "INSERT", _SP),
    WriteKind.APP_RESOURCE_UPDATE: WriteRule("app_resources", "UPDATE", _SP),
    WriteKind.APP_RESOURCE_DELETE: WriteRule("app_resources", "DELETE", _SP),
    WriteKind.METRIC_INSERT: WriteRule("metrics", "INSERT", _SP),
    WriteKind.METRIC_DELETE: WriteRule("metrics", "DELETE", _SP),
    # Insert only: no identity is granted UPDATE or DELETE, which keeps the log append-only.
    WriteKind.AUDIT_INSERT: WriteRule("audit_log", "INSERT", _EITHER),
}


def require_identity(kind: WriteKind, identity: Identity) -> None:
    """Return when ``identity`` may make this write, raise ``WriteIdentityError`` when it may not.

    The check fails closed: an identity that is neither of the two, or a kind with no rule, raises.
    """
    rule = WRITE_RULES.get(kind)
    if rule is None:
        raise WriteIdentityError(f"no write rule for '{kind}'; add it to WRITE_RULES")
    if identity not in rule.identities:
        allowed = " or ".join(sorted(who.value for who in rule.identities))
        raise WriteIdentityError(
            f"write '{kind.value}' must use the identity {allowed}, not '{identity}'; "
            "route it through the other connection"
        )
