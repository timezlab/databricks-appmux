"""The psycopg registry client, tested against a fake connection (no database, no network).

The fake stands in for ``psycopg.AsyncConnection``: it records every statement as rendered text with
its placeholders next to the parameters, answers by the start of the statement, and records the
transaction events, so these tests can state what the client sends and when it opens, reuses and
closes a connection. What the real server answers (the SQLSTATEs below, the login refusals) comes
from the spikes of 2026-10-30 (spikes 1 and 9) and from the PostgreSQL error-code table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import errors as pg
from psycopg import pq
from psycopg.rows import dict_row
from psycopg.sql import SQL, Composable, Identifier
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from appmux.registry.audit import AuditEntry
from appmux.registry.client import (
    GENERATED_COLUMNS,
    JSONB_COLUMNS,
    ConnectionParameters,
    DatabaseConnection,
    DatabaseCursor,
    RegistryClient,
    connect_with_psycopg,
)
from appmux.registry.errors import (
    ChangedByOthers,
    InvalidRow,
    RegistryError,
    RegistryRoleMissing,
    RegistryUnavailable,
    RowAlreadyExists,
    RowMissing,
)
from appmux.registry.models import (
    App,
    AuditLogRow,
    MetricRow,
    PermissionRequest,
    RequestPayload,
)
from appmux.registry.protocol import Change
from appmux.registry.snapshot import SNAPSHOT_TABLES
from appmux.registry.writers import WriteIdentityError, WriteKind
from appmux.sandbox.rules import EgressRule

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
ADMIN = "admin@example.com"
OWNER = "owner@example.com"
# Fake credentials. The tests look for these exact strings in messages, reprs and logs.
ADMIN_TOKEN = SecretStr("fake-admin-oauth-token-0001")
SP_TOKEN = SecretStr("fake-sp-oauth-token-0001")
NEXT_SP_TOKEN = SecretStr("fake-sp-oauth-token-0002")
LEAK = "hunter2-row-value-that-must-not-travel"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


# --- the fake connection ------------------------------------------------------------------------


class Executed:
    """One statement as the connection received it: the text with placeholders, and the params."""

    def __init__(self, sql: str, params: tuple[object, ...]) -> None:
        self.sql = sql
        self.params = params


class Gate:
    """An answer held back: the statement stays in flight until the test calls ``open``."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.event = asyncio.Event()
        self.rows = rows

    def open(self) -> None:
        self.event.set()


Outcome = list[dict[str, object]] | int | BaseException | str | Gate


