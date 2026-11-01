"""The psycopg registry client: ``Registry`` and ``RowSource`` over Lakebase (data model 6).

Two kinds of connection, as in the write-identity rule (``writers.py``):

* **The appmux service principal**: one long-lived connection, opened with the OAuth token that
  ``token_provider`` returns and opened again when the provider returns a different token. Lakebase
  checks a token only at login (spike 1), so an open connection keeps working after its token
  expires; the provider is asked before every operation, which is how a token minted anew is
  noticed. All reads, the snapshot reload, the metrics and every write the service principal may
  make run on it, one operation at a time (an ``asyncio.Lock``).
* **A platform admin**: ``as_platform_admin(token, role=...)`` logs in as the person with the
  person's own OBO token, runs one transaction and closes the connection. Nothing of it is kept.

No pool library is used (ADR 0014) and the Databricks SDK is not imported: the token is whatever the
injected ``token_provider`` returns. The connect parameters arrive as arguments and nothing is read
from the environment. The login is the one spike 1 used: SSL required, the role name as the user,
the token as the password, ``autocommit`` so that a transaction is an explicit ``transaction()``.

Every statement is parameterised. A table or column name is written into the SQL only after it
came from a row model of ``models.py``, from ``PRIMARY_KEYS`` or from a fixed list below, and is
quoted as an identifier; a value is never part of the SQL text. A token never appears in a message,
a repr or a log line; a log line names the exception class and its SQLSTATE, never the driver's
message, which for a constraint error repeats the row.

How a failure is told to the caller (``errors.py``):

======================================  =======================================================
SQLSTATE 42501 on a platform admin      ``RegistryRoleMissing``
SQLSTATE 42501 for the service          ``RegistryError`` (migrations/0002_grants.sql not applied)
login refused for a platform admin      ``RegistryRoleMissing`` (the role does not exist or the
                                        token is not accepted; spike 1 saw both as a refused login)
23505                                   ``RowAlreadyExists``
23503                                   ``RowMissing``
other class 23, class 22, and a value    ``InvalidRow`` (a value with a NUL character is refused
the driver refuses before sending        before any connection is opened)
versioned update, or update with an      ``ChangedByOthers`` (the row exists) or ``RowMissing``
``expected_status``, matching no row
40001, 40P01                            ``ChangedByOthers``
connection, operational and OS errors,  ``RegistryUnavailable``
and an operation past its deadline
======================================  =======================================================

Measured on 2026-10-30 (spikes 1 and 9): 42501, 23505, 23503 (at COMMIT, for the deferred foreign
keys of ``app_config``) and a refused login as ``OperationalError`` whose text says "authentication
failed" or "authentication token". Not measured, and mapped by the PostgreSQL error-code table
alone: 40001 and 40P01 (nothing here writes under a serializable isolation level, so they should not
occur), and every other class 22 and 23 code. A refused login has no SQLSTATE in psycopg, so it is
recognised by class 28 or by the word "authentication" in the driver message (inspected, never
copied).

A connection that may be in an unknown state (a lost connection, a cancelled or timed-out
operation, anything that is not an answered error) is closed and dropped, and the next operation
logs in again. An answered error (a conflict, a refused privilege) leaves it in use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal, NoReturn, Protocol, TypeVar

import psycopg
from psycopg import sql
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, SecretStr, ValidationError

from appmux.registry.audit import EMAIL_PATTERN, AuditEntry, insert_statement
from appmux.registry.errors import (
    ChangedByOthers,
    InvalidRow,
    RegistryError,
    RegistryRoleMissing,
    RegistryUnavailable,
    RowAlreadyExists,
    RowMissing,
)
from appmux.registry.models import ROW_MODELS, AuditLogRow, MetricRow, RowModel
from appmux.registry.protocol import (
    VERSIONED_UPDATES,
    Change,
    require_something_to_write,
    resolve_identity,
)
from appmux.registry.snapshot import (
    DEFAULT_POLL_INTERVAL_S,
    PRIMARY_KEYS,
    SNAPSHOT_TABLES,
    RegistrySnapshot,
    SnapshotLoader,
    _quarantine_key,
)
from appmux.registry.writers import WRITE_RULES, Identity

logger = logging.getLogger(__name__)

DEFAULT_OPERATION_TIMEOUT_S: Final = 30.0
DEFAULT_CONNECT_TIMEOUT_S: Final = 30  # spike 1's login used 30 seconds
# 500 rows of the 29 metric columns are 14500 parameters, far under the 65535 of the protocol.
_METRIC_CHUNK_ROWS: Final = 500

# The columns of each table that hold ``jsonb`` (migrations/0001_baseline.sql). A value for one of
# them is sent as JSON; ``tests/unit/test_registry_client.py`` compares this table with the
# migration. ``audit_log.detail`` is not here: audit rows are written by ``audit.insert_statement``.
JSONB_COLUMNS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "platform_settings": frozenset({"value"}),
        "apps": frozenset({"egress"}),
        "permission_requests": frozenset({"payload"}),
        "releases": frozenset({"detected", "config", "wheels"}),
        "metrics": frozenset({"egress_dropped_by_rule"}),
    }
)
# ``GENERATED ALWAYS`` columns: the database refuses a value for them, so a change may not name one.
GENERATED_COLUMNS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {"permission_requests": frozenset({"request_id"})}
)

_SERVICE_PRIVILEGE_MESSAGE: Final = (
    "the appmux service principal may not make this write; "
    "check that migrations/0002_grants.sql was applied"
)
_TIMEOUT_MESSAGE: Final = "the registry did not answer in time; try again"
_NO_LOGIN_MESSAGE: Final = "the registry has no database token right now; try again in a moment"
_SAFE_IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
# Class 22 (data) and class 23 (integrity) errors quote the offending row in their DETAIL, and so
# may a driver-side DataError.
_ROW_QUOTING_CLASSES: Final = frozenset({"22", "23"})

_T = TypeVar("_T")
_Row = TypeVar("_Row", bound=RowModel)

Query = sql.Composed | str


# --- the seam: what the client needs from a database connection ---------------------------------


class DatabaseCursor(Protocol):
    """The result of a statement: the rows it returned and the number it touched."""

    @property
    def rowcount(self) -> int: ...

    async def fetchall(self) -> Sequence[Mapping[str, object]]: ...


class DatabaseConnection(Protocol):
    """The part of ``psycopg.AsyncConnection`` the client uses; tests pass a fake."""

    @property
    def closed(self) -> bool: ...

    async def execute(
        self, query: Query, params: Sequence[object] | None = None
    ) -> DatabaseCursor: ...

    def transaction(self) -> AbstractAsyncContextManager[object]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ConnectionParameters:
    """Where and as whom to log in. The password is the OAuth token and is hidden from the repr."""

    host: str
    port: int
    database: str
    user: str
    password: SecretStr = field(repr=False)
    connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S


ConnectionFactory = Callable[[ConnectionParameters], Awaitable[DatabaseConnection]]
TokenProvider = Callable[[], Awaitable[SecretStr]]


class _PsycopgConnection:
    """``psycopg.AsyncConnection`` behind ``DatabaseConnection``.

    The wrapper exists because psycopg types a query as ``LiteralString``: the one place a ``str``
    is accepted (the audit INSERT of ``audit.py``, a module constant) is converted here.
    """

    def __init__(self, connection: psycopg.AsyncConnection[DictRow]) -> None:
        self._connection = connection

    @property
    def closed(self) -> bool:
        return self._connection.closed

    async def execute(self, query: Query, params: Sequence[object] | None = None) -> DatabaseCursor:
        # Only module constants reach this as a str; mypy treats LiteralString as str.
        statement = sql.SQL(query) if isinstance(query, str) else query
        return await self._connection.execute(statement, params)

    def transaction(self) -> AbstractAsyncContextManager[object]:
        return self._connection.transaction()

    async def close(self) -> None:
        await self._connection.close()


async def connect_with_psycopg(parameters: ConnectionParameters) -> DatabaseConnection:
    """Log in with psycopg: SSL required, ``autocommit``, rows as dicts (spike 1's login)."""
    connection = await psycopg.AsyncConnection.connect(
        host=parameters.host,
        port=parameters.port,
        dbname=parameters.database,
        user=parameters.user,
        password=parameters.password.get_secret_value(),
        sslmode="require",
        connect_timeout=parameters.connect_timeout_s,
        autocommit=True,
        row_factory=dict_row,
    )
    return _PsycopgConnection(connection)


# --- building statements ------------------------------------------------------------------------


def _column_list(columns: Sequence[str]) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(column) for column in columns)


