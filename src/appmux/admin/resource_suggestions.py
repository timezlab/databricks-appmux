"""The pickers behind the attach form: the resource catalogue and the objects to pick (T195).

Two routes, both ``resource.suggest`` (a platform admin, or a person with ``CAN_MANAGE`` on at least
one app), and neither writes or records anything:

* ``GET /resources/types``: the catalogue of ``registry.resources.RESOURCE_TYPES`` with the words
  the form shows (a description and a target example per type), so the UI keeps no copy of it.
  Nothing is called upstream. ``TYPE_PRESENTATION`` holds the words; a test fails when it and the
  catalogue disagree about which types exist.
* ``GET /resources/suggestions?type=&q=&limit=``: objects of one type, read with the person's own
  token and filtered here. Five types have a list call: SQL warehouses, serving endpoints, secret
  scopes (and, for ``q`` of the form ``scope/prefix``, the key names of one scope; never a value),
  MLflow experiments and jobs. Every other type answers ``not_supported``.

**A suggestion helps and never gates.** Whatever Databricks refuses or does not answer (no token, a
scope the App does not declare, a failure, a body appmux cannot read) is a ``200`` with
``source: "unavailable"``, no items and a note in words, so the form keeps free typing. The note
repeats neither the platform's text nor the token. Measured on the Free Edition workspace, an App's
``user_api_scopes`` refuse ``secrets``, ``mlflow`` and ``jobs``, so those three often answer
``unavailable`` on a real App.

**Targets.** A suggestion's ``target`` is what ``AttachResourceRequest.target`` takes, and a target
the attach form would refuse is left out. The one exception is a secret scope, whose target is the
prefix ``<scope>/`` so that the next keystrokes name the key.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, get_args

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from appmux.admin.api import AdminContext
from appmux.admin.errors import BadRequest
from appmux.admin.resources import (
    missing_scope_names,
    names_a_missing_scope,
    send_with_token,
)
from appmux.admin.schemas import (
    AccessAs,
    ResourceSuggestion,
    ResourceSuggestionList,
    ResourceTypeEntry,
    ResourceTypeList,
)
from appmux.authz.actions import Action
from appmux.registry.resources import RESOURCE_TYPES, SECRET_TYPE, is_valid_target

DEFAULT_LIMIT: Final = 20
MAX_LIMIT: Final = 50
MAX_QUERY_LENGTH: Final = 256

_LIST_TIMEOUT_S: Final = 15.0
_JOBS_PAGE_SIZE: Final = 100  # the most the jobs list takes in one page
_EXPERIMENTS_PAGE_SIZE: Final = 1000  # below the 3000 the search takes
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_NOT_FOUND: Final = 404
_SCOPE_STATUSES: Final = frozenset({400, 403, 404})  # the statuses that may name a missing scope
# Names `user_api_scopes` refuses (spikes 15 and 16, Free Edition, 2026-10-30): a platform admin
# cannot add them, so the note must not send anyone to ask.
_UNDECLARABLE_SCOPES: Final = frozenset(
    {
        "secrets",
        "mlflow",
        "jobs",
        "access-management",
        "workspace",
        "unity-catalog",
        "catalog.volumes",
    }
)
_CONTROL_CATEGORY: Final = "Cc"
_SHARED_SP: Final = "shared_sp"

WAREHOUSES_PATH: Final = "/api/2.0/sql/warehouses"
ENDPOINTS_PATH: Final = "/api/2.0/serving-endpoints"
SCOPES_PATH: Final = "/api/2.0/secrets/scopes/list"
KEYS_PATH: Final = "/api/2.0/secrets/list"  # key names only; the value endpoint is never called
EXPERIMENTS_PATH: Final = "/api/2.0/mlflow/experiments/search"
JOBS_PATH: Final = "/api/2.2/jobs/list"


# --- the catalogue as data ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TypePresentation:
    """The words the form shows for one catalogue type (the rest is read from the catalogue)."""

    description: str
    target_hint: str


TYPE_PRESENTATION: Final[Mapping[str, TypePresentation]] = MappingProxyType(
    {
        "secret": TypePresentation(
            "One key of a secret scope; the app reads its value as an environment variable.",
            "my-scope/my-key",
        ),
        "sql_warehouse": TypePresentation(
            "A SQL warehouse the app runs statements on.", "1234567890abcdef"
        ),
        "mlflow_experiment": TypePresentation(
            "An MLflow experiment the app logs runs to; the target is its id.", "1234567890123456"
        ),
        "serving_endpoint": TypePresentation(
            "A model serving endpoint the app queries; the target is its name.", "my-endpoint"
        ),
        "lakebase_database": TypePresentation(
            "A Lakebase database the app connects to; the target is the endpoint path of an "
            "Autoscaling database or the host of a Provisioned one.",
            "my-lakebase-host",
        ),
        "job": TypePresentation("A job the app runs or reads; the target is its id.", "123456789"),
        "uc_table": TypePresentation(
            "A Unity Catalog table the app reads or writes.", "main.sales.orders"
        ),
        "uc_volume": TypePresentation(
            "A Unity Catalog volume the app reads or writes files in.", "/Volumes/main/sales/files"
        ),
        "uc_function": TypePresentation(
            "A Unity Catalog function the app calls.", "main.sales.score"
        ),
        "uc_connection": TypePresentation(
            "A Unity Catalog connection to an outside system.", "my_connection"
        ),
        "genie_space": TypePresentation(
            "A Genie space the app asks questions of; the target is its id.",
            "01ef1234567890abcdef1234567890ab",
        ),
        "vector_search_index": TypePresentation(
            "An AI Search (vector search) index the app queries.", "main.sales.docs_index"
        ),
        "databricks_app": TypePresentation(
            "Another Databricks app this app calls; the target is its name.", "my-other-app"
        ),
        "gateway_model_service": TypePresentation(
            "A model service behind the Unity AI gateway that this app calls.",
            "main.sales.chat_service",
        ),
    }
)


def resource_type_list() -> ResourceTypeList:
    """The catalogue in its own order, each type with its words and the modes it allows."""
    return ResourceTypeList(items=[_entry_of(name) for name in RESOURCE_TYPES])


def _entry_of(type_name: str) -> ResourceTypeEntry:
    catalogue = RESOURCE_TYPES[type_name]
    words = TYPE_PRESENTATION[type_name]
    return ResourceTypeEntry(
        type=type_name,
        label=catalogue.label,
        description=words.description,
        permissions=list(catalogue.levels),
        access_as=_access_modes_of(type_name),
        target_hint=words.target_hint,
        can_suggest=type_name in SUGGESTABLE_TYPES,
    )


def _access_modes_of(type_name: str) -> list[AccessAs]:
    """What ``verify_attach`` lets the type use: none for a secret; ``shared_sp`` needs a check."""
    if type_name == SECRET_TYPE:
        return []
    is_checked = RESOURCE_TYPES[type_name].is_verifiable
    return [mode for mode in get_args(AccessAs) if mode != _SHARED_SP or is_checked]


# --- the filter, the order and the cut ------------------------------------------------------


def select_suggestions(
    candidates: Iterable[ResourceSuggestion],
    query: str,
    *,
    limit: int,
    is_label_only: bool = False,
) -> tuple[list[ResourceSuggestion], bool]:
    """The candidates whose label or target holds ``query`` (any case), by label, at most ``limit``.

    The second value is true when more matched than were returned. The query is plain text, not a
    pattern. ``is_label_only`` matches the label alone: a secret key's target starts with the scope
    the query already named, which would match every key.
    """
    needle = query.strip().casefold()
    matched = sorted(
        (
            candidate
            for candidate in candidates
            if needle in candidate.label.casefold()
            or (not is_label_only and needle in candidate.target.casefold())
        ),
        key=lambda candidate: (candidate.label.casefold(), candidate.label, candidate.target),
    )
    return matched[:limit], len(matched) > limit


# --- what a list call gave ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Page:
    """The objects one list call named, ready to filter.

    ``filter_text`` is what ``q`` is matched against (for a secret key it is the part after the
    slash, matched against the key names only: ``is_label_only``); ``is_partial`` is true when
    Databricks says more exist behind this page.
    """

    candidates: list[ResourceSuggestion]
    filter_text: str
    noun: str
    is_partial: bool
    is_label_only: bool = False


class _NoItems(Exception):
    """A list that gave nothing, and why in words: an answer, not an error page."""

    def __init__(self, source: Literal["caller_token", "unavailable"], note: str) -> None:
        super().__init__(note)
        self.source = source
        self.note = note


class _Reader:
    """Reads the list calls of one request with the person's token; failures become notes."""

    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token

    async def read(
        self,
        method: str,
        path: str,
        noun: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
        missing_note: str | None = None,
    ) -> Mapping[str, Any]:
        """The JSON object Databricks answered, or ``_NoItems``.

        ``missing_note`` is set where a ``404`` means "no such object" (the secret scope named in
        the query) and not that the call itself failed.
        """
        response = await send_with_token(
            self._client,
            self._token,
            method,
            path,
            timeout_s=_LIST_TIMEOUT_S,
            json=json,
            params=params,
        )
        if response is None:
            raise _NoItems(
                "unavailable",
                f"Databricks did not answer the list of {noun}; try again, or type the target",
            )
        if response.status_code != httpx.codes.OK:
            raise _refusal(response, noun, missing_note)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise _shape_problem(noun)
        return body