class FakeCursor:
    def __init__(self, rows: list[dict[str, object]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    async def fetchall(self) -> list[dict[str, object]]:
        return list(self._rows)


class FakeConnection:
    def __init__(self, database: FakeDatabase, parameters: ConnectionParameters) -> None:
        self.database = database
        self.parameters = parameters
        self.closed = False
        self.executed: list[Executed] = []
        self.events: list[str] = []

    async def execute(
        self, query: object, params: Sequence[object] | None = None
    ) -> DatabaseCursor:
        text = query.as_string() if isinstance(query, Composable) else str(query)
        entry = Executed(text, tuple(params or ()))
        self.executed.append(entry)
        outcome = self.database.outcome_for(entry)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, Gate):
            await outcome.event.wait()
            return FakeCursor(outcome.rows, len(outcome.rows))
        if outcome == HANG:
            await asyncio.Event().wait()
        if isinstance(outcome, int):
            return FakeCursor([], outcome)
        assert isinstance(outcome, list)
        return FakeCursor(outcome, len(outcome))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        if self.database.commit_error is not None:
            self.events.append("commit-failed")
            raise self.database.commit_error
        self.events.append("commit")

    async def close(self) -> None:
        self.closed = True
        self.events.append("close")

    def writes(self) -> list[Executed]:
        return [e for e in self.executed if e.sql.startswith(("INSERT", "UPDATE", "DELETE"))]

    def reads(self) -> list[Executed]:
        return [e for e in self.executed if e.sql.startswith("SELECT")]


HANG = "hang"


class FakeDatabase:
    """The connection factory of the client, and the rules that answer statements."""

    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.connect_error: BaseException | None = None
        self.commit_error: BaseException | None = None
        self._rules: list[tuple[str, Outcome]] = []

    def on(self, prefix: str, outcome: Outcome) -> None:
        """Answer statements that start with ``prefix``; the first matching rule wins."""
        self._rules.append((prefix, outcome))

    def clear_rules(self) -> None:
        """Back to answering every statement with no rows: the server is healthy again."""
        self._rules.clear()

    def outcome_for(self, entry: Executed) -> Outcome:
        for prefix, outcome in self._rules:
            if entry.sql.startswith(prefix):
                return outcome
        return []

    async def connect(self, parameters: ConnectionParameters) -> DatabaseConnection:
        if self.connect_error is not None:
            raise self.connect_error
        connection = FakeConnection(self, parameters)
        self.connections.append(connection)
        return connection


class TokenSource:
    """A token provider whose token changes when a test says the SP token was minted anew."""

    def __init__(self, token: SecretStr = SP_TOKEN) -> None:
        self.token = token
        self.calls = 0
        self.error: Exception | None = None

    async def __call__(self) -> SecretStr:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.token


def make_client(
    database: FakeDatabase,
    tokens: TokenSource | None = None,
    *,
    poll_interval_s: float = 5.0,
    operation_timeout_s: float = 30.0,
) -> RegistryClient:
    return RegistryClient(
        host="lakebase.example.invalid",
        port=5432,
        database="appmux_registry",
        user="sp-client-id",
        token_provider=tokens if tokens is not None else TokenSource(),
        connection_factory=database.connect,
        poll_interval_s=poll_interval_s,
        operation_timeout_s=operation_timeout_s,
    )


# --- rows and values ----------------------------------------------------------------------------


def app_values(slug: str = "sales") -> dict[str, object]:
    return {
        "slug": slug,
        "display_name": "Sales",
        "owner": OWNER,
        "source_path": f"/Workspace/Users/{OWNER}/{slug}",
        "memory_budget_mb": 256,
        "created_by": ADMIN,
        "updated_by": ADMIN,
    }


def app_row(slug: str = "sales", version: int = 1) -> dict[str, object]:
    return {
        **app_values(slug),
        "obo_delegated": False,
        "shared_sp_delegated": False,
        "egress": [],
        "version": version,
        "created_at": NOW,
        "updated_at": NOW,
    }


def config_row(slug: str = "sales", version: int = 1) -> dict[str, object]:
    return {
        "app_slug": slug,
        "desired_state": "stopped",
        "target_release_no": None,
        "health_path": "/",
        "health_deadline_s": 60,
        "own_sp_client_id_key": None,
        "own_sp_client_secret_key": None,
        "version": version,
        "updated_by": OWNER,
        "updated_at": NOW,
    }


def grant_values(principal: str = "reader@example.com") -> dict[str, object]:
    return {
        "app_slug": "sales",
        "principal_type": "user",
        "principal": principal,
        "permission": "CAN_USE",
        "granted_by": OWNER,
    }


def request_row(request_id: int = 7) -> dict[str, object]:
    return {
        "request_id": request_id,
        "app_slug": "sales",
        "kind": "delegation",
        "payload": {"obo": True},
        "reason": "needs the visitor's identity",
        "requested_by": OWNER,
        "requested_at": NOW,
        "status": "pending",
        "closed_by": None,
        "closed_at": None,
        "closed_note": None,
    }


def metric_row(
    app_slug: str = "sales", minute: datetime = NOW, requests: int = 3
) -> dict[str, object]:
    return {"app_slug": app_slug, "minute": minute, "requests": requests, "lat_hist": [0] * 11}


def audit_entry(
    action: str = "app.register", actor: str = ADMIN, slug: str | None = "sales"
) -> AuditEntry:
    return AuditEntry.model_validate({"actor": actor, "action": action, "app_slug": slug})


def register_changes() -> list[Change]:
    return [
        Change(WriteKind.APP_INSERT, values=app_values()),
        Change(WriteKind.APP_CONFIG_INSERT, values={"app_slug": "sales", "updated_by": ADMIN}),
    ]


# --- the client ---------------------------------------------------------------------------------


def test_the_baseline_migration_agrees_with_the_jsonb_and_generated_column_tables() -> None:
    """The client wraps the jsonb columns of the migration and refuses the generated ones."""
    jsonb: dict[str, set[str]] = {}
    generated: dict[str, set[str]] = {}
    table = ""
    for line in (REPOSITORY_ROOT / "migrations" / "0001_baseline.sql").read_text().splitlines():
        line = line.split("--", 1)[0]
        opened = re.match(r"CREATE TABLE (\w+) \(", line)
        if opened:
            table = opened.group(1)
        column = re.match(r"\s{2}(\w+)\s+(\w+)(\[\])?\s*(.*)", line)
        if not column or not table:
            continue
        name, type_name, _, rest = column.groups()
        if type_name == "jsonb":
            jsonb.setdefault(table, set()).add(name)
        if "GENERATED ALWAYS" in rest:
            generated.setdefault(table, set()).add(name)
    # audit_log is left out of both tables: no change can name it, because audit rows are written
    # through audit.insert_statement, which sends its detail as JSON text and has no id to send.
    assert {t: set(c) for t, c in JSONB_COLUMNS.items()} == {
        t: c for t, c in jsonb.items() if t != "audit_log"
    }
    assert {t: set(c) for t, c in GENERATED_COLUMNS.items()} == {
        t: c for t, c in generated.items() if t != "audit_log"
    }


async def test_a_first_operation_logs_in_as_the_service_principal_with_the_minted_token() -> None:
    database = FakeDatabase()
    client = make_client(database)
    await client.refresh()

    assert len(database.connections) == 1
    parameters = database.connections[0].parameters
    assert (parameters.host, parameters.port, parameters.database, parameters.user) == (
        "lakebase.example.invalid",
        5432,
        "appmux_registry",
        "sp-client-id",
    )
    assert parameters.password == SP_TOKEN
    # Seven reads, one per snapshot table, on the one connection.
    assert len(database.connections[0].reads()) == len(SNAPSHOT_TABLES)


async def test_the_long_lived_connection_is_reused_while_the_token_is_unchanged() -> None:
    database = FakeDatabase()
    tokens = TokenSource()
    client = make_client(database, tokens)
    await client.refresh()
    tokens.token = SecretStr(SP_TOKEN.get_secret_value())  # a new object, the same token
    await client.refresh()

    assert len(database.connections) == 1
    # The provider is asked on every operation, which is how a token minted anew gets noticed.
    assert tokens.calls > 1
    assert not database.connections[0].closed


async def test_a_token_minted_anew_closes_the_old_connection_and_opens_one_with_the_new_token() -> (
    None
):
    database = FakeDatabase()
    tokens = TokenSource()
    client = make_client(database, tokens)
    await client.refresh()
    tokens.token = NEXT_SP_TOKEN
    await client.refresh()

    first, second = database.connections
    assert first.closed
    assert not second.closed
    assert second.parameters.password == NEXT_SP_TOKEN
    assert len(second.reads()) == len(SNAPSHOT_TABLES)


async def test_a_connection_the_driver_reports_closed_is_replaced() -> None:
    database = FakeDatabase()
    client = make_client(database)
    await client.refresh()
    database.connections[0].closed = True
    await client.refresh()

    assert len(database.connections) == 2


async def test_close_closes_the_service_principal_connection() -> None:
    database = FakeDatabase()
    client = make_client(database)
    await client.refresh()
    await client.close()

    assert database.connections[0].closed


async def test_a_token_provider_that_fails_is_registry_unavailable_and_opens_nothing() -> None:
    database = FakeDatabase()
    tokens = TokenSource()
    tokens.error = RuntimeError(f"token endpoint said {LEAK}")
    client = make_client(database, tokens)

    with pytest.raises(RegistryUnavailable) as raised:
        await client.refresh()

    assert database.connections == []
    assert LEAK not in str(raised.value)
    assert client.last_error is not None


async def test_an_empty_service_principal_token_is_not_used_to_log_in() -> None:
    database = FakeDatabase()
    client = make_client(database, TokenSource(SecretStr("")))

    with pytest.raises(RegistryUnavailable):
        await client.refresh()

    assert database.connections == []


# --- snapshot reads -----------------------------------------------------------------------------


async def test_refresh_loads_the_snapshot_through_the_row_source() -> None:
    database = FakeDatabase()
    database.on('SELECT "slug"', [app_row("sales"), app_row("docs")])
    database.on('SELECT "app_slug", "desired_state"', [config_row("sales")])
    client = make_client(database)

    with pytest.raises(RegistryUnavailable):
        _ = client.snapshot  # nothing is loaded before the first refresh
    await client.refresh()

    assert [a.slug for a in client.snapshot.apps] == ["docs", "sales"]
    assert client.snapshot.app_config("sales") is not None
    assert client.last_error is None


async def test_a_read_names_the_model_columns_and_nothing_else() -> None:
    database = FakeDatabase()
    client = make_client(database)
    rows = await client.fetch_rows("apps")

    assert rows == []
    sql = database.connections[0].reads()[0].sql
    assert sql == (
        'SELECT "slug", "display_name", "owner", "source_path", "memory_budget_mb", '
        '"obo_delegated", "shared_sp_delegated", "egress", "version", "created_by", '
        '"created_at", "updated_by", "updated_at" FROM "apps"'
    )


@pytest.mark.parametrize("table", ["metrics", "audit_log", 'apps"; DROP TABLE apps; --', ""])
async def test_fetch_rows_refuses_a_table_outside_the_snapshot_without_touching_the_database(
    table: str,
) -> None:
    database = FakeDatabase()
    client = make_client(database)

    with pytest.raises(ValueError, match="not a snapshot table"):
        await client.fetch_rows(table)

    assert database.connections == []


async def test_a_failed_reload_keeps_serving_the_last_snapshot_and_reports_the_error() -> None:
    database = FakeDatabase()
    database.on('SELECT "slug"', [app_row("sales")])
    client = make_client(database)
    await client.refresh()

    database.connections[0].closed = True
    database.connect_error = psycopg.OperationalError("connection refused")
    await client.refresh()

    assert [a.slug for a in client.snapshot.apps] == ["sales"]
    assert client.last_error is not None

    database.connect_error = None
    await client.refresh()
    assert client.last_error is None


async def test_poll_reloads_until_cancelled() -> None:
    database = FakeDatabase()
    client = make_client(database, poll_interval_s=0.01)
    task = asyncio.create_task(client.poll())
    for _ in range(200):
        if len(database.connections) and len(database.connections[0].reads()) >= 14:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(database.connections[0].reads()) >= 2 * len(SNAPSHOT_TABLES)


# --- the SQL of a write -------------------------------------------------------------------------


async def test_an_insert_sends_values_as_parameters_and_never_inside_the_sql_text() -> None:
    sneaky = "Sales'); DROP TABLE apps; --"
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', [app_row()])
    database.on('INSERT INTO "app_config"', [config_row()])
    client = make_client(database)
    values = {**app_values(), "display_name": sneaky}

    await client.write(
        [
            Change(WriteKind.APP_INSERT, values=values),
            Change(WriteKind.APP_CONFIG_INSERT, values={"app_slug": "sales", "updated_by": ADMIN}),
        ],
        [audit_entry()],
        admin_token=ADMIN_TOKEN,
    )

    write_connection = database.connections[0]
    first = write_connection.writes()[0]
    assert first.sql == (
        'INSERT INTO "apps" ("slug", "display_name", "owner", "source_path", "memory_budget_mb", '
        '"created_by", "updated_by") VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING "slug", '
        '"display_name", "owner", "source_path", "memory_budget_mb", "obo_delegated", '
        '"shared_sp_delegated", "egress", "version", "created_by", "created_at", "updated_by", '
        '"updated_at"'
    )
    assert first.params == tuple(values.values())
    for statement in write_connection.executed:
        assert sneaky not in statement.sql
        assert "Sales" not in statement.sql


async def test_an_update_of_a_versioned_row_checks_and_bumps_the_version() -> None:
    database = FakeDatabase()
    database.on('UPDATE "app_config"', [config_row(version=4)])
    client = make_client(database)

    rows = await client.write(
        [
            Change(
                WriteKind.APP_CONFIG_UPDATE,
                key={"app_slug": "sales"},
                values={"desired_state": "running", "updated_by": OWNER},
                expected_version=3,
            )
        ],
        [audit_entry("app.start")],
    )

    update = database.connections[0].writes()[0]
    assert update.sql.startswith(
        'UPDATE "app_config" SET "desired_state" = %s, "updated_by" = %s, "updated_at" = now(), '
        '"version" = version + 1 WHERE "app_slug" = %s AND "version" = %s RETURNING '
    )
    assert update.params == ("running", OWNER, "sales", 3)
    assert getattr(rows[0], "version", None) == 4


async def test_an_update_of_an_unversioned_table_fills_only_what_the_table_has() -> None:
    database = FakeDatabase()
    database.on(
        'UPDATE "app_grants"', [{**grant_values(), "permission": "CAN_MANAGE", "granted_at": NOW}]
    )
    client = make_client(database)
    key = {"app_slug": "sales", "principal_type": "user", "principal": "reader@example.com"}

    await client.write(
        [Change(WriteKind.APP_GRANT_UPDATE, key=key, values={"permission": "CAN_MANAGE"})],
        [audit_entry("grant.app")],
    )

    update = database.connections[0].writes()[0]
    assert update.sql.startswith(
        'UPDATE "app_grants" SET "permission" = %s WHERE "app_slug" = %s AND '
        '"principal_type" = %s AND "principal" = %s RETURNING '
    )
    assert update.params == ("CAN_MANAGE", "sales", "user", "reader@example.com")


async def test_a_release_status_change_stamps_status_at_unless_the_caller_gave_one() -> None:
    row = {
        "app_slug": "sales",
        "release_no": 2,
        "snapshot_digest": "sha256:" + "a" * 64,
        "detected": {},
        "status_at": NOW,
        "created_by": OWNER,
        "created_at": NOW,
    }
    database = FakeDatabase()
    database.on('UPDATE "releases"', [row])
    client = make_client(database)
    key = {"app_slug": "sales", "release_no": 2}

    await client.write([Change(WriteKind.RELEASE_UPDATE, key=key, values={"status": "healthy"})])
    await client.write(
        [Change(WriteKind.RELEASE_UPDATE, key=key, values={"status": "failed", "status_at": NOW})]
    )
    await client.write([Change(WriteKind.RELEASE_UPDATE, key=key, values={"label": "stable"})])

    updates = [w for w in database.connections[0].writes() if w.sql.startswith("UPDATE")]
    assert '"status_at" = now()' in updates[0].sql
    assert '"status_at" = now()' not in updates[1].sql
    assert updates[1].params[:3] == ("failed", NOW, "sales")
    assert '"status_at" = now()' not in updates[2].sql


async def test_a_delete_names_its_key_in_order_and_returns_none() -> None:
    database = FakeDatabase()
    database.on('DELETE FROM "app_grants"', 1)
    client = make_client(database)
    key = {"principal": "reader@example.com", "app_slug": "sales", "principal_type": "user"}

    rows = await client.write(
        [Change(WriteKind.APP_GRANT_DELETE, key=key)], [audit_entry("grant.app")]
    )

    assert rows == (None,)
    delete = database.connections[0].writes()[0]
    assert delete.sql == (
        'DELETE FROM "app_grants" WHERE "app_slug" = %s AND "principal_type" = %s AND '
        '"principal" = %s'
    )
    assert delete.params == ("sales", "user", "reader@example.com")


async def test_a_jsonb_column_is_sent_as_json_and_a_model_is_dumped_as_set() -> None:
    rule = EgressRule(action="allow", cidr="10.0.0.0/8", port="443", comment="")
    database = FakeDatabase()
    database.on('INSERT INTO "permission_requests"', [request_row()])
    database.on('UPDATE "apps"', [app_row(version=2)])
    client = make_client(database)

    await client.write(
        [
            Change(
                WriteKind.REQUEST_CREATE,
                values={
                    "app_slug": "sales",
                    "kind": "delegation",
                    "payload": RequestPayload(obo=True),
                    "reason": "why",
                    "requested_by": OWNER,
                },
            )
        ],
        [audit_entry("request.create")],
    )
    await client.write(
        [
            Change(
                WriteKind.APP_UPDATE,
                key={"slug": "sales"},
                values={"egress": [rule], "updated_by": ADMIN},
                expected_version=1,
            )
        ],
        [audit_entry("egress.edit")],
        admin_token=ADMIN_TOKEN,
    )

    request_insert = database.connections[0].writes()[0]
    payload = next(p for p in request_insert.params if isinstance(p, Jsonb))
    assert payload.dumps is not None
    assert json.loads(payload.dumps(payload.obj)) == {"obo": True}
    app_update = next(c for c in database.connections if c.parameters.user == ADMIN).writes()[0]
    egress = next(p for p in app_update.params if isinstance(p, Jsonb))
    assert egress.dumps is not None
    assert json.loads(egress.dumps(egress.obj)) == [
        {"action": "allow", "cidr": "10.0.0.0/8", "port": "443", "comment": ""}
    ]


async def test_a_none_stays_sql_null_even_in_a_jsonb_column() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "platform_settings"', [])
    client = make_client(database)

    with pytest.raises(RegistryError):  # the empty answer is no stored row; see below
        await client.write(
            [
                Change(
                    WriteKind.PLATFORM_SETTING_INSERT,
                    values={"key": "k", "value": None, "updated_by": ADMIN},
                )
            ],
            [audit_entry("settings.edit", slug=None)],
            admin_token=ADMIN_TOKEN,
        )

    params = database.connections[0].writes()[0].params
    assert params[1] is None


async def test_a_value_that_is_not_json_is_refused_before_any_connection() -> None:
    database = FakeDatabase()
    client = make_client(database)

    with pytest.raises(InvalidRow, match="payload"):
        await client.write(
            [
                Change(
                    WriteKind.REQUEST_CREATE,
                    values={"app_slug": "sales", "kind": "delegation", "payload": {1, 2}},
                )
            ],
            [audit_entry("request.create")],
        )

    assert database.connections == []


@pytest.mark.parametrize(
    "bad_column",
    ["nope", 'slug"; DROP TABLE apps; --', "request_id", "Slug", ""],
)
async def test_a_column_outside_the_model_or_generated_is_refused_without_touching_a_connection(
    bad_column: str,
) -> None:
    database = FakeDatabase()
    client = make_client(database)
    values: dict[str, object] = {
        "app_slug": "sales",
        "kind": "delegation",
        "reason": "r",
        "requested_by": OWNER,
        bad_column: "x",
    }

    with pytest.raises(InvalidRow) as raised:
        await client.write(
            [Change(WriteKind.REQUEST_CREATE, values=values)], [audit_entry("request.create")]
        )

    assert database.connections == []
    assert "DROP TABLE" not in str(raised.value)
    assert "permission_requests" in str(raised.value)


# --- transactions and identities ----------------------------------------------------------------


async def test_every_statement_and_audit_row_of_a_write_runs_in_one_transaction() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', [app_row()])
    database.on('INSERT INTO "app_config"', [config_row()])
    client = make_client(database)

    rows = await client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)

    admin_connection = database.connections[0]
    assert [type(r) for r in rows] == [App, type(rows[1])]
    assert [w.sql.split(" (")[0] for w in admin_connection.writes()] == [
        'INSERT INTO "apps"',
        'INSERT INTO "app_config"',
        "INSERT INTO audit_log",
    ]
    assert admin_connection.events == ["begin", "commit", "close"]
    audit = admin_connection.writes()[2]
    assert audit.params[:3] == (ADMIN, "app.register", "sales")


