"""What credential a child gets, and how the shared service principal's token reaches it (T142).

FR-036, FR-039, design 8.5. A child has no Databricks credential unless one was delegated or
attached: an own service principal sets the three standard variables from its ``secret``
references, the shared service principal's token is minted by appmux and written to a file the
child's SDK reads through a token command (spike 10), and OBO is a header at the edge that is
never stored here.

What these tests prove is how appmux mints, writes and chooses against fakes and an
``httpx.MockTransport``; that the unmodified SDK keeps working across a real expiry inside the
sandbox is the integration test of T147 and is not shown here. Every client id, secret and token is
a fake placeholder.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import stat
import subprocess
import time
import urllib.parse
from collections.abc import Collection, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from pydantic import SecretStr

from appmux.reconciler.child_env import (
    EnvironmentRefused,
    PlatformIdentity,
    ReleaseEnvironment,
    ResourcesMissing,
)
from appmux.registry.models import App, AppConfig, AppResource, PlatformSetting, Release
from appmux.registry.snapshot import SNAPSHOT_TABLES, RegistrySnapshot, build_snapshot
from appmux.sandbox.run_dir import RUN_DIR_MOUNT_PATH
from appmux.secrets.credentials import ChildCredentials, CredentialsRefused
from appmux.secrets.resolver import MissingResourceKeys, SecretUnreadable, UnreadableSecret
from appmux.secrets.shared_sp_token import (
    REFRESH_MARGIN_S,
    TOKEN_COMMAND_NAME,
    TOKEN_COMMAND_PATH,
    TOKEN_FILE_NAME,
    TOKEN_FILE_PATH,
    ClientCredentialsMinter,
    MintedToken,
    SharedSpTokenFiles,
    SharedSpTokens,
    TokenFileKeeper,
    TokenMintFailed,
    install_token_command,
    token_file_text,
    uses_shared_sp_token,
    write_token_file,
)

pytestmark = pytest.mark.anyio

NOW: Final = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
START_EPOCH: Final = 1_793_437_200.0  # 2026-10-31T09:00:00Z
CLIENT_ID: Final = "client-id-placeholder"
CLIENT_SECRET: Final = "client-secret-placeholder"  # noqa: S105 - a placeholder, not a credential
TOKEN_ONE: Final = "tok-test-shared-one"  # noqa: S105 - a placeholder, not a credential
TOKEN_TWO: Final = "tok-test-shared-two"  # noqa: S105 - a placeholder, not a credential
# What ``ScriptedMinter`` mints on its first and second call.
MINTED_FIRST: Final = "tok-test-shared-1"
MINTED_SECOND: Final = "tok-test-shared-2"
LIFETIME_S: Final = 3600
USER: Final = "alice@example.com"


# --- fakes ---------------------------------------------------------------------------------------


class FakeTime:
    """Epoch seconds that move only when a test says so."""

    def __init__(self, start: float = START_EPOCH) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedMinter:
    """Mints ``tok-test-shared-<n>`` valid for ``lifetime_s``; can be made to fail."""

    def __init__(self, clock: FakeTime, *, lifetime_s: int = LIFETIME_S) -> None:
        self._clock = clock
        self._lifetime_s = lifetime_s
        self.mints = 0
        self.failing = False
        self.delay_s = 0.0

    async def mint(self) -> MintedToken:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.failing:
            raise TokenMintFailed("unavailable")
        self.mints += 1
        return MintedToken(
            access_token=SecretStr(f"tok-test-shared-{self.mints}"),
            token_type="Bearer",  # noqa: S106 - the OAuth token type, not a credential
            expires_at=self._clock() + self._lifetime_s,
        )


def token_at(clock: FakeTime, secret: str = TOKEN_ONE, lifetime_s: int = LIFETIME_S) -> MintedToken:
    return MintedToken(
        access_token=SecretStr(secret),
        token_type="Bearer",  # noqa: S106 - the OAuth token type, not a credential
        expires_at=clock() + lifetime_s,
    )


def entries_of(directory: Path) -> list[Path]:
    return list(directory.iterdir())


def read_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture
def local_time_zone() -> Iterator[None]:
    """The process in UTC+7 for one test, so a UTC expiry and a local one differ visibly."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Etc/GMT-7"  # POSIX sign: this is seven hours ahead of UTC
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


# --- the mint ------------------------------------------------------------------------------------


def minter_over(
    handler: httpx.MockTransport, clock: FakeTime, *, advertised_lifetime_s: int | None = None
) -> ClientCredentialsMinter:
    client = httpx.AsyncClient(transport=handler, base_url="https://workspace.example.invalid")
    return ClientCredentialsMinter(
        client,
        client_id=CLIENT_ID,
        client_secret=SecretStr(CLIENT_SECRET),
        clock=clock,
        advertised_lifetime_s=advertised_lifetime_s,
    )


