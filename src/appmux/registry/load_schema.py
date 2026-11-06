"""The schema-loading command: ``python -m appmux.registry.load_schema`` (T217, design 13.1).

It fills an empty registry database from ``migrations/*.sql`` with ``apply_migrations`` and tells
the person (or the deploy script) who ran it what happened. It runs on the deployer's side, **with
the deployer's own database login**, never inside the App and never as the appmux service
principal: the App's role must not own the tables or hold DDL rights, or a child holding the shared
service principal's token could rewrite ``apps`` (ADR 0004, data model 6).

Settings come from the standard libpq environment variables, and from nothing else, so no token
ever sits on a command line (a command line is visible in the process list and the shell history):

=============  ========  ====================================================================
``PGHOST``     required  the Lakebase endpoint's host
``PGUSER``     required  the deployer's database role (their e-mail address)
``PGPASSWORD`` required  an OAuth token for that role (``databricks postgres
                         generate-database-credential``); it expires after about an hour
``PGPORT``     optional  default 5432
``PGDATABASE`` optional  default ``databricks_postgres``, the database of a new Lakebase project
=============  ========  ====================================================================

The one argument is ``--migrations FOLDER`` (default ``migrations`` in the current folder: the
repository root, or the unpacked release folder, which both hold it).

Exit codes, so that a deploy script can run the command on every deploy:

==  =========================================================================================
0   the registry was empty and is loaded; the applied files are named
3   a table named ``apps`` exists, so the registry is already loaded; nothing was changed
2   refused before anything ran: a setting is missing, a database role is missing, the
    migrations folder is unusable
1   failed: the login, the database or a file was refused; nothing was committed (a failure of
    the commit itself is the one case where running the command again tells which way it went)
==  =========================================================================================

Exit 3 is not a failure, but it is also not "up to date": the loader keeps no record of what it
applied (no tenth table, ADR 0004), so a migration file added after the first load is applied by
hand (``docs/tech-debt.md`` TD-008). The message says so.

Output never holds the token or a driver message: the driver's text can quote a connection string
or a row, so a database error is shown by its class only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Protocol

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from appmux.registry.client import ConnectionParameters
from appmux.registry.migrations import (
    MigrationConnection,
    MigrationCursor,
    MigrationFailed,
    MigrationRefused,
    RegistryAlreadySetUp,
    apply_migrations,
)

EXIT_LOADED: Final = 0
EXIT_FAILED: Final = 1
EXIT_REFUSED: Final = 2
EXIT_ALREADY_LOADED: Final = 3

DEFAULT_PORT: Final = 5432
# A new Lakebase project's database; app.yaml names the same one.
DEFAULT_DATABASE: Final = "databricks_postgres"
DEFAULT_MIGRATIONS_FOLDER: Final = Path("migrations")
_CONNECT_TIMEOUT_S: Final = 30  # as the registry client's own login (spike 1)


class LoaderSettings(BaseModel):
    """The login, parsed from the environment at the boundary; a refused value is never echoed."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    host: str = Field(alias="PGHOST", min_length=1)
    user: str = Field(alias="PGUSER", min_length=1)
    password: SecretStr = Field(alias="PGPASSWORD", min_length=1)
    port: int = Field(default=DEFAULT_PORT, alias="PGPORT", ge=1, le=65535)
    database: str = Field(default=DEFAULT_DATABASE, alias="PGDATABASE", min_length=1)


class LoaderConnection(MigrationConnection, Protocol):
    """What the command needs from a login: the loader's connection, and a way to close it."""

    async def close(self) -> None: ...


OpenConnection = Callable[[ConnectionParameters], Awaitable[LoaderConnection]]


class _PsycopgLoaderConnection:
    """A psycopg connection as ``LoaderConnection``.

    psycopg types a query as ``LiteralString``; the loader hands over the text of a migration file,
    so the conversion to ``sql.SQL`` is made here, once (as ``client._PsycopgConnection`` does).
    """

    def __init__(self, connection: psycopg.AsyncConnection[Any]) -> None:
        self._connection = connection

    async def execute(self, query: str, params: Any = None) -> MigrationCursor:
        return await self._connection.execute(sql.SQL(query), params)

    def transaction(self) -> Any:
        return self._connection.transaction()

    async def close(self) -> None:
        await self._connection.close()