async def test_an_admin_write_uses_the_admin_s_own_login_and_closes_it_afterwards() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', [app_row()])
    database.on('INSERT INTO "app_config"', [config_row()])
    client = make_client(database)

    await client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)

    admin_connection, sp_connection = database.connections
    assert admin_connection.parameters.user == ADMIN
    assert admin_connection.parameters.password == ADMIN_TOKEN
    assert admin_connection.closed
    # The follow-up snapshot reload is the service principal's, not the admin's.
    assert sp_connection.parameters.user == "sp-client-id"
    assert sp_connection.writes() == []
    assert len(sp_connection.reads()) == len(SNAPSHOT_TABLES)


async def test_a_service_principal_write_uses_the_long_lived_connection() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', [{**grant_values(), "granted_at": NOW}])
    client = make_client(database)

    await client.write(
        [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
    )
    await client.write(
        [Change(WriteKind.APP_GRANT_INSERT, values=grant_values("b@example.com"))],
        [audit_entry("grant.app")],
    )

    assert len(database.connections) == 1
    assert database.connections[0].events.count("begin") == 2
    assert not database.connections[0].closed


async def test_the_admin_connection_is_closed_when_a_statement_fails() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', pg.UniqueViolation(f"Key (slug)=({LEAK}) already exists."))
    client = make_client(database)

    with pytest.raises(RowAlreadyExists):
        await client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)

    admin_connection = database.connections[0]
    assert admin_connection.closed
    assert admin_connection.events == ["begin", "rollback", "close"]
    # Nothing after the failed statement ran: not the second change and not the audit row.
    assert len(admin_connection.executed) == 1


