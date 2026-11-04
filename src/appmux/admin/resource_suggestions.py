"""The pickers behind the attach form: the resource catalogue and the objects to pick (T195, T211).

Two routes, both ``resource.suggest`` (a platform admin, or a person with ``CAN_MANAGE`` on at least
one app), and neither writes or records anything:

* ``GET /resources/types``: the catalogue of ``registry.resources.RESOURCE_TYPES`` with the words
  the form shows (a description and a target example per type), so the UI keeps no copy of it.
  Nothing is called upstream. ``TYPE_PRESENTATION`` holds the words; a test fails when it and the
  catalogue disagree about which types exist.
* ``GET /resources/suggestions?type=&q=&limit=``: objects of one type, read with the person's own
  token and filtered here. Every type of the catalogue has a list call, as in the Databricks Apps
  console. ``not_supported`` stays in the contract for a type added to the catalogue before its
  list exists.

**What each type is listed from** (every call is a ``GET`` unless noted). The scope in brackets is
the OAuth scope the call is expected to need, taken from the REST reference and the names an App's
``user_api_scopes`` accepts or refuses; it is documented, not measured. What was measured is the
shape of each answer (field names, counts, which field holds the name), read on the Free Edition
workspace with the Databricks CLI's token on 2026-11-04, except for model services, whose path is a
guess no answer has confirmed. No call was made with a person's App token, and the platform-token
retry below is not measured yet.

* SQL warehouses: ``/api/2.0/sql/warehouses`` (``sql``).
* Serving endpoints: ``/api/2.0/serving-endpoints`` (``serving.serving-endpoints:read``).
* Secrets: ``/api/2.0/secrets/scopes/list``, then ``/api/2.0/secrets/list`` for the key names
  (``secrets``, which an App refuses).
* MLflow experiments: ``POST /api/2.0/mlflow/experiments/search`` (``mlflow``, refused).
* Jobs: ``/api/2.2/jobs/list`` (``jobs``, refused).
* Tables: ``/api/2.1/unity-catalog/catalogs``, ``schemas``, ``tables``
  (``catalog.catalogs:read``, ``catalog.schemas:read``, ``catalog.tables:read``).
* Functions: the same catalogs and schemas, then ``functions`` (``unity-catalog``, refused).
* Volumes: the same catalogs and schemas, then ``volumes`` (``catalog.volumes``, refused).
* Connections: ``/api/2.1/unity-catalog/connections`` (``catalog.connections:read``).
* Genie spaces: ``/api/2.0/genie/spaces`` (``dashboards.genie:read``).
* AI Search: ``/api/2.0/vector-search/endpoints``, then ``/api/2.0/vector-search/indexes``
  (``vectorsearch.vector-search-indexes:read``).
* Databricks apps: ``/api/2.0/apps`` (``apps``).
* Model services: ``/api/2.1/unity-catalog/model-services`` (no scope documented; a refusal that
  names one is shown as such).
* Lakebase: ``/api/2.0/postgres/projects`` and its ``branches`` and ``endpoints`` (``postgres``),
  and ``/api/2.0/database/instances`` for Provisioned instances (no scope documented).

**Three levels, one field.** A Unity Catalog name has three parts, and a secret, a vector search
index and a Lakebase endpoint have a parent. The list answers the level the query stands at, and
the levels above the last are *prefix* targets, which end in the separator and are not targets the
attach form takes (``is_valid_target`` refuses them), so the field can go on: ``""`` lists
catalogs (target ``main.``), ``main.`` lists schemas (``main.sales.``), ``main.sales.`` lists the
objects. Volumes are the same under ``/Volumes/``, a vector search index is ``<endpoint>/`` then
the index, a secret is ``<scope>/`` then the key, and a Lakebase database is
``projects/<project>/`` then ``branches/<branch>/`` then the endpoint (Provisioned instances are
offered beside the projects). The text after the last separator filters the names at that level,
by name alone where the target would match every item.

**Whose access.** The person's own token is tried first. An App's ``user_api_scopes`` cannot hold
``secrets``, ``mlflow``, ``jobs``, ``workspace``, ``unity-catalog`` or ``catalog.volumes`` (spikes
15 and 16), so for secrets, experiments, jobs, functions and volumes that token is always refused.
When the refusal is a ``403``, or a ``400``/``403``/``404`` that names a missing scope, the same
list is read again from its first page with appmux's own service-principal token
(``platform_token``, the one ``GET /principals`` falls back to) and answers
``source: "platform_token"``: it shows what appmux can read, not what the person can, which the
form says. A ``401``, a request that got no answer, any other status and an unreadable body are
not retried. If the second try fails, or the platform has no token to give, the answer is the
person's own refusal. A person whose token did not arrive is listed with the platform token too.

**A suggestion helps and never gates.** Whatever Databricks refuses or does not answer is a ``200``
with ``source: "unavailable"``, no items and a note in words, so the form keeps free typing. The
note repeats neither the platform's text nor a token.

**Targets.** A suggestion's ``target`` is what ``AttachResourceRequest.target`` takes, and a target
the attach form would refuse is left out; the prefix targets above are the one exception.

**Pages.** A list that pages is followed for up to ``LIST_MAX_PAGES`` pages, because Databricks may
answer a short or empty page and still hold a ``next_page_token``. A list longer than that is cut
and the answer says so.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal, get_args

import httpx
from pydantic import SecretStr
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
LIST_MAX_PAGES: Final = 5  # pages followed for one list; at 200 a page, 1000 objects

logger = logging.getLogger(__name__)

_LIST_TIMEOUT_S: Final = 15.0
_JOBS_PAGE_SIZE: Final = 100  # the most the jobs list takes in one page
_EXPERIMENTS_PAGE_SIZE: Final = 1000  # below the 3000 the search takes
_UNITY_CATALOG_PAGE_SIZE: Final = 200  # a page of Unity Catalog names; more are paged
_PAGE_SIZE: Final = 100  # a page of Genie spaces, apps and Lakebase objects
_PAGE_CURSOR_PARAM: Final = "page_token"
_UNITY_CATALOG_DEPTH: Final = 3  # catalog, schema, object
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
CATALOGS_PATH: Final = "/api/2.1/unity-catalog/catalogs"
SCHEMAS_PATH: Final = "/api/2.1/unity-catalog/schemas"
TABLES_PATH: Final = "/api/2.1/unity-catalog/tables"
FUNCTIONS_PATH: Final = "/api/2.1/unity-catalog/functions"
VOLUMES_PATH: Final = "/api/2.1/unity-catalog/volumes"
CONNECTIONS_PATH: Final = "/api/2.1/unity-catalog/connections"
MODEL_SERVICES_PATH: Final = "/api/2.1/unity-catalog/model-services"
GENIE_SPACES_PATH: Final = "/api/2.0/genie/spaces"
VECTOR_ENDPOINTS_PATH: Final = "/api/2.0/vector-search/endpoints"
VECTOR_INDEXES_PATH: Final = "/api/2.0/vector-search/indexes"
APPS_PATH: Final = "/api/2.0/apps"
LAKEBASE_PROJECTS_PATH: Final = "/api/2.0/postgres/projects"  # also the parent of branches
LAKEBASE_INSTANCES_PATH: Final = "/api/2.0/database/instances"


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
    Databricks says more exist behind this page. ``note`` says something else the person should
    know about the list (a part of it that could not be read) and is used in place of the partial
    note.
    """

    candidates: list[ResourceSuggestion]
    filter_text: str
    noun: str
    is_partial: bool
    is_label_only: bool = False
    note: str | None = None


