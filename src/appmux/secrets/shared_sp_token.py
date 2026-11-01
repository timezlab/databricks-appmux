"""The shared service principal's token: mint it, write it to the child's run directory, renew it.

Design 8.5 and spike 10. A child that may use the shared service principal never holds its client
secret. appmux mints a short-lived token (``client_credentials``, scope ``all-apis``, about an hour)
and writes it atomically to a file in the child's run directory. The Databricks SDK in the child is
told ``DATABRICKS_AUTH_TYPE=databricks-cli`` and ``DATABRICKS_CLI_PATH`` points at a small script
that prints that file, so the unmodified SDK asks for a token whenever it needs one (spike 10:
15 of 15 calls across an expiry; a static ``DATABRICKS_TOKEN`` failed from the first call after it).

Three rules come from that spike and are kept here:

* The SDK keeps a token until it expires, so the replacement must already be in the file when it
  asks: appmux renews when less than ``REFRESH_MARGIN_S`` (10 minutes) of life remain.
* The file's ``expiry`` is read as *local* naive time, not UTC.
* The file is replaced whole (a temporary file, then ``os.replace``), so a reader sees the old
  token or the new one and never half of either.

One minted token is shared by every child that uses it (``SharedSpTokens`` caches it), so ten
children cost one mint an hour, not ten. A token value is a ``SecretStr`` in memory, goes to the
files and to nowhere else: no log line, error text, ``repr`` or audit row holds one, and a failure
names the reason only (``refused``, ``unavailable`` or ``malformed``).

What is NOT proven here: that the sandbox shows the file at ``TOKEN_FILE_PATH``. The run directory
is a tmpfs inside the sandbox unless the launcher is given ``SandboxSpec.host_run_dir`` (see
``sandbox/run_dir.py``); the files below are written to a host directory, so they reach the child
only through that bind. The container run of T147 is the first place that can show it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, Protocol

import httpx
from pydantic import SecretStr

from appmux.sandbox.run_dir import RUN_DIR_MOUNT_PATH

logger = logging.getLogger(__name__)

TOKEN_ENDPOINT_PATH: Final = "/oidc/v1/token"  # noqa: S105 - a URL path, not a credential
TOKEN_SCOPE: Final = "all-apis"  # noqa: S105 - spike 10: the scope the SDK needs, not a credential
REFRESH_MARGIN_S: Final = 600
KEEP_INTERVAL_S: Final = 30.0  # how often the files are looked at; far below the margin
TOKEN_FILE_NAME: Final = "shared-sp-token.json"  # noqa: S105 - a file name
TOKEN_COMMAND_NAME: Final = "token-command.sh"  # noqa: S105 - a file name
# Where the child sees them (the run directory's mount point inside the sandbox).
TOKEN_FILE_PATH: Final = f"{RUN_DIR_MOUNT_PATH}/{TOKEN_FILE_NAME}"
TOKEN_COMMAND_PATH: Final = f"{RUN_DIR_MOUNT_PATH}/{TOKEN_COMMAND_NAME}"

_COMMAND_SOURCE: Final = Path(__file__).with_name("token_command.sh")
_COMMAND_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_MINT_TIMEOUT_S: Final = 30.0
_EXPIRY_FORMAT: Final = "%Y-%m-%dT%H:%M:%S"
_REFUSED_STATUSES: Final = frozenset({400, 401, 403})

MintFailure = Literal["refused", "unavailable", "malformed"]
_ADVICE: Final[dict[str, str]] = {
    "refused": "Databricks refused the appmux service principal's credentials; check them",
    "unavailable": "Databricks did not answer",
    "malformed": "Databricks answered with something that is not a token",
}


class TokenMintFailed(Exception):
    """A token could not be minted. ``reason`` is one of ``MintFailure``; the text shows no body."""

    def __init__(self, reason: MintFailure) -> None:
        super().__init__(f"the shared service principal's token could not be minted ({reason})")
        self.reason = reason

    @property
    def advice(self) -> str:
        return _ADVICE[self.reason]


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A token, its type and the epoch second it stops working. The value prints as stars."""

    access_token: SecretStr
    token_type: str
    expires_at: float


class TokenMinter(Protocol):
    async def mint(self) -> MintedToken:
        """A new token; raises ``TokenMintFailed`` and nothing else."""
        ...