def _equals_each(columns: Sequence[str]) -> sql.Composed:
    return sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in columns)


def _placeholders(count: int) -> sql.Composed:
    return sql.SQL(", ").join(sql.Placeholder() for _ in range(count))


def _json_value(value: object) -> object:
    """A value as plain JSON data: a model becomes what the caller set on it."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True)
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


def _dump_json(document: object) -> str:
    # allow_nan=False: NaN and Infinity are not JSON and Postgres would refuse them.
    return json.dumps(document, allow_nan=False)


def _has_nul(value: object) -> bool:
    """Whether a text anywhere in ``value`` holds a NUL character, which PostgreSQL cannot store."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, Mapping):
        return any(_has_nul(key) or _has_nul(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nul(item) for item in value)
    return False


def _refuse_nul(table: str, column: str, value: object) -> None:
    # The driver would refuse it client-side (psycopg.DataError) or the server in a jsonb value;
    # refusing here names the column and never touches a connection.
    if _has_nul(value):
        raise InvalidRow(f"the values do not fit {table}: {column} contains a NUL character")


def _parameter(table: str, column: str, value: object) -> object:
    """A value as the driver should send it; a ``jsonb`` column gets JSON, ``None`` stays NULL."""
    is_json = column in JSONB_COLUMNS.get(table, frozenset())
    document = _json_value(value) if is_json else value
    _refuse_nul(table, column, document)
    if value is None or not is_json:
        return value
    try:
        _dump_json(document)
    except (TypeError, ValueError):
        # No cause is chained: the driver's text for a bad value can quote the value.
        raise InvalidRow(f"the values do not fit {table}: {column} is not valid JSON") from None
    return Jsonb(document, dumps=_dump_json)


def _checked_columns(table: str, values: Mapping[str, object]) -> tuple[str, ...]:
    allowed = set(ROW_MODELS[table].model_fields) - GENERATED_COLUMNS.get(table, frozenset())
    if any(column not in allowed for column in values):
        # The offending name is not repeated: it came from the caller and goes into an error body.
        raise InvalidRow(
            f"the values do not fit {table}: a column is unknown or set by the database"
        )
    return tuple(values)


@dataclass(frozen=True, slots=True)
class _Statement:
    privilege: Literal["INSERT", "UPDATE", "DELETE"]
    table: str
    query: sql.Composed
    params: tuple[object, ...]
    # A versioned update that matched no row asks this to tell a stale version from a missing row.
    presence_query: sql.Composed | None = None
    presence_params: tuple[object, ...] = ()


def _statement_for(change: Change) -> _Statement:
    rule = WRITE_RULES[change.kind]
    table = rule.table
    model_columns = tuple(ROW_MODELS[table].model_fields)
    returning = _column_list(model_columns)
    columns = _checked_columns(table, change.values)
    values = tuple(_parameter(table, column, change.values[column]) for column in columns)
    if rule.privilege == "INSERT":
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
            sql.Identifier(table), _column_list(columns), _placeholders(len(columns)), returning
        )
        return _Statement("INSERT", table, query, values)

    key_columns = PRIMARY_KEYS[table]
    key_values = tuple(change.key[column] for column in key_columns)
    for column, key_value in zip(key_columns, key_values, strict=True):
        _refuse_nul(table, column, key_value)
    if rule.privilege == "DELETE":
        query = sql.SQL("DELETE FROM {} WHERE {}").format(
            sql.Identifier(table), _equals_each(key_columns)
        )
        return _Statement("DELETE", table, query, key_values)

    assignments = [sql.SQL("{} = %s").format(sql.Identifier(column)) for column in columns]
    # What the registry fills in (protocol.py): the caller states the columns it changes only.
    if "updated_at" in model_columns and "updated_at" not in change.values:
        assignments.append(sql.SQL("{} = now()").format(sql.Identifier("updated_at")))
    if table == "releases" and "status" in change.values and "status_at" not in change.values:
        assignments.append(sql.SQL("{} = now()").format(sql.Identifier("status_at")))
    if "version" in model_columns:
        assignments.append(sql.SQL("{} = version + 1").format(sql.Identifier("version")))
    where = _equals_each(key_columns)
    params = (*values, *key_values)
    # A precondition is a condition of the UPDATE itself, so that checking and writing are one step;
    # when nothing matched, the presence query tells a changed row from a missing one.
    preconditions: list[tuple[str, object]] = []
    if change.kind in VERSIONED_UPDATES:
        preconditions.append(("version", change.expected_version))
    if change.expected_status is not None:
        preconditions.append(("status", change.expected_status))
    presence_query = None
    if preconditions:
        presence_query = sql.SQL("SELECT 1 FROM {} WHERE {}").format(sql.Identifier(table), where)
        for column, expected in preconditions:
            where = sql.SQL("{} AND {} = %s").format(where, sql.Identifier(column))
            params = (*params, expected)
    query = sql.SQL("UPDATE {} SET {} WHERE {} RETURNING {}").format(
        sql.Identifier(table), sql.SQL(", ").join(assignments), where, returning
    )
    return _Statement("UPDATE", table, query, params, presence_query, key_values)