async def open_psycopg_connection(parameters: ConnectionParameters) -> LoaderConnection:
    """Log in as the deployer: SSL required, ``autocommit`` so the loader owns the transaction."""
    connection = await psycopg.AsyncConnection.connect(
        host=parameters.host,
        port=parameters.port,
        dbname=parameters.database,
        user=parameters.user,
        password=parameters.password.get_secret_value(),
        sslmode="require",
        connect_timeout=parameters.connect_timeout_s,
        autocommit=True,
    )
    return _PsycopgLoaderConnection(connection)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m appmux.registry.load_schema",
        description="Load the appmux registry schema into an empty Lakebase database.",
        epilog="The login comes from PGHOST, PGUSER and PGPASSWORD (and optionally PGPORT and "
        "PGDATABASE); a token is never taken from the command line.",
    )
    parser.add_argument(
        "--migrations",
        type=Path,
        default=DEFAULT_MIGRATIONS_FOLDER,
        help="the folder of NNNN_description.sql files (default: ./migrations)",
    )
    return parser


def _say(message: str) -> None:
    print(message, flush=True)


def _complain(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _read_settings(environ: Mapping[str, str]) -> LoaderSettings | None:
    """The settings, or ``None`` after naming each setting that is missing or unusable."""
    names = [field.alias for field in LoaderSettings.model_fields.values() if field.alias]
    try:
        return LoaderSettings.model_validate({n: environ[n] for n in names if n in environ})
    except ValidationError as exc:
        problems = {
            str(problem["loc"][0])
            for problem in exc.errors(include_url=False, include_context=False, include_input=False)
        }
        _complain(
            "cannot load the registry: set "
            + ", ".join(sorted(problems))
            + " in the environment (a missing or unusable value; the values are not shown). "
            "Token: databricks postgres generate-database-credential. "
            "See the module docstring for the list."
        )
        return None


async def run(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str],
    connect: OpenConnection = open_psycopg_connection,
) -> int:
    """Run the command and return its exit code (see the module docstring)."""
    arguments = build_parser().parse_args(argv)
    settings = _read_settings(environ)
    if settings is None:
        return EXIT_REFUSED

    parameters = ConnectionParameters(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=settings.user,
        password=settings.password,
        connect_timeout_s=_CONNECT_TIMEOUT_S,
    )
    try:
        connection = await connect(parameters)
    except (psycopg.Error, OSError) as exc:
        # The class only: libpq's text can name the host and the role.
        _complain(
            f"cannot log in to the registry database ({type(exc).__name__}). Check PGHOST and "
            "PGUSER, that the token in PGPASSWORD is fresh (it lasts about an hour), and that the "
            "role is a member of the project's owner role."
        )
        return EXIT_FAILED
    try:
        return await _load(connection, arguments.migrations)
    finally:
        await connection.close()


async def _load(connection: LoaderConnection, folder: Path) -> int:
    try:
        applied = await apply_migrations(connection, folder)
    except RegistryAlreadySetUp:
        _say(
            "the registry is already loaded (a table named apps exists); nothing was changed. "
            "This command keeps no record of what it applied, so a migration file added since "
            "the first load is applied by hand (design 13.1, docs/tech-debt.md TD-008)."
        )
        return EXIT_ALREADY_LOADED
    except MigrationRefused as refusal:
        _complain(str(refusal))
        return EXIT_REFUSED
    except MigrationFailed as failure:
        # The message names the file and carries the driver's text as its cause; show the file and
        # the class of the cause, which is enough to find it and cannot quote a row or a token.
        cause = type(failure.__cause__).__name__ if failure.__cause__ is not None else "unknown"
        _complain(
            f"{failure.file_name} failed ({cause}) and nothing was committed, so the database "
            "is as it was before. Fix the cause, then run the command again."
        )
        return EXIT_FAILED
    except (psycopg.Error, OSError) as exc:
        _complain(
            f"the load stopped on a database error ({type(exc).__name__}). Nothing was committed "
            "unless the error came from the commit itself; run the command again: if the registry "
            "was loaded it says so."
        )
        return EXIT_FAILED

    _say(f"loaded the registry: applied {len(applied)} migration file(s)")
    for name in applied:
        _say(f"  {name}")
    return EXIT_LOADED


def main() -> None:
    raise SystemExit(asyncio.run(run(sys.argv[1:], environ=os.environ)))


if __name__ == "__main__":
    main()