async def test_the_admin_connection_is_closed_when_the_commit_fails() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', [app_row()])
    database.on('INSERT INTO "app_config"', [config_row()])
    database.commit_error = pg.ForeignKeyViolation(f"Key (x)=({LEAK}) is not present")
    client = make_client(database)

    with pytest.raises(RowMissing):
        await client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)

    assert database.connections[0].closed
    assert database.connections[0].events == ["begin", "commit-failed", "close"]


async def test_the_admin_connection_is_closed_when_the_caller_is_cancelled() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', HANG)
    client = make_client(database)
    task = asyncio.create_task(
        client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)
    )
    for _ in range(200):
        if database.connections and database.connections[0].executed:
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert database.connections[0].closed


async def test_as_platform_admin_runs_one_transaction_on_its_own_connection() -> None:
    database = FakeDatabase()
    client = make_client(database)

    async with client.as_platform_admin(ADMIN_TOKEN, role=ADMIN) as connection:
        await connection.execute("SELECT 1")

    only = database.connections[0]
    assert len(database.connections) == 1
    assert only.events == ["begin", "commit", "close"]
    assert only.parameters.user == ADMIN


async def test_an_empty_admin_token_is_treated_as_no_token() -> None:
    database = FakeDatabase()
    client = make_client(database)

    with pytest.raises(RegistryRoleMissing):
        await client.write(register_changes(), [audit_entry()], admin_token=SecretStr(""))

    with pytest.raises(RegistryRoleMissing):
        async with client.as_platform_admin(SecretStr(""), role=ADMIN):
            pass
    assert database.connections == []


async def test_a_platform_write_without_a_token_never_opens_a_connection() -> None:
    database = FakeDatabase()
    client = make_client(database)

    with pytest.raises(RegistryRoleMissing) as raised:
        await client.write(register_changes(), [audit_entry()])

    assert str(raised.value) == (
        "your database role cannot write platform data; ask the Lakebase project owner"
    )
    assert database.connections == []


async def test_a_service_principal_change_with_an_admin_token_never_opens_a_connection() -> None:
    database = FakeDatabase()
    client = make_client(database)

    with pytest.raises(WriteIdentityError):
        await client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())],
            [audit_entry("grant.app")],
            admin_token=ADMIN_TOKEN,
        )

    assert database.connections == []


async def test_a_write_with_neither_changes_nor_audit_entries_is_refused() -> None:
    client = make_client(FakeDatabase())

    with pytest.raises(ValueError, match="nothing to write"):
        await client.write([])
    with pytest.raises(ValueError, match="nothing to write"):
        await client.write([], [])


@pytest.mark.parametrize(
    "audit",
    [
        [],
        [audit_entry(actor="appmux")],
        [audit_entry(actor="admin@example.com"), audit_entry(actor="other@example.com")],
    ],
    ids=["none", "no person", "two people"],
)
async def test_an_admin_write_names_exactly_one_person_in_its_audit_entries(
    audit: list[AuditEntry],
) -> None:
    database = FakeDatabase()
    client = make_client(database)

    with pytest.raises(ValueError, match="exactly one"):
        await client.write(register_changes(), audit, admin_token=ADMIN_TOKEN)

    assert database.connections == []


async def test_an_automatic_audit_entry_beside_the_person_s_does_not_change_the_login_role() -> (
    None
):
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', [app_row()])
    database.on('INSERT INTO "app_config"', [config_row()])
    client = make_client(database)

    await client.write(
        register_changes(),
        [audit_entry(), audit_entry("app.start", actor="appmux")],
        admin_token=ADMIN_TOKEN,
    )

    assert database.connections[0].parameters.user == ADMIN


# --- what a write returns -----------------------------------------------------------------------


async def test_a_write_returns_the_stored_rows_in_order_with_none_for_a_delete() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "permission_requests"', [request_row(7)])
    database.on('DELETE FROM "app_grants"', 1)
    client = make_client(database)

    rows = await client.write(
        [
            Change(
                WriteKind.REQUEST_CREATE,
                values={
                    "app_slug": "sales",
                    "kind": "delegation",
                    "payload": {"obo": True},
                    "reason": "r",
                    "requested_by": OWNER,
                },
            ),
            Change(
                WriteKind.APP_GRANT_DELETE,
                key={"app_slug": "sales", "principal_type": "user", "principal": "x@example.com"},
            ),
        ],
        [audit_entry("request.create")],
    )

    assert isinstance(rows[0], PermissionRequest)
    assert rows[0].request_id == 7
    assert rows[1] is None


async def test_a_stored_row_that_breaks_its_model_rolls_the_write_back() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', [{**app_row(), "memory_budget_mb": 1}])
    client = make_client(database)

    with pytest.raises(InvalidRow) as raised:
        await client.write(
            [Change(WriteKind.APP_INSERT, values=app_values())],
            [audit_entry()],
            admin_token=ADMIN_TOKEN,
        )

    assert "memory_budget_mb" in str(raised.value)
    assert database.connections[0].events == ["begin", "rollback", "close"]


async def test_a_committed_write_refreshes_the_snapshot_before_it_returns() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', [{**grant_values(), "granted_at": NOW}])
    client = make_client(database)

    await client.write(
        [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
    )

    connection = database.connections[0]
    kinds = [e.sql.split(" ")[0] for e in connection.executed]
    assert kinds[:2] == ["INSERT", "INSERT"]  # the grant and its audit row
    assert kinds[2:] == ["SELECT"] * len(SNAPSHOT_TABLES)
    assert connection.events[:2] == ["begin", "commit"]


async def test_a_write_is_not_reported_failed_because_the_reload_after_it_failed() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', [{**grant_values(), "granted_at": NOW}])
    client = make_client(database)
    await client.refresh()
    database.on('SELECT "app_slug", "principal_type"', pg.AdminShutdown("terminating"))

    rows = await client.write(
        [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
    )

    assert rows[0] is not None
    assert client.last_error is not None


# --- error mapping ------------------------------------------------------------------------------


GRANT_KEY = {"app_slug": "sales", "principal_type": "user", "principal": "x@example.com"}


def check_violation(table: str, constraint: str) -> pg.CheckViolation:
    # A real server names the table and the constraint of every class 23 error in separate fields.
    return pg.CheckViolation(
        f"Failing row contains ({LEAK})",
        info={
            pq.DiagnosticField.TABLE_NAME: table.encode(),
            pq.DiagnosticField.CONSTRAINT_NAME: constraint.encode(),
        },
    )


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (pg.UniqueViolation(f"Key (slug)=({LEAK}) already exists."), RowAlreadyExists),
        (pg.ForeignKeyViolation(f"Key (app_slug)=({LEAK}) is not present"), RowMissing),
        (check_violation("app_grants", "app_grants_check"), InvalidRow),
        (pg.NotNullViolation(f"null value, row ({LEAK})"), InvalidRow),
        (pg.InvalidTextRepresentation(f"invalid input syntax: {LEAK}"), InvalidRow),
        (pg.NumericValueOutOfRange(f"out of range: {LEAK}"), InvalidRow),
        (pg.SerializationFailure("could not serialize access"), ChangedByOthers),
        (pg.DeadlockDetected("deadlock detected"), ChangedByOthers),
        (psycopg.OperationalError("the connection is lost"), RegistryUnavailable),
        (pg.AdminShutdown("terminating connection"), RegistryUnavailable),
        (psycopg.InterfaceError("the connection is closed"), RegistryUnavailable),
        (ConnectionResetError("reset by peer"), RegistryUnavailable),
        (pg.UndefinedTable("relation does not exist"), RegistryError),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else value.__name__,
)
async def test_a_failing_write_statement_maps_to_the_error_the_routes_expect(
    raised: BaseException, expected: type[RegistryError]
) -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', raised)
    client = make_client(database)

    with pytest.raises(expected) as caught:
        await client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )

    assert type(caught.value) is expected
    for shown in (str(caught.value), repr(caught.value), repr(caught.value.args)):
        assert LEAK not in shown