def _failed_fields(model: type[RowModel], error: ValidationError) -> str:
    """The columns a row failed on, from the model's own names: no value and no stored key."""
    names = {
        str(problem["loc"][0])
        if problem["loc"] and problem["loc"][0] in model.model_fields
        else "?"
        for problem in error.errors(include_url=False, include_context=False, include_input=False)
    }
    return ", ".join(sorted(names))


def _stored_row(table: str, row: Mapping[str, object]) -> RowModel:
    model = ROW_MODELS[table]
    try:
        return model.model_validate(row)
    except ValidationError as exc:
        raise InvalidRow(
            f"the stored {table} row does not fit its model: {_failed_fields(model, exc)}"
        ) from None


async def _run_statement(connection: DatabaseConnection, statement: _Statement) -> RowModel | None:
    cursor = await connection.execute(statement.query, statement.params)
    if statement.privilege == "DELETE":
        if cursor.rowcount < 1:
            raise RowMissing
        return None
    stored = await cursor.fetchall()
    if stored:
        return _stored_row(statement.table, stored[0])
    if statement.privilege == "INSERT":
        raise RegistryError("the database stored no row; nothing was written")
    if statement.presence_query is not None:
        present = await connection.execute(statement.presence_query, statement.presence_params)
        if await present.fetchall():
            raise ChangedByOthers
    raise RowMissing