def _refusal(response: httpx.Response, noun: str, missing_note: str | None) -> _NoItems:
    status = response.status_code
    if status == _HTTP_UNAUTHORIZED:
        return _NoItems(
            "unavailable",
            "your Databricks session was not accepted; reload this page to sign in again, "
            "or type the target",
        )
    if status in _SCOPE_STATUSES and names_a_missing_scope(response):
        return _NoItems("unavailable", _missing_scope_note(response, noun))
    if status == _HTTP_NOT_FOUND and missing_note is not None:
        return _NoItems("caller_token", missing_note)
    if status == httpx.codes.FORBIDDEN:
        return _NoItems(
            "unavailable", f"your Databricks account cannot list {noun}; type the target"
        )
    return _NoItems(
        "unavailable",
        f"Databricks answered {status} to the list of {noun}; try again, or type the target",
    )


def _missing_scope_note(response: httpx.Response, noun: str) -> str:
    """Says which scope the App lacks when Databricks named one, and who can fix it."""
    names = missing_scope_names(response)
    cannot_list = f"your Databricks session cannot list {noun}"
    if not names:
        return (
            f"{cannot_list}: the user authorization of the appmux App does not include the scope "
            "this needs; ask a platform admin to add it, or type the target"
        )
    quoted = ", ".join(f'"{name}"' for name in names)
    refused = [name for name in names if name in _UNDECLARABLE_SCOPES]
    if refused:
        return (
            f'{cannot_list}: it needs the "{refused[0]}" scope, which cannot be added to the '
            "user authorization of an App; type the target"
        )
    scope_words = "scope" if len(names) == 1 else "scopes"
    return (
        f"{cannot_list}: the user authorization of the appmux App does not include the {quoted} "
        f"{scope_words}; ask a platform admin to add {'it' if len(names) == 1 else 'them'} and to "
        "stop and start the App, or type the target"
    )


