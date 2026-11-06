"""The schema-loading command (T217): environment in, exit code out, no secret in any output.

The command wraps ``apply_migrations`` (tested in ``test_registry_migrations.py``) for whoever
deploys. What it adds, and what is tested here with no database: the settings come from the
environment and never from the command line; every outcome has its own exit code, so a deploy
script can tell "loaded" and "already loaded" from a failure; the connection is closed on every
path; and neither the token nor the driver's own message reaches the output.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from appmux.registry import load_schema
from appmux.registry.client import ConnectionParameters
from appmux.registry.load_schema import (
    EXIT_ALREADY_LOADED,
    EXIT_FAILED,
    EXIT_LOADED,
    EXIT_REFUSED,
    run,
)
from appmux.registry.migrations import (
    MigrationConnection,
    MigrationFailed,
    MigrationRefused,
    RegistryAlreadySetUp,
    RolesMissing,
)

pytestmark = pytest.mark.anyio

# Fake, built at run time so no secret-shaped literal sits in the source.
FAKE_TOKEN = "tok-" + "q7" * 12
FAKE_HOST = "registry.example.test"
FAKE_USER = "deployer@example.test"
ENVIRONMENT = {"PGHOST": FAKE_HOST, "PGUSER": FAKE_USER, "PGPASSWORD": FAKE_TOKEN}


class FakeSession:
    """The part of a connection the command touches itself: it only closes it."""

    def __init__(self) -> None:
        self.closed = False

    async def execute(self, query: str, params: object = None) -> object:
        raise AssertionError("the command ran a statement itself; only apply_migrations may")

    def transaction(self) -> object:
        raise AssertionError("the command opened a transaction itself")

    async def close(self) -> None:
        self.closed = True


class Opener:
    """Stands in for the psycopg login: records the parameters, hands out one session."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.parameters: list[ConnectionParameters] = []
        self.session = FakeSession()

    async def __call__(self, parameters: ConnectionParameters) -> FakeSession:
        self.parameters.append(parameters)
        if self.error is not None:
            raise self.error
        return self.session


class Applier:
    """Stands in for ``apply_migrations``: records its folder, returns or raises what it is told."""

    def __init__(self, *, result: list[str] | None = None, error: Exception | None = None) -> None:
        self.result = result if result is not None else []
        self.error = error
        self.folders: list[Path] = []

    async def __call__(self, connection: MigrationConnection, folder: Path) -> list[str]:
        self.folders.append(folder)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def opener() -> Opener:
    return Opener()


def refused_file(name: str, cause_text: str) -> MigrationFailed:
    """What ``apply_migrations`` raises: the failure, chained to the driver's own error."""
    cause = RuntimeError(cause_text)
    failure = MigrationFailed(name, cause)
    failure.__cause__ = cause
    return failure


def use(monkeypatch: pytest.MonkeyPatch, applier: Applier) -> Applier:
    monkeypatch.setattr(load_schema, "apply_migrations", applier)
    return applier


async def run_command(
    opener: Opener, argv: list[str] | None = None, environment: dict[str, str] | None = None
) -> int:
    return await run(
        argv or [], environ=ENVIRONMENT if environment is None else environment, connect=opener
    )