async def _apply_all(
    connection: DatabaseConnection,
    statements: Sequence[_Statement],
    audit_statements: Sequence[tuple[str, tuple[object, ...]]],
) -> tuple[RowModel | None, ...]:
    rows = [await _run_statement(connection, statement) for statement in statements]
    for query, params in audit_statements:
        await connection.execute(query, params)
    return tuple(rows)


async def _apply_in_transaction(
    connection: DatabaseConnection,
    statements: Sequence[_Statement],
    audit_statements: Sequence[tuple[str, tuple[object, ...]]],
) -> tuple[RowModel | None, ...]:
    async with connection.transaction():
        return await _apply_all(connection, statements, audit_statements)


def _admin_role(audit: Sequence[AuditEntry]) -> str:
    """The database role of the platform admin: the one email among the audit entries' actors.

    A platform admin's Postgres role is their email, which is also the actor of the audit entry of
    their action; entries of the system itself (``appmux``) do not count.
    """
    people = {entry.actor for entry in audit if re.fullmatch(EMAIL_PATTERN, entry.actor)}
    if len(people) != 1:
        raise ValueError(
            "a platform-admin write needs audit entries that name exactly one person "
            "(the admin's email as actor)"
        )
    return next(iter(people))


_AUDIT_INSERT_COLUMNS: Final = ("actor", "action", "app_slug", "detail", "reason")


def _audit_statement(entry: AuditEntry) -> tuple[str, tuple[object, ...]]:
    query, params = insert_statement(entry)
    for column, value in zip(_AUDIT_INSERT_COLUMNS, params, strict=True):
        _refuse_nul("audit_log", column, value)
    return query, params


# --- mapping failures ---------------------------------------------------------------------------


def _safe_identifier(name: str | None) -> str | None:
    return name if name is not None and _SAFE_IDENTIFIER.fullmatch(name) else None


def _invalid_row_message(exc: psycopg.Error) -> str:
    """Name the table and the constraint or column of a class 23 error, never a value."""
    diag = exc.diag
    table = _safe_identifier(diag.table_name)
    message = "the values do not fit " + (table if table in ROW_MODELS else "the table")
    column, constraint = _safe_identifier(diag.column_name), _safe_identifier(diag.constraint_name)
    if exc.sqlstate == "23502" and column is not None:
        return f"{message}: {column} is required"
    if constraint is not None:
        return f"{message}: {constraint}"
    return message


def _map_error(exc: psycopg.Error | OSError, identity: Identity | None) -> RegistryError:
    """The registry error for a driver error; ``identity`` is ``None`` for a read."""
    if isinstance(exc, OSError) or identity is None:
        return RegistryUnavailable()
    sqlstate = exc.sqlstate or ""
    if sqlstate == "42501":
        if identity is Identity.PLATFORM_ADMIN:
            return RegistryRoleMissing()
        return RegistryError(_SERVICE_PRIVILEGE_MESSAGE)
    if sqlstate == "23505":
        return RowAlreadyExists()
    if sqlstate == "23503":
        return RowMissing()
    if sqlstate.startswith("23"):
        return InvalidRow(_invalid_row_message(exc))
    if sqlstate.startswith("22"):
        return InvalidRow()
    # The driver refuses some values before sending them (a NUL in a text), with no SQLSTATE.
    if not sqlstate and isinstance(exc, psycopg.DataError | psycopg.IntegrityError):
        return InvalidRow()
    # Before the generic operational case: psycopg makes these two OperationalError subclasses.
    if sqlstate in ("40001", "40P01"):
        return ChangedByOthers()
    if isinstance(exc, psycopg.OperationalError | psycopg.InterfaceError):
        return RegistryUnavailable()
    return RegistryError()