async def test_an_integrity_error_does_not_keep_the_driver_message_as_its_cause() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', pg.UniqueViolation(f"Key (slug)=({LEAK}) exists."))
    client = make_client(database)

    with pytest.raises(RowAlreadyExists) as caught:
        await client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )

    # The driver message repeats the row; a traceback printed by a caller must not show it.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


async def test_an_invalid_row_names_the_constraint_or_column_and_never_a_value() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', check_violation("apps", "apps_memory_budget_mb_check"))
    client = make_client(database)

    with pytest.raises(InvalidRow) as caught:
        await client.write(
            [Change(WriteKind.APP_INSERT, values=app_values())],
            [audit_entry()],
            admin_token=ADMIN_TOKEN,
        )

    assert str(caught.value) == "the values do not fit apps: apps_memory_budget_mb_check"


async def test_insufficient_privilege_on_a_platform_write_means_the_role_is_missing() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "apps"', pg.InsufficientPrivilege("permission denied for table apps"))
    client = make_client(database)

    with pytest.raises(RegistryRoleMissing) as caught:
        await client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)

    assert caught.value.message == (
        "your database role cannot write platform data; ask the Lakebase project owner"
    )
    assert database.connections[0].closed


async def test_insufficient_privilege_for_the_service_principal_is_not_a_missing_person_role() -> (
    None
):
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', pg.InsufficientPrivilege("permission denied"))
    client = make_client(database)

    with pytest.raises(RegistryError) as caught:
        await client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )

    assert type(caught.value) is RegistryError
    assert "0002_grants.sql" in caught.value.message
    # The connection answered, so it stays in use.
    assert not database.connections[0].closed


async def test_a_stale_version_is_changed_by_others_when_the_row_still_exists() -> None:
    database = FakeDatabase()
    database.on('UPDATE "app_config"', [])
    database.on("SELECT 1 FROM", [{"present": 1}])
    client = make_client(database)

    with pytest.raises(ChangedByOthers) as caught:
        await client.write(
            [
                Change(
                    WriteKind.APP_CONFIG_UPDATE,
                    key={"app_slug": "sales"},
                    values={"desired_state": "running", "updated_by": OWNER},
                    expected_version=3,
                )
            ],
            [audit_entry("app.start")],
        )

    assert caught.value.message == "changed by someone else, reload and retry"
    check = database.connections[0].reads()[0]
    assert check.sql == 'SELECT 1 FROM "app_config" WHERE "app_slug" = %s'
    assert check.params == ("sales",)
    assert database.connections[0].events == ["begin", "rollback"]
    assert not database.connections[0].closed


async def test_a_versioned_update_of_a_row_that_is_gone_is_row_missing() -> None:
    database = FakeDatabase()
    database.on('UPDATE "apps"', [])
    database.on("SELECT 1 FROM", [])
    client = make_client(database)

    with pytest.raises(RowMissing):
        await client.write(
            [
                Change(
                    WriteKind.APP_UPDATE,
                    key={"slug": "gone"},
                    values={"display_name": "X", "updated_by": ADMIN},
                    expected_version=1,
                )
            ],
            [audit_entry("app.edit_platform_fields", slug="gone")],
            admin_token=ADMIN_TOKEN,
        )


async def test_an_unversioned_update_or_delete_that_finds_no_row_is_row_missing() -> None:
    database = FakeDatabase()
    database.on('UPDATE "app_grants"', [])
    database.on('DELETE FROM "app_grants"', 0)
    client = make_client(database)
    key = {"app_slug": "sales", "principal_type": "user", "principal": "x@example.com"}

    with pytest.raises(RowMissing):
        await client.write(
            [Change(WriteKind.APP_GRANT_UPDATE, key=key, values={"permission": "CAN_USE"})],
            [audit_entry("grant.app")],
        )
    with pytest.raises(RowMissing):
        await client.write(
            [Change(WriteKind.APP_GRANT_DELETE, key=key)], [audit_entry("grant.app")]
        )


async def test_a_conflict_error_does_not_cost_the_service_principal_its_connection() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', pg.UniqueViolation("duplicate"))
    client = make_client(database)

    for _ in range(2):
        with pytest.raises(RowAlreadyExists):
            await client.write(
                [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())],
                [audit_entry("grant.app")],
            )

    assert len(database.connections) == 1


async def test_a_lost_connection_is_dropped_and_the_next_operation_logs_in_again() -> None:
    database = FakeDatabase()
    database.on(
        'INSERT INTO "app_grants"', psycopg.OperationalError("server closed the connection")
    )
    client = make_client(database)

    with pytest.raises(RegistryUnavailable):
        await client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )
    assert database.connections[0].closed
    assert client.last_error is not None

    database.clear_rules()  # the server is back
    await client.refresh()

    assert len(database.connections) == 2
    assert not database.connections[1].closed
    assert client.last_error is None


async def test_reads_map_every_driver_error_to_registry_unavailable() -> None:
    database = FakeDatabase()
    database.on("SELECT", pg.UndefinedColumn("column does not exist"))
    client = make_client(database)
    window = {"start": NOW - timedelta(hours=1), "end": NOW, "limit": 10}

    with pytest.raises(RegistryUnavailable):
        await client.fetch_rows("apps")
    with pytest.raises(RegistryUnavailable):
        await client.read_metrics(app_slug=None, **window)
    with pytest.raises(RegistryUnavailable):
        await client.read_audit(app_slug=None, **window)


async def test_an_operation_that_does_not_answer_in_time_is_unavailable_and_frees_the_lock() -> (
    None
):
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', HANG)
    client = make_client(database, operation_timeout_s=0.05)

    with pytest.raises(RegistryUnavailable) as caught:
        await client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )

    assert "in time" in caught.value.message
    assert database.connections[0].closed
    database.clear_rules()
    await client.refresh()  # the lock was released and a new connection opens
    assert len(database.connections) == 2


# --- logging in ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "refusal",
    [
        pg.InvalidPassword("FATAL: password authentication failed"),
        psycopg.OperationalError(
            "connection failed: FATAL:  password authentication failed for user 'x'"
        ),
        psycopg.OperationalError("connection failed: ERROR: Provided authentication token is bad"),
    ],
    ids=["28P01", "refused, no sqlstate", "token not a JWT"],
)
async def test_a_login_the_server_refuses_for_a_platform_admin_means_the_role_is_missing(
    refusal: BaseException,
) -> None:
    database = FakeDatabase()
    database.connect_error = refusal
    client = make_client(database)

    with pytest.raises(RegistryRoleMissing):
        await client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)


async def test_a_network_failure_while_logging_in_as_an_admin_is_unavailable() -> None:
    database = FakeDatabase()
    database.connect_error = psycopg.OperationalError("connection failed: connection refused")
    client = make_client(database)

    with pytest.raises(RegistryUnavailable):
        await client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)

    database.connect_error = OSError("name resolution failed")
    with pytest.raises(RegistryUnavailable):
        await client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)


async def test_a_refused_login_of_the_service_principal_is_unavailable_not_a_role_error() -> None:
    database = FakeDatabase()
    database.connect_error = pg.InvalidPassword("FATAL: password authentication failed")
    client = make_client(database)

    with pytest.raises(RegistryUnavailable):
        await client.refresh()

    assert client.last_error is not None


# --- secrets ------------------------------------------------------------------------------------