def shown(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


async def test_loads_the_registry_and_names_the_files_it_applied(
    opener: Opener, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    applier = use(monkeypatch, Applier(result=["0001_baseline.sql", "0002_grants.sql"]))

    code = await run_command(opener)

    assert code == EXIT_LOADED
    output = shown(capsys)
    assert "0001_baseline.sql" in output
    assert "0002_grants.sql" in output
    assert applier.folders == [Path("migrations")]
    assert opener.session.closed


async def test_logs_in_with_the_settings_of_the_environment(
    opener: Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, Applier(result=["0001_baseline.sql"]))

    await run_command(opener)

    [parameters] = opener.parameters
    assert (parameters.host, parameters.user) == (FAKE_HOST, FAKE_USER)
    assert parameters.password.get_secret_value() == FAKE_TOKEN
    assert (parameters.port, parameters.database) == (5432, "databricks_postgres")


async def test_the_port_and_the_database_can_be_set_in_the_environment(
    opener: Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, Applier(result=["0001_baseline.sql"]))
    environment = {**ENVIRONMENT, "PGPORT": "6543", "PGDATABASE": "registry_copy"}

    await run_command(opener, environment=environment)

    [parameters] = opener.parameters
    assert (parameters.port, parameters.database) == (6543, "registry_copy")


async def test_the_migrations_folder_is_an_argument(
    opener: Opener, monkeypatch: pytest.MonkeyPatch
) -> None:
    applier = use(monkeypatch, Applier(result=["0001_baseline.sql"]))

    await run_command(opener, ["--migrations", "unpacked/migrations"])

    assert applier.folders == [Path("unpacked/migrations")]


async def test_a_loaded_registry_is_its_own_exit_code_and_changes_nothing(
    opener: Opener, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A deploy script runs the command on every deploy; the second one must not look like a failure.
    use(monkeypatch, Applier(error=RegistryAlreadySetUp("a table named apps already exists")))

    code = await run_command(opener)

    assert code == EXIT_ALREADY_LOADED
    output = shown(capsys)
    assert "already" in output
    assert "nothing was changed" in output
    assert "by hand" in output, "a later migration file is not applied by this command"
    assert opener.session.closed


async def test_a_missing_role_is_refused_and_names_every_role(
    opener: Opener, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, Applier(error=RolesMissing(("appmux_sp", "appmux_platform_writers"))))

    code = await run_command(opener)

    assert code == EXIT_REFUSED
    output = shown(capsys)
    assert "appmux_sp" in output
    assert "appmux_platform_writers" in output
    assert opener.session.closed


async def test_any_other_refusal_of_the_loader_shows_its_message(
    opener: Opener, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, Applier(error=MigrationRefused("no .sql file in migrations; pass the folder")))

    code = await run_command(opener)

    assert code == EXIT_REFUSED
    assert "no .sql file in migrations" in shown(capsys)
    assert opener.session.closed


async def test_a_file_the_database_refused_is_a_failure_that_names_the_file(
    opener: Opener, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, Applier(error=refused_file("0002_grants.sql", "boom")))

    code = await run_command(opener)

    assert code == EXIT_FAILED
    output = shown(capsys)
    assert "0002_grants.sql" in output
    assert "RuntimeError" in output, "the class of the cause is shown"
    assert "boom" not in output, "the driver's text is not"
    assert opener.session.closed


async def test_a_driver_error_during_the_load_is_a_failure_that_shows_only_its_class(
    opener: Opener, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    driver_error = psycopg.OperationalError(f"server closed the connection ({FAKE_TOKEN})")
    use(monkeypatch, Applier(error=driver_error))

    code = await run_command(opener)

    assert code == EXIT_FAILED
    output = shown(capsys)
    assert "OperationalError" in output
    assert FAKE_TOKEN not in output
    assert "run the command again" in output
    assert opener.session.closed


@pytest.mark.parametrize("missing", ["PGHOST", "PGUSER", "PGPASSWORD"])
async def test_a_missing_setting_is_refused_by_name_before_any_login(
    opener: Opener, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    environment = {name: value for name, value in ENVIRONMENT.items() if name != missing}

    code = await run_command(opener, environment=environment)

    assert code == EXIT_REFUSED
    assert missing in shown(capsys)
    assert opener.parameters == []


async def test_an_empty_setting_counts_as_missing(
    opener: Opener, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await run_command(opener, environment={**ENVIRONMENT, "PGPASSWORD": ""})

    assert code == EXIT_REFUSED
    assert "PGPASSWORD" in shown(capsys)
    assert opener.parameters == []


async def test_a_bad_port_is_refused_by_name_and_its_value_is_not_repeated(
    opener: Opener, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_port = "not-a-port-7731"

    code = await run_command(opener, environment={**ENVIRONMENT, "PGPORT": bad_port})

    assert code == EXIT_REFUSED
    output = shown(capsys)
    assert "PGPORT" in output
    assert bad_port not in output
    assert opener.parameters == []


async def test_a_failed_login_is_a_failure_that_shows_only_the_class_of_the_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    applier = use(monkeypatch, Applier())
    refused = Opener(error=psycopg.OperationalError(f"password authentication failed {FAKE_TOKEN}"))

    code = await run_command(refused)

    assert code == EXIT_FAILED
    output = shown(capsys)
    assert "OperationalError" in output
    assert FAKE_TOKEN not in output
    assert "PGHOST" in output, "the message says what to check"
    assert applier.folders == []


async def test_the_token_is_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    errors: list[Exception | None] = [
        None,
        RegistryAlreadySetUp("already"),
        RolesMissing(("appmux_sp",)),
        refused_file("0001_baseline.sql", f"cause {FAKE_TOKEN}"),
    ]
    for error in errors:
        use(monkeypatch, Applier(result=["0001_baseline.sql"], error=error))
        await run_command(Opener())
        output = shown(capsys)
        assert FAKE_TOKEN not in output, f"the token reached the output after {error!r}"


def test_the_token_cannot_be_given_on_the_command_line() -> None:
    with pytest.raises(SystemExit) as stopped:
        load_schema.build_parser().parse_args(["--password", "anything"])

    assert stopped.value.code == 2
