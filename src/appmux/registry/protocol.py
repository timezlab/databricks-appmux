"""The ``Registry`` protocol: what the rest of appmux depends on, and the shape of a write.

Reads come from the immutable ``RegistrySnapshot`` (``snapshot.py``), which is re-read every few
seconds and after each write, so a route or the edge never waits on the database to read. Writes go
through ``Registry.write``: one or more typed ``Change`` values and the ``AuditEntry`` values that
record them, applied in one transaction under one database identity (data model 6). Metrics and the
audit log are not in the snapshot and have their own methods.

Who calls what (task ids of specs/001-appmux-v1/tasks.md):

* ``snapshot``, ``last_error``: the authz data builder (T037) and the connection sweeper (T040)
  read the snapshot; the edge pipeline reads the route inputs from it; the admin API reads every
  table from it and answers ``503`` for a route that needs the database while ``last_error`` is set
  (T023, T024). The startup gate's status page (T025) never shows ``last_error``: it prints a
  fixed ``BlockReason`` text, so no exception message can reach a visitor.
* ``refresh()``: startup (T095, raises ``RegistryUnavailable`` when Lakebase is down, so the gate
  shows the status page) and the poll task; ``write`` calls it before it returns.
* ``write``: register and update an app, the delegation flags and the egress list (T094, T107,
  T066), grants (T041), releases and their status (T092, T093, T104, T107), resources (T127),
  requests and their answers (T144), platform settings (T066) and platform admins (T154). A write
  of audit rows alone records an action that changes no row (viewing a release's source, "view as
  user").
* ``append_metrics``, ``prune_metrics``: the flusher and the hourly retention job (T168).
  ``read_metrics``: ``GET /metrics`` (T169). ``read_metric_steps``: ``GET /metrics`` with a
  ``step`` longer than a minute; ``read_metric_breakdown``: ``GET /metrics/breakdown`` (T193).
* ``read_audit``: ``GET /audit`` (T169).

What the registry fills in, so that no caller does: column defaults of the baseline migration
(timestamps with the database clock, ``request_id``, ``version = 1``, ``status``), ``updated_at`` on
every update of ``platform_settings``, ``apps`` and ``app_config``, ``status_at`` when an update
sets a release's ``status``, and ``version + 1`` on every update of ``apps`` and ``app_config``. A
caller passes ``updated_by`` and the columns it changes, never ``version``.

Two preconditions make a check and its write one step, so that nothing can happen between them:
``Change.expected_version`` (``apps``, ``app_config``) and ``Change.expected_status`` (an update
of a permission request: approve, deny and withdraw all need it to be still ``pending``). In SQL
each is a condition of the ``UPDATE`` itself (``WHERE ... AND version = %s``, ``AND status = %s``);
when no row changed, a ``SELECT`` tells ``RowMissing`` (no such row) from ``ChangedByOthers`` (the
row is there but is no longer what the caller read).

``psycopg`` is the client's business (``registry/client.py``); this module opens no connection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Final, Protocol

from pydantic import AwareDatetime, SecretStr

from appmux.registry.audit import AuditEntry
from appmux.registry.errors import RegistryRoleMissing
from appmux.registry.metric_steps import MetricBreakdownRow
from appmux.registry.models import AuditLogRow, MetricRow, RowModel
from appmux.registry.snapshot import PRIMARY_KEYS, SNAPSHOT_TABLES, RegistrySnapshot, deep_freeze
from appmux.registry.writers import WRITE_RULES, Identity, WriteKind, require_identity

# Only these two tables carry ``version`` (data model 4.2 and 4.4). An update of one of them states
# the version the caller read, and a stale one raises ``ChangedByOthers``.
VERSIONED_UPDATES: Final = frozenset({WriteKind.APP_UPDATE, WriteKind.APP_CONFIG_UPDATE})
# The two updates of ``permission_requests``; either may state the status it expects (data model
# 4.3: approve "checks the request again as it stands now", rule 7: withdraw only while pending).
REQUEST_UPDATES: Final = frozenset({WriteKind.REQUEST_WITHDRAW, WriteKind.REQUEST_ANSWER})
REQUEST_STATUSES: Final = frozenset({"pending", "approved", "denied", "withdrawn"})


def _frozen(values: Mapping[str, object]) -> Mapping[str, object]:
    # deep_freeze copies, so the caller's nested lists and dicts are not shared with the change.
    return MappingProxyType(deep_freeze(dict(values)))


@dataclass(frozen=True, slots=True)
class Change:
    """One write to one row of a snapshot table.

    * ``kind``: which table and which statement (``WriteKind``); it decides the database identity.
    * ``key``: the primary-key columns of the row to update or delete (for example
      ``{"slug": "sales"}``); empty for an insert, whose key is in ``values`` (or generated).
    * ``values``: the columns to insert, or the columns to change; empty for a delete.
    * ``expected_version``: the ``version`` the caller read, required for the updates of ``apps``
      and ``app_config`` and refused for every other kind.
    * ``expected_status``: optional, only on the two updates of ``permission_requests`` (withdraw
      and answer): the ``status`` the caller read, normally ``"pending"``. When the row exists with
      another status the write raises ``ChangedByOthers``; when it does not exist, ``RowMissing``.

    The mappings in ``values`` and ``key`` are copied and frozen all the way down: a nested list or
    dict cannot be changed after the ``Change`` is built, and the caller's own objects stay theirs.

    ``metrics`` and ``audit_log`` writes are not changes: see ``Registry.append_metrics`` and the
    ``audit`` argument of ``Registry.write``.
    """

    kind: WriteKind
    values: Mapping[str, object] = field(default_factory=dict)
    key: Mapping[str, object] = field(default_factory=dict)
    expected_version: int | None = None
    expected_status: str | None = None

    def __post_init__(self) -> None:
        rule = WRITE_RULES.get(self.kind)
        if rule is None or rule.table not in SNAPSHOT_TABLES:
            raise ValueError(
                f"'{self.kind.value}' is not a change of a snapshot table; "
                "metrics and audit rows have their own methods"
            )
        object.__setattr__(self, "values", _frozen(self.values))
        object.__setattr__(self, "key", _frozen(self.key))
        if rule.privilege == "INSERT":
            if self.key:
                raise ValueError(f"'{self.kind.value}' is an insert; its key belongs in values")
            if not self.values:
                raise ValueError(f"'{self.kind.value}' needs values")
        else:
            if set(self.key) != set(PRIMARY_KEYS[rule.table]):
                raise ValueError(
                    f"'{self.kind.value}' needs exactly the key columns "
                    f"{', '.join(PRIMARY_KEYS[rule.table])}"
                )
            if rule.privilege == "DELETE" and self.values:
                raise ValueError(f"'{self.kind.value}' is a delete and takes no values")
            if rule.privilege == "UPDATE" and not self.values:
                raise ValueError(f"'{self.kind.value}' needs the columns to change")
        if "version" in self.values:
            raise ValueError("the registry sets 'version'; pass expected_version instead")
        if (self.kind in VERSIONED_UPDATES) != (self.expected_version is not None):
            raise ValueError(
                f"'{self.kind.value}' "
                + ("needs" if self.kind in VERSIONED_UPDATES else "takes no")
                + " expected_version"
            )
        if self.expected_status is not None:
            if self.kind not in REQUEST_UPDATES:
                raise ValueError(f"'{self.kind.value}' takes no expected_status")
            if self.expected_status not in REQUEST_STATUSES:
                raise ValueError("expected_status is not a request status")


def require_something_to_write(changes: Sequence[Change], audit: Sequence[AuditEntry]) -> None:
    """Refuse a write with neither a change nor an audit entry (``ValueError``).

    A write of audit entries alone is legal: it records an action that changes no row, such as
    viewing a release's source or "view as user". Both lists empty is a caller's bug.
    """
    if not changes and not audit:
        raise ValueError("there is nothing to write: pass at least one change or one audit entry")


def resolve_identity(changes: Sequence[Change], admin_token: SecretStr | None) -> Identity:
    """The one database identity a transaction of these changes runs under.

    A transaction has one connection, so every change must use the same identity. With an admin
    token the identity is the platform admin's own role, and a change that only the appmux SP may
    write (``WriteIdentityError``, from ``require_identity``) is refused; a mixed transaction is
    refused the same way. Without a token the identity is the SP, and a change that only a platform
    admin may write raises ``RegistryRoleMissing``: the person has no platform role to write with.
    With no changes at all (a write of audit rows alone) the identity is the SP, or the platform
    admin when a token is given; both hold INSERT on ``audit_log``.
    """
    if admin_token is None:
        for change in changes:
            if Identity.APPMUX_SP not in WRITE_RULES[change.kind].identities:
                raise RegistryRoleMissing
        identity = Identity.APPMUX_SP
    else:
        identity = Identity.PLATFORM_ADMIN
    for change in changes:
        require_identity(change.kind, identity)
    return identity


class Registry(Protocol):
    """The registry as the rest of the code sees it."""

    @property
    def snapshot(self) -> RegistrySnapshot:
        """The latest good snapshot; raises ``RegistryUnavailable`` before the first load."""
        ...

    @property
    def last_error(self) -> str | None:
        """Why the latest read or write failed; ``None`` when the registry is working."""
        ...

    async def refresh(self) -> None:
        """Re-read the snapshot tables now.

        Raises ``RegistryUnavailable`` when nothing has been loaded yet; once a snapshot exists, a
        failure keeps serving it and sets ``last_error``.
        """
        ...

    async def write(
        self,
        changes: Sequence[Change],
        audit: Sequence[AuditEntry] = (),
        *,
        admin_token: SecretStr | None = None,
    ) -> tuple[RowModel | None, ...]:
        """Apply ``changes`` and ``audit`` in one transaction, then refresh the snapshot.

        ``audit`` is empty only for results the reconciler records (a release's status), which are
        not administrative actions; every other write names its audit entries here, so a row and
        its record cannot be written apart. ``changes`` is empty only for an action that changes no
        row (``release.view_source``, ``app.view_as_user``): the audit rows are then written alone,
        as the appmux SP unless ``admin_token`` is given. Both empty is a ``ValueError``
        (``require_something_to_write``). ``admin_token`` is the platform admin's OBO token for
        kinds only that role may write (see ``resolve_identity``); it is never logged, stored or
        put in an error message.

        Returns the rows as stored, one per change in order (``None`` for a delete; ``()`` for an
        audit-only write), so a caller reads back the generated ``request_id`` and the new
        ``version``.

        Raises ``ChangedByOthers`` (stale ``expected_version``, or a request whose status is no
        longer ``expected_status``), ``RegistryRoleMissing``,
        ``RowAlreadyExists``, ``RowMissing``, ``InvalidRow``, ``RegistryUnavailable`` and, for a
        change that no identity can make together with the others, ``WriteIdentityError``. On any
        error nothing is written.
        """
        ...

    async def append_metrics(self, rows: Sequence[MetricRow]) -> None:
        """Insert minute rows; a row for a (app, minute) already stored is ignored.

        The flusher can therefore send a batch again after a failure without double counting.
        """
        ...

    async def prune_metrics(self, *, older_than: AwareDatetime) -> int:
        """Delete the rows whose ``minute`` is before ``older_than``; returns how many."""
        ...

    async def read_metrics(
        self,
        *,
        app_slug: str | None,
        start: AwareDatetime,
        end: AwareDatetime,
        limit: int,
    ) -> tuple[MetricRow, ...]:
        """Rows with ``start <= minute < end`` (all apps when ``app_slug`` is ``None``).

        Ordered by minute, then app. At most ``limit`` rows (none when ``limit`` is below 1): when
        the window holds more, the OLDEST ROWS are dropped, so the oldest minute returned can be a
        partial one when several apps share it. The admin route owns the contract's "whole minutes"
        and ``truncated`` flag: it asks for ``limit + 1`` rows, treats more than ``limit`` rows as
        truncated, and drops the oldest minute it has only part of.
        """
        ...

    async def read_audit(
        self,
        *,
        app_slug: str | None,
        start: AwareDatetime,
        end: AwareDatetime,
        limit: int,
    ) -> tuple[AuditLogRow, ...]:
        """Audit rows with ``start <= ts < end`` (all apps when ``app_slug`` is ``None``).

        Newest first, at most ``limit`` rows (none when ``limit`` is below 1).
        """
        ...

    async def read_audit_of(
        self, *, actor: str, action: str, limit: int
    ) -> tuple[AuditLogRow, ...]:
        """The newest rows of one ``actor`` and ``action``, whatever their age, at most ``limit``.

        For a start-up decision that needs the last row of a kind (ADR 0022), not a window of
        time. Newest first; none when ``limit`` is below 1. A row the model cannot read is left out,
        as in ``read_audit``, so a caller asks for a few and takes the first it can use.
        """
        ...

    async def read_metric_steps(
        self,
        *,
        app_slug: str | None,
        start: AwareDatetime,
        end: AwareDatetime,
        step: timedelta,
        limit: int,
    ) -> tuple[MetricRow, ...]:
        """The minute rows of the window added up per app and ``step`` (``metric_steps.py``).

        A row's ``minute`` is the start of its step, a whole multiple of ``step`` from
        ``STEP_ORIGIN``; a step the window cuts holds only the minutes inside it. Ordered and
        limited as ``read_metrics``, with a step in place of a minute: the OLDEST steps are dropped.
        """
        ...

    async def read_metric_breakdown(
        self,
        *,
        start: AwareDatetime,
        end: AwareDatetime,
        step: timedelta,
        top: int,
    ) -> tuple[MetricBreakdownRow, ...]:
        """Per ``step``, the ``top`` apps by requests over the window, then the rest summed.

        The platform row is not an app. Apps rank by their requests in the window, most first,
        then by slug; the rest is one series with ``app_slug`` ``None``, every column summed across
        its apps step by step. Rows come series by series in rank order, oldest step first.
        """
        ...
