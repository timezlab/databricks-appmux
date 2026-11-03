"""Workspace directory reads for the Sharing form: SCIM users, groups and service principals (T187).

``DirectoryLookup.search`` asks one SCIM v2 list endpoint of the workspace
(``/api/2.0/preview/scim/v2/Users``, ``/Groups`` or ``/ServicePrincipals``) for the entries whose
names contain the person's text, with a ``co`` filter: ``userName`` or ``displayName`` for users,
``displayName`` for the other two. ``count`` caps the answer and ``attributes`` keeps it to the
names the form shows.

Which token: the person's own first. When Databricks refuses it (``401`` or ``403``), or the
request carries none, the same request is sent once more with the platform's service-principal
token. Which OAuth scopes the person's token carries on a Databricks App is not measured yet
(docs/reference/databricks-apps-platform.md), so the fallback is the normal path until it is.
Any other failure, and a failure of the platform token, is ``unavailable``: a suggestion list is
best-effort, the form keeps free typing, and nothing here raises for a failure of Databricks.

The query goes into the filter text, so it is held to ``QUERY_PATTERN`` (letters, digits, space
and ``. _ @ + -``, at most 64 characters): no quote, backslash or parenthesis can end the string
or the expression. The admin schema refuses anything else with a ``400``; ``search`` refuses it
again with ``ValueError`` before any request, so no other caller can skip the rule.

What is sent follows ``identity.groups``: the request is built here, carries only
``Authorization: Bearer <token>`` (and ``Host``), is sent with authentication off and never
follows a redirect. The constructor refuses a client with a request event hook or a ``base_url``
that is not absolute or carries credentials, and a hook added later stops the request before the
token leaves. A log line names a status or an error class, never a token, a header or a body.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

logger = logging.getLogger(__name__)

QUERY_MAX_LENGTH: Final = 64
# The contract's allow-list (contracts/admin-api.md, GET /principals). Empty is allowed here so
# the admin schema can share it; ``search`` also refuses a blank query.
QUERY_PATTERN: Final = rf"^[A-Za-z0-9 ._@+-]{{0,{QUERY_MAX_LENGTH}}}$"
# The read sits on the typing path of the Sharing form, so a silent Databricks must not hold it.
DIRECTORY_REQUEST_TIMEOUT_S: Final = 5.0
SCIM_BASE_PATH: Final = "/api/2.0/preview/scim/v2/"

# See identity.groups: a header value must be printable ASCII without whitespace.
_BEARER_TOKEN_PATTERN: Final = re.compile(r"[\x21-\x7e]+")
_QUERY_SHAPE: Final = re.compile(QUERY_PATTERN)
_TIMEOUT_KEYS: Final = ("connect", "read", "write", "pool")
_REFUSED_STATUSES: Final = frozenset({httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN})

DirectoryKind = Literal["users", "groups", "service_principals"]
DirectorySource = Literal["caller_token", "platform_token", "unavailable"]


@dataclass(frozen=True, slots=True)
class _Endpoint:
    path: str
    filter_fields: tuple[str, ...]
    attributes: str

    def params(self, query: str, count: int) -> dict[str, str]:
        return {
            "filter": " or ".join(f'{field} co "{query}"' for field in self.filter_fields),
            "count": str(count),
            "attributes": self.attributes,
        }


_ENDPOINTS: Final[dict[DirectoryKind, _Endpoint]] = {
    "users": _Endpoint("Users", ("userName", "displayName"), "userName,displayName"),
    "groups": _Endpoint("Groups", ("displayName",), "displayName"),
    "service_principals": _Endpoint(
        "ServicePrincipals", ("displayName",), "applicationId,displayName"
    ),
}


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """One entry as Databricks listed it; a field Databricks left out is ``None``.

    Nothing here is checked against a grant's rules: the caller decides what it can offer.
    """

    user_name: str | None = None
    display_name: str | None = None
    application_id: str | None = None


@dataclass(frozen=True, slots=True)
class DirectoryAnswer:
    """What one directory read gave, and with which token (``unavailable``: no entries)."""

    source: DirectorySource
    entries: tuple[DirectoryEntry, ...]


_UNAVAILABLE: Final = DirectoryAnswer(source="unavailable", entries=())


class _ScimResource(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    user_name: str | None = Field(default=None, alias="userName")
    display_name: str | None = Field(default=None, alias="displayName")
    application_id: str | None = Field(default=None, alias="applicationId")


class _ScimList(BaseModel):
    """A SCIM list answer; one that matched nothing may leave ``Resources`` out."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    resources: list[_ScimResource] = Field(default_factory=list, alias="Resources")


class _ReadFailed(Exception):
    """Databricks gave no usable answer; the message names a status or an error class only."""


class _TokenRefused(_ReadFailed):
    """Databricks refused the token (``401`` or ``403``)."""


def _usable(token: SecretStr | None) -> SecretStr | None:
    if token is None or _BEARER_TOKEN_PATTERN.fullmatch(token.get_secret_value()) is None:
        return None
    return token


