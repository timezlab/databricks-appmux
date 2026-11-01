"""The writes that register an app (T094; data model 5.1 and 6).

``register_app_changes`` builds the ``Change`` values the register route writes, so that the tables,
the columns and the starting state of a new app are named in one place. Both rows are platform data:
the ``apps`` row decides what runs in the slot and the first ``app_config`` row is the one
``app_config`` insert a platform admin's own role may make (``registry/writers.py``). The route
passes the admin's OBO token with them; with no token the registry refuses the write
(``RegistryRoleMissing``), and the service principal's role cannot make it at all.

A new app starts stopped, with no target release and both delegations off: nothing runs and no
credential is lent until a platform admin and the app's admin decide so later. The ``version``
counters and the timestamps are the database's (``registry/protocol.py``), so they are not set here.

``platform_fields_change`` and ``owner_change`` (T107) are the later edits of the same row by a
platform admin: the display name, the source folder, the memory budget, the slug (a rename: the
tables that point at it follow, ``ON UPDATE CASCADE``) and the owner. Like the insert they are
written under the admin's own token, and they carry the ``version`` the caller read.
"""

from __future__ import annotations

from appmux.registry.models import App
from appmux.registry.protocol import Change
from appmux.registry.writers import WriteKind


def register_app_changes(
    *,
    slug: str,
    display_name: str,
    owner: str,
    source_path: str,
    memory_budget_mb: int,
    registered_by: str,
) -> tuple[Change, Change]:
    """The ``apps`` insert and the first ``app_config`` insert of a new app, in write order.

    The values are not checked here: the request schema checks the form of each one, the row models
    and the database check it again, and an app whose ``slug`` is taken raises ``RowAlreadyExists``
    from the write. ``registered_by`` is the platform admin's email, recorded as the author of both
    rows.
    """
    app_insert = Change(
        WriteKind.APP_INSERT,
        values={
            "slug": slug,
            "display_name": display_name,
            "owner": owner,
            "source_path": source_path,
            "memory_budget_mb": memory_budget_mb,
            "obo_delegated": False,
            "shared_sp_delegated": False,
            "created_by": registered_by,
            "updated_by": registered_by,
        },
    )
    config_insert = Change(
        WriteKind.APP_CONFIG_INSERT,
        values={"app_slug": slug, "desired_state": "stopped", "updated_by": registered_by},
    )
    return app_insert, config_insert


def platform_fields_change(
    app: App,
    *,
    updated_by: str,
    slug: str | None = None,
    display_name: str | None = None,
    source_path: str | None = None,
    memory_budget_mb: int | None = None,
) -> Change:
    """The ``apps`` update that sets the fields given (not ``None``), at ``app.version``.

    A value equal to the stored one is still written; the caller drops those first, so that an
    edit that changes nothing writes nothing. A ``slug`` is the rename.
    """
    given = {
        "slug": slug,
        "display_name": display_name,
        "source_path": source_path,
        "memory_budget_mb": memory_budget_mb,
    }
    values: dict[str, object] = {name: value for name, value in given.items() if value is not None}
    values["updated_by"] = updated_by
    return Change(
        WriteKind.APP_UPDATE,
        key={"slug": app.slug},
        values=values,
        expected_version=app.version,
    )


def owner_change(app: App, owner: str, *, updated_by: str) -> Change:
    """The ``apps`` update that makes ``owner`` the app's owner, checked against ``app.version``."""
    return Change(
        WriteKind.APP_UPDATE,
        key={"slug": app.slug},
        values={"owner": owner, "updated_by": updated_by},
        expected_version=app.version,
    )
