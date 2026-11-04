"""What the suggestion contract tests share: a recording workspace and the admin API (T195, T211).

``Workspace`` is an ``httpx.MockTransport`` handler that records every request, so a test pins the
call the platform would receive (method, path, query, bearer token) and what the route makes of the
answer. ``Suggester`` mounts ``ResourceSuggestionRoutes`` behind the real guard and authorizer on
the in-memory registry. All names and ids are placeholders; both tokens are fake values.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

import httpx
from edge_harness import ALICE, AdminApi, seeded_registry
from pydantic import SecretStr
from stub_registry import StubRegistry

from appmux.admin.resource_suggestions import ResourceSuggestionRoutes

PLATFORM_TOKEN: Final = "platform-token-value-for-tests"  # noqa: S105 - a fake value for a test
DENIED_TEXT: Final = "User does not have permission to list the thing named internal-secret-name"

Answer = tuple[int, object] | Callable[[httpx.Request], httpx.Response]

# type -> (method, path, key of the list in the answer): the first call a query of "" makes
ROOT_CALLS: Final = {
    "sql_warehouse": ("GET", "/api/2.0/sql/warehouses", "warehouses"),
    "serving_endpoint": ("GET", "/api/2.0/serving-endpoints", "endpoints"),
    "secret": ("GET", "/api/2.0/secrets/scopes/list", "scopes"),
    "mlflow_experiment": ("POST", "/api/2.0/mlflow/experiments/search", "experiments"),
    "job": ("GET", "/api/2.2/jobs/list", "jobs"),
    "uc_table": ("GET", "/api/2.1/unity-catalog/catalogs", "catalogs"),
    "uc_function": ("GET", "/api/2.1/unity-catalog/catalogs", "catalogs"),
    "uc_volume": ("GET", "/api/2.1/unity-catalog/catalogs", "catalogs"),
    "uc_connection": ("GET", "/api/2.1/unity-catalog/connections", "connections"),
    "genie_space": ("GET", "/api/2.0/genie/spaces", "spaces"),
    "vector_search_index": ("GET", "/api/2.0/vector-search/endpoints", "endpoints"),
    "databricks_app": ("GET", "/api/2.0/apps", "apps"),
    "gateway_model_service": ("GET", "/api/2.1/unity-catalog/model-services", "model_services"),
    "lakebase_database": ("GET", "/api/2.0/postgres/projects", "projects"),
}
# A query of "" asks Lakebase twice: Autoscaling projects, then Provisioned instances.
SECOND_ROOT_CALLS: Final = {
    "lakebase_database": [("GET", "/api/2.0/database/instances", "database_instances")]
}


class Workspace:
    """A recording workspace: ``answers`` maps (method, path) to a status and a JSON body.

    An answer may instead be a function of the request, for a path whose answer depends on its
    query or on the bearer token. A path nobody answered is ``404``.
    """

    def __init__(self, answers: Mapping[tuple[str, str], Answer] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._answers = dict(answers or {})
        self._everything: Callable[[httpx.Request], httpx.Response] | None = None

    def answer_everything_with(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self._everything = answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._everything is not None:
            return self._everything(request)
        found = self._answers.get((request.method, request.url.path))
        if found is None:
            return httpx.Response(404, json={"error_code": "NOT_FOUND", "message": DENIED_TEXT})
        if callable(found):
            return found(request)
        status, body = found
        return httpx.Response(status, json=body)

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]

    @property
    def bearers(self) -> list[str]:
        """The token each request carried, in order, without the ``Bearer`` word."""
        return [
            request.headers["Authorization"].removeprefix("Bearer ") for request in self.requests
        ]


class PlatformTokenProvider:
    """The platform's service-principal token source: counts asks, and can fail or be unusable."""

    def __init__(self, token: str = PLATFORM_TOKEN, *, error: Exception | None = None) -> None:
        self.asked = 0
        self._token = token
        self._error = error

    async def __call__(self) -> SecretStr:
        self.asked += 1
        if self._error is not None:
            raise self._error
        return SecretStr(self._token)


class Suggester:
    """The admin API with the resource routes mounted over a recording workspace."""

    def __init__(
        self,
        workspace: Workspace | None = None,
        *,
        platform_token: Callable[[], Awaitable[SecretStr]] | None = None,
    ) -> None:
        self.workspace = workspace if workspace is not None else Workspace()
        self.registry: StubRegistry = seeded_registry()
        client = httpx.AsyncClient(
            base_url="https://workspace.example.invalid",
            transport=httpx.MockTransport(self.workspace.handle),
        )
        routes = (
            ResourceSuggestionRoutes(client)
            if platform_token is None
            else ResourceSuggestionRoutes(client, platform_token=platform_token)
        )
        self.api = AdminApi(self.registry, lambda authorizer: [routes])

    def types(
        self, email: str | None = ALICE, headers: Mapping[str, str] | None = None
    ) -> httpx.Response:
        return self.api.call("GET", "/resources/types", email, headers=headers)

    def suggest(
        self,
        type_name: str | None = None,
        q: str | None = None,
        limit: str | None = None,
        email: str | None = ALICE,
        *,
        query: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        if query is None:
            pairs = {"type": type_name, "q": q, "limit": limit}
            query = "?" + str(httpx.QueryParams({k: v for k, v in pairs.items() if v is not None}))
        return self.api.call("GET", f"/resources/suggestions{query}", email, headers=headers)


def listed(body: object, type_name: str = "sql_warehouse") -> Suggester:
    """A suggester whose workspace answers the root call(s) of ``type_name`` with ``body``."""
    method, path, _ = ROOT_CALLS[type_name]
    answers: dict[tuple[str, str], Answer] = {(method, path): (200, body)}
    for second_method, second_path, _ in SECOND_ROOT_CALLS.get(type_name, []):
        answers[(second_method, second_path)] = (200, body)
    return Suggester(Workspace(answers))


def targets_of(response: httpx.Response) -> list[str]:
    return [item["target"] for item in response.json()["items"]]


def labels_of(response: httpx.Response) -> list[str]:
    return [item["label"] for item in response.json()["items"]]
