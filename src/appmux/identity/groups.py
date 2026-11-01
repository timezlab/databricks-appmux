"""A person's Databricks groups, looked up with their own token and cached (design 9.1, spike 12).

``GroupLookup.groups_for`` asks the SCIM endpoint ``GET /api/2.0/preview/scim/v2/Me`` with the
visitor's own access token, so it needs no workspace admin and no service-principal grant. The
answer lists direct and nested groups alike (spike 12); only the display names are kept, lower
cased, and the ``direct``/``indirect`` type is ignored.

The result is ``frozenset[str]`` or ``None`` for "unknown". ``None`` means the lookup could not
tell (no token, Databricks failed, the answer had an unexpected shape) and nothing usable is
cached; a caller must treat it as "no group matches", so a group-based grant fails closed.

Cache, per person and keyed by email, never by token (the token changes, the person does not):

- an answer younger than ``GROUPS_CACHE_TTL_S`` is returned without a request;
- when a refresh fails, the last answer is returned while it is no older than
  ``GROUPS_STALE_WINDOW_S`` (a failure does not make it any younger);
- otherwise the answer is ``None``.

What is sent: the lookup builds its own ``GET`` and sends it through the client's transport with
authentication switched off, so the only header besides ``Host`` is the visitor's
``Authorization: Bearer <token>``. A client-level ``auth``, default headers, cookies (including
ones a response set for another person), query parameters or redirect setting cannot add to it or
replace it. The constructor refuses a client with a request event hook (a hook may rewrite any
request) and a ``base_url`` that is not absolute or carries credentials.

Concurrent lookups for one person share one request. The cache lives in one process and one
event loop and holds one entry per person the proxy let in; entries are not evicted by age until
a failed refresh finds them too old.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from appmux.identity.actor import Actor

logger = logging.getLogger(__name__)

SCIM_ME_PATH: Final = "/api/2.0/preview/scim/v2/Me"
GROUPS_CACHE_TTL_S: Final = 300.0
GROUPS_STALE_WINDOW_S: Final = 3600.0
# The lookup sits on the request path of a visitor, so a silent Databricks must not hold it long.
SCIM_REQUEST_TIMEOUT_S: Final = 10.0
# A bearer credential is printable ASCII without whitespace. Anything else cannot be sent as a
# header value (non-ASCII raises UnicodeEncodeError inside httpx, a CR/LF would split the header),
# so such a token counts as no token.
_BEARER_TOKEN_PATTERN: Final = re.compile(r"[\x21-\x7e]+")
_TIMEOUT_KEYS: Final = ("connect", "read", "write", "pool")


class _ScimGroup(BaseModel):
    """One entry of ``groups`` in the ``Me`` answer; ``type`` (direct or indirect) is not read."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    display: str