class ClientCredentialsMinter:
    """Mints with the appmux service principal's client id and secret (``/oidc/v1/token``).

    ``client`` has its ``base_url`` set to the workspace host from settings; no URL comes from the
    registry or a request, and a redirect is not followed. The client secret goes in one header and
    nowhere else. ``advertised_lifetime_s`` shortens the life the token claims, for the integration
    test that must see a renewal in minutes (spike 10's ``ADVERTISED_LIFETIME_S``); production
    leaves it unset.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: SecretStr,
        clock: Callable[[], float] = time.time,
        advertised_lifetime_s: int | None = None,
    ) -> None:
        self._client = client
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._advertised_lifetime_s = advertised_lifetime_s

    async def mint(self) -> MintedToken:
        response = await self._post()
        if response is None:
            raise TokenMintFailed("unavailable")
        if response.status_code in _REFUSED_STATUSES:
            raise TokenMintFailed("refused")
        if response.status_code != 200:
            raise TokenMintFailed("unavailable")
        token = self._token_from(response)
        if token is None:
            raise TokenMintFailed("malformed")
        return token

    # Returns ``None`` instead of raising from inside ``except``: a transport error's text can hold
    # this request's Authorization header, and raising from there would keep it as ``__context__``.
    async def _post(self) -> httpx.Response | None:
        try:
            return await self._client.post(
                TOKEN_ENDPOINT_PATH,
                data={"grant_type": "client_credentials", "scope": TOKEN_SCOPE},
                auth=httpx.BasicAuth(self._client_id, self._client_secret.get_secret_value()),
                follow_redirects=False,
                timeout=_MINT_TIMEOUT_S,
            )
        except (httpx.HTTPError, httpx.InvalidURL):
            return None

    def _token_from(self, response: httpx.Response) -> MintedToken | None:
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        access_token, token_type, expires_in = (
            body.get("access_token"),
            body.get("token_type"),
            body.get("expires_in"),
        )
        if not (isinstance(access_token, str) and access_token):
            return None
        if not (isinstance(token_type, str) and token_type):
            return None
        if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
            return None
        lifetime_s = self._advertised_lifetime_s or expires_in
        return MintedToken(
            access_token=SecretStr(access_token),
            token_type=token_type,
            expires_at=self._clock() + lifetime_s,
        )


class SharedSpTokens:
    """The one cached token of the shared service principal, renewed before it runs short.

    ``current`` returns the cached token while more than ``refresh_margin_s`` of its life remain
    and mints a new one otherwise. Callers that arrive together cause one mint. A renewal that
    fails while the cached token still works keeps serving that token (the next round tries
    again); once it has expired, the failure is raised.
    """

    def __init__(
        self,
        minter: TokenMinter,
        *,
        clock: Callable[[], float] = time.time,
        refresh_margin_s: int = REFRESH_MARGIN_S,
    ) -> None:
        self._minter = minter
        self._clock = clock
        self._refresh_margin_s = refresh_margin_s
        self._token: MintedToken | None = None
        self._lock = asyncio.Lock()

    async def current(self) -> MintedToken:
        token = self._token
        if token is not None and not self._is_running_short(token):
            return token
        async with self._lock:
            token = self._token  # another caller may have renewed it while this one waited
            if token is not None and not self._is_running_short(token):
                return token
            try:
                renewed = await self._minter.mint()
            except TokenMintFailed as failure:
                if token is not None and token.expires_at > self._clock():
                    logger.warning(
                        "the shared service principal token could not be renewed (%s); "
                        "the current one is kept until it expires",
                        failure.reason,
                    )
                    return token
                raise
            self._token = renewed
            return renewed

    def _is_running_short(self, token: MintedToken) -> bool:
        return token.expires_at - self._clock() < self._refresh_margin_s


def token_file_text(token: MintedToken) -> str:
    """The JSON the SDK's ``databricks-cli`` auth reads; ``expiry`` is local time (spike 10)."""
    expiry = datetime.fromtimestamp(token.expires_at).strftime(_EXPIRY_FORMAT)
    return json.dumps(
        {
            "access_token": token.access_token.get_secret_value(),
            "token_type": token.token_type,
            "expiry": expiry,
        }
    )