async def test_no_token_reaches_a_message_a_repr_or_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    database = FakeDatabase()
    # A hostile driver message that repeats the credentials, as a careless wrapper might.
    database.connect_error = psycopg.OperationalError(
        f"connection failed for {ADMIN_TOKEN.get_secret_value()} and {SP_TOKEN.get_secret_value()}"
    )
    client = make_client(database)
    shown: list[str] = []

    for attempt in (
        client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN),
        client.refresh(),
    ):
        with pytest.raises(RegistryError) as caught:
            await attempt
        shown += [str(caught.value), repr(caught.value), repr(caught.value.args)]
    database.connect_error = None
    await client.refresh()
    shown += [repr(client), repr(database.connections[0].parameters), str(client.last_error)]
    shown += [record.getMessage() for record in caplog.records]

    for secret in (ADMIN_TOKEN, SP_TOKEN):
        value = secret.get_secret_value()
        assert all(value not in text for text in shown)


@pytest.mark.parametrize(
    ("driver_text", "reason"),
    [
        ('connection failed: FATAL:  database "x" does not exist', "the database does not exist"),
        ('connection failed: FATAL:  role "x" does not exist', "the role does not exist"),
        ("connection failed: password authentication failed", "refused the token or password"),
        ('could not translate host name "x" to address', "the host name did not resolve"),
        ("connection failed: connection refused", "the connection was refused"),
        ("connection failed: timeout expired", "the connection timed out"),
        ("something no list knows", "not one of the known reasons"),
    ],
)
async def test_a_failed_login_logs_a_reason_from_a_fixed_list_and_never_the_drivers_text(
    driver_text: str, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    # The first run on a real Lakebase could not say why the login failed: the log held the
    # class and the sqlstate only. The driver's text stays out (it may repeat a token), so the
    # reason is a label picked from what the text says.
    caplog.set_level(logging.DEBUG)
    database = FakeDatabase()
    client = make_client(database)
    database.connect_error = psycopg.OperationalError(
        f"{driver_text} (login with {SP_TOKEN.get_secret_value()} {ADMIN_TOKEN.get_secret_value()})"
    )

    with pytest.raises(RegistryError):
        await client.refresh()

    assert reason in caplog.text
    assert driver_text not in caplog.text
    for secret in (ADMIN_TOKEN, SP_TOKEN):
        assert secret.get_secret_value() not in caplog.text


def test_connection_parameters_hide_the_password_in_their_repr() -> None:
    parameters = ConnectionParameters(
        host="h.example.invalid",
        port=5432,
        database="d",
        user="u",
        password=SP_TOKEN,
        connect_timeout_s=30,
    )

    assert SP_TOKEN.get_secret_value() not in repr(parameters)
    assert SP_TOKEN.get_secret_value() not in str(parameters)


async def test_the_log_names_a_failure_by_class_and_sqlstate_and_never_by_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', pg.UniqueViolation(f"Key (slug)=({LEAK}) exists."))
    client = make_client(database)

    with pytest.raises(RowAlreadyExists):
        await client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )

    assert LEAK not in caplog.text
    assert "UniqueViolation" in caplog.text
    assert "23505" in caplog.text


# --- metrics and audit --------------------------------------------------------------------------


async def test_append_metrics_inserts_in_chunks_and_ignores_a_repeated_minute() -> None:
    database = FakeDatabase()
    client = make_client(database)
    rows = [
        MetricRow.model_validate(metric_row(f"app{n}", NOW + timedelta(minutes=n)))
        for n in range(1001)
    ]

    await client.append_metrics(rows)

    connection = database.connections[0]
    statements = connection.writes()
    assert len(statements) == 3  # 500 + 500 + 1 rows, not 1001 round trips
    assert all(s.sql.endswith('ON CONFLICT ("app_slug", "minute") DO NOTHING') for s in statements)
    assert len(statements[0].params) == 500 * len(MetricRow.model_fields)
    assert len(statements[2].params) == len(MetricRow.model_fields)
    assert connection.events == ["begin", "commit"]
    first_row_params = statements[0].params[: len(MetricRow.model_fields)]
    assert first_row_params[0] == "app0"
    assert first_row_params[list(MetricRow.model_fields).index("lat_hist")] == [0] * 11
    dropped = first_row_params[list(MetricRow.model_fields).index("egress_dropped_by_rule")]
    assert isinstance(dropped, Jsonb)


async def test_append_metrics_with_no_rows_does_not_touch_the_database() -> None:
    database = FakeDatabase()
    client = make_client(database)

    await client.append_metrics([])

    assert database.connections == []


async def test_append_metrics_failure_is_a_registry_error_the_flusher_can_catch() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "metrics"', psycopg.OperationalError("lost"))
    client = make_client(database)

    with pytest.raises(RegistryUnavailable):
        await client.append_metrics([MetricRow.model_validate(metric_row())])


async def test_prune_metrics_deletes_before_the_cutoff_and_returns_the_count() -> None:
    database = FakeDatabase()
    database.on('DELETE FROM "metrics"', 42)
    client = make_client(database)
    cutoff = NOW - timedelta(days=7)

    deleted = await client.prune_metrics(older_than=cutoff)

    assert deleted == 42
    statement = database.connections[0].writes()[0]
    assert statement.sql == 'DELETE FROM "metrics" WHERE "minute" < %s'
    assert statement.params == (cutoff,)


async def test_read_metrics_selects_the_window_and_keeps_the_newest_rows_in_order() -> None:
    database = FakeDatabase()
    database.on("SELECT", [metric_row("a", NOW), metric_row("b", NOW)])
    client = make_client(database)
    start, end = NOW - timedelta(hours=1), NOW + timedelta(hours=1)

    rows = await client.read_metrics(app_slug=None, start=start, end=end, limit=5)
    await client.read_metrics(app_slug="a", start=start, end=end, limit=5)

    assert [(r.app_slug, r.minute) for r in rows] == [("a", NOW), ("b", NOW)]
    first, second = database.connections[0].reads()
    assert (
        'WHERE "minute" >= %s AND "minute" < %s ORDER BY "minute" DESC, "app_slug" DESC LIMIT %s'
        in first.sql
    )
    assert first.sql.endswith('ORDER BY "minute", "app_slug"')
    assert first.params == (start, end, 5)
    assert 'AND "app_slug" = %s' in second.sql
    assert second.params == (start, end, "a", 5)


async def test_a_window_with_no_room_returns_nothing_without_a_query() -> None:
    database = FakeDatabase()
    client = make_client(database)

    assert await client.read_metrics(app_slug=None, start=NOW, end=NOW, limit=0) == ()
    assert await client.read_audit(app_slug=None, start=NOW, end=NOW, limit=0) == ()
    assert database.connections == []


async def test_read_audit_is_newest_first_within_the_window() -> None:
    database = FakeDatabase()
    database.on(
        "SELECT",
        [
            {
                "id": 9,
                "ts": NOW,
                "actor": ADMIN,
                "action": "app.register",
                "app_slug": "sales",
                "detail": {},
                "reason": None,
            },
            {
                "id": 8,
                "ts": NOW,
                "actor": ADMIN,
                "action": "app.start",
                "app_slug": "sales",
                "detail": {"x": 1},
                "reason": None,
            },
        ],
    )
    client = make_client(database)
    start, end = NOW - timedelta(hours=1), NOW + timedelta(hours=1)

    rows = await client.read_audit(app_slug="sales", start=start, end=end, limit=50)

    assert [type(r) for r in rows] == [AuditLogRow, AuditLogRow]
    assert [r.id for r in rows] == [9, 8]
    statement = database.connections[0].reads()[0]
    assert statement.sql.endswith(
        'WHERE "ts" >= %s AND "ts" < %s AND "app_slug" = %s ORDER BY "ts" DESC, "id" DESC LIMIT %s'
    )
    assert statement.params == (start, end, "sales", 50)


def audit_row(row_id: int, detail: object = None) -> dict[str, object]:
    return {
        "id": row_id,
        "ts": NOW,
        "actor": ADMIN,
        "action": "app.start",
        "app_slug": "sales",
        "detail": {} if detail is None else detail,
        "reason": None,
    }