def _shape_problem(noun: str) -> _NoItems:
    return _NoItems(
        "unavailable",
        f"Databricks answered the list of {noun} in a shape appmux does not read; type the target",
    )


def _objects_of(body: Mapping[str, Any], key: str, noun: str) -> list[Mapping[str, Any]]:
    """The objects under ``key``; none when it is absent (Databricks answers ``{}`` for none)."""
    listed = body.get(key, [])
    if not isinstance(listed, list):
        raise _shape_problem(noun)
    return [entry for entry in listed if isinstance(entry, dict)]


def _is_partial(body: Mapping[str, Any]) -> bool:
    return bool(body.get("next_page_token")) or body.get("has_more") is True


def _suggestion(
    type_name: str, target: object, label: object, detail: str | None = None
) -> ResourceSuggestion | None:
    """The suggestion, or ``None`` for an object this code cannot offer as a target."""
    if not isinstance(target, str) or not isinstance(label, str) or not label.strip():
        return None
    catalogue = RESOURCE_TYPES[type_name]
    # The same two rules ``verify_attach`` applies to a target: a checked type has a form; any other
    # type takes printable text.
    if catalogue.is_verifiable:
        is_target = is_valid_target(type_name, target)
    else:
        is_target = bool(target.strip()) and target.isprintable()
    if not is_target:
        return None
    return ResourceSuggestion(
        target=target, label=label, detail=detail, permissions=list(catalogue.levels)
    )


def _joined(*parts: object) -> str | None:
    texts = [part for part in parts if isinstance(part, str) and part]
    return ", ".join(texts) if texts else None


