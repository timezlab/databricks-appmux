"""The migration loader: fill an empty registry database from ``migrations/*.sql``, once.

``apply_migrations`` reads the ``.sql`` files of a folder, orders them by their number, and runs
them in ONE transaction, so a file that fails leaves the database as it was. Before it runs
anything it refuses in two cases (design 13.1, "What the operator sets up"):

* a table named ``apps`` already exists. The registry was loaded before, or something else owns
  that name; either way the loader must not run the baseline on top of it.
* a role the grants need does not exist (``appmux_sp``, ``appmux_platform_writers``). The Lakebase
  project owner creates both before the loader runs and ``0002_grants.sql`` creates no role.

It invents no migration table: v1 has the baseline, its grants and one later file
(``0003_service_principal_grants.sql``, ADR 0020), and a later migration needs its own decision.
The consequence is that the loader keeps no record of what it applied, so the ``apps`` check is
also what stops a second run, and a registry loaded before a later file existed gets that file
applied by hand.

Choices that are easy to get wrong:

* **A file is passed whole to one ``execute`` call, with no parameters.** psycopg runs a
  multi-statement string given without parameters in one round trip (the simple query protocol),
  so the loader has no statement splitter that could cut inside a string, a comment or a
  dollar-quoted body, and a ``%`` in the SQL (``LIKE '/Workspace/%'``) is not read as a
  placeholder. A migration file therefore holds no ``BEGIN``/``COMMIT`` of its own (the baseline
  says so in its header): the loader owns the transaction.
* **The checks run inside the transaction.** A psycopg connection that has already run a
  statement turns ``transaction()`` into a savepoint, which nothing commits; opening the block
  first and asking the questions inside it keeps the block the outermost one. The connection
  handed in must be idle, with nothing run on it before.
* **The order is the number, not the text of the name.** ``9_x.sql`` runs before ``10_y.sql``.
  A ``.sql`` file with no ``NNNN_description.sql`` name, or two files with the same number, are
  refused: their place in the order would be a guess.

The folder is a parameter, because the migrations folder of a repository checkout and that of the
release zip are not found the same way. This module opens no connection and imports no database
or network library: the caller opens the connection (a psycopg ``AsyncConnection`` satisfies
``MigrationConnection``), calls ``apply_migrations`` and closes it. The command-line entry point
is ``appmux.registry.load_schema`` (T217).
"""

from __future__ import annotations

import logging
import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from appmux.registry.writers import Identity

logger = logging.getLogger(__name__)

# The roles 0002_grants.sql grants to: one per write identity, defined once in writers.py.
MIGRATION_ROLES: Final[tuple[str, ...]] = tuple(identity.value for identity in Identity)

_MIGRATION_NAME: Final = re.compile(r"(?P<number>[0-9]+)_.+\.sql", re.IGNORECASE)
_NAME_FORM: Final = "NNNN_description.sql"

# Both questions return a row when the answer is yes and none when it is no.
# to_regclass resolves the name along the search path, as the unqualified names of the
# migrations do, and needs no privilege on the table.
_APPS_TABLE_EXISTS: Final = "SELECT 1 WHERE to_regclass('apps') IS NOT NULL"
_ROLE_EXISTS: Final = "SELECT 1 FROM pg_roles WHERE rolname = %s"


class MigrationRefused(Exception):
    """The loader declined to start; nothing was run. The message says what to do next."""


class RegistryAlreadySetUp(MigrationRefused):
    """A table named ``apps`` exists, so this database is not empty."""