async def test_read_audit_quarantines_a_row_its_model_cannot_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    database = FakeDatabase()
    # detail must be an object; a holder of the shared-SP token can store a list.
    database.on("SELECT", [audit_row(9), audit_row(8, [LEAK]), audit_row(7)])
    client = make_client(database)

    rows = await client.read_audit(app_slug=None, start=NOW, end=NOW + timedelta(hours=1), limit=5)

    assert [r.id for r in rows] == [9, 7]
    assert client.unreadable_rows == 1
    assert client.last_error is None
    assert LEAK not in caplog.text
    quarantine_logs = [r.getMessage() for r in caplog.records if "quarantine" in r.getMessage()]
    assert len(quarantine_logs) == 1
    assert "audit_log" in quarantine_logs[0]
    assert "key=8" in quarantine_logs[0]


async def test_read_metrics_quarantines_a_row_its_model_cannot_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    database = FakeDatabase()
    bad = {**metric_row("broken"), "egress_dropped_by_rule": {"rule": LEAK}}
    database.on("SELECT", [metric_row("a"), bad, metric_row("b")])
    client = make_client(database)

    rows = await client.read_metrics(
        app_slug=None, start=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1), limit=5
    )

    assert [r.app_slug for r in rows] == ["a", "b"]
    assert client.unreadable_rows == 1
    assert LEAK not in caplog.text
    quarantine_logs = [r.getMessage() for r in caplog.records if "quarantine" in r.getMessage()]
    assert len(quarantine_logs) == 1
    assert "metrics" in quarantine_logs[0]
    assert "broken" in quarantine_logs[0]


async def test_the_unreadable_row_count_adds_up_over_reads() -> None:
    database = FakeDatabase()
    database.on("SELECT", [audit_row(8, [LEAK])])
    client = make_client(database)
    window = {"app_slug": None, "start": NOW, "end": NOW + timedelta(hours=1), "limit": 5}

    assert await client.read_audit(**window) == ()
    assert await client.read_audit(**window) == ()

    assert client.unreadable_rows == 2


# --- audit-only writes (D1) ---------------------------------------------------------------------


async def test_an_audit_only_write_runs_as_the_service_principal_in_one_transaction() -> None:
    database = FakeDatabase()
    client = make_client(database)

    rows = await client.write(
        [], [audit_entry("release.view_source"), audit_entry("app.view_as_user")]
    )

    assert rows == ()
    connection = database.connections[0]
    assert connection.parameters.user == "sp-client-id"
    assert [w.sql.split(" ")[:3] for w in connection.writes()] == [
        ["INSERT", "INTO", "audit_log"]
    ] * 2
    assert connection.events == ["begin", "commit"]
    assert connection.reads() == []  # no row changed, so there is nothing to reload


async def test_an_audit_only_write_with_an_admin_token_logs_in_as_that_person() -> None:
    database = FakeDatabase()
    client = make_client(database)

    await client.write([], [audit_entry("release.view_source")], admin_token=ADMIN_TOKEN)

    connection = database.connections[0]
    assert connection.parameters.user == ADMIN
    assert connection.parameters.password.get_secret_value() == ADMIN_TOKEN.get_secret_value()
    assert connection.events == ["begin", "commit", "close"]


async def test_a_failing_audit_only_write_is_reported_and_leaves_nothing() -> None:
    database = FakeDatabase()
    database.on("INSERT INTO audit_log", pg.InsufficientPrivilege("denied"))
    client = make_client(database)

    with pytest.raises(RegistryError):
        await client.write([], [audit_entry("release.view_source")])

    assert database.connections[0].events == ["begin", "rollback"]


# --- a precondition on the stored status (D2) ---------------------------------------------------


def closed_request_row(status: str, closed_by: str) -> dict[str, object]:
    return {**request_row(7), "status": status, "closed_by": closed_by, "closed_at": NOW}


def withdraw(expected_status: str | None = "pending") -> Change:
    return Change(
        WriteKind.REQUEST_WITHDRAW,
        key={"request_id": 7},
        values={"status": "withdrawn", "closed_by": OWNER},
        expected_status=expected_status,
    )


async def test_an_expected_status_is_a_condition_of_the_update_itself() -> None:
    database = FakeDatabase()
    database.on('UPDATE "permission_requests"', [closed_request_row("withdrawn", OWNER)])
    client = make_client(database)

    rows = await client.write([withdraw()], [audit_entry("request.withdraw")])

    update = database.connections[0].writes()[0]
    assert update.sql.startswith(
        'UPDATE "permission_requests" SET "status" = %s, "closed_by" = %s '
        'WHERE "request_id" = %s AND "status" = %s RETURNING '
    )
    assert update.params == ("withdrawn", OWNER, 7, "pending")
    assert isinstance(rows[0], PermissionRequest)


async def test_without_an_expected_status_the_update_does_not_check_one() -> None:
    database = FakeDatabase()
    database.on('UPDATE "permission_requests"', [closed_request_row("withdrawn", OWNER)])
    client = make_client(database)

    await client.write([withdraw(None)], [audit_entry("request.withdraw")])

    update = database.connections[0].writes()[0]
    assert '"status" = %s RETURNING' not in update.sql
    assert update.params == ("withdrawn", OWNER, 7)


async def test_a_request_that_is_no_longer_in_the_expected_status_is_changed_by_others() -> None:
    database = FakeDatabase()
    database.on('UPDATE "permission_requests"', [])
    database.on("SELECT 1 FROM", [{"present": 1}])
    client = make_client(database)

    with pytest.raises(ChangedByOthers):
        await client.write([withdraw()], [audit_entry("request.withdraw")])

    connection = database.connections[0]
    check = connection.reads()[0]
    assert check.sql == 'SELECT 1 FROM "permission_requests" WHERE "request_id" = %s'
    assert check.params == (7,)
    assert connection.events == ["begin", "rollback"]


async def test_a_request_that_does_not_exist_is_row_missing_even_with_an_expected_status() -> None:
    database = FakeDatabase()
    database.on('UPDATE "permission_requests"', [])
    database.on("SELECT 1 FROM", [])
    client = make_client(database)

    with pytest.raises(RowMissing):
        await client.write([withdraw()], [audit_entry("request.withdraw")])


async def test_a_platform_admin_answer_carries_the_expected_status_too() -> None:
    database = FakeDatabase()
    database.on('UPDATE "permission_requests"', [closed_request_row("approved", ADMIN)])
    client = make_client(database)

    await client.write(
        [
            Change(
                WriteKind.REQUEST_ANSWER,
                key={"request_id": 7},
                values={"status": "approved", "closed_by": ADMIN},
                expected_status="pending",
            )
        ],
        [audit_entry("request.approve")],
        admin_token=ADMIN_TOKEN,
    )

    update = database.connections[0].writes()[0]
    assert update.sql.endswith(' AND "status" = %s RETURNING ' + update.sql.split("RETURNING ")[1])
    assert update.params == ("approved", ADMIN, 7, "pending")


# --- the login ----------------------------------------------------------------------------------


class FakePsycopgConnection:
    """What ``psycopg.AsyncConnection.connect`` returns, as far as the wrapper uses it."""

    def __init__(self) -> None:
        self.closed = False
        self.executed: list[tuple[object, object]] = []
        self.transactions = 0

    async def execute(self, query: object, params: object = None) -> str:
        self.executed.append((query, params))
        return "cursor"

    def transaction(self) -> str:
        self.transactions += 1
        return "transaction"

    async def close(self) -> None:
        self.closed = True