def token_answer(access_token: str = TOKEN_ONE, expires_in: int = LIFETIME_S) -> httpx.Response:
    return httpx.Response(
        200,
        json={"access_token": access_token, "token_type": "Bearer", "expires_in": expires_in},
    )


async def test_the_minter_asks_for_client_credentials_and_reads_the_token_and_its_life() -> None:
    clock = FakeTime()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return token_answer()

    minted = await minter_over(httpx.MockTransport(handler), clock).mint()

    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == "/oidc/v1/token"
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {basic}"
    form = urllib.parse.parse_qs(request.content.decode())
    assert form == {"grant_type": ["client_credentials"], "scope": ["all-apis"]}
    assert minted.access_token.get_secret_value() == TOKEN_ONE
    assert minted.token_type == "Bearer"  # noqa: S105 - the OAuth token type, not a credential
    assert minted.expires_at == START_EPOCH + LIFETIME_S


async def test_a_shorter_advertised_lifetime_makes_the_token_look_younger_than_it_is() -> None:
    # Spike 10's dry-run knob: the refresh path runs in minutes without waiting for an hour.
    clock = FakeTime()
    minter = minter_over(
        httpx.MockTransport(lambda request: token_answer()), clock, advertised_lifetime_s=900
    )

    minted = await minter.mint()

    assert minted.expires_at == START_EPOCH + 900


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (httpx.Response(401, json={"error": "invalid_client"}), "refused"),
        (httpx.Response(400, text="bad request"), "refused"),
        (httpx.Response(500, text="server error"), "unavailable"),
        (httpx.Response(200, text="not json"), "malformed"),
        (httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600}), "malformed"),
        (
            httpx.Response(200, json={"access_token": "", "token_type": "Bearer", "expires_in": 1}),
            "malformed",
        ),
        (
            httpx.Response(
                200, json={"access_token": TOKEN_ONE, "token_type": "Bearer", "expires_in": "x"}
            ),
            "malformed",
        ),
    ],
)
async def test_a_mint_that_does_not_give_a_token_says_why_and_shows_no_credential(
    answer: httpx.Response, reason: str
) -> None:
    minter = minter_over(httpx.MockTransport(lambda request: answer), FakeTime())

    with pytest.raises(TokenMintFailed) as failure:
        await minter.mint()

    assert failure.value.reason == reason
    for secret in (CLIENT_SECRET, TOKEN_ONE, "invalid_client"):
        assert secret not in str(failure.value)
        assert secret not in repr(failure.value)


async def test_a_transport_failure_is_unavailable_and_its_text_is_not_kept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connection to {request.url} failed for {CLIENT_SECRET}")

    minter = minter_over(httpx.MockTransport(handler), FakeTime())

    with pytest.raises(TokenMintFailed) as failure:
        await minter.mint()

    assert failure.value.reason == "unavailable"
    assert CLIENT_SECRET not in str(failure.value)
    # The transport error's text can hold the request's credentials, so it is not chained.
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None


async def test_a_minted_token_hides_its_value_in_repr() -> None:
    token = token_at(FakeTime())

    assert TOKEN_ONE not in repr(token)
    assert TOKEN_ONE not in str(token)


# --- re-minting before expiry --------------------------------------------------------------------


async def test_the_cached_token_is_reused_while_more_than_the_margin_of_life_remains() -> None:
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    tokens = SharedSpTokens(minter, clock=clock)

    first = await tokens.current()
    clock.advance(LIFETIME_S - REFRESH_MARGIN_S - 1)  # 601 s of life left
    second = await tokens.current()

    assert minter.mints == 1
    assert second.access_token.get_secret_value() == first.access_token.get_secret_value()


async def test_a_token_with_less_than_ten_minutes_left_is_minted_again() -> None:
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    tokens = SharedSpTokens(minter, clock=clock)

    await tokens.current()
    clock.advance(LIFETIME_S - REFRESH_MARGIN_S + 1)  # 599 s of life left
    renewed = await tokens.current()

    assert REFRESH_MARGIN_S == 600
    assert minter.mints == 2
    assert renewed.access_token.get_secret_value() == "tok-test-shared-2"


async def test_callers_that_arrive_together_cause_one_mint() -> None:
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    minter.delay_s = 0.05
    tokens = SharedSpTokens(minter, clock=clock)

    results = await asyncio.gather(*(tokens.current() for _ in range(6)))

    assert minter.mints == 1
    assert {token.access_token.get_secret_value() for token in results} == {"tok-test-shared-1"}


