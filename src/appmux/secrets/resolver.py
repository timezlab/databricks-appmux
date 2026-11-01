"""Read the value of a ``secret`` resource when a child starts (FR-038, design 8.3, T126).

A ``secret`` resource stores ``scope/key``. At child start the appmux service principal reads the
value (Databricks ``secrets/get``; spike 11 measured on the Free Edition workspace on 2026-10-30
that an App's service principal with only ``READ`` on the scope can, and that one without gets
``403``). The value is returned to the caller of ``ResourceValueResolver.resolve`` and nowhere
else: no log line, exception, ``repr`` or return value of this module holds one.

``ResourceValueResolver`` matches the ``ResourceValues`` seam of ``reconciler/child_env.py``: it
answers a value for each key a release names, a secret's value or any other resource's target. It
does not import the reconciler (design 3.4); the reconciler's side maps ``MissingResourceKeys`` and
``SecretUnreadable`` to its own refusals, whose message becomes the release's ``status_reason``.

Two limits worth knowing:

* A revocation of the service principal's ``READ`` is not immediate (spike 11: up to about six
  minutes), so for a few minutes a child may still start with a secret it should no longer get.
  To stop a leaked secret at once, rotate the secret.
* ``scrub_secrets`` replaces known values; a log that holds a *transformation* of a value (its
  base64, a prefix) is not caught.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol, get_args

import httpx
from pydantic import SecretStr

from appmux.appyaml.requirements import RequirementCheck
from appmux.registry.resources import SECRET_TYPE, is_valid_target
from appmux.registry.snapshot import RegistrySnapshot

logger = logging.getLogger(__name__)

REDACTED: Final = "[redacted]"
SECRETS_GET_PATH: Final = "/api/2.0/secrets/get"

_READ_TIMEOUT_S: Final = 15.0
_MAX_CONCURRENT_READS: Final = 8
_MAX_SHOWN_FAILURES: Final = 10

ReadFailure = Literal["denied", "not_found", "unavailable", "malformed", "invalid_reference"]
_READ_FAILURES: Final[tuple[str, ...]] = get_args(ReadFailure)

_ADVICE: Final[dict[str, str]] = {
    "denied": "the appmux service principal needs READ on it",
    "not_found": "the scope or the key does not exist",
    "unavailable": "Databricks did not answer",
    "malformed": "the value is not usable text",
    "invalid_reference": "the reference is not a valid <scope>/<key>",
}


class SecretReadFailed(Exception):
    """One read did not give a value. ``reason`` is one of ``ReadFailure``.

    The text holds the reason only: not the scope's name (a caller may show it after checking it),
    not the key, not a response body, not a token.
    """

    def __init__(self, scope: str, reason: str) -> None:
        if reason not in _READ_FAILURES:
            raise ValueError(f"reason is one of: {', '.join(_READ_FAILURES)}")
        super().__init__(f"a secret could not be read ({reason})")
        self.scope = scope
        self.reason: ReadFailure = reason  # type: ignore[assignment]  # narrowed by the check above


@dataclass(frozen=True, slots=True)
class UnreadableSecret:
    """One failure ``SecretUnreadable`` reports: what it names, the scope, and why."""

    subject: str  # what the message names: a checked scope, or a resource key
    scope: str | None
    reason: str


class SecretUnreadable(Exception):
    """A release needs secrets the appmux service principal cannot read (FR-038).

    ``message`` names every scope at fault (never a key or a value), why, and what to do; the
    reconciler stores it as the release's ``status_reason``.
    """

    def __init__(self, failures: Sequence[UnreadableSecret]) -> None:
        unique = list(dict.fromkeys((f.subject, f.scope, f.reason) for f in failures))
        shown = [f"cannot read {subject} ({_ADVICE[reason]})" for subject, _, reason in unique]
        hidden = len(shown) - _MAX_SHOWN_FAILURES
        text = "; ".join(shown[:_MAX_SHOWN_FAILURES])
        if hidden > 0:
            text += f"; and {hidden} more"
        self.message = f"{text}; fix that and start the release again"
        self.scopes = tuple(sorted({scope for _, scope, _ in unique if scope is not None}))
        super().__init__(self.message)


class MissingResourceKeys(Exception):
    """The release names keys that are not resources of the app (they were detached since)."""

    def __init__(self, keys: Iterable[str]) -> None:
        self.keys = tuple(sorted(set(keys)))
        self.message = RequirementCheck(required=self.keys, missing=self.keys).message()
        super().__init__(self.message)


class SecretReader(Protocol):
    """Reads one secret's value. ``DatabricksSecretReader`` is the real one, tests have a stub."""

    async def read(self, scope: str, key: str) -> str:
        """The value of ``key`` in ``scope``; raises ``SecretReadFailed`` and nothing else."""
        ...


class ResolvedResources(dict[str, str]):
    """Resource keys to resolved values; its ``repr`` lists the keys only.

    A log line that formats this object (``%s``, ``%r``, an f-string, a traceback frame) must not
    print a value. It is a plain ``dict`` otherwise; a copy made with ``dict(...)`` or ``{**...}``
    is a plain ``dict`` again, so the protection ends where the caller makes one.
    """

    def __init__(self, values: Mapping[str, str], *, secret_keys: Iterable[str] = ()) -> None:
        super().__init__(values)
        self._secret_keys = frozenset(secret_keys)

    @property
    def secret_values(self) -> tuple[str, ...]:
        """The values that are secrets (not a warehouse id), for ``scrub_secrets``."""
        return tuple(self[key] for key in sorted(self._secret_keys) if key in self)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(keys={sorted(self)})"