class _ScimMe(BaseModel):
    """The part of the SCIM user that the lookup reads; a user in no group has no ``groups``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    groups: list[_ScimGroup] = Field(default_factory=list)


class _LookupFailed(Exception):
    """Databricks gave no usable answer.

    The message names a status code or an error class and never a header, a body or a value:
    the request carried the visitor's token.
    """


@dataclass(frozen=True, slots=True)
class _CachedGroups:
    groups: frozenset[str]
    fetched_at: float


class GroupLookup:
    """Looks up and caches the Databricks groups of the people who visit.

    ``client`` is an injected ``httpx.AsyncClient`` whose ``base_url`` is the workspace. It lends
    its transport and its ``base_url`` and nothing else: the request is built here, carries only
    the visitor's ``Authorization`` header, is sent with authentication off and never follows a
    redirect, so the token cannot be sent to another host and no other credential joins it.
    ``clock`` is a monotonic clock in seconds.

    Raises ``ValueError`` for a client the lookup cannot use safely (see the module docstring).
    """

    def __init__(
        self, client: httpx.AsyncClient, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if client.event_hooks["request"]:
            raise ValueError(
                "GroupLookup refuses a client with a request event hook: a hook can rewrite the "
                "Authorization header; pass a client built without event_hooks['request']"
            )
        base_url = client.base_url
        if not base_url.is_absolute_url or base_url.userinfo:  # absolute means scheme and host
            raise ValueError(
                "GroupLookup needs a client whose base_url is an absolute workspace URL without "
                "credentials, for example httpx.AsyncClient(base_url='https://<workspace-host>')"
            )
        self._client = client
        self._clock = clock
        self._cache: dict[str, _CachedGroups] = {}
        self._in_flight: dict[str, asyncio.Task[frozenset[str] | None]] = {}

    async def groups_for(self, actor: Actor) -> frozenset[str] | None:
        """The lower-cased group names of ``actor``, or ``None`` when they are unknown.

        Never raises for a failure of Databricks. An actor without a token is unknown even when an
        earlier answer is cached: with no token there is no way to ask, and the cache must not
        grant what the request itself cannot back.
        """
        token = actor.visitor_token
        if token is None or _BEARER_TOKEN_PATTERN.fullmatch(token.get_secret_value()) is None:
            return None

        key = actor.email.lower()
        cached = self._cache.get(key)
        if cached is not None and self._clock() - cached.fetched_at < GROUPS_CACHE_TTL_S:
            return cached.groups

        request = self._in_flight.get(key)
        if request is None:
            request = asyncio.create_task(self._refresh(key, token))
            self._in_flight[key] = request
            request.add_done_callback(lambda done: self._forget(key, done))
        # The request is shared, so one caller giving up (a visitor closing the tab) must not
        # cancel it for the others: shield keeps the cancellation on this caller alone.
        return await asyncio.shield(request)

    def _forget(self, key: str, done: asyncio.Task[frozenset[str] | None]) -> None:
        if self._in_flight.get(key) is done:
            del self._in_flight[key]

    async def _refresh(self, key: str, token: SecretStr) -> frozenset[str] | None:
        try:
            groups = await self._fetch(token)
        except _LookupFailed as failure:
            return self._last_answer(key, str(failure))
        self._cache[key] = _CachedGroups(groups, self._clock())
        return groups

    async def _fetch(self, token: SecretStr) -> frozenset[str]:
        # The constructor refuses a client with a request hook, but the client is shared and a hook
        # can be attached afterwards; check again before the token leaves, and fail closed.
        if self._client.event_hooks["request"]:
            raise _LookupFailed("the client gained a request event hook")
        try:
            # Built by hand, not with client.build_request: that would merge the client's default
            # headers, cookies and params into a request that carries the visitor's credential.
            request = httpx.Request(
                "GET",
                self._client.base_url.join(SCIM_ME_PATH.lstrip("/")),
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                extensions={"timeout": dict.fromkeys(_TIMEOUT_KEYS, SCIM_REQUEST_TIMEOUT_S)},
            )
            response = await self._client.send(request, auth=None, follow_redirects=False)
        except httpx.HTTPError as exc:
            # Only the class is reported: an httpx message may carry the URL.
            raise _LookupFailed(f"request failed ({type(exc).__name__})") from exc
        if response.status_code != httpx.codes.OK:
            raise _LookupFailed(f"status {response.status_code}")
        try:
            me = _ScimMe.model_validate_json(response.content)
        except ValidationError as exc:
            # The validation message quotes the offending input, so it is not reported.
            raise _LookupFailed("status 200 with an unexpected body") from exc
        return frozenset(group.display.lower() for group in me.groups)

    def _last_answer(self, key: str, reason: str) -> frozenset[str] | None:
        """After a failed refresh: the cached groups while they are still usable, else ``None``."""
        cached = self._cache.get(key)
        if cached is not None:
            age_s = self._clock() - cached.fetched_at
            if age_s <= GROUPS_STALE_WINDOW_S:
                logger.warning(
                    "group lookup failed (%s); using the groups cached %.0f s ago", reason, age_s
                )
                return cached.groups
            del self._cache[key]
        logger.warning(
            "group lookup failed (%s) and no usable answer is cached; group grants will not match",
            reason,
        )
        return None
