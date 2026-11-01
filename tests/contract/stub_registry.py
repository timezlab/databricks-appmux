"""An in-memory ``Registry`` for contract tests of the admin routes, the reconciler and the edge.

A test double, not a database: no SQL is emulated. It keeps what a route can observe and a test can
get wrong by accident:

* the write-identity table (``resolve_identity``): a platform kind without an admin token raises
  ``RegistryRoleMissing``, an SP-only kind given a token raises ``WriteIdentityError``;
* ``version`` on ``apps`` and ``app_config``: a stale ``expected_version`` raises
  ``ChangedByOthers``;
* the row models (a value that breaks one raises ``InvalidRow``, without echoing the value) and the
  keys that matter to the routes: a duplicate primary key, a child of an unknown app, one pending
  request per (app or proposed slug, kind), one active release per app, and a rename or delete of an
  app following into its child tables;
* one ``audit_log`` row per ``AuditEntry`` passed to ``write``, in the same all-or-nothing step as
  the changes (a failing change leaves no row and no audit entry).

It does not emulate the deferred foreign keys of ``app_config`` (target release, own-SP keys),
``CHECK``s the models do not repeat, or concurrency between writers.

Test helpers beyond the ``Registry`` protocol: ``seed``, ``audit_rows``, ``writes``, ``go_down``
and ``come_back``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from pydantic import AwareDatetime, SecretStr, ValidationError

from appmux.registry.audit import AuditEntry
from appmux.registry.errors import (
    ChangedByOthers,
    InvalidRow,
    RegistryRoleMissing,
    RegistryUnavailable,
    RowAlreadyExists,
    RowMissing,
)
from appmux.registry.models import (
    ROW_MODELS,
    App,
    AppConfig,
    AuditLogRow,
    MetricRow,
    PermissionRequest,
    Release,
    RowModel,
)
from appmux.registry.protocol import (
    VERSIONED_UPDATES,
    Change,
    require_something_to_write,
    resolve_identity,
)
from appmux.registry.snapshot import (
    PRIMARY_KEYS,
    SNAPSHOT_TABLES,
    RegistrySnapshot,
    build_snapshot,
)
from appmux.registry.writers import WRITE_RULES, Identity, WriteKind

# The tables whose rows belong to an app and follow its slug (ON UPDATE / ON DELETE CASCADE).
_CHILD_TABLES: Final = (
    "permission_requests",
    "app_config",
    "app_grants",
    "releases",
    "app_resources",
)
# Columns the database fills with now() when an insert leaves them out.
_INSERT_TIMESTAMPS: Final = (
    "created_at",
    "updated_at",
    "requested_at",
    "granted_at",
    "status_at",
    "attached_at",
)

_Key = tuple[object, ...]
_Tables = dict[str, dict[_Key, RowModel]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class WriteRecord:
    """One write that was applied: the kinds of its changes and the identity it ran under.

    ``admin_token`` is the platform admin's token the caller passed (``None`` for a write made as
    the appmux service principal), so a test can tell which identity a route chose.
    """

    kinds: tuple[WriteKind, ...]
    identity: Identity
    admin_token: SecretStr | None


def _key_of(table: str, row: RowModel) -> _Key:
    return tuple(getattr(row, column) for column in PRIMARY_KEYS[table])


def _fields_of(row: RowModel) -> dict[str, object]:
    # Not model_dump(): the instances stay as they are, so a union-typed value cannot change type.
    return {name: getattr(row, name) for name in type(row).model_fields}


def _validated(table: str, values: Mapping[str, object]) -> RowModel:
    try:
        return ROW_MODELS[table].model_validate(values)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        # `from None`: pydantic's message repeats the rejected input, which may hold a secret.
        raise InvalidRow(f"the values do not fit {table}: {', '.join(fields)}") from None


class StubRegistry:
    """The ``Registry`` protocol over dictionaries.

    ``platform_writer_tokens`` stands for the members of ``appmux_platform_writers``: ``None`` lets
    every admin token write platform data, a collection of token values lets only those do and
    raises ``RegistryRoleMissing`` for any other.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        platform_writer_tokens: Collection[str] | None = None,
    ) -> None:
        self._clock = clock
        self._writer_tokens = platform_writer_tokens
        self._rows: _Tables = {table: {} for table in SNAPSHOT_TABLES}
        self._audit: list[AuditLogRow] = []
        self._writes: list[WriteRecord] = []
        self._metrics: dict[tuple[str, datetime], MetricRow] = {}
        self._next_request_id = 1
        self._next_audit_id = 1
        self._is_down = False
        self._last_error: str | None = None
        self._snapshot = self._build_snapshot()

    # --- Registry: reads ---------------------------------------------------------------------

    @property
    def snapshot(self) -> RegistrySnapshot:
        return self._snapshot

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def refresh(self) -> None:
        if self._is_down:
            self._last_error = RegistryUnavailable.default_message
            return
        self._snapshot = self._build_snapshot()
        self._last_error = None

    async def read_audit(
        self,
        *,
        app_slug: str | None,
        start: AwareDatetime,
        end: AwareDatetime,
        limit: int,
    ) -> tuple[AuditLogRow, ...]:
        self._require_up()
        rows = [
            row
            for row in self._audit
            if start <= row.ts < end and (app_slug is None or row.app_slug == app_slug)
        ]
        newest_first = sorted(rows, key=lambda row: row.id, reverse=True)
        return tuple(newest_first[:limit]) if limit > 0 else ()

    async def read_metrics(
        self,
        *,
        app_slug: str | None,
        start: AwareDatetime,
        end: AwareDatetime,
        limit: int,
    ) -> tuple[MetricRow, ...]:
        self._require_up()
        rows = sorted(
            (
                row
                for row in self._metrics.values()
                if start <= row.minute < end and (app_slug is None or row.app_slug == app_slug)
            ),
            key=lambda row: (row.minute, row.app_slug),
        )
        return tuple(rows[-limit:]) if limit > 0 else ()

    # --- Registry: metrics writes ------------------------------------------------------------

    async def append_metrics(self, rows: Sequence[MetricRow]) -> None:
        self._require_up()
        for row in rows:
            self._metrics.setdefault((row.app_slug, row.minute), row)

    async def prune_metrics(self, *, older_than: AwareDatetime) -> int:
        self._require_up()
        doomed = [key for key, row in self._metrics.items() if row.minute < older_than]
        for key in doomed:
            del self._metrics[key]
        return len(doomed)

    # --- Registry: write ---------------------------------------------------------------------

    async def write(
        self,
        changes: Sequence[Change],
        audit: Sequence[AuditEntry] = (),
        *,
        admin_token: SecretStr | None = None,
    ) -> tuple[RowModel | None, ...]:
        require_something_to_write(changes, audit)  # a caller bug is not an outage
        self._require_up()
        identity = resolve_identity(changes, admin_token)
        if identity is Identity.PLATFORM_ADMIN and admin_token is not None:
            self._require_platform_writer(admin_token)

        # All-or-nothing: work on copies and keep them only when every change and entry applied.
        working: _Tables = {table: dict(rows) for table, rows in self._rows.items()}
        now = self._clock()
        next_request_id = self._next_request_id
        results: list[RowModel | None] = []
        for change in changes:
            values = change.values
            rule = WRITE_RULES[change.kind]
            if rule.table == "permission_requests" and rule.privilege == "INSERT":
                if "request_id" in values:
                    raise InvalidRow("the values do not fit permission_requests: request_id")
                values = {**values, "request_id": next_request_id}
                next_request_id += 1
            results.append(self._apply(working, change, values, now))
        audit_rows = self._audit_rows_for(audit, now)

        self._rows = working
        self._next_request_id = next_request_id
        self._audit.extend(audit_rows)
        self._writes.append(
            WriteRecord(tuple(change.kind for change in changes), identity, admin_token)
        )
        self._next_audit_id += len(audit_rows)
        self._snapshot = self._build_snapshot()
        return tuple(results)

    # --- test helpers ------------------------------------------------------------------------

    @property
    def audit_rows(self) -> tuple[AuditLogRow, ...]:
        """Every audit row written so far, oldest first."""
        return tuple(self._audit)

    @property
    def writes(self) -> tuple[WriteRecord, ...]:
        """Every write applied so far, oldest first (a write that raised leaves no record)."""
        return tuple(self._writes)

    def seed(self, table: str, values: Mapping[str, object]) -> RowModel:
        """Put a row in without identity, foreign-key or audit checks (a test's starting data)."""
        row = _validated(table, values)
        self._rows[table][_key_of(table, row)] = row
        if isinstance(row, PermissionRequest):
            self._next_request_id = max(self._next_request_id, row.request_id + 1)
        self._snapshot = self._build_snapshot()
        return row

    def go_down(self) -> None:
        """Lakebase stops answering: writes and the other reads raise ``RegistryUnavailable``."""
        self._is_down = True
        self._last_error = RegistryUnavailable.default_message

    def come_back(self) -> None:
        self._is_down = False
        self._last_error = None
        self._snapshot = self._build_snapshot()

    # --- internals ---------------------------------------------------------------------------

    def _require_up(self) -> None:
        if self._is_down:
            raise RegistryUnavailable

    def _require_platform_writer(self, token: SecretStr) -> None:
        if self._writer_tokens is not None and token.get_secret_value() not in self._writer_tokens:
            raise RegistryRoleMissing

    def _build_snapshot(self) -> RegistrySnapshot:
        return build_snapshot(
            {table: list(rows.values()) for table, rows in self._rows.items()},
            loaded_at=self._clock(),
        )

    def _apply(
        self, tables: _Tables, change: Change, values: Mapping[str, object], now: datetime
    ) -> RowModel | None:
        rule = WRITE_RULES[change.kind]
        table = rule.table
        if rule.privilege == "INSERT":
            return self._insert(tables, table, values, now)
        key = tuple(change.key[column] for column in PRIMARY_KEYS[table])
        current = tables[table].get(key)
        if current is None:
            raise RowMissing
        if rule.privilege == "DELETE":
            self._delete(tables, table, key, current)
            return None
        return self._update(tables, change, current, values, now)

    def _insert(
        self, tables: _Tables, table: str, values: Mapping[str, object], now: datetime
    ) -> RowModel:
        fields = ROW_MODELS[table].model_fields
        stamped: dict[str, object] = {c: now for c in _INSERT_TIMESTAMPS if c in fields}
        row = _validated(table, {**stamped, **values})
        self._check_parent(tables, table, row)
        key = _key_of(table, row)
        if key in tables[table]:
            raise RowAlreadyExists
        self._check_unique_indexes(tables, table, row, ignoring=None)
        tables[table][key] = row
        return row

    def _update(
        self,
        tables: _Tables,
        change: Change,
        current: RowModel,
        values: Mapping[str, object],
        now: datetime,
    ) -> RowModel:
        table = WRITE_RULES[change.kind].table
        merged = {**_fields_of(current), **values}
        if change.kind in VERSIONED_UPDATES and isinstance(current, App | AppConfig):
            if current.version != change.expected_version:
                raise ChangedByOthers
            merged["version"] = current.version + 1
        if (
            change.expected_status is not None
            and isinstance(current, PermissionRequest)
            and current.status != change.expected_status
        ):
            raise ChangedByOthers
        fields = type(current).model_fields
        if "updated_at" in fields:
            merged["updated_at"] = now
        if table == "releases" and "status" in values and "status_at" not in values:
            merged["status_at"] = now
        row = _validated(table, merged)

        old_key = _key_of(table, current)
        new_key = _key_of(table, row)
        if new_key != old_key:
            if table != "apps":
                raise InvalidRow(f"the values do not fit {table}: the key cannot change")
            if new_key in tables[table]:
                raise RowAlreadyExists
        self._check_unique_indexes(tables, table, row, ignoring=old_key)
        del tables[table][old_key]
        tables[table][new_key] = row
        if new_key != old_key:
            self._rename_children(tables, str(old_key[0]), str(new_key[0]))
        return row

    def _delete(self, tables: _Tables, table: str, key: _Key, current: RowModel) -> None:
        del tables[table][key]
        if table == "apps":
            slug = str(key[0])
            for child_table in _CHILD_TABLES:
                tables[child_table] = {
                    child_key: child
                    for child_key, child in tables[child_table].items()
                    if getattr(child, "app_slug", None) != slug
                }

    def _rename_children(self, tables: _Tables, old_slug: str, new_slug: str) -> None:
        for child_table in _CHILD_TABLES:
            renamed: dict[_Key, RowModel] = {}
            for child_key, child in tables[child_table].items():
                if getattr(child, "app_slug", None) == old_slug:
                    child = child.model_copy(update={"app_slug": new_slug})
                    child_key = _key_of(child_table, child)
                renamed[child_key] = child
            tables[child_table] = renamed

    def _check_parent(self, tables: _Tables, table: str, row: RowModel) -> None:
        if table not in _CHILD_TABLES:
            return
        slug = getattr(row, "app_slug", None)
        if slug is not None and (slug,) not in tables["apps"]:
            raise RowMissing

    def _check_unique_indexes(
        self, tables: _Tables, table: str, row: RowModel, ignoring: _Key | None
    ) -> None:
        """``one_pending_request`` and ``one_active_release`` of the baseline migration."""
        if isinstance(row, PermissionRequest) and row.status == "pending":
            wanted = _pending_slot(row)
            for key, other in tables[table].items():
                if (
                    key != ignoring
                    and isinstance(other, PermissionRequest)
                    and other.status == "pending"
                    and _pending_slot(other) == wanted
                ):
                    raise RowAlreadyExists
        if isinstance(row, Release) and row.status == "active":
            for key, other in tables[table].items():
                if (
                    key != ignoring
                    and isinstance(other, Release)
                    and other.app_slug == row.app_slug
                    and other.status == "active"
                ):
                    raise RowAlreadyExists

    def _audit_rows_for(self, entries: Sequence[AuditEntry], now: datetime) -> list[AuditLogRow]:
        rows: list[AuditLogRow] = []
        for offset, entry in enumerate(entries):
            detail = json.loads(
                entry.row_detail().model_dump_json(by_alias=True, exclude_none=True)
            )
            rows.append(
                AuditLogRow(
                    id=self._next_audit_id + offset,
                    ts=now,
                    actor=entry.actor,
                    action=entry.action,
                    app_slug=entry.app_slug,
                    detail=detail,
                    reason=entry.reason,
                )
            )
        return rows


def _pending_slot(request: PermissionRequest) -> tuple[str, str]:
    return (request.app_slug or str(request.payload.slug), request.kind)
