"""``GET /principals``: who the Sharing form can suggest (T187, contracts/admin-api.md addendum).

``GET /principals?q=&types=&limit=`` (``principal.search``: ``CAN_MANAGE`` on at least one app, or
a platform admin) answers a ``PrincipalList``. It writes nothing and records no audit entry.

- ``q`` is at most 64 characters of ``A-Z a-z 0-9 space . _ @ + -`` (``PrincipalSearchQuery``,
  because the text goes into a SCIM filter); anything else is ``400`` before any directory is
  asked. ``types`` is a comma list of grant types (default all four); ``limit`` is 1 to 50
  (default 10). A parameter sent twice is ``400``; a message names the parameter, never the value.
- An empty (or blank) ``q`` reads no directory and offers only ``everyone``, when it is asked for.
- Otherwise each directory asked for is read once, in parallel, through ``PrincipalDirectory``
  (``identity.directory.DirectoryLookup``) with the person's token, which falls back to the
  platform's token and then to ``unavailable``; ``sources`` says which, and ``not_read`` for a
  directory that was not asked. A directory that cannot be read never fails the request.
- A directory entry becomes a suggestion only when ``GrantRequest`` would take its name for its
  type (a lower-case email, a group name that is not blank or ``*``, a lower-case application
  id), so the form can save whatever it is offered. ``everyone`` is offered when ``q`` starts the
  word ``everyone`` (in any case).
- Order: ``everyone``, then groups, users and service principals, each by the name shown (the
  display name, else the name), case-folded. Each directory is asked for ``limit + 1`` entries;
  the list is cut to ``limit`` after ordering, and ``truncated`` is ``true`` when anything was cut
  or a directory had more than ``limit``. Databricks picks which ``limit + 1`` entries it returns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Final, Protocol

from pydantic import SecretStr, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext
from appmux.admin.errors import BadRequest
from appmux.admin.schemas import (
    EVERYONE_NAME,
    PRINCIPAL_SEARCH_MAX_LIMIT,
    GrantRequest,
    GrantType,
    PrincipalList,
    PrincipalSearchQuery,
    PrincipalSources,
    PrincipalSuggestion,
)
from appmux.authz.actions import Action
from appmux.identity.directory import (
    QUERY_MAX_LENGTH,
    DirectoryAnswer,
    DirectoryEntry,
    DirectoryKind,
)

EVERYONE_DISPLAY_NAME: Final = "Everyone"
_PARAMETERS: Final = ("q", "types", "limit")
_PROBLEMS: Final = {
    "q": (
        f"q may hold at most {QUERY_MAX_LENGTH} of the characters A-Z a-z 0-9 space . _ @ + -; "
        "remove the others"
    ),
    "types": "types is a comma list of user, group, service_principal and everyone",
    "limit": f"limit must be a whole number from 1 to {PRINCIPAL_SEARCH_MAX_LIMIT}",
}
_DIRECTORY_OF: Final[dict[GrantType, DirectoryKind]] = {
    "group": "groups",
    "user": "users",
    "service_principal": "service_principals",
}
_TYPE_ORDER: Final[tuple[GrantType, ...]] = ("everyone", "group", "user", "service_principal")


class PrincipalDirectory(Protocol):
    """``identity.directory.DirectoryLookup.search``: never raises for a failure of Databricks."""

    async def search(
        self,
        kind: DirectoryKind,
        query: str,
        *,
        count: int,
        caller_token: SecretStr | None,
    ) -> DirectoryAnswer: ...


def _query_of(request: Request) -> PrincipalSearchQuery:
    params: dict[str, str] = {}
    for name in _PARAMETERS:
        values = request.query_params.getlist(name)
        if len(values) > 1:
            raise BadRequest(f"{name} may be sent once")
        if values:
            params[name] = values[0]
    try:
        return PrincipalSearchQuery.from_query(params)
    except ValidationError as exc:
        field = str(exc.errors(include_input=False)[0]["loc"][0])
        raise BadRequest(_PROBLEMS.get(field, "the query is not valid")) from None


def _shown(text: str | None) -> str | None:
    return text.strip() if text is not None and text.strip() else None


def _offer(
    type_: GrantType, name: str, display_name: str | None, detail: str | None
) -> PrincipalSuggestion | None:
    """The suggestion, or ``None`` when no grant could name it (the form could not save it)."""
    try:
        GrantRequest.model_validate({"type": type_, "name": name, "permission": "CAN_USE"})
    except ValidationError:
        return None
    return PrincipalSuggestion(type=type_, name=name, display_name=display_name, detail=detail)


def _suggestion(type_: GrantType, entry: DirectoryEntry) -> PrincipalSuggestion | None:
    display_name = _shown(entry.display_name)
    if type_ == "user":
        if entry.user_name is None:
            return None
        email = entry.user_name.lower()
        return _offer("user", email, display_name, email if display_name else None)
    if type_ == "group":
        # A group is granted by its display name, so that name is the suggestion's name.
        if entry.display_name is None:
            return None
        return _offer("group", entry.display_name, None, None)
    if entry.application_id is None:
        return None
    application_id = entry.application_id.lower()
    return _offer("service_principal", application_id, display_name, application_id)


def _order(suggestion: PrincipalSuggestion) -> tuple[int, str, str]:
    shown = suggestion.display_name or suggestion.name
    return _TYPE_ORDER.index(suggestion.type), shown.casefold(), suggestion.name


class PrincipalRoutes:
    """The route provider of ``GET /principals``; ``directory`` reads the workspace directory."""

    def __init__(self, directory: PrincipalDirectory) -> None:
        self._directory = directory

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def get_principals(request: Request) -> Response:
            await context.require(request, Action.PRINCIPAL_SEARCH)
            query = _query_of(request)
            text = query.q.strip()
            to_read = [t for t in _DIRECTORY_OF if t in query.types] if text else []
            answers = await asyncio.gather(
                *(
                    self._directory.search(
                        _DIRECTORY_OF[type_],
                        text,
                        count=query.limit + 1,
                        caller_token=context.actor(request).visitor_token,
                    )
                    for type_ in to_read
                )
            )
            by_type = dict(zip(to_read, answers, strict=True))

            suggestions = [
                suggestion
                for type_, answer in by_type.items()
                for entry in answer.entries
                if (suggestion := _suggestion(type_, entry)) is not None
            ]
            if "everyone" in query.types and "everyone".startswith(text.casefold()):
                suggestions.append(
                    PrincipalSuggestion(
                        type="everyone",
                        name=EVERYONE_NAME,
                        display_name=EVERYONE_DISPLAY_NAME,
                        detail=None,
                    )
                )
            suggestions.sort(key=_order)
            has_more = any(len(answer.entries) > query.limit for answer in answers)

            def source(type_: GrantType) -> str:
                answer = by_type.get(type_)
                return "not_read" if answer is None else answer.source

            body = PrincipalList(
                items=suggestions[: query.limit],
                truncated=has_more or len(suggestions) > query.limit,
                sources=PrincipalSources.model_validate(
                    {
                        "users": source("user"),
                        "groups": source("group"),
                        "service_principals": source("service_principal"),
                    }
                ),
            )
            return JSONResponse(body.model_dump(mode="json"))

        return [Route("/principals", get_principals, methods=["GET"])]
