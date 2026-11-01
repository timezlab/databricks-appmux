"""The migration loader (T021): one transaction, two refusals, file-name order, no database.

The loader talks to the database only through a small protocol, so a fake connection stands in
for psycopg here. The fake keeps what a real transaction would keep: SQL executed inside a
``transaction()`` block is *committed* when the block ends without an error and *discarded* when
it ends with one. A test that sees nothing committed after a failure therefore proves that every
file ran inside the one block.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from appmux.registry.migrations import (
    MIGRATION_ROLES,
    MigrationFailed,
    MigrationRefused,
    RegistryAlreadySetUp,
    RolesMissing,
    apply_migrations,
)
from appmux.registry.writers import Identity

pytestmark = pytest.mark.anyio

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOTH_ROLES = ("appmux_sp", "appmux_platform_writers")


class FakeDatabaseError(Exception):
    """What a driver raises when the server refuses a statement."""


class FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class FakeTransaction:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeTransaction:
        connection = self._connection
        assert not connection.is_in_transaction, "the loader opened a second transaction"
        connection.is_in_transaction = True
        connection.transactions_opened += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        connection = self._connection
        try:
            if exc_type is not None:
                connection.rollbacks += 1
            elif connection.commit_error is not None:
                # The server refused the COMMIT: nothing is kept, and the driver raises.
                connection.rollbacks += 1
                raise connection.commit_error
            else:
                connection.committed_sql.extend(connection.pending_sql)
                connection.commits += 1
        finally:
            connection.pending_sql = []
            connection.is_in_transaction = False


class FakeConnection:
    """Records every statement; answers the loader's two existence questions from its state."""

    def __init__(
        self,
        *,
        tables: tuple[str, ...] = (),
        roles: tuple[str, ...] = BOTH_ROLES,
        fail_when_sql_contains: str | None = None,
        commit_error: Exception | None = None,
    ) -> None:
        self.tables = tables
        self.roles = roles
        self.fail_when_sql_contains = fail_when_sql_contains
        self.commit_error = commit_error
        self.is_in_transaction = False
        self.transactions_opened = 0
        self.commits = 0
        self.rollbacks = 0
        self.pending_sql: list[str] = []
        self.committed_sql: list[str] = []
        self.outside_transaction: list[str] = []
        self.migration_calls: list[tuple[str, Any]] = []
        self.question_calls: list[tuple[str, Any]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execute(self, query: str, params: Any = None) -> FakeCursor:
        if not self.is_in_transaction:
            self.outside_transaction.append(query)
        table = re.search(r"to_regclass\('(\w+)'\)", query)
        if table is not None:
            self.question_calls.append((query, params))
            return FakeCursor((1,) if table.group(1) in self.tables else None)
        if "pg_roles" in query:
            self.question_calls.append((query, params))
            return FakeCursor((1,) if params[0] in self.roles else None)
        self.migration_calls.append((query, params))
        if self.fail_when_sql_contains and self.fail_when_sql_contains in query:
            raise FakeDatabaseError('relation "nothing" does not exist')
        self.pending_sql.append(query)
        return FakeCursor(None)

    @property
    def migration_sql(self) -> list[str]:
        return [query for query, _ in self.migration_calls]


def write_files(folder: Path, files: dict[str, str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def two_files(tmp_path: Path) -> Path:
    return write_files(
        tmp_path / "migrations",
        {"0001_first.sql": "CREATE TABLE apps (slug text);", "0002_second.sql": "GRANT 1;"},
    )


async def test_files_are_applied_in_file_name_order_whatever_the_order_they_were_written(
    tmp_path: Path,
) -> None:
    # Written last-first, so a loader that trusts directory order gets it wrong.
    folder = write_files(
        tmp_path,
        {"0003_c.sql": "THIRD;", "0001_a.sql": "FIRST;", "0002_b.sql": "SECOND;"},
    )
    connection = FakeConnection()

    applied = await apply_migrations(connection, folder)

    assert connection.migration_sql == ["FIRST;", "SECOND;", "THIRD;"]
    assert applied == ["0001_a.sql", "0002_b.sql", "0003_c.sql"]


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        pytest.param(
            ["0010_late.sql", "0002_grants.sql", "0001_baseline.sql"],
            ["0001_baseline.sql", "0002_grants.sql", "0010_late.sql"],
            id="0002 before 0010",
        ),
        pytest.param(
            ["10_late.sql", "9_early.sql", "100_last.sql"],
            ["9_early.sql", "10_late.sql", "100_last.sql"],
            id="unpadded numbers go by value, not by text",
        ),
        pytest.param(
            ["0002_b.SQL", "0001_a.sql"],
            ["0001_a.sql", "0002_b.SQL"],
            id="the extension is not case sensitive",
        ),
    ],
)
async def test_numbers_decide_the_order_where_the_names_sort_wrongly_as_text(
    tmp_path: Path, names: list[str], expected: list[str]
) -> None:
    # As text "10_late.sql" sorts before "9_early.sql": a plain sorted() of the names is wrong.
    folder = write_files(tmp_path, {name: f"-- {name}" for name in names})
    connection = FakeConnection()

    applied = await apply_migrations(connection, folder)

    assert applied == expected
    assert connection.migration_sql == [f"-- {name}" for name in expected]


async def test_every_file_and_both_checks_run_in_one_transaction_that_commits_once(
    tmp_path: Path,
) -> None:
    connection = FakeConnection()

    await apply_migrations(connection, two_files(tmp_path))

    assert connection.transactions_opened == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.outside_transaction == []
    assert connection.committed_sql == ["CREATE TABLE apps (slug text);", "GRANT 1;"]


async def test_a_failing_second_file_rolls_back_the_first(tmp_path: Path) -> None:
    connection = FakeConnection(fail_when_sql_contains="GRANT 1")

    with pytest.raises(MigrationFailed) as failure:
        await apply_migrations(connection, two_files(tmp_path))

    # The first file did run, and nothing of it survived.
    assert connection.migration_sql == ["CREATE TABLE apps (slug text);", "GRANT 1;"]
    assert connection.committed_sql == []
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.transactions_opened == 1
    assert failure.value.file_name == "0002_second.sql"
    assert "0002_second.sql" in str(failure.value)
    assert "nothing was committed" in str(failure.value)
    # The server's reason stays reachable: in the chain and in the message.
    assert isinstance(failure.value.__cause__, FakeDatabaseError)
    assert 'relation "nothing" does not exist' in str(failure.value)


async def test_an_error_raised_by_the_commit_itself_reaches_the_caller_unchanged(
    tmp_path: Path,
) -> None:
    # Every file ran without error, so no file is to blame: the loader must not dress a commit
    # failure up as a MigrationFailed naming the last file, and it must not swallow it.
    commit_error = FakeDatabaseError("could not serialize access")
    connection = FakeConnection(commit_error=commit_error)

    with pytest.raises(FakeDatabaseError) as failure:
        await apply_migrations(connection, two_files(tmp_path))

    assert failure.value is commit_error
    assert connection.migration_sql == ["CREATE TABLE apps (slug text);", "GRANT 1;"]
    assert connection.committed_sql == []
    assert connection.commits == 0


async def test_a_failing_first_file_leaves_the_second_unrun(tmp_path: Path) -> None:
    connection = FakeConnection(fail_when_sql_contains="CREATE TABLE")

    with pytest.raises(MigrationFailed) as failure:
        await apply_migrations(connection, two_files(tmp_path))

    assert connection.migration_sql == ["CREATE TABLE apps (slug text);"]
    assert failure.value.file_name == "0001_first.sql"
    assert connection.committed_sql == []


async def test_a_table_named_apps_refuses_the_load_before_anything_runs(tmp_path: Path) -> None:
    connection = FakeConnection(tables=("apps",))

    with pytest.raises(RegistryAlreadySetUp) as refusal:
        await apply_migrations(connection, two_files(tmp_path))

    message = str(refusal.value)
    assert "apps" in message
    assert "empty database" in message  # the next step
    assert connection.migration_calls == []
    assert connection.committed_sql == []
    assert connection.commits == 0
    assert isinstance(refusal.value, MigrationRefused)


@pytest.mark.parametrize(
    ("present", "missing"),
    [
        pytest.param(("appmux_sp",), ("appmux_platform_writers",), id="writers missing"),
        pytest.param(("appmux_platform_writers",), ("appmux_sp",), id="service principal missing"),
        pytest.param((), BOTH_ROLES, id="both missing"),
    ],
)
async def test_a_missing_role_refuses_the_load_before_anything_runs_and_names_each_missing_role(
    tmp_path: Path, present: tuple[str, ...], missing: tuple[str, ...]
) -> None:
    connection = FakeConnection(roles=present)

    with pytest.raises(RolesMissing) as refusal:
        await apply_migrations(connection, two_files(tmp_path))

    message = str(refusal.value)
    assert refusal.value.missing == missing
    for role in missing:
        assert role in message
    for role in present:
        assert role not in message
    assert "13.1" in message
    assert connection.migration_calls == []
    assert connection.committed_sql == []
    assert connection.commits == 0
    assert isinstance(refusal.value, MigrationRefused)


async def test_the_refusals_are_asked_inside_the_transaction_so_a_driver_that_has_begun_one_commits(
    tmp_path: Path,
) -> None:
    # psycopg turns a transaction() block into a savepoint, which nothing commits, when a
    # statement ran on the connection before it. So even the questions run inside the block.
    connection = FakeConnection()

    await apply_migrations(connection, two_files(tmp_path))

    assert connection.question_calls  # the checks did run
    assert connection.outside_transaction == []


async def test_the_table_check_is_asked_about_apps_and_each_role_by_parameter(
    tmp_path: Path,
) -> None:
    connection = FakeConnection()

    await apply_migrations(connection, two_files(tmp_path))

    asked_roles = [params[0] for query, params in connection.question_calls if "pg_roles" in query]
    asked_tables = [query for query, _ in connection.question_calls if "to_regclass" in query]
    assert asked_roles == list(MIGRATION_ROLES)
    assert len(asked_tables) == 1
    assert "to_regclass('apps')" in asked_tables[0]
    # No role name is spliced into the SQL text.
    assert not any(role in query for query, _ in connection.question_calls for role in BOTH_ROLES)


async def test_an_empty_folder_is_refused_without_touching_the_database(tmp_path: Path) -> None:
    folder = tmp_path / "migrations"
    folder.mkdir()
    connection = FakeConnection()

    with pytest.raises(MigrationRefused) as refusal:
        await apply_migrations(connection, folder)

    assert "no .sql" in str(refusal.value)
    assert connection.transactions_opened == 0
    assert connection.migration_calls == []
    assert connection.question_calls == []


async def test_a_folder_that_does_not_exist_is_refused_without_touching_the_database(
    tmp_path: Path,
) -> None:
    connection = FakeConnection()

    with pytest.raises(MigrationRefused):
        await apply_migrations(connection, tmp_path / "nowhere")

    assert connection.transactions_opened == 0


async def test_files_that_are_not_sql_and_folders_are_ignored(tmp_path: Path) -> None:
    folder = write_files(
        tmp_path,
        {
            "0001_a.sql": "ONLY ME;",
            "README.md": "# not sql",
            "0002_notes.txt": "SELECT 'no';",
            "0003_backup.sql.bak": "SELECT 'no';",
            "0004_editor.sql~": "SELECT 'no';",
            "schema": "no extension",
        },
    )
    write_files(folder / "old", {"0001_old.sql": "SELECT 'in a subfolder';"})
    (folder / "0005_folder.sql").mkdir()
    connection = FakeConnection()

    applied = await apply_migrations(connection, folder)

    assert applied == ["0001_a.sql"]
    assert connection.migration_sql == ["ONLY ME;"]


@pytest.mark.parametrize(
    "bad_name",
    ["schema.sql", "notes_0003.sql", "_0003.sql", "0003.sql", "-1_x.sql", "٣_arabic_digit.sql"],
)
async def test_a_sql_file_without_a_number_and_a_description_is_refused_not_guessed(
    tmp_path: Path, bad_name: str
) -> None:
    folder = write_files(tmp_path, {"0001_a.sql": "FIRST;", bad_name: "STRAY;"})
    connection = FakeConnection()

    with pytest.raises(MigrationRefused) as refusal:
        await apply_migrations(connection, folder)

    assert bad_name in str(refusal.value)
    assert "NNNN_description.sql" in str(refusal.value)
    assert connection.transactions_opened == 0
    assert connection.migration_calls == []


async def test_two_files_with_the_same_number_are_refused_because_their_order_is_open(
    tmp_path: Path,
) -> None:
    folder = write_files(
        tmp_path, {"0002_grants.sql": "A;", "0002_indexes.sql": "B;", "2_old_style.sql": "C;"}
    )
    connection = FakeConnection()

    with pytest.raises(MigrationRefused) as refusal:
        await apply_migrations(connection, folder)

    message = str(refusal.value)
    assert "0002_grants.sql" in message
    assert "0002_indexes.sql" in message
    assert "2_old_style.sql" in message
    assert connection.transactions_opened == 0


async def test_a_file_that_is_not_utf8_text_is_refused_before_anything_runs(
    tmp_path: Path,
) -> None:
    folder = write_files(tmp_path, {"0001_a.sql": "FIRST;"})
    (folder / "0002_binary.sql").write_bytes(b"\xff\xfe\x00 not text")
    connection = FakeConnection()

    with pytest.raises(MigrationRefused) as refusal:
        await apply_migrations(connection, folder)

    assert "0002_binary.sql" in str(refusal.value)
    assert connection.transactions_opened == 0
    assert connection.migration_calls == []


async def test_a_file_reaches_the_driver_whole_and_unchanged_with_no_parameters(
    tmp_path: Path,
) -> None:
    # Choice: the loader never splits a file into statements. psycopg runs a multi-statement
    # string passed without parameters in one call (the simple query protocol), so a ";" inside
    # a string, a comment or a dollar-quoted body is never seen by a splitter that could get it
    # wrong, and a "%" (as in LIKE '/Workspace/%') is never read as a placeholder.
    text = (
        "-- a comment with a ; in it\n"
        "CREATE TABLE a (x text DEFAULT 'one; two', y text CHECK (y LIKE '/Workspace/%'));\n"
        "/* block; comment */\n"
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $body$ SELECT 1; SELECT 2; $body$;\n"
        "COMMENT ON TABLE a IS 'it''s; \"quoted\"';\n"
    )
    folder = write_files(tmp_path, {"0001_tricky.sql": text})
    connection = FakeConnection()

    await apply_migrations(connection, folder)

    assert connection.migration_calls == [(text, None)]


async def test_the_migrations_of_the_repository_are_applied_whole_and_in_order() -> None:
    folder = REPOSITORY_ROOT / "migrations"
    connection = FakeConnection()

    applied = await apply_migrations(connection, folder)

    # v1 has the baseline and its grants; a later migration needs its own decision.
    assert applied[:2] == ["0001_baseline.sql", "0002_grants.sql"]
    assert connection.migration_calls == [
        ((folder / name).read_text(encoding="utf-8"), None) for name in applied
    ]
    assert connection.committed_sql == connection.migration_sql


def test_the_roles_the_loader_demands_are_the_two_write_identities() -> None:
    assert set(MIGRATION_ROLES) == {identity.value for identity in Identity}
    assert len(MIGRATION_ROLES) == len(set(MIGRATION_ROLES))


def test_every_role_the_grants_file_names_is_demanded_by_the_loader() -> None:
    grants = (REPOSITORY_ROOT / "migrations" / "0002_grants.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in grants.splitlines())
    named = {
        role.strip()
        for target in re.findall(r"\bTO\s+([a-z_, ]+);", code)
        for role in target.split(",")
    }

    assert named
    assert named <= set(MIGRATION_ROLES)