def _reraise(exc: psycopg.Error | OSError, identity: Identity | None) -> NoReturn:
    mapped = _map_error(exc, identity)
    sqlstate = exc.sqlstate if isinstance(exc, psycopg.Error) else None
    level = (
        logging.WARNING
        if isinstance(mapped, RegistryUnavailable) or type(mapped) is RegistryError
        else logging.INFO
    )
    # The class and SQLSTATE say what happened; the driver's message is left out of the log.
    logger.log(
        level,
        "registry operation failed: %s sqlstate=%s, reported as %s",
        type(exc).__name__,
        sqlstate,
        mapped.code,
    )
    quotes_row = (sqlstate is not None and sqlstate[:2] in _ROW_QUOTING_CLASSES) or isinstance(
        exc, psycopg.DataError | psycopg.IntegrityError
    )
    raise mapped from (None if quotes_row else exc)


def _login_failure(exc: psycopg.Error | OSError, *, admin: bool) -> RegistryError:
    """A refused login of a platform admin means the role is missing; any other failure is down."""
    refused = isinstance(exc, psycopg.Error) and (
        (exc.sqlstate or "").startswith("28") or "authentication" in str(exc).lower()
    )
    logger.warning(
        "registry login failed: %s sqlstate=%s, refused=%s, reason: %s",
        type(exc).__name__,
        exc.sqlstate if isinstance(exc, psycopg.Error) else None,
        refused,
        _login_failure_reason(exc),
    )
    return RegistryRoleMissing() if admin and refused else RegistryUnavailable()


# A fixed vocabulary on purpose. libpq reports a failed connect from the client side, so there is
# no sqlstate and the text is the only clue. That text is never logged: a wrapper might repeat a
# token in it (test_no_token_reaches_a_message_a_repr_or_the_log), so it is only matched here.
_LOGIN_FAILURE_REASONS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("the database does not exist", ("database", "does not exist")),
    ("the role does not exist", ("role", "does not exist")),
    ("the server refused the token or password", ("authentication",)),
    ("the host name did not resolve", ("translate host name",)),
    ("the host name did not resolve", ("name or service not known",)),
    ("the connection was refused", ("connection refused",)),
    ("the connection timed out", ("timeout expired",)),
    ("the connection timed out", ("timed out",)),
    ("the server needs a different SSL setting", ("ssl",)),
    ("the server's access rules refuse this client", ("pg_hba.conf",)),
)
_UNKNOWN_LOGIN_FAILURE_REASON: Final = "not one of the known reasons"


def _login_failure_reason(exc: BaseException) -> str:
    text = str(exc).lower()
    for reason, needles in _LOGIN_FAILURE_REASONS:
        if all(needle in text for needle in needles):
            return reason
    return _UNKNOWN_LOGIN_FAILURE_REASON


def _leaves_connection_in_doubt(exc: BaseException) -> bool:
    """False only for an error the server answered, after which the connection is still sound."""
    if isinstance(exc, RegistryUnavailable):
        return True
    return not isinstance(
        exc, RegistryError | psycopg.IntegrityError | psycopg.DataError | psycopg.ProgrammingError
    )


# --- the window queries -------------------------------------------------------------------------


def _window_conditions(
    time_column: str, app_slug: str | None, start: AwareDatetime, end: AwareDatetime
) -> tuple[sql.Composed, list[object]]:
    conditions = [
        sql.SQL("{} >= %s").format(sql.Identifier(time_column)),
        sql.SQL("{} < %s").format(sql.Identifier(time_column)),
    ]
    params: list[object] = [start, end]
    if app_slug is not None:
        conditions.append(sql.SQL("{} = %s").format(sql.Identifier("app_slug")))
        params.append(app_slug)
    return sql.SQL(" AND ").join(conditions), params


def _metric_insert(rows: Sequence[MetricRow]) -> tuple[sql.Composed, list[object]]:
    columns = tuple(MetricRow.model_fields)
    one_row = sql.SQL("({})").format(_placeholders(len(columns)))
    query = sql.SQL("INSERT INTO {} ({}) VALUES {} ON CONFLICT ({}) DO NOTHING").format(
        sql.Identifier("metrics"),
        _column_list(columns),
        sql.SQL(", ").join(one_row for _ in rows),
        _column_list(PRIMARY_KEYS["metrics"]),
    )
    params = [
        _parameter("metrics", column, getattr(row, column)) for row in rows for column in columns
    ]
    return query, params