def write_token_file(directory: Path, token: MintedToken) -> Path:
    """Replace ``<directory>/shared-sp-token.json`` with ``token``, whole or not at all.

    The temporary file is created by ``mkstemp`` (a fresh random name opened ``O_EXCL``, mode 0600)
    in the same directory, because the run directory is writable by the child: a name the child
    could guess and pre-plant as a symlink would make this write follow it. ``os.replace`` then
    swaps the final name in one step and does not follow a link at the destination either.
    """
    final = directory / TOKEN_FILE_NAME
    descriptor, temporary_name = tempfile.mkstemp(dir=directory, prefix=".token-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token_file_text(token))
        os.chmod(temporary_name, _FILE_MODE)
        os.replace(temporary_name, final)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
    return final


def install_token_command(directory: Path) -> Path:
    """Write the script the SDK runs for a token into ``directory``; owner-executable only."""
    path = directory / TOKEN_COMMAND_NAME
    text = _COMMAND_SOURCE.read_text(encoding="utf-8")
    descriptor, temporary_name = tempfile.mkstemp(dir=directory, prefix=".command-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary_name, _COMMAND_MODE)
        os.replace(temporary_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
    return path


def uses_shared_sp_token(environment: Mapping[str, str]) -> bool:
    """Whether a child's environment names the token command, so its run directory needs files."""
    return environment.get("DATABRICKS_CLI_PATH") == TOKEN_COMMAND_PATH


Sleep = Callable[[float], Awaitable[None]]


class TokenFileKeeper:
    """Keeps one child's token file current for as long as the child runs.

    ``install`` writes the script and the first token; ``run`` is the loop that looks every
    ``interval_s`` and rewrites the file when ``SharedSpTokens`` has renewed the token. A round
    that fails (no token, a full disk) is logged by its reason and tried again at the next one: a
    keeper that quietly ended would let the file run out while the child still runs.
    """

    def __init__(
        self,
        tokens: SharedSpTokens,
        directory: Path,
        *,
        interval_s: float = KEEP_INTERVAL_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._tokens = tokens
        self._directory = directory
        self._interval_s = interval_s
        self._sleep = sleep
        self._written: MintedToken | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def install(self) -> None:
        """Write the script and the current token; raises ``TokenMintFailed`` before any file."""
        token = await self._tokens.current()
        await asyncio.to_thread(install_token_command, self._directory)
        await asyncio.to_thread(write_token_file, self._directory, token)
        self._written = token

    async def refresh_once(self) -> bool:
        """Rewrite the file if the token changed since the last write; say whether it did."""
        token = await self._tokens.current()
        if token == self._written:
            return False
        await asyncio.to_thread(write_token_file, self._directory, token)
        self._written = token
        return True

    async def run(self) -> None:
        """Loop until cancelled."""
        while True:
            await self._sleep(self._interval_s)
            try:
                await self.refresh_once()
            except TokenMintFailed as failure:
                logger.warning(
                    "could not renew the shared service principal token file (%s); will retry",
                    failure.reason,
                )
            except OSError as failure:
                # Only the errno's name: a path or a message could name a child's directory.
                logger.warning(
                    "could not write the shared service principal token file (%s); will retry",
                    failure.strerror or type(failure).__name__,
                )

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Cancel the loop, wait for it to end and remove the token file; a second call is harmless.

        The file goes with the child it served: a token left in a scratch directory would outlive
        the process that was meant to hold it. The script stays (it holds no secret).
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # A failure to remove is not the caller's problem: the next start empties the directory.
        with contextlib.suppress(OSError):
            await asyncio.to_thread((self._directory / TOKEN_FILE_NAME).unlink, missing_ok=True)


class SharedSpTokenFiles:
    """Attaches a keeper to a child's run directory (the host side of ``/run/appmux``)."""

    def __init__(
        self,
        tokens: SharedSpTokens,
        *,
        interval_s: float = KEEP_INTERVAL_S,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._tokens = tokens
        self._interval_s = interval_s
        self._sleep = sleep

    def needed_by(self, environment: Mapping[str, str]) -> bool:
        """Whether a child with this environment needs its run directory (and so these files)."""
        return uses_shared_sp_token(environment)

    async def attach(self, directory: Path) -> TokenFileKeeper:
        """Install the script and a token in ``directory``, then keep the token current.

        Raises ``TokenMintFailed`` (nothing written) when no token can be had: the child must not
        start with a credential variable that points at nothing. The caller ``stop``s the keeper
        when the child ends.
        """
        keeper = TokenFileKeeper(
            self._tokens, directory, interval_s=self._interval_s, sleep=self._sleep
        )
        await keeper.install()
        keeper.start()
        return keeper