class DirectoryLookup:
    """Reads the workspace directory with the person's token, else the platform's.

    ``client`` is an ``httpx.AsyncClient`` whose ``base_url`` is the workspace; it lends its
    transport and ``base_url`` and nothing else. ``platform_token`` returns the platform's
    service-principal token (``assembly.Service``'s provider).

    Raises ``ValueError`` for a client the lookup cannot use safely (see the module docstring).
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        platform_token: Callable[[], Awaitable[SecretStr]],
    ) -> None:
        if client.event_hooks["request"]:
            raise ValueError(
                "DirectoryLookup refuses a client with a request event hook: a hook can rewrite "
                "the Authorization header; pass a client built without event_hooks['request']"
            )
        base_url = client.base_url
        if not base_url.is_absolute_url or base_url.userinfo:
            raise ValueError(
                "DirectoryLookup needs a client whose base_url is an absolute workspace URL "
                "without credentials, for example httpx.AsyncClient(base_url='https://<host>')"
            )
        self._client = client
        self._platform_token = platform_token

    async def search(
        self,
        kind: DirectoryKind,
        query: str,
        *,
        count: int,
        caller_token: SecretStr | None,
    ) -> DirectoryAnswer:
        """The entries of ``kind`` whose names contain ``query``, at most ``count`` of them.

        Raises ``ValueError`` for a blank query or one outside ``QUERY_PATTERN``, before any
        request. Never raises for a failure of Databricks or of the platform token.
        """
        if not query.strip() or _QUERY_SHAPE.fullmatch(query) is None:
            raise ValueError(
                f"the query must hold 1 to {QUERY_MAX_LENGTH} of the characters "
                "A-Z a-z 0-9 space . _ @ + -"
            )
        endpoint = _ENDPOINTS[kind]
        token = _usable(caller_token)
        if token is not None:
            try:
                entries = await self._read(endpoint, query, count, token)
            except _TokenRefused as refusal:
                logger.info(
                    "the person's token cannot list %s (%s); asking with the platform token",
                    kind,
                    refusal,
                )
            except _ReadFailed as failure:
                logger.warning("listing %s with the person's token failed (%s)", kind, failure)
                return _UNAVAILABLE
            else:
                return DirectoryAnswer(source="caller_token", entries=entries)

        platform = await self._platform()
        if platform is None:
            return _UNAVAILABLE
        try:
            entries = await self._read(endpoint, query, count, platform)
        except _ReadFailed as failure:
            logger.warning("listing %s with the platform token failed (%s)", kind, failure)
            return _UNAVAILABLE
        return DirectoryAnswer(source="platform_token", entries=entries)

    async def _platform(self) -> SecretStr | None:
        try:
            token = await self._platform_token()
        except Exception as exc:
            # The provider is an injected OAuth client with many ways to fail; each one only means
            # the suggestions are unavailable, which is the answer the caller handles. Only the
            # class is logged, not logger.exception: the message could hold a credential.
            logger.warning("the platform token is not available (%s)", type(exc).__name__)
            return None
        usable = _usable(token)
        if usable is None:
            logger.warning("the platform token cannot be sent as a bearer token")
        return usable

    async def _read(
        self, endpoint: _Endpoint, query: str, count: int, token: SecretStr
    ) -> tuple[DirectoryEntry, ...]:
        # The constructor refuses a client with a request hook, but the client is shared and a hook
        # can be attached afterwards; check again before the token leaves, and fail closed.
        if self._client.event_hooks["request"]:
            raise _ReadFailed("the client gained a request event hook")
        try:
            # Built by hand, not with client.build_request: that would merge the client's default
            # headers, cookies and params into a request that carries a credential.
            request = httpx.Request(
                "GET",
                self._client.base_url.join(f"{SCIM_BASE_PATH.lstrip('/')}{endpoint.path}"),
                params=endpoint.params(query, count),
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                extensions={"timeout": dict.fromkeys(_TIMEOUT_KEYS, DIRECTORY_REQUEST_TIMEOUT_S)},
            )
            response = await self._client.send(request, auth=None, follow_redirects=False)
        except httpx.HTTPError as exc:
            # Only the class is reported: an httpx message may carry the URL.
            raise _ReadFailed(f"request failed ({type(exc).__name__})") from exc
        if response.status_code in _REFUSED_STATUSES:
            raise _TokenRefused(f"status {response.status_code}")
        if response.status_code != httpx.codes.OK:
            raise _ReadFailed(f"status {response.status_code}")
        try:
            listed = _ScimList.model_validate_json(response.content)
        except ValidationError as exc:
            # The validation message quotes the offending input, so it is not reported.
            raise _ReadFailed("status 200 with an unexpected body") from exc
        return tuple(
            DirectoryEntry(
                user_name=resource.user_name,
                display_name=resource.display_name,
                application_id=resource.application_id,
            )
            for resource in listed.resources
        )