class RegistryClient:
    """The registry over psycopg: ``Registry`` (protocol.py) and ``RowSource`` (snapshot.py).

    ``host``, ``port``, ``database`` and ``user`` (the service principal's role) are connection
    parameters; ``token_provider`` returns the service principal's current OAuth token and is asked
    before every operation, so it should answer from a cache. ``connection_factory`` opens a
    connection and is replaced by a fake in tests. ``operation_timeout_s`` bounds one operation
    including the wait for the connection; past it the operation raises ``RegistryUnavailable`` and
    its connection is closed.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        token_provider: TokenProvider,
        connection_factory: ConnectionFactory = connect_with_psycopg,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        operation_timeout_s: float = DEFAULT_OPERATION_TIMEOUT_S,
        connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S,
    ) -> None:
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._token_provider = token_provider
        self._connection_factory = connection_factory
        self._operation_timeout_s = operation_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._loader = SnapshotLoader(self, poll_interval_s=poll_interval_s)
        self._connection: DatabaseConnection | None = None
        self._login_token: SecretStr | None = None
        # One operation at a time on the service principal's connection. It is not reentrant:
        # nothing that holds it calls refresh() or another operation of this client.
        self._lock = asyncio.Lock()
        self._last_error: str | None = None
        self._unreadable_rows = 0

    # --- Registry: the snapshot -----------------------------------------------------------------

    @property
    def snapshot(self) -> RegistrySnapshot:
        return self._loader.snapshot

    @property
    def last_error(self) -> str | None:
        """Why the latest operation failed, or the loader's own reason; ``None`` when it worked."""
        return self._last_error if self._last_error is not None else self._loader.last_error

    @property
    def unreadable_rows(self) -> int:
        """How many audit and metric rows reads have skipped because they fail their model."""
        return self._unreadable_rows

    async def refresh(self) -> None:
        await self._loader.refresh()

    async def poll(self) -> None:
        """Reload every interval until cancelled (``SnapshotLoader.poll``)."""
        await self._loader.poll()

    async def fetch_rows(self, table: str) -> Sequence[Mapping[str, object]]:
        """Every row of one snapshot table as a column-to-value mapping (``RowSource``)."""
        if table not in SNAPSHOT_TABLES:
            raise ValueError(f"{table!r} is not a snapshot table")
        query = sql.SQL("SELECT {} FROM {}").format(
            _column_list(tuple(ROW_MODELS[table].model_fields)), sql.Identifier(table)
        )
        return await self._read(query, ())

    # --- Registry: writes -----------------------------------------------------------------------

    async def write(
        self,
        changes: Sequence[Change],
        audit: Sequence[AuditEntry] = (),
        *,
        admin_token: SecretStr | None = None,
    ) -> tuple[RowModel | None, ...]:
        """Apply ``changes`` and ``audit`` in one transaction, then refresh the snapshot.

        ``changes`` may be empty when ``audit`` is not: the audit rows are then written alone (an
        action that changes no row), as the service principal unless ``admin_token`` is given, and
        the snapshot is not reloaded. Both empty is a ``ValueError``.

        Under the service principal unless ``admin_token`` is given and the changes need a platform
        admin. The admin's database role is their email, taken from the audit entries: they must
        name exactly one person, and entries with the actor ``appmux`` do not count. Everything
        that can be refused without a connection is refused before one is opened.
        """
        require_something_to_write(changes, audit)
        # An empty token is no token: it cannot log in, and an admin-only change must not try.
        token = admin_token if admin_token is not None and admin_token.get_secret_value() else None
        identity = resolve_identity(changes, token)
        statements = [_statement_for(change) for change in changes]
        audit_statements = [_audit_statement(entry) for entry in audit]
        if token is None:
            rows = await self._guarded(
                self._on_service(
                    lambda connection: _apply_in_transaction(
                        connection, statements, audit_statements
                    )
                ),
                identity=identity,
            )
        else:
            role = _admin_role(audit)
            rows = await self._guarded(
                self._write_as_admin(token, role, statements, audit_statements), identity=identity
            )
        if not changes:
            return rows  # an audit-only write changed no row: the snapshot is as it was
        try:
            await self.refresh()
        except RegistryUnavailable:
            # The write is committed: reporting it as failed would make the caller repeat it. The
            # loader has recorded last_error and the next poll reloads.
            logger.warning("registry write committed; the snapshot reload after it failed")
        return rows

    async def _write_as_admin(
        self,
        token: SecretStr,
        role: str,
        statements: Sequence[_Statement],
        audit_statements: Sequence[tuple[str, tuple[object, ...]]],
    ) -> tuple[RowModel | None, ...]:
        async with self.as_platform_admin(token, role=role) as connection:
            return await _apply_all(connection, statements, audit_statements)

    @asynccontextmanager
    async def as_platform_admin(
        self, token: SecretStr, *, role: str
    ) -> AsyncIterator[DatabaseConnection]:
        """Log in as the platform admin ``role`` with their own OBO ``token``, for one transaction.

        The connection is closed when the block ends, whatever ended it. A driver error inside the
        block leaves as a ``RegistryError``; a refused login is ``RegistryRoleMissing``.
        """
        if not token.get_secret_value() or not role:
            raise RegistryRoleMissing
        connection = await self._connect(self._parameters(role, token), admin=True)
        try:
            async with connection.transaction():
                yield connection
        except (psycopg.Error, OSError) as exc:
            _reraise(exc, Identity.PLATFORM_ADMIN)
        finally:
            await connection.close()

    # --- Registry: metrics and audit ------------------------------------------------------------

    async def append_metrics(self, rows: Sequence[MetricRow]) -> None:
        """Insert minute rows in chunks of one transaction; a repeated (app, minute) is ignored."""
        if not rows:
            return

        async def insert(connection: DatabaseConnection) -> None:
            async with connection.transaction():
                for first in range(0, len(rows), _METRIC_CHUNK_ROWS):
                    query, params = _metric_insert(rows[first : first + _METRIC_CHUNK_ROWS])
                    await connection.execute(query, params)

        await self._guarded(self._on_service(insert), identity=Identity.APPMUX_SP)

    async def prune_metrics(self, *, older_than: AwareDatetime) -> int:
        query = sql.SQL("DELETE FROM {} WHERE {} < %s").format(
            sql.Identifier("metrics"), sql.Identifier("minute")
        )

        async def delete(connection: DatabaseConnection) -> int:
            return (await connection.execute(query, (older_than,))).rowcount

        return await self._guarded(self._on_service(delete), identity=Identity.APPMUX_SP)

    async def read_metrics(
        self, *, app_slug: str | None, start: AwareDatetime, end: AwareDatetime, limit: int
    ) -> tuple[MetricRow, ...]:
        """The newest ``limit`` rows of the window, oldest minute first."""
        if limit <= 0:
            return ()
        conditions, params = _window_conditions("minute", app_slug, start, end)
        columns = _column_list(tuple(MetricRow.model_fields))
        order = sql.SQL("{}, {}").format(sql.Identifier("minute"), sql.Identifier("app_slug"))
        query = sql.SQL(
            "SELECT {columns} FROM (SELECT {columns} FROM {table} WHERE {conditions} "
            "ORDER BY {minute} DESC, {slug} DESC LIMIT %s) AS {newest} ORDER BY {order}"
        ).format(
            columns=columns,
            table=sql.Identifier("metrics"),
            conditions=conditions,
            minute=sql.Identifier("minute"),
            slug=sql.Identifier("app_slug"),
            newest=sql.Identifier("newest"),
            order=order,
        )
        rows = await self._read(query, (*params, limit))
        return self._readable(MetricRow, "metrics", rows)

    async def read_audit(
        self, *, app_slug: str | None, start: AwareDatetime, end: AwareDatetime, limit: int
    ) -> tuple[AuditLogRow, ...]:
        """Audit rows of the window, newest first, at most ``limit``."""
        if limit <= 0:
            return ()
        conditions, params = _window_conditions("ts", app_slug, start, end)
        query = sql.SQL(
            "SELECT {columns} FROM {table} WHERE {conditions} "
            "ORDER BY {ts} DESC, {id} DESC LIMIT %s"
        ).format(
            columns=_column_list(tuple(AuditLogRow.model_fields)),
            table=sql.Identifier("audit_log"),
            conditions=conditions,
            ts=sql.Identifier("ts"),
            id=sql.Identifier("id"),
        )
        rows = await self._read(query, (*params, limit))
        return self._readable(AuditLogRow, "audit_log", rows)

    def _readable(
        self, model: type[_Row], table: str, rows: Sequence[Mapping[str, object]]
    ) -> tuple[_Row, ...]:
        """The rows that fit ``model``; one that does not is skipped, counted and logged.

        A holder of the shared-SP token can insert a malformed ``jsonb`` value into ``audit_log`` or
        ``metrics``, and no appmux role can delete an audit row (data model 6). Failing the whole
        read would let one row blind the view of its period, so, as the snapshot does, the row is
        left out and only its table and key are logged, never its content.
        """
        readable: list[_Row] = []
        for row in rows:
            try:
                readable.append(model.model_validate(row))
            except ValidationError:
                self._unreadable_rows += 1
                logger.warning(
                    "registry row quarantined (fails its model): table=%s key=%s",
                    table,
                    _quarantine_key(table, row),
                )
        return tuple(readable)

    async def close(self) -> None:
        """Close the service principal's connection; a later operation logs in again."""
        async with self._lock:
            await self._drop_service_connection()

    # --- the connections ------------------------------------------------------------------------

    def _parameters(self, user: str, token: SecretStr) -> ConnectionParameters:
        return ConnectionParameters(
            host=self._host,
            port=self._port,
            database=self._database,
            user=user,
            password=token,
            connect_timeout_s=self._connect_timeout_s,
        )

    async def _connect(
        self, parameters: ConnectionParameters, *, admin: bool
    ) -> DatabaseConnection:
        try:
            return await self._connection_factory(parameters)
        except (psycopg.Error, OSError) as exc:
            # The cause stays chained: a connect failure's text (host, "connection refused") is what
            # an operator needs, and libpq never puts the password in it.
            raise _login_failure(exc, admin=admin) from exc

    async def _service_token(self) -> SecretStr:
        try:
            token = await self._token_provider()
        except Exception as exc:
            # The provider is injected code with exceptions of its own, and any failure means there
            # is no login. Its message may quote a credential, so only the class is logged and no
            # cause is chained.
            logger.warning("registry token provider failed: %s", type(exc).__name__)
            raise RegistryUnavailable(_NO_LOGIN_MESSAGE) from None
        if not token.get_secret_value():
            raise RegistryUnavailable(_NO_LOGIN_MESSAGE)
        return token

    async def _service_connection(self) -> DatabaseConnection:
        """The service principal's connection, logged in again if its token was minted anew."""
        token = await self._service_token()
        connection = self._connection
        if (
            connection is not None
            and not connection.closed
            and self._login_token is not None
            and self._login_token.get_secret_value() == token.get_secret_value()
        ):
            return connection
        await self._drop_service_connection()
        opened = await self._connect(self._parameters(self._user, token), admin=False)
        self._connection = opened
        self._login_token = token
        return opened

    async def _drop_service_connection(self) -> None:
        connection, self._connection, self._login_token = self._connection, None, None
        if connection is None or connection.closed:
            return
        try:
            await connection.close()
        except (psycopg.Error, OSError):
            # Closing a connection that is already broken: there is nothing left to release.
            logger.debug("closing the registry connection failed; it is dropped anyway")

    # --- running an operation -------------------------------------------------------------------

    async def _on_service(self, work: Callable[[DatabaseConnection], Awaitable[_T]]) -> _T:
        """Run ``work`` on the service principal's connection, alone, dropping it if in doubt."""
        async with self._lock:
            connection = await self._service_connection()
            try:
                return await work(connection)
            except BaseException as exc:
                # BaseException on purpose: a cancelled or timed-out statement leaves the
                # connection in an unknown state, and it is re-raised unchanged.
                if _leaves_connection_in_doubt(exc):
                    await self._drop_service_connection()
                raise

    async def _read(
        self, query: sql.Composed, params: Sequence[object]
    ) -> list[Mapping[str, object]]:
        async def select(connection: DatabaseConnection) -> list[Mapping[str, object]]:
            return list(await (await connection.execute(query, params or None)).fetchall())

        return await self._guarded(self._on_service(select), identity=None)

    async def _guarded(self, operation: Awaitable[_T], *, identity: Identity | None) -> _T:
        """Bound ``operation`` by the deadline, turn driver errors into registry errors, and keep
        ``last_error`` (``identity`` is ``None`` for a read)."""
        try:
            try:
                async with asyncio.timeout(self._operation_timeout_s):
                    result = await operation
            except TimeoutError as exc:
                raise RegistryUnavailable(_TIMEOUT_MESSAGE) from exc
            except (psycopg.Error, OSError) as exc:
                _reraise(exc, identity)
        except RegistryError as failure:
            # Only "cannot serve" failures are the registry's health; a conflict or a refused
            # privilege means the database answered.
            unhealthy = isinstance(failure, RegistryUnavailable) or type(failure) is RegistryError
            self._last_error = failure.message if unhealthy else None
            raise
        self._last_error = None
        return result