def _present(items: Iterable[ResourceSuggestion | None]) -> list[ResourceSuggestion]:
    return [item for item in items if item is not None]


# --- the list call of each type -------------------------------------------------------------

Lister = Callable[[_Reader, str], Awaitable[_Page]]


async def _list_warehouses(reader: _Reader, query: str) -> _Page:
    noun = "SQL warehouses"
    body = await reader.read("GET", WAREHOUSES_PATH, noun)
    candidates = []
    for entry in _objects_of(body, "warehouses", noun):
        size = entry.get("cluster_size")
        detail = _joined(entry.get("state"), f"size {size}" if isinstance(size, str) else None)
        candidates.append(_suggestion("sql_warehouse", entry.get("id"), entry.get("name"), detail))
    return _Page(_present(candidates), query, noun, _is_partial(body))


async def _list_serving_endpoints(reader: _Reader, query: str) -> _Page:
    noun = "serving endpoints"
    body = await reader.read("GET", ENDPOINTS_PATH, noun)
    candidates = []
    for entry in _objects_of(body, "endpoints", noun):
        state = entry.get("state")
        ready = state.get("ready") if isinstance(state, dict) else None
        candidates.append(
            _suggestion(
                "serving_endpoint",
                entry.get("name"),
                entry.get("name"),
                _joined(ready, entry.get("task")),
            )
        )
    return _Page(_present(candidates), query, noun, _is_partial(body))


async def _list_secrets(reader: _Reader, query: str) -> _Page:
    scope, has_slash, key_query = query.partition("/")
    if not has_slash:
        return await _list_scopes(reader, query)
    if "/" in key_query or not _is_scope_name(scope):
        raise _NoItems(
            "caller_token",
            "that is not <scope>/<key>: a scope name has letters, digits and the characters "
            ". _ @ -, "
            "then comes one slash and the start of a key",
        )
    noun = "secret keys"
    body = await reader.read(
        "GET",
        KEYS_PATH,
        noun,
        params={"scope": scope},
        missing_note="no secret scope with that name is visible to you",
    )
    candidates = _present(
        _suggestion(
            SECRET_TYPE,
            f"{scope}/{entry['key']}" if isinstance(entry.get("key"), str) else None,
            entry.get("key"),
        )
        for entry in _objects_of(body, "secrets", noun)
    )
    return _Page(candidates, key_query, noun, _is_partial(body), is_label_only=True)


async def _list_scopes(reader: _Reader, query: str) -> _Page:
    noun = "secret scopes"
    body = await reader.read("GET", SCOPES_PATH, noun)
    candidates: list[ResourceSuggestion] = []
    for entry in _objects_of(body, "scopes", noun):
        name = entry.get("name")
        if isinstance(name, str) and _is_scope_name(name):
            # The target is a prefix, not a complete target: the person types the key next.
            candidates.append(
                ResourceSuggestion(
                    target=f"{name}/",
                    label=name,
                    detail=None,
                    permissions=list(RESOURCE_TYPES[SECRET_TYPE].levels),
                )
            )
    return _Page(candidates, query, noun, _is_partial(body))


def _is_scope_name(name: str) -> bool:
    # A scope name has the charset of the first half of a secret target, so ask the one rule that
    # knows it with a key that always fits.
    return is_valid_target(SECRET_TYPE, f"{name}/k")


async def _list_experiments(reader: _Reader, query: str) -> _Page:
    noun = "MLflow experiments"
    body = await reader.read(
        "POST",
        EXPERIMENTS_PATH,
        noun,
        json={"max_results": _EXPERIMENTS_PAGE_SIZE, "view_type": "ACTIVE_ONLY"},
    )
    candidates = _present(
        _suggestion("mlflow_experiment", entry.get("experiment_id"), entry.get("name"))
        for entry in _objects_of(body, "experiments", noun)
        if entry.get("lifecycle_stage") != "deleted"
    )
    return _Page(candidates, query, noun, _is_partial(body))


async def _list_jobs(reader: _Reader, query: str) -> _Page:
    # The ``name`` parameter of the jobs list is an exact match, so the search is made here.
    noun = "jobs"
    body = await reader.read("GET", JOBS_PATH, noun, params={"limit": str(_JOBS_PAGE_SIZE)})
    candidates = []
    for entry in _objects_of(body, "jobs", noun):
        job_id = entry.get("job_id")
        settings = entry.get("settings")
        candidates.append(
            _suggestion(
                "job",
                str(job_id) if isinstance(job_id, int) and not isinstance(job_id, bool) else None,
                settings.get("name") if isinstance(settings, dict) else None,
            )
        )
    return _Page(_present(candidates), query, noun, _is_partial(body))