async def test_a_failed_first_mint_raises_and_the_next_call_tries_again() -> None:
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    minter.failing = True
    tokens = SharedSpTokens(minter, clock=clock)

    with pytest.raises(TokenMintFailed):
        await tokens.current()
    minter.failing = False
    token = await tokens.current()

    assert token.access_token.get_secret_value() == "tok-test-shared-1"


async def test_a_failed_renewal_keeps_serving_the_token_that_is_still_valid(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    tokens = SharedSpTokens(minter, clock=clock)
    first = await tokens.current()
    clock.advance(LIFETIME_S - 300)  # inside the margin, still 300 s from expiry
    minter.failing = True

    served = await tokens.current()

    assert served.access_token.get_secret_value() == first.access_token.get_secret_value()
    assert "tok-test-shared-1" not in caplog.text


async def test_a_failed_renewal_of_an_expired_token_raises() -> None:
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    tokens = SharedSpTokens(minter, clock=clock)
    await tokens.current()
    clock.advance(LIFETIME_S + 1)
    minter.failing = True

    with pytest.raises(TokenMintFailed):
        await tokens.current()


# --- the token file ------------------------------------------------------------------------------


def test_the_token_file_holds_the_token_its_type_and_an_expiry_in_local_time(
    local_time_zone: None,
) -> None:
    clock = FakeTime()  # 09:00:00 UTC, which is 16:00:00 at UTC+7
    token = token_at(clock)

    written = json.loads(token_file_text(token))

    assert written == {
        "access_token": TOKEN_ONE,
        "token_type": "Bearer",
        "expiry": "2026-10-31T17:00:00",  # an hour later, as local time, no zone suffix
    }


def test_the_file_is_written_whole_with_owner_only_access(tmp_path: Path) -> None:
    path = write_token_file(tmp_path, token_at(FakeTime()))

    assert path == tmp_path / TOKEN_FILE_NAME
    assert read_json(path)["access_token"] == TOKEN_ONE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in tmp_path.iterdir()] == [TOKEN_FILE_NAME]


def test_a_second_write_replaces_the_file_and_leaves_no_temporary_one(tmp_path: Path) -> None:
    clock = FakeTime()
    write_token_file(tmp_path, token_at(clock, TOKEN_ONE))

    write_token_file(tmp_path, token_at(clock, TOKEN_TWO))

    assert read_json(tmp_path / TOKEN_FILE_NAME)["access_token"] == TOKEN_TWO
    assert [entry.name for entry in tmp_path.iterdir()] == [TOKEN_FILE_NAME]