class _NoItems(Exception):
    """A list that gave nothing, and why in words: an answer, not an error page.

    ``retry_with_platform_token`` is true when the refusal was of the person's token itself (a
    ``403``, or a missing scope the App cannot or does not declare), so appmux's own access may
    still read the list. Every other failure is final.
    """

    def __init__(
        self,
        source: Literal["caller_token", "platform_token", "unavailable"],
        note: str,
        *,
        retry_with_platform_token: bool = False,
    ) -> None:
        super().__init__(note)
        self.source = source
        self.note = note
        self.retry_with_platform_token = retry_with_platform_token


class _Reader:
    """Reads the list calls of one request with one token; failures become notes.

    ``viewer`` says whose token it is, the person's or appmux's own, so that "no such object" is
    said about the right reader: one the person cannot see may be visible to appmux, and back.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        *,
        viewer: Literal["caller_token", "platform_token"],
    ) -> None:
        self._client = client
        self._token = token
        self._viewer = viewer

    async def read(
        self,
        method: str,
        path: str,
        noun: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, object] | None = None,
        missing_object: str | None = None,
    ) -> Mapping[str, Any]:
        """The JSON object Databricks answered, or ``_NoItems``.

        ``missing_object`` is set where a ``404`` means "no such object" (the secret scope named in
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
            raise _refusal(response, noun, missing_object, self._viewer)
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise _shape_problem(noun)
        return body

    async def read_list(
        self,
        path: str,
        noun: str,
        key: str,
        *,
        params: Mapping[str, str] | None = None,
        missing_object: str | None = None,
    ) -> tuple[list[Mapping[str, Any]], bool]:
        """The objects under ``key`` of a ``GET`` list, following its pages, and whether it was cut.

        Databricks may answer a short or even an empty page and still hold a ``next_page_token``,
        so pages are followed until none is left or ``LIST_MAX_PAGES`` were read; the second value
        is true when the cap stopped the read. A failure on any page is the answer: a half-read
        list is not offered as if it were whole.
        """
        objects: list[Mapping[str, Any]] = []
        page_token: str | None = None
        for _ in range(LIST_MAX_PAGES):
            page_params = dict(params or {})
            if page_token is not None:
                page_params[_PAGE_CURSOR_PARAM] = page_token
            body = await self.read(
                "GET", path, noun, params=page_params, missing_object=missing_object
            )
            objects.extend(_objects_of(body, key, noun))
            next_token = body.get("next_page_token")
            if not isinstance(next_token, str) or not next_token:
                return objects, False
            page_token = next_token
        return objects, True


def _refusal(
    response: httpx.Response,
    noun: str,
    missing_object: str | None,
    viewer: Literal["caller_token", "platform_token"],
) -> _NoItems:
    status = response.status_code
    if status == _HTTP_UNAUTHORIZED:
        return _NoItems(
            "unavailable",
            "your Databricks session was not accepted; reload this page to sign in again, "
            "or type the target",
        )
    if status in _SCOPE_STATUSES and names_a_missing_scope(response):
        return _NoItems(
            "unavailable", _missing_scope_note(response, noun), retry_with_platform_token=True
        )
    if status == _HTTP_NOT_FOUND and missing_object is not None:
        return _NoItems(viewer, _missing_object_note(missing_object, viewer))
    if status == httpx.codes.FORBIDDEN:
        return _NoItems(
            "unavailable",
            f"your Databricks account cannot list {noun}; type the target",
            retry_with_platform_token=True,
        )
    return _NoItems(
        "unavailable",
        f"Databricks answered {status} to the list of {noun}; try again, or type the target",
    )


def _missing_object_note(
    missing_object: str, viewer: Literal["caller_token", "platform_token"]
) -> str:
    """Says an object is not there for the reader that looked; appmux's own look is not theirs."""
    if viewer == "caller_token":
        return f"{missing_object} is visible to you"
    return (
        f"{missing_object} is visible to appmux, which lists with its own access and not yours; "
        "check the name, or type the target"
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


def _is_name_part(text: str) -> bool:
    """Whether ``text`` is one part of a Unity Catalog name (or a project, branch, endpoint id).

    A part has the charset of a connection's whole name, so ask the one rule that knows it.
    """
    return is_valid_target("uc_connection", text)


def _last_part(name: object) -> str | None:
    """The part of a dotted name after the last dot (``main.sales.chat`` is ``chat``)."""
    return name.rpartition(".")[2] if isinstance(name, str) else None


def _tail_of(name: object, prefix: str) -> str | None:
    """What follows ``prefix`` in ``name``, or ``None`` when ``name`` does not start with it."""
    if isinstance(name, str) and name.startswith(prefix):
        return name[len(prefix) :]
    return None


def _prefix_suggestion(
    type_name: str, target: str, label: str, detail: str | None
) -> ResourceSuggestion:
    """A step toward a target: it ends in the separator, so the attach form refuses it as it is."""
    return ResourceSuggestion(
        target=target,
        label=label,
        detail=detail,
        permissions=list(RESOURCE_TYPES[type_name].levels),
    )


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
        missing_object="no secret scope with that name",
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
            candidates.append(_prefix_suggestion(SECRET_TYPE, f"{name}/", name, None))
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


# --- Unity Catalog: catalog.schema.name, and /Volumes/catalog/schema/name -------------------------

_LEVEL_WORDS: Final = ("catalog", "schema")  # what a prefix target stands for, by depth


@dataclass(frozen=True, slots=True)
class _ThreeLevelName:
    """How one Unity Catalog type writes its name, and the call that lists its objects.

    ``objects`` is both the noun and the key of the list in the answer (``tables``).
    """

    type_name: str
    head: str  # text before the first name: nothing, or ``/Volumes/``
    separator: str
    shape: str  # what the person is told when the query is not that shape
    objects: str
    objects_path: str
    detail_field: str | None  # the field of an object shown beside its name
    object_params: Mapping[str, str]  # sent with the objects call, beside the two names

    def join(self, names: Sequence[str]) -> str:
        return self.head + self.separator.join(names)


def _parse_three_level_name(query: str, form: _ThreeLevelName) -> tuple[tuple[str, ...], str]:
    """The names typed so far (catalog, then schema) and the text after the last separator.

    Raises ``_NoItems`` for a query that can start no name of this type, before anything is called.
    """
    text = query.strip()
    if form.head:
        if form.head.casefold().startswith(text.casefold()):
            return (), ""  # "", "/", "/Vol", "/Volumes/": the start of the head, no name yet
        if text.casefold().startswith(form.head.casefold()):
            text = text[len(form.head) :]
        elif form.separator in text:
            raise _NoItems("caller_token", form.shape)
    *parents, filter_text = text.split(form.separator)
    if (
        len(parents) >= _UNITY_CATALOG_DEPTH
        or not all(_is_name_part(parent) for parent in parents)
        or (filter_text and not _is_name_part(filter_text))
    ):
        raise _NoItems("caller_token", form.shape)
    return tuple(parents), filter_text


def _unity_catalog_candidate(
    form: _ThreeLevelName, parents: tuple[str, ...], entry: Mapping[str, Any]
) -> ResourceSuggestion | None:
    name = entry.get("name")
    if not isinstance(name, str):
        return None
    depth = len(parents)
    if depth < len(_LEVEL_WORDS):
        if not _is_name_part(name):
            return None
        target = form.join((*parents, name)) + form.separator
        return _prefix_suggestion(form.type_name, target, name, _LEVEL_WORDS[depth])
    detail = entry.get(form.detail_field) if form.detail_field else None
    return _suggestion(
        form.type_name,
        form.join((*parents, name)),
        name,
        detail if isinstance(detail, str) else None,
    )


def _unity_catalog_lister(form: _ThreeLevelName) -> Lister:
    async def list_names(reader: _Reader, query: str) -> _Page:
        parents, filter_text = _parse_three_level_name(query, form)
        params = {"max_results": str(_UNITY_CATALOG_PAGE_SIZE)}
        missing_object: str | None = None
        if not parents:
            noun, path = "catalogs", CATALOGS_PATH
        elif len(parents) == 1:
            noun, path = "schemas", SCHEMAS_PATH
            params["catalog_name"] = parents[0]
            missing_object = "no catalog with that name"
        else:
            noun, path = form.objects, form.objects_path
            params |= {"catalog_name": parents[0], "schema_name": parents[1], **form.object_params}
            missing_object = "no schema with that name"
        objects, is_partial = await reader.read_list(
            path, noun, noun, params=params, missing_object=missing_object
        )
        candidates = _present(_unity_catalog_candidate(form, parents, entry) for entry in objects)
        # By name alone: every target holds the catalog and schema the query already named.
        return _Page(candidates, filter_text, noun, is_partial, is_label_only=True)

    return list_names


_DOTTED_SHAPE: Final = (
    "that is not <catalog>.<schema>.<{last}>: each part has letters, digits, _ and -, "
    "with a dot between the parts"
)
_list_tables = _unity_catalog_lister(
    _ThreeLevelName(
        "uc_table",
        "",
        ".",
        _DOTTED_SHAPE.format(last="table"),
        "tables",
        TABLES_PATH,
        "table_type",
        MappingProxyType({"omit_columns": "true", "omit_properties": "true"}),
    )
)
_list_functions = _unity_catalog_lister(
    _ThreeLevelName(
        "uc_function",
        "",
        ".",
        _DOTTED_SHAPE.format(last="function"),
        "functions",
        FUNCTIONS_PATH,
        None,
        MappingProxyType({}),
    )
)
_list_volumes = _unity_catalog_lister(
    _ThreeLevelName(
        "uc_volume",
        "/Volumes/",
        "/",
        "that is not /Volumes/<catalog>/<schema>/<volume>: each part has letters, digits, _ and -",
        "volumes",
        VOLUMES_PATH,
        "volume_type",
        MappingProxyType({}),
    )
)


# --- the flat lists -------------------------------------------------------------------------


async def _list_connections(reader: _Reader, query: str) -> _Page:
    noun = "connections"
    objects, is_partial = await reader.read_list(
        CONNECTIONS_PATH,
        noun,
        "connections",
        params={"max_results": str(_UNITY_CATALOG_PAGE_SIZE)},
    )
    candidates = _present(
        _suggestion(
            "uc_connection",
            entry.get("name"),
            entry.get("name"),
            _joined(entry.get("connection_type")),
        )
        for entry in objects
    )
    return _Page(candidates, query, noun, is_partial)


async def _list_genie_spaces(reader: _Reader, query: str) -> _Page:
    noun = "Genie spaces"
    objects, is_partial = await reader.read_list(
        GENIE_SPACES_PATH, noun, "spaces", params={"page_size": str(_PAGE_SIZE)}
    )
    candidates = _present(
        _suggestion("genie_space", entry.get("space_id"), entry.get("title")) for entry in objects
    )
    return _Page(candidates, query, noun, is_partial)


async def _list_apps(reader: _Reader, query: str) -> _Page:
    noun = "Databricks apps"
    objects, is_partial = await reader.read_list(
        APPS_PATH, noun, "apps", params={"page_size": str(_PAGE_SIZE)}
    )
    candidates = []
    for entry in objects:
        compute = entry.get("compute_status")
        state = compute.get("state") if isinstance(compute, dict) else None
        candidates.append(
            _suggestion("databricks_app", entry.get("name"), entry.get("name"), _joined(state))
        )
    return _Page(_present(candidates), query, noun, is_partial)


async def _list_model_services(reader: _Reader, query: str) -> _Page:
    noun = "model services"
    objects, is_partial = await reader.read_list(MODEL_SERVICES_PATH, noun, "model_services")
    candidates = []
    for entry in objects:
        api_types = entry.get("supported_api_types")
        candidates.append(
            _suggestion(
                "gateway_model_service",
                entry.get("name"),
                _last_part(entry.get("name")),
                _joined(*api_types) if isinstance(api_types, list) else None,
            )
        )
    return _Page(_present(candidates), query, noun, is_partial)


# --- AI Search: the endpoint, then its index ------------------------------------------------


async def _list_vector_search(reader: _Reader, query: str) -> _Page:
    text = query.strip()
    endpoint, has_slash, index_query = text.partition("/")
    if not has_slash:
        return await _list_vector_endpoints(reader, text)
    if "/" in index_query or not _is_name_part(endpoint):
        raise _NoItems(
            "caller_token",
            "that is not <endpoint>/<index>: an endpoint name has letters, digits, _ and -, "
            "then comes one slash and the start of an index name",
        )
    noun = "AI Search indexes"
    objects, is_partial = await reader.read_list(
        VECTOR_INDEXES_PATH,
        noun,
        "vector_indexes",
        params={"endpoint_name": endpoint},
        missing_object="no AI Search endpoint with that name",
    )
    candidates = _present(
        _suggestion(
            "vector_search_index",
            entry.get("name"),
            _last_part(entry.get("name")),
            _joined(entry.get("index_type")),
        )
        for entry in objects
    )
    return _Page(candidates, index_query, noun, is_partial)


async def _list_vector_endpoints(reader: _Reader, query: str) -> _Page:
    noun = "AI Search endpoints"
    objects, is_partial = await reader.read_list(VECTOR_ENDPOINTS_PATH, noun, "endpoints")
    candidates: list[ResourceSuggestion] = []
    for entry in objects:
        name = entry.get("name")
        status = entry.get("endpoint_status")
        state = status.get("state") if isinstance(status, dict) else None
        if isinstance(name, str) and _is_name_part(name):
            candidates.append(
                _prefix_suggestion(
                    "vector_search_index", f"{name}/", name, _joined("endpoint", state)
                )
            )
    return _Page(candidates, query, noun, is_partial, is_label_only=True)


# --- Lakebase: Autoscaling project, branch, endpoint; and Provisioned instances -------------


def _lakebase_shape() -> _NoItems:
    return _NoItems(
        "caller_token",
        "that is not projects/<project>/branches/<branch>/endpoints/<endpoint>: each name has "
        "letters, digits, _ and -; a Provisioned instance is typed as its host",
    )


async def _list_lakebase(reader: _Reader, query: str) -> _Page:
    text = query.strip()
    segments = text.split("/")
    if len(segments) == 1 or (len(segments) == 2 and segments[0].casefold() == "projects"):
        return await _list_lakebase_roots(reader, text)
    _, project, *tail = segments
    if segments[0].casefold() != "projects" or not _is_name_part(project):
        raise _lakebase_shape()
    match tail:
        case [name_filter] | ["branches", name_filter]:
            return await _list_lakebase_branches(reader, project, name_filter)
        case ["branches", branch, name_filter] | ["branches", branch, "endpoints", name_filter]:
            if _is_name_part(branch):
                return await _list_lakebase_endpoints(reader, project, branch, name_filter)
    raise _lakebase_shape()


_LakebaseRootRead = Callable[[_Reader], Awaitable[tuple[list[ResourceSuggestion], bool]]]


async def _list_lakebase_roots(reader: _Reader, query: str) -> _Page:
    """Autoscaling projects and Provisioned instances; one list failing leaves the other."""
    reads: tuple[_LakebaseRootRead, ...] = (_read_autoscaling_projects, _read_provisioned_instances)
    candidates: list[ResourceSuggestion] = []
    is_partial = False
    failures: list[_NoItems] = []
    for read in reads:
        try:
            found, is_cut = await read(reader)
        except _NoItems as nothing:
            failures.append(nothing)
        else:
            candidates.extend(found)
            is_partial = is_partial or is_cut
    if len(failures) == len(reads):
        # Nothing to list: the answer is a refusal that appmux's own access may still get past
        # when either of the two was one.
        retryable = [failure for failure in failures if failure.retry_with_platform_token]
        raise (retryable or failures)[0]
    note = failures[0].note if failures else None
    return _Page(candidates, query, "Lakebase databases", is_partial, note=note)


async def _read_autoscaling_projects(reader: _Reader) -> tuple[list[ResourceSuggestion], bool]:
    objects, is_partial = await reader.read_list(
        LAKEBASE_PROJECTS_PATH,
        "Lakebase Autoscaling projects",
        "projects",
        params={"page_size": str(_PAGE_SIZE)},
    )
    found: list[ResourceSuggestion] = []
    for entry in objects:
        project = _tail_of(entry.get("name"), "projects/")
        if project is None or not _is_name_part(project):
            continue
        status = entry.get("status")
        display_name = status.get("display_name") if isinstance(status, dict) else None
        label = display_name if isinstance(display_name, str) and display_name.strip() else project
        found.append(
            _prefix_suggestion(
                "lakebase_database", f"projects/{project}/", label, "Autoscaling project"
            )
        )
    return found, is_partial


async def _read_provisioned_instances(reader: _Reader) -> tuple[list[ResourceSuggestion], bool]:
    objects, is_partial = await reader.read_list(
        LAKEBASE_INSTANCES_PATH,
        "Lakebase Provisioned instances",
        "database_instances",
        params={"page_size": str(_PAGE_SIZE)},
    )
    found = _present(
        _suggestion(
            "lakebase_database",
            entry.get("read_write_dns"),
            entry.get("name"),
            _joined("Provisioned", entry.get("state")),
        )
        for entry in objects
    )
    return found, is_partial


async def _list_lakebase_branches(reader: _Reader, project: str, name_filter: str) -> _Page:
    noun = "Lakebase branches"
    parent = f"projects/{project}/branches/"
    objects, is_partial = await reader.read_list(
        f"{LAKEBASE_PROJECTS_PATH}/{project}/branches",
        noun,
        "branches",
        params={"page_size": str(_PAGE_SIZE)},
        missing_object="no Lakebase project with that name",
    )
    candidates: list[ResourceSuggestion] = []
    for entry in objects:
        branch = _tail_of(entry.get("name"), parent)
        if branch is not None and _is_name_part(branch):
            candidates.append(
                _prefix_suggestion("lakebase_database", f"{parent}{branch}/", branch, "branch")
            )
    return _Page(candidates, name_filter, noun, is_partial, is_label_only=True)


async def _list_lakebase_endpoints(
    reader: _Reader, project: str, branch: str, name_filter: str
) -> _Page:
    noun = "Lakebase endpoints"
    parent = f"projects/{project}/branches/{branch}/endpoints/"
    objects, is_partial = await reader.read_list(
        f"{LAKEBASE_PROJECTS_PATH}/{project}/branches/{branch}/endpoints",
        noun,
        "endpoints",
        params={"page_size": str(_PAGE_SIZE)},
        missing_object="no Lakebase branch with that name",
    )
    candidates: list[ResourceSuggestion | None] = []
    for entry in objects:
        endpoint = _tail_of(entry.get("name"), parent)
        if endpoint is None or not _is_name_part(endpoint):
            continue
        status = entry.get("status")
        fields = status if isinstance(status, dict) else {}
        kind = fields.get("endpoint_type")
        detail = _joined(
            kind.removeprefix("ENDPOINT_TYPE_") if isinstance(kind, str) else None,
            fields.get("current_state"),
        )
        candidates.append(_suggestion("lakebase_database", f"{parent}{endpoint}", endpoint, detail))
    return _Page(_present(candidates), name_filter, noun, is_partial, is_label_only=True)


_LISTERS: Final[Mapping[str, Lister]] = MappingProxyType(
    {
        "sql_warehouse": _list_warehouses,
        "serving_endpoint": _list_serving_endpoints,
        SECRET_TYPE: _list_secrets,
        "mlflow_experiment": _list_experiments,
        "job": _list_jobs,
        "uc_table": _list_tables,
        "uc_function": _list_functions,
        "uc_volume": _list_volumes,
        "uc_connection": _list_connections,
        "genie_space": _list_genie_spaces,
        "vector_search_index": _list_vector_search,
        "databricks_app": _list_apps,
        "gateway_model_service": _list_model_services,
        "lakebase_database": _list_lakebase,
    }
)
SUGGESTABLE_TYPES: Final = frozenset(_LISTERS)


# --- the routes -----------------------------------------------------------------------------


PlatformToken = Callable[[], Awaitable[SecretStr]]

_NOT_RECEIVED_NOTE: Final = (
    "appmux did not receive your Databricks access token, so it cannot list objects as you; "
    "type the target"
)


class ResourceSuggestionRoutes:
    """The route provider of the two resource routes; ``workspace`` is the workspace client.

    ``platform_token`` (assembly's service-principal token, as ``DirectoryLookup`` takes it) is the
    second try when the person's own token is refused; without it a refusal is final.
    """

    def __init__(
        self, workspace: httpx.AsyncClient, *, platform_token: PlatformToken | None = None
    ) -> None:
        self._workspace = workspace
        self._platform_token = platform_token

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
        refusal = _NoItems("unavailable", _NOT_RECEIVED_NOTE)
        if token is not None:
            try:
                page = await lister(_Reader(self._workspace, token, viewer="caller_token"), query)
            except _NoItems as nothing:
                if not nothing.retry_with_platform_token:
                    return _nothing(nothing.source, nothing.note)
                refusal = nothing
            else:
                return _answer(page, "caller_token", limit)
        platform = await self._platform_bearer()
        if platform is not None:
            try:
                page = await lister(
                    _Reader(self._workspace, platform, viewer="platform_token"), query
                )
            except _NoItems as nothing:
                if nothing.source == "platform_token":
                    # Appmux's own access reached the list and found no such object: that is the
                    # answer, and a better one than the person's refusal, which says to ask for a
                    # scope that would not help.
                    return _nothing(nothing.source, nothing.note)
                # The platform's own refusal is not the person's business; what they were told is
                # why their token could not list it.
                logger.info("listing %s with the platform token failed", type_name)
            else:
                return _answer(page, "platform_token", limit)
        return _nothing(refusal.source, refusal.note)

    async def _platform_bearer(self) -> str | None:
        if self._platform_token is None:
            return None
        try:
            token = await self._platform_token()
        except Exception as exc:
            # The provider is an injected OAuth client with many ways to fail; each one only means
            # the platform's access is unavailable, which the caller answers with a note. Only the
            # class is logged: the message could hold a credential.
            logger.warning("the platform token is not available (%s)", type(exc).__name__)
            return None
        value = token.get_secret_value()
        return value if value.strip() else None


def _answer(
    page: _Page, source: Literal["caller_token", "platform_token"], limit: int
) -> ResourceSuggestionList:
    items, is_cut = select_suggestions(
        page.candidates, page.filter_text, limit=limit, is_label_only=page.is_label_only
    )
    partial_note = (
        f"Only part of the list of {page.noun} could be read, so the one you want may be "
        "missing; type it if it is"
    )
    return ResourceSuggestionList(
        items=items,
        truncated=is_cut or page.is_partial,
        source=source,
        note=page.note or (partial_note if page.is_partial else None),
    )


def _nothing(
    source: Literal["unavailable", "not_supported", "caller_token", "platform_token"], note: str
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