def scrub_secrets(text: str, values: Iterable[str]) -> str:
    """``text`` with every occurrence of each value replaced by ``REDACTED``.

    Longest value first, so a value that contains another is not left half showing. An empty value
    replaces nothing. A one-character value turns a log into noise, which is the safe side.
    """
    for value in sorted({value for value in values if value}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


class DatabricksSecretReader:
    """Reads a secret with the appmux service principal's token (``GET /api/2.0/secrets/get``).

    ``client`` has its ``base_url`` set to the workspace host from settings; no URL comes from the
    registry or a request, and a redirect is not followed. ``token_provider`` is asked on every
    read so the SDK's own refresh applies. The token goes in one header and nowhere else.
    """

    def __init__(
        self, client: httpx.AsyncClient, *, token_provider: Callable[[], Awaitable[SecretStr]]
    ) -> None:
        self._client = client
        self._token_provider = token_provider

    async def read(self, scope: str, key: str) -> str:
        if not is_valid_target(SECRET_TYPE, f"{scope}/{key}"):
            raise SecretReadFailed(scope, "invalid_reference")
        token = await self._token()
        if token is None:
            raise SecretReadFailed(scope, "unavailable")
        response = await self._get(scope, key, token)
        if response is None:
            raise SecretReadFailed(scope, "unavailable")
        return _value_of(response, scope)

    # The two helpers below catch and return ``None`` instead of raising from inside ``except``:
    # the failure they swallow is a transport or SDK error whose text this request's bearer token
    # may appear in, and raising from there would keep it as the new error's ``__context__``.

    async def _token(self) -> SecretStr | None:
        try:
            return await self._token_provider()
        except Exception as exc:
            # Operational boundary: the provider wraps the Databricks SDK and can raise anything.
            # Only the class name is logged, never the text.
            logger.warning("the service principal token is unavailable (%s)", type(exc).__name__)
            return None

    async def _get(self, scope: str, key: str, token: SecretStr) -> httpx.Response | None:
        try:
            return await self._client.get(
                SECRETS_GET_PATH,
                params={"scope": scope, "key": key},
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                # The client may carry another credential; this call carries only the token.
                auth=None,
                follow_redirects=False,
                timeout=_READ_TIMEOUT_S,
            )
        except (httpx.HTTPError, httpx.InvalidURL):
            return None


def _value_of(response: httpx.Response, scope: str) -> str:
    if response.status_code == 403:
        raise SecretReadFailed(scope, "denied")
    if response.status_code in (400, 404):
        raise SecretReadFailed(scope, "not_found")
    if response.status_code != 200:
        raise SecretReadFailed(scope, "unavailable")
    value = _decoded(response)
    if value is None or "\x00" in value:  # an environment variable cannot hold NUL
        raise SecretReadFailed(scope, "malformed")
    return value


def _decoded(response: httpx.Response) -> str | None:
    """The text of the base64 ``value`` of an answer, or ``None`` when there is none."""
    try:
        body = response.json()
        encoded = body.get("value") if isinstance(body, dict) else None
        if not isinstance(encoded, str):
            return None
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, binascii.Error):  # not JSON, not base64, not UTF-8 (UnicodeDecodeError)
        return None


class ResourceValueResolver:
    """Resolves the resource keys a release names to the values its child receives.

    A ``secret`` resource gives the value read from its scope; any other type gives its target
    string (a warehouse id, an endpoint name). ``snapshot`` is called on each ``resolve`` so a
    resource detached since the release was built is seen.
    """

    def __init__(self, *, snapshot: Callable[[], RegistrySnapshot], reader: SecretReader) -> None:
        self._snapshot = snapshot
        self._reader = reader

    async def resolve(self, app_slug: str, keys: Collection[str]) -> ResolvedResources:
        """A value for each of ``keys``.

        Raises ``MissingResourceKeys`` naming every key that is not a resource of the app, before
        any read; then ``SecretUnreadable`` naming every scope that could not be read.
        """
        wanted = sorted(set(keys))
        resources = {item.key: item for item in self._snapshot().resources_for(app_slug)}
        missing = [key for key in wanted if key not in resources]
        if missing:
            raise MissingResourceKeys(missing)

        values: dict[str, str] = {}
        failures: list[UnreadableSecret] = []
        references: dict[str, tuple[str, str]] = {}
        for key in wanted:
            resource = resources[key]
            if resource.type != SECRET_TYPE:
                values[key] = resource.target
            elif is_valid_target(SECRET_TYPE, resource.target):
                scope, _, secret_key = resource.target.partition("/")
                references[key] = (scope, secret_key)
            else:
                # A row written around the attach check. Its text is not shown: it is not trusted.
                failures.append(UnreadableSecret(f"resource '{key}'", None, "invalid_reference"))

        outcomes = await self._read_all(references)
        for key, outcome in outcomes.items():
            if isinstance(outcome, SecretReadFailed):
                failures.append(
                    UnreadableSecret(
                        f"secret scope '{outcome.scope}'", outcome.scope, outcome.reason
                    )
                )
            else:
                values[key] = outcome
        if failures:
            raise SecretUnreadable(failures)
        return ResolvedResources(
            {key: values[key] for key in wanted}, secret_keys=references.keys()
        )

    async def _read_all(
        self, references: Mapping[str, tuple[str, str]]
    ) -> dict[str, str | SecretReadFailed]:
        limit = asyncio.Semaphore(_MAX_CONCURRENT_READS)

        async def read_one(scope: str, key: str) -> str | SecretReadFailed:
            async with limit:
                try:
                    return await self._reader.read(scope, key)
                except SecretReadFailed as failed:
                    return failed

        async with asyncio.TaskGroup() as group:
            tasks = {
                key: group.create_task(read_one(scope, secret_key))
                for key, (scope, secret_key) in references.items()
            }
        return {key: task.result() for key, task in tasks.items()}