def test_a_write_that_fails_halfway_leaves_the_old_token_whole_and_no_debris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeTime()
    write_token_file(tmp_path, token_at(clock, TOKEN_ONE))

    def refuse(source: object, destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        write_token_file(tmp_path, token_at(clock, TOKEN_TWO))

    assert read_json(tmp_path / TOKEN_FILE_NAME)["access_token"] == TOKEN_ONE
    assert [entry.name for entry in tmp_path.iterdir()] == [TOKEN_FILE_NAME]


def test_the_token_is_never_written_to_a_world_readable_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[int] = []
    real_replace = os.replace

    def watching_replace(source: Any, destination: Any) -> None:
        modes.append(stat.S_IMODE(os.stat(source).st_mode))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", watching_replace)

    write_token_file(tmp_path, token_at(FakeTime()))

    assert modes == [0o600]


# --- the token command ---------------------------------------------------------------------------


def test_the_token_command_is_installed_executable_by_its_owner_only(tmp_path: Path) -> None:
    path = install_token_command(tmp_path)

    assert path == tmp_path / TOKEN_COMMAND_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert [entry.name for entry in tmp_path.iterdir()] == [TOKEN_COMMAND_NAME]


def test_the_token_command_script_is_strict_and_names_the_file_the_child_sees(
    tmp_path: Path,
) -> None:
    text = install_token_command(tmp_path).read_text(encoding="utf-8")

    assert text.startswith("#!/bin/bash\n")
    assert "set -euo pipefail" in text
    assert f'"{TOKEN_FILE_PATH}"' in text
    assert f"{RUN_DIR_MOUNT_PATH}/{TOKEN_FILE_NAME}" == TOKEN_FILE_PATH
    assert f"{RUN_DIR_MOUNT_PATH}/{TOKEN_COMMAND_NAME}" == TOKEN_COMMAND_PATH


def test_the_token_command_prints_the_token_file_and_nothing_else(tmp_path: Path) -> None:
    # The script names the in-sandbox path; this run points that path at a real directory.
    token_path = write_token_file(tmp_path, token_at(FakeTime()))
    script = install_token_command(tmp_path).read_text(encoding="utf-8")
    runnable = tmp_path / "run-here.sh"
    runnable.write_text(script.replace(TOKEN_FILE_PATH, str(token_path)), encoding="utf-8")

    done = subprocess.run(
        ["bash", str(runnable), "auth", "token", "--host", "https://workspace.example.invalid"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"]},
    )

    assert done.returncode == 0
    assert done.stderr == ""
    assert json.loads(done.stdout) == read_json(token_path)


def test_the_token_command_fails_loudly_when_there_is_no_token_file(tmp_path: Path) -> None:
    script = install_token_command(tmp_path).read_text(encoding="utf-8")
    runnable = tmp_path / "run-here.sh"
    runnable.write_text(
        script.replace(TOKEN_FILE_PATH, str(tmp_path / "missing.json")), encoding="utf-8"
    )

    done = subprocess.run(
        ["bash", str(runnable)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"]},
    )

    assert done.returncode != 0
    assert done.stdout == ""


# --- keeping the file fresh while a child runs ---------------------------------------------------


async def test_attaching_writes_the_script_and_a_token_before_it_returns(tmp_path: Path) -> None:
    clock = FakeTime()
    files = SharedSpTokenFiles(SharedSpTokens(ScriptedMinter(clock), clock=clock))

    keeper = await files.attach(tmp_path)
    try:
        assert read_json(tmp_path / TOKEN_FILE_NAME)["access_token"] == MINTED_FIRST
        assert (tmp_path / TOKEN_COMMAND_NAME).is_file()
    finally:
        await keeper.stop()


async def test_attaching_without_a_token_raises_and_leaves_no_script(tmp_path: Path) -> None:
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    minter.failing = True
    files = SharedSpTokenFiles(SharedSpTokens(minter, clock=clock))

    with pytest.raises(TokenMintFailed):
        await files.attach(tmp_path)

    assert entries_of(tmp_path) == []


async def test_a_round_rewrites_the_file_only_once_the_token_has_been_renewed(
    tmp_path: Path,
) -> None:
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    files = SharedSpTokenFiles(SharedSpTokens(minter, clock=clock))
    keeper = await files.attach(tmp_path)
    try:
        before = (tmp_path / TOKEN_FILE_NAME).stat().st_mtime_ns
        clock.advance(60)
        assert await keeper.refresh_once() is False
        assert (tmp_path / TOKEN_FILE_NAME).stat().st_mtime_ns == before

        clock.advance(LIFETIME_S - REFRESH_MARGIN_S)  # now inside the margin
        assert await keeper.refresh_once() is True
        assert read_json(tmp_path / TOKEN_FILE_NAME)["access_token"] == MINTED_SECOND
    finally:
        await keeper.stop()


async def test_the_file_always_holds_a_token_with_more_than_the_margin_of_life_left(
    tmp_path: Path,
) -> None:
    # Over three hours of rounds every thirty seconds: the file is never read as nearly expired.
    clock = FakeTime()
    files = SharedSpTokenFiles(SharedSpTokens(ScriptedMinter(clock), clock=clock))
    keeper = await files.attach(tmp_path)
    try:
        for _ in range(3 * 3600 // 30):
            clock.advance(30)
            await keeper.refresh_once()
            expiry = datetime.strptime(
                read_json(tmp_path / TOKEN_FILE_NAME)["expiry"], "%Y-%m-%dT%H:%M:%S"
            )
            assert expiry.timestamp() - clock() >= REFRESH_MARGIN_S - 30
    finally:
        await keeper.stop()


async def test_a_round_that_cannot_mint_is_logged_without_the_token_and_the_loop_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) == 2:
            minter.failing = False  # Databricks answers again before the second round
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    keeper = TokenFileKeeper(
        SharedSpTokens(minter, clock=clock), tmp_path, interval_s=1800.0, sleep=sleep
    )
    await keeper.install()
    minter.failing = True
    clock.advance(LIFETIME_S - 600)  # the first token is inside its margin; the first round fails

    with pytest.raises(asyncio.CancelledError):
        await keeper.run()

    assert len(sleeps) == 4
    assert caplog.text.count("could not renew") == 1
    assert "tok-test-shared" not in caplog.text
    assert read_json(tmp_path / TOKEN_FILE_NAME)["access_token"] == MINTED_SECOND


async def test_stopping_ends_the_background_loop(tmp_path: Path) -> None:
    clock = FakeTime()
    files = SharedSpTokenFiles(SharedSpTokens(ScriptedMinter(clock), clock=clock), interval_s=60.0)
    keeper = await files.attach(tmp_path)

    await keeper.stop()
    await keeper.stop()  # a second stop is harmless

    assert keeper.is_running is False


async def test_stopping_removes_the_token_file_and_keeps_the_script(
    tmp_path: Path,
) -> None:
    clock = FakeTime()
    files = SharedSpTokenFiles(SharedSpTokens(ScriptedMinter(clock), clock=clock))
    keeper = await files.attach(tmp_path)

    await keeper.stop()

    assert [entry.name for entry in entries_of(tmp_path)] == [TOKEN_COMMAND_NAME]


async def test_stopping_a_keeper_whose_file_is_already_gone_is_harmless(tmp_path: Path) -> None:
    clock = FakeTime()
    files = SharedSpTokenFiles(SharedSpTokens(ScriptedMinter(clock), clock=clock))
    keeper = await files.attach(tmp_path)
    await asyncio.to_thread((tmp_path / TOKEN_FILE_NAME).unlink)

    await keeper.stop()


def test_only_an_environment_that_names_the_token_command_needs_the_files() -> None:
    files = SharedSpTokenFiles(SharedSpTokens(ScriptedMinter(FakeTime())))

    assert files.needed_by({"DATABRICKS_CLI_PATH": TOKEN_COMMAND_PATH}) is True
    assert files.needed_by({"DATABRICKS_CLI_PATH": "/usr/bin/databricks"}) is False
    assert files.needed_by({}) is False


# --- which credential a child gets ---------------------------------------------------------------


def app_row(*, obo: bool = False, shared_sp: bool = False, slug: str = "sales") -> App:
    return App(
        slug=slug,
        display_name="Sales",
        owner=USER,
        source_path=f"/Workspace/Users/{USER}/sales",
        memory_budget_mb=256,
        obo_delegated=obo,
        shared_sp_delegated=shared_sp,
        created_by="dana@example.com",
        created_at=NOW,
        updated_by="dana@example.com",
        updated_at=NOW,
    )


def config_row(
    *, id_key: str | None = None, secret_key: str | None = None, slug: str = "sales"
) -> AppConfig:
    return AppConfig(
        app_slug=slug,
        desired_state="running",
        own_sp_client_id_key=id_key,
        own_sp_client_secret_key=secret_key,
        updated_by="dana@example.com",
        updated_at=NOW,
    )


def secret_resource(key: str, target: str, *, slug: str = "sales") -> AppResource:
    return AppResource(
        app_slug=slug,
        key=key,
        type="secret",
        target=target,
        permission="READ",
        access_as=None,
        check_result="ok",
        attached_by=USER,
        attached_at=NOW,
    )


def warehouse_resource(
    key: str, *, access_as: str, check_result: str = "ok", slug: str = "sales"
) -> AppResource:
    fields: dict[str, Any] = {
        "app_slug": slug,
        "key": key,
        "type": "sql_warehouse",
        "target": "a1b2c3d4e5f67890",
        "permission": "CAN_USE",
        "access_as": access_as,
        "check_result": check_result,
        "attached_by": USER,
        "attached_at": NOW,
    }
    if access_as == "shared_sp" and check_result != "ok":
        # The model refuses this pair, so no registry read can produce it. The start still checks
        # (defence in depth on the confused-deputy path), and this row proves it by skipping the
        # model's validation.
        return AppResource.model_construct(**fields)
    return AppResource(**fields)


def snapshot_of(
    app: App,
    config: AppConfig,
    *resources: AppResource,
    shared_sp_allowed: bool | None = None,
) -> RegistrySnapshot:
    rows: dict[str, list[Any]] = {table: [] for table in SNAPSHOT_TABLES}
    rows["apps"] = [app]
    rows["app_config"] = [config]
    rows["app_resources"] = list(resources)
    if shared_sp_allowed is not None:
        rows["platform_settings"] = [
            PlatformSetting(
                key="shared_sp_delegation_allowed",
                value=shared_sp_allowed,
                updated_by="dana@example.com",
                updated_at=NOW,
            )
        ]
    return build_snapshot(rows, loaded_at=NOW)


class FakeResources:
    """The resolver seam: answers from a table of resource key to value, and remembers asks."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)
        self.asked: list[frozenset[str]] = []
        self.failure: Exception | None = None

    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]:
        self.asked.append(frozenset(keys))
        if self.failure is not None:
            raise self.failure
        missing = [key for key in keys if key not in self.values]
        if missing:
            raise MissingResourceKeys(missing)
        return {key: self.values[key] for key in keys}


OWN_SP_VALUES: Final = {"sp-client-id": CLIENT_ID, "sp-client-secret": CLIENT_SECRET}


def own_sp_snapshot(**kwargs: Any) -> RegistrySnapshot:
    return snapshot_of(
        app_row(**kwargs),
        config_row(id_key="sp-client-id", secret_key="sp-client-secret"),  # noqa: S106 - a resource key, not a credential
        secret_resource("sp-client-id", "sales-sp/client-id"),
        secret_resource("sp-client-secret", "sales-sp/client-secret"),
    )


def credentials_over(
    snapshot: RegistrySnapshot,
    *,
    values: Mapping[str, str] | None = None,
    tokens: SharedSpTokens | None = None,
) -> tuple[ChildCredentials, FakeResources]:
    resources = FakeResources(OWN_SP_VALUES if values is None else values)
    return ChildCredentials(
        snapshot=lambda: snapshot, resources=resources, tokens=tokens
    ), resources


def shared_tokens() -> tuple[SharedSpTokens, ScriptedMinter]:
    clock = FakeTime()
    minter = ScriptedMinter(clock)
    return SharedSpTokens(minter, clock=clock), minter


async def test_a_child_with_nothing_delegated_or_attached_gets_no_credential_and_no_lookup() -> (
    None
):
    tokens, minter = shared_tokens()
    credentials, resources = credentials_over(snapshot_of(app_row(), config_row()), tokens=tokens)

    variables = await credentials.credentials_for("sales")

    assert dict(variables) == {}
    assert resources.asked == []
    assert minter.mints == 0


async def test_an_obo_delegation_gives_the_child_nothing_to_store() -> None:
    # The visitor's token arrives per request in a header and is the edge's business (design 8.5).
    tokens, minter = shared_tokens()
    credentials, resources = credentials_over(
        snapshot_of(app_row(obo=True), config_row()), tokens=tokens
    )

    variables = await credentials.credentials_for("sales")

    assert dict(variables) == {}
    assert resources.asked == []
    assert minter.mints == 0


async def test_an_own_service_principal_sets_its_client_id_and_secret_from_its_references() -> None:
    credentials, resources = credentials_over(own_sp_snapshot())

    variables = await credentials.credentials_for("sales")

    assert dict(variables) == {
        "DATABRICKS_CLIENT_ID": CLIENT_ID,
        "DATABRICKS_CLIENT_SECRET": CLIENT_SECRET,
    }
    assert resources.asked == [frozenset({"sp-client-id", "sp-client-secret"})]


async def test_the_variables_it_returns_show_names_and_never_values_when_printed() -> None:
    credentials, _ = credentials_over(own_sp_snapshot())

    variables = await credentials.credentials_for("sales")

    for text in (repr(variables), str(variables), f"{variables}", f"{variables!r}"):
        assert CLIENT_SECRET not in text
        assert CLIENT_ID not in text


async def test_an_own_service_principal_with_one_key_missing_is_refused_naming_the_field() -> None:
    snapshot = snapshot_of(
        app_row(),
        config_row(id_key="sp-client-id", secret_key="sp-client-secret"),  # noqa: S106 - a resource key, not a credential
        secret_resource("sp-client-id", "sales-sp/client-id"),
    )
    credentials, resources = credentials_over(snapshot)

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "own_sp_client_secret_key" in refusal.value.message
    assert resources.asked == []  # nothing is read for a pair that cannot stand


async def test_an_own_service_principal_whose_secret_cannot_be_read_is_refused_unvalued() -> None:
    credentials, resources = credentials_over(own_sp_snapshot())
    resources.failure = SecretUnreadable(
        [UnreadableSecret("secret scope 'sales-sp'", "sales-sp", "denied")]
    )

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "sales-sp" in refusal.value.message
    assert CLIENT_SECRET not in refusal.value.message


async def test_a_personal_access_token_is_not_accepted_as_an_own_service_principal() -> None:
    pat = "dapi" + "0123456789abcdef" * 2
    credentials, _ = credentials_over(
        own_sp_snapshot(), values={"sp-client-id": CLIENT_ID, "sp-client-secret": pat}
    )

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "personal access token" in refusal.value.message
    assert pat not in refusal.value.message
    assert pat not in str(refusal.value)


async def test_no_mode_ever_produces_a_personal_access_token_variable() -> None:
    tokens, _ = shared_tokens()
    names: set[str] = set()
    for snapshot in (
        own_sp_snapshot(),
        snapshot_of(app_row(shared_sp=True), config_row(), shared_sp_allowed=True),
    ):
        credentials, _ = credentials_over(snapshot, tokens=tokens)
        names |= set(await credentials.credentials_for("sales"))

    assert "DATABRICKS_TOKEN" not in names
    assert names <= {
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_AUTH_TYPE",
        "DATABRICKS_CLI_PATH",
    }


async def test_the_shared_service_principal_is_a_token_command_and_never_a_static_token() -> None:
    tokens, minter = shared_tokens()
    snapshot = snapshot_of(app_row(shared_sp=True), config_row(), shared_sp_allowed=True)
    credentials, _ = credentials_over(snapshot, tokens=tokens)

    variables = await credentials.credentials_for("sales")

    assert dict(variables) == {
        "DATABRICKS_AUTH_TYPE": "databricks-cli",
        "DATABRICKS_CLI_PATH": TOKEN_COMMAND_PATH,
    }
    assert uses_shared_sp_token(variables) is True
    assert not any("tok-test-shared" in value for value in variables.values())
    assert "tok-test-shared" not in repr(variables)
    assert (
        minter.mints == 1
    )  # a start that could not get a token is refused, not left to fail later


async def test_a_start_that_cannot_get_a_shared_token_is_refused_with_what_to_do() -> None:
    tokens, minter = shared_tokens()
    minter.failing = True
    snapshot = snapshot_of(app_row(shared_sp=True), config_row(), shared_sp_allowed=True)
    credentials, _ = credentials_over(snapshot, tokens=tokens)

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "shared service principal" in refusal.value.message
    assert "start" in refusal.value.message


async def test_a_process_that_cannot_mint_refuses_a_shared_delegation() -> None:
    snapshot = snapshot_of(app_row(shared_sp=True), config_row(), shared_sp_allowed=True)
    credentials, _ = credentials_over(snapshot, tokens=None)

    with pytest.raises(CredentialsRefused):
        await credentials.credentials_for("sales")


async def test_a_shared_delegation_has_no_effect_while_the_platform_switch_is_off() -> None:
    tokens, minter = shared_tokens()
    for allowed in (False, None):  # off, and never set (the default is off)
        snapshot = snapshot_of(app_row(shared_sp=True), config_row(), shared_sp_allowed=allowed)
        credentials, _ = credentials_over(snapshot, tokens=tokens)

        assert dict(await credentials.credentials_for("sales")) == {}

    assert minter.mints == 0


async def test_the_platform_switch_alone_gives_an_app_nothing() -> None:
    tokens, minter = shared_tokens()
    snapshot = snapshot_of(app_row(shared_sp=False), config_row(), shared_sp_allowed=True)
    credentials, _ = credentials_over(snapshot, tokens=tokens)

    assert dict(await credentials.credentials_for("sales")) == {}
    assert minter.mints == 0


async def test_an_own_service_principal_wins_over_a_delegation_because_its_reach_is_smaller() -> (
    None
):
    tokens, minter = shared_tokens()
    snapshot = snapshot_of(
        app_row(shared_sp=True),
        config_row(id_key="sp-client-id", secret_key="sp-client-secret"),  # noqa: S106 - a resource key, not a credential
        secret_resource("sp-client-id", "sales-sp/client-id"),
        secret_resource("sp-client-secret", "sales-sp/client-secret"),
        shared_sp_allowed=True,
    )
    credentials, _ = credentials_over(snapshot, tokens=tokens)

    variables = await credentials.credentials_for("sales")

    assert set(variables) == {"DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"}
    assert minter.mints == 0


async def test_a_resource_asking_for_the_shared_identity_without_a_verified_check_is_refused() -> (
    None
):
    tokens, _ = shared_tokens()
    snapshot = snapshot_of(
        app_row(shared_sp=True),
        config_row(),
        warehouse_resource("wh", access_as="shared_sp", check_result="unverified"),
        shared_sp_allowed=True,
    )
    credentials, _ = credentials_over(snapshot, tokens=tokens)

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "'wh'" in refusal.value.message
    assert "unverified" in refusal.value.message


async def test_a_verified_resource_on_the_shared_identity_is_accepted() -> None:
    tokens, _ = shared_tokens()
    snapshot = snapshot_of(
        app_row(shared_sp=True),
        config_row(),
        warehouse_resource("wh", access_as="shared_sp", check_result="ok"),
        shared_sp_allowed=True,
    )
    credentials, _ = credentials_over(snapshot, tokens=tokens)

    assert "DATABRICKS_CLI_PATH" in await credentials.credentials_for("sales")


async def test_an_unknown_app_gets_no_credential() -> None:
    credentials, _ = credentials_over(snapshot_of(app_row(), config_row()))

    assert dict(await credentials.credentials_for("gone")) == {}


# --- what reaches the child's environment --------------------------------------------------------

PLATFORM = PlatformIdentity(
    workspace_host="https://workspace.example.invalid",
    workspace_id="1234567890",
    parent_url="https://appmux.example.invalid",
)


class RefusingAdapter:
    """What the assembly puts between the two: a credential refusal becomes an environment one."""

    def __init__(self, source: ChildCredentials) -> None:
        self._source = source

    async def credentials_for(self, app_slug: str) -> Mapping[str, str]:
        try:
            return await self._source.credentials_for(app_slug)
        except CredentialsRefused as refusal:
            raise EnvironmentRefused(refusal.message) from refusal


class NoResourceValues:
    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]:
        if keys:
            raise ResourcesMissing(list(keys))
        return {}


def release_of(slug: str = "sales") -> Release:
    return Release.model_validate(
        {
            "app_slug": slug,
            "release_no": 1,
            "snapshot_digest": "sha256:" + "0" * 64,
            "detected": {"runtime": "python", "command": ["python", "app.py"]},
            "status": "pending",
            "status_at": NOW,
            "created_by": USER,
            "created_at": NOW,
        }
    )


async def test_an_own_service_principal_child_gets_the_three_standard_variables() -> None:
    credentials, _ = credentials_over(own_sp_snapshot())
    factory = ReleaseEnvironment(
        platform=PLATFORM, resources=NoResourceValues(), credentials=RefusingAdapter(credentials)
    )

    environment = await factory.environment_for(release_of(), port=8000)

    assert environment["DATABRICKS_HOST"] == PLATFORM.workspace_host
    assert environment["DATABRICKS_CLIENT_ID"] == CLIENT_ID
    assert environment["DATABRICKS_CLIENT_SECRET"] == CLIENT_SECRET
    assert "DATABRICKS_AUTH_TYPE" not in environment
    assert CLIENT_SECRET not in repr(environment)


async def test_a_shared_service_principal_child_gets_the_token_command_variables() -> None:
    tokens, _ = shared_tokens()
    snapshot = snapshot_of(app_row(shared_sp=True), config_row(), shared_sp_allowed=True)
    credentials, _ = credentials_over(snapshot, tokens=tokens)
    factory = ReleaseEnvironment(
        platform=PLATFORM, resources=NoResourceValues(), credentials=RefusingAdapter(credentials)
    )

    environment = await factory.environment_for(release_of(), port=8000)

    assert environment["DATABRICKS_AUTH_TYPE"] == "databricks-cli"
    assert environment["DATABRICKS_CLI_PATH"] == TOKEN_COMMAND_PATH
    assert "DATABRICKS_CLIENT_SECRET" not in environment
    assert not any("tok-test-shared" in value for value in environment.values())


async def test_a_child_with_no_credential_has_no_databricks_credential_variable() -> None:
    credentials, _ = credentials_over(snapshot_of(app_row(), config_row()))
    factory = ReleaseEnvironment(
        platform=PLATFORM, resources=NoResourceValues(), credentials=RefusingAdapter(credentials)
    )

    environment = await factory.environment_for(release_of(), port=8000)

    for name in (
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_TOKEN",
        "DATABRICKS_AUTH_TYPE",
        "DATABRICKS_CLI_PATH",
    ):
        assert name not in environment


async def test_a_refused_credential_stops_the_environment_with_the_reason() -> None:
    tokens, minter = shared_tokens()
    minter.failing = True
    snapshot = snapshot_of(app_row(shared_sp=True), config_row(), shared_sp_allowed=True)
    credentials, _ = credentials_over(snapshot, tokens=tokens)
    factory = ReleaseEnvironment(
        platform=PLATFORM, resources=NoResourceValues(), credentials=RefusingAdapter(credentials)
    )

    with pytest.raises(EnvironmentRefused, match="shared service principal"):
        await factory.environment_for(release_of(), port=8000)


async def test_an_environment_without_a_credential_source_is_what_it_was_before() -> None:
    factory = ReleaseEnvironment(platform=PLATFORM, resources=NoResourceValues())

    environment = await factory.environment_for(release_of(), port=8000)

    assert "DATABRICKS_CLIENT_ID" not in environment
    assert environment["DATABRICKS_APP_NAME"] == "sales"


def test_a_credential_never_overrides_a_variable_appmux_owns() -> None:
    # The layering is sandbox.environment's; one pinned case here because a credential is the one
    # input this lane adds to it.
    from appmux.sandbox.environment import ChildIdentity, appmux_variables

    identity = ChildIdentity(
        slug="sales",
        port=8000,
        workspace_host=PLATFORM.workspace_host,
        workspace_id=PLATFORM.workspace_id,
        parent_url=PLATFORM.parent_url,
        home_directory="/run/appmux/home",
        credentials={
            "DATABRICKS_HOST": "https://other.example.invalid",
            "DATABRICKS_CLIENT_ID": "x",
        },
    )

    variables = appmux_variables(identity)

    assert variables["DATABRICKS_HOST"] == PLATFORM.workspace_host
    assert variables["DATABRICKS_CLIENT_ID"] == "x"
    assert "https://other.example.invalid" not in repr(identity)