class RolesMissing(MigrationRefused):
    """A role the grants need does not exist; ``missing`` lists every one that does not."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(
            f"cannot load the registry: missing database role(s): {', '.join(missing)}. "
            "The Lakebase project owner creates the roles before the loader runs "
            "(design 13.1, 'What the operator sets up', step 2). "
            "Create them, then run the loader again."
        )


class MigrationFailed(Exception):
    """A file was refused by the database; the transaction was not committed."""

    def __init__(self, file_name: str, cause: Exception) -> None:
        self.file_name = file_name
        super().__init__(
            f"{file_name} failed and nothing was committed, so the database is as it was "
            f"before the loader ran. Fix the cause, then run the loader again. Cause: {cause}"
        )


class MigrationCursor(Protocol):
    """What the loader reads from the result of a question."""

    async def fetchone(self) -> object | None: ...


class MigrationConnection(Protocol):
    """The part of a psycopg ``AsyncConnection`` the loader uses.

    ``execute`` runs a statement (the loader gives ``params`` only to its two questions);
    ``transaction`` returns the block that commits when it ends without an error and rolls back
    when it ends with one.
    """

    async def execute(self, query: str, params: Any = None) -> MigrationCursor: ...

    def transaction(self) -> AbstractAsyncContextManager[Any]: ...


@dataclass(frozen=True, slots=True)
class Migration:
    """One file of the migrations folder: its number, its name and its SQL text."""

    number: int
    name: str
    sql: str


def discover_migrations(folder: Path) -> list[Migration]:
    """Read the ``.sql`` files of ``folder`` (not its subfolders), in the order to apply them.

    Raises ``MigrationRefused`` when the folder is missing or holds no ``.sql`` file, when a
    ``.sql`` file is not named ``NNNN_description.sql`` or cannot be read as UTF-8 text, or when
    two files share a number. Files that do not end in ``.sql`` are ignored.
    """
    if not folder.is_dir():
        raise MigrationRefused(
            f"{folder} is not a folder; pass the migrations folder of the release."
        )
    paths = [p for p in folder.iterdir() if p.suffix.lower() == ".sql" and p.is_file()]
    if not paths:
        raise MigrationRefused(
            f"no .sql file in {folder}; pass the migrations folder of the release."
        )

    migrations: list[Migration] = []
    for path in paths:
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationRefused(
                f"{path.name} in {folder} is not named {_NAME_FORM}, so its place in the order "
                "is unknown. Rename it, or move it out of the folder."
            )
        try:
            # Blocking reads are fine: a few small files, read once before the database is touched.
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MigrationRefused(
                f"cannot read {path.name} as UTF-8 text. Fix or remove the file, then run the "
                "loader again."
            ) from exc
        migrations.append(Migration(number=int(match["number"]), name=path.name, sql=sql))

    _refuse_repeated_numbers(migrations)
    return sorted(migrations, key=lambda migration: (migration.number, migration.name))


def _refuse_repeated_numbers(migrations: list[Migration]) -> None:
    names_by_number: dict[int, list[str]] = {}
    for migration in migrations:
        names_by_number.setdefault(migration.number, []).append(migration.name)
    for number, names in sorted(names_by_number.items()):
        if len(names) > 1:
            raise MigrationRefused(
                f"{', '.join(sorted(names))} share the number {number}, so their order is "
                "open. Give each migration its own number."
            )


async def apply_migrations(connection: MigrationConnection, folder: Path) -> list[str]:
    """Apply every migration of ``folder`` in one transaction; return their file names in order.

    ``connection`` must be idle (see the module docstring). Raises ``MigrationRefused`` (or one
    of its two subclasses) before any file runs, and ``MigrationFailed``, chained to the
    driver's error, when the database refuses a file. An error that the commit itself raises
    reaches the caller unchanged.
    """
    migrations = discover_migrations(folder)

    async with connection.transaction():
        await _refuse_unless_empty_and_granted(connection)
        for migration in migrations:
            logger.info("applying migration %s", migration.name)
            try:
                await connection.execute(migration.sql)
            except Exception as exc:
                # Broad on purpose: the driver's error class cannot be named here (this module
                # imports no driver). It is chained and shown, and it leaves the transaction
                # block, which rolls back.
                raise MigrationFailed(migration.name, exc) from exc
    return [migration.name for migration in migrations]


async def _refuse_unless_empty_and_granted(connection: MigrationConnection) -> None:
    if await _answers_yes(connection, _APPS_TABLE_EXISTS):
        raise RegistryAlreadySetUp(
            "cannot load the registry: a table named apps already exists in this database. "
            "The loader fills an empty database once and keeps no record of what it applied, "
            "so it cannot update a loaded one. If appmux's tables are already here there is "
            "nothing to load; otherwise run the loader against an empty database."
        )
    missing: list[str] = []
    # Two fixed roles, so two questions; the loader has no list-fetching method on purpose.
    for role in MIGRATION_ROLES:
        if not await _answers_yes(connection, _ROLE_EXISTS, (role,)):
            missing.append(role)
    if missing:
        raise RolesMissing(tuple(missing))


async def _answers_yes(
    connection: MigrationConnection, question: str, params: tuple[str, ...] | None = None
) -> bool:
    cursor = await connection.execute(question, params)
    return await cursor.fetchone() is not None