_LISTERS: Final[Mapping[str, Lister]] = MappingProxyType(
    {
        "sql_warehouse": _list_warehouses,
        "serving_endpoint": _list_serving_endpoints,
        SECRET_TYPE: _list_secrets,
        "mlflow_experiment": _list_experiments,
        "job": _list_jobs,
    }
)
SUGGESTABLE_TYPES: Final = frozenset(_LISTERS)


# --- the routes -----------------------------------------------------------------------------


class ResourceSuggestionRoutes:
    """The route provider of the two resource routes; ``workspace`` is the workspace client."""

    def __init__(self, workspace: httpx.AsyncClient) -> None:
        self._workspace = workspace

    def routes(self, context: AdminContext) -> Sequence[Route]:
        async def types(request: Request) -> Response:
            await context.require(request, Action.RESOURCE_SUGGEST)
            return JSONResponse(resource_type_list().model_dump(mode="json"))

        async def suggestions(request: Request) -> Response:
            await context.require(request, Action.RESOURCE_SUGGEST)
            actor = context.actor(request)
            type_name = _type_param(request)
            query = _query_param(request)
            limit = _limit_param(request)
            answer = await self._suggest(
                type_name,
                query,
                limit,
                None if actor.visitor_token is None else actor.visitor_token.get_secret_value(),
            )
            return JSONResponse(answer.model_dump(mode="json"))

        return [
            Route("/resources/types", types, methods=["GET"]),
            Route("/resources/suggestions", suggestions, methods=["GET"]),
        ]

    async def _suggest(
        self, type_name: str, query: str, limit: int, token: str | None
    ) -> ResourceSuggestionList:
        lister = _LISTERS.get(type_name)
        if lister is None:
            return _nothing(
                "not_supported",
                "appmux has no list to pick from for this type; type the target, for example "
                f"{TYPE_PRESENTATION[type_name].target_hint}",
            )
        if token is None:
            return _nothing(
                "unavailable",
                "appmux did not receive your Databricks access token, so it cannot list objects as "
                "you; type the target",
            )
        try:
            page = await lister(_Reader(self._workspace, token), query)
        except _NoItems as nothing:
            return _nothing(nothing.source, nothing.note)
        items, is_cut = select_suggestions(
            page.candidates, page.filter_text, limit=limit, is_label_only=page.is_label_only
        )
        return ResourceSuggestionList(
            items=items,
            truncated=is_cut or page.is_partial,
            source="caller_token",
            note=(
                f"Databricks returned only part of the list of {page.noun}, so the one you want "
                "may be missing; type it if it is"
                if page.is_partial
                else None
            ),
        )


def _nothing(
    source: Literal["unavailable", "not_supported", "caller_token"], note: str
) -> ResourceSuggestionList:
    return ResourceSuggestionList(items=[], truncated=False, source=source, note=note)


# --- the parameters -------------------------------------------------------------------------


def _single(request: Request, name: str) -> str | None:
    values = request.query_params.getlist(name)
    if len(values) > 1:
        raise BadRequest(f"{name} may be sent once")
    return values[0] if values else None


def _type_param(request: Request) -> str:
    text = _single(request, "type")
    if text is None or text not in RESOURCE_TYPES:
        raise BadRequest(f"type is one of: {', '.join(RESOURCE_TYPES)}", code="unknown_type")
    return text


def _query_param(request: Request) -> str:
    text = _single(request, "q") or ""
    if len(text) > MAX_QUERY_LENGTH or any(
        unicodedata.category(character) == _CONTROL_CATEGORY for character in text
    ):
        raise BadRequest(
            f"q is text of at most {MAX_QUERY_LENGTH} characters with no control character",
            code="invalid_query",
        )
    return text


def _limit_param(request: Request) -> int:
    text = _single(request, "limit")
    if text is None:
        return DEFAULT_LIMIT
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= MAX_LIMIT:
        raise BadRequest(f"limit is a whole number from 1 to {MAX_LIMIT}", code="invalid_limit")
    return int(text)