class PsycopgConnect:
    """Stands in for ``psycopg.AsyncConnection.connect`` and records the keyword arguments."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.error = error
        self.connection = FakePsycopgConnection()

    async def __call__(self, **keywords: object) -> FakePsycopgConnection:
        self.calls.append(keywords)
        if self.error is not None:
            raise self.error
        return self.connection


async def test_the_psycopg_login_requires_ssl_and_uses_autocommit_and_dict_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = PsycopgConnect()
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    parameters = ConnectionParameters(
        host="lakebase.example.invalid",
        port=5433,
        database="appmux_registry",
        user=ADMIN,
        password=ADMIN_TOKEN,
        connect_timeout_s=7,
    )

    await connect_with_psycopg(parameters)

    assert connect.calls == [
        {
            "host": "lakebase.example.invalid",
            "port": 5433,
            "dbname": "appmux_registry",
            "user": ADMIN,
            "password": ADMIN_TOKEN.get_secret_value(),
            "sslmode": "require",
            "connect_timeout": 7,
            "autocommit": True,
            "row_factory": dict_row,
        }
    ]


async def test_the_psycopg_wrapper_turns_a_text_query_into_sql_and_passes_the_rest_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connect = PsycopgConnect()
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    parameters = ConnectionParameters("h.example.invalid", 5432, "d", "u", SP_TOKEN)
    wrapper = await connect_with_psycopg(parameters)
    composed = SQL("SELECT {}").format(Identifier("slug"))

    await wrapper.execute("INSERT INTO audit_log (actor) VALUES (%s)", ("x",))
    await wrapper.execute(composed)
    transaction = wrapper.transaction()
    assert not wrapper.closed
    await wrapper.close()

    text_query, text_params = connect.connection.executed[0]
    assert isinstance(text_query, SQL)
    assert text_query.as_string() == "INSERT INTO audit_log (actor) VALUES (%s)"
    assert text_params == ("x",)
    assert connect.connection.executed[1] == (composed, None)
    assert transaction == "transaction"
    assert connect.connection.closed
    assert wrapper.closed


async def test_the_client_logs_in_through_psycopg_by_default_and_never_shows_the_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    connect = PsycopgConnect(
        psycopg.OperationalError(
            f"connection failed: password authentication failed ({SP_TOKEN.get_secret_value()})"
        )
    )
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    client = RegistryClient(
        host="lakebase.example.invalid",
        port=5432,
        database="appmux_registry",
        user="sp-client-id",
        token_provider=TokenSource(),
        connect_timeout_s=9,
    )

    with pytest.raises(RegistryUnavailable) as service:
        await client.refresh()
    with pytest.raises(RegistryRoleMissing) as admin:
        await client.write(register_changes(), [audit_entry()], admin_token=ADMIN_TOKEN)

    assert [c["connect_timeout"] for c in connect.calls] == [9, 9]
    assert [c["user"] for c in connect.calls] == ["sp-client-id", ADMIN]
    assert [c["sslmode"] for c in connect.calls] == ["require", "require"]
    shown = [str(service.value), repr(service.value), str(admin.value), repr(admin.value)]
    shown += [str(client.last_error), caplog.text]
    assert all(SP_TOKEN.get_secret_value() not in text for text in shown)


# --- one operation at a time on the service principal's connection ------------------------------


async def settle() -> None:
    """Let every task that can run, run."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_read_waits_while_a_write_is_inside_its_transaction() -> None:
    database = FakeDatabase()
    gate = Gate([{**grant_values(), "granted_at": NOW}])
    database.on('INSERT INTO "app_grants"', gate)
    client = make_client(database)

    writing = asyncio.create_task(
        client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )
    )
    await settle()
    reading = asyncio.create_task(client.fetch_rows("apps"))
    await settle()

    connection = database.connections[0]
    assert connection.events == ["begin"]
    assert connection.reads() == []  # the read has not started: it waits for the lock
    assert not reading.done()

    gate.open()
    await writing
    await reading
    assert len(database.connections) == 1
    assert connection.events[:2] == ["begin", "commit"]


async def test_a_new_token_does_not_close_the_connection_under_a_running_write() -> None:
    database = FakeDatabase()
    gate = Gate([{**grant_values(), "granted_at": NOW}])
    database.on('INSERT INTO "app_grants"', gate)
    tokens = TokenSource()
    client = make_client(database, tokens)

    writing = asyncio.create_task(
        client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )
    )
    await settle()
    tokens.token = NEXT_SP_TOKEN  # the token was minted anew while the write is in flight
    reading = asyncio.create_task(client.fetch_rows("apps"))
    await settle()

    first = database.connections[0]
    assert not first.closed
    assert len(database.connections) == 1

    gate.open()
    await writing
    await reading
    assert first.events[:2] == ["begin", "commit"]
    assert first.closed
    assert len(database.connections) == 2
    assert database.connections[1].parameters.password.get_secret_value() == (
        NEXT_SP_TOKEN.get_secret_value()
    )


# --- a write that committed ---------------------------------------------------------------------


async def test_a_write_before_the_first_load_is_not_failed_by_the_reload_after_it() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', [{**grant_values(), "granted_at": NOW}])
    database.on("SELECT", pg.AdminShutdown("terminating"))
    client = make_client(database)

    # No snapshot has ever been loaded, so the reload raises RegistryUnavailable on its own.
    rows = await client.write(
        [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
    )

    assert rows[0] is not None
    assert database.connections[0].events[:2] == ["begin", "commit"]
    assert client.last_error is not None
    with pytest.raises(RegistryUnavailable):
        _ = client.snapshot


# --- values the database cannot take ------------------------------------------------------------


@pytest.mark.parametrize(
    "raised",
    [
        pg.InvalidTextRepresentation(f"invalid input syntax: {LEAK}"),
        pg.CheckViolation(f"new row violates check: {LEAK}"),
        psycopg.DataError(f"PostgreSQL text fields cannot contain NUL: {LEAK}"),
    ],
    ids=["22P02", "23514", "client side"],
)
async def test_a_value_error_is_invalid_row_with_a_fixed_message_and_no_cause(
    raised: BaseException,
) -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', raised)
    client = make_client(database)

    with pytest.raises(InvalidRow) as caught:
        await client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )

    assert LEAK not in str(caught.value)
    assert LEAK not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert client.last_error is None  # the database answered: this is not an outage
    assert not database.connections[0].closed


async def test_a_client_side_data_error_has_the_default_invalid_row_message() -> None:
    database = FakeDatabase()
    database.on('INSERT INTO "app_grants"', psycopg.DataError("cannot contain NUL"))
    client = make_client(database)

    with pytest.raises(InvalidRow) as caught:
        await client.write(
            [Change(WriteKind.APP_GRANT_INSERT, values=grant_values())], [audit_entry("grant.app")]
        )

    assert caught.value.message == InvalidRow().message


async def test_a_nul_in_a_value_is_refused_before_any_connection_and_names_the_column() -> None:
    database = FakeDatabase()
    client = make_client(database)
    values = {**app_values(), "display_name": "Sa\x00les"}

    with pytest.raises(InvalidRow) as caught:
        await client.write(
            [Change(WriteKind.APP_INSERT, values=values)], [audit_entry()], admin_token=ADMIN_TOKEN
        )

    assert "display_name" in str(caught.value)
    assert database.connections == []


async def test_a_nul_inside_a_json_value_or_a_key_is_refused_before_any_connection() -> None:
    database = FakeDatabase()
    client = make_client(database)

    with pytest.raises(InvalidRow) as in_json:
        await client.write(
            [
                Change(
                    WriteKind.APP_UPDATE,
                    key={"slug": "sales"},
                    values={"egress": ["a\x00b"]},
                    expected_version=1,
                )
            ],
            [audit_entry()],
            admin_token=ADMIN_TOKEN,
        )
    with pytest.raises(InvalidRow) as in_key:
        await client.write(
            [
                Change(
                    WriteKind.APP_GRANT_DELETE, key={**GRANT_KEY, "principal": "x\x00@example.com"}
                )
            ],
            [audit_entry("grant.app")],
        )

    assert "egress" in str(in_json.value)
    assert "principal" in str(in_key.value)
    assert database.connections == []


async def test_a_nul_in_an_audit_reason_is_refused_before_any_connection() -> None:
    database = FakeDatabase()
    client = make_client(database)
    entry = AuditEntry.model_validate(
        {"actor": ADMIN, "action": "app.start", "app_slug": "sales", "reason": "no\x00pe"}
    )

    with pytest.raises(InvalidRow) as caught:
        await client.write([], [entry])

    assert "reason" in str(caught.value)
    assert database.connections == []


# --- the token provider -------------------------------------------------------------------------


async def test_a_token_provider_failure_names_its_class_in_the_log_and_chains_no_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    database = FakeDatabase()
    tokens = TokenSource()
    tokens.error = TimeoutError(f"token endpoint said {LEAK}")
    client = make_client(database, tokens)

    with pytest.raises(RegistryUnavailable) as raised:
        await client.refresh()

    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert "TimeoutError" in caplog.text
    assert LEAK not in caplog.text
