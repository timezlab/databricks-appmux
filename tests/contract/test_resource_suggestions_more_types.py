"""``GET /resources/suggestions`` for the nine types that had no picker (T211).

The Databricks Apps console suggests objects for every resource type, so the attach form does too.
Five types were already listed (``test_resource_suggestions_api.py``); these are the others, each
against a recording ``MockTransport`` workspace so what is pinned is the call the platform would
receive (method, path, query, bearer token) and what the route makes of the answer:

* Unity Catalog tables, functions and volumes are three levels deep. A query that stops at a level
  lists the next one: no dot (or no ``/Volumes/``) lists catalogs, ``catalog.`` lists schemas,
  ``catalog.schema.`` lists objects. The catalogs and schemas are *prefix* targets (a trailing
  ``.`` or ``/``, not a target the attach form takes) with the level in ``detail``, so the field
  can go on; only objects are real targets. Text after the last separator filters by name alone.
* Connections, Genie spaces, apps and model services are one flat list each.
* A vector search index is found through its endpoint: ``<endpoint>/`` lists the endpoint's
  indexes, whose targets are their Unity Catalog names.
* Lakebase is Autoscaling projects, then branches, then endpoints (the endpoint path is the
  target), plus Provisioned instances (the host is the target).
* A page of any list can hold only part of it: the pages are followed up to ``LIST_MAX_PAGES``.
* Anything the attach form would refuse as a target is left out, and a query that cannot be a
  name calls nothing.

All names and ids are placeholders; the token is a fake value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

import httpx
import pytest
from edge_harness import VISITOR_TOKEN
from suggestion_harness import Answer, Suggester, Workspace, labels_of, targets_of

from appmux.admin.resource_suggestions import LIST_MAX_PAGES
from appmux.registry.resources import RESOURCE_TYPES, is_valid_target

UC: Final = "/api/2.1/unity-catalog"
CATALOGS: Final = f"{UC}/catalogs"
SCHEMAS: Final = f"{UC}/schemas"
TABLES: Final = f"{UC}/tables"
FUNCTIONS: Final = f"{UC}/functions"
VOLUMES: Final = f"{UC}/volumes"
CONNECTIONS: Final = f"{UC}/connections"
MODEL_SERVICES: Final = f"{UC}/model-services"
SPACES: Final = "/api/2.0/genie/spaces"
VECTOR_ENDPOINTS: Final = "/api/2.0/vector-search/endpoints"
VECTOR_INDEXES: Final = "/api/2.0/vector-search/indexes"
APPS: Final = "/api/2.0/apps"
PROJECTS: Final = "/api/2.0/postgres/projects"
INSTANCES: Final = "/api/2.0/database/instances"
BRANCHES: Final = f"{PROJECTS}/my-project/branches"
ENDPOINTS: Final = f"{BRANCHES}/production/endpoints"

CATALOG_LIST: Final = {"catalogs": [{"name": "main"}, {"name": "samples"}, {"name": "bad name"}]}
SCHEMA_LIST: Final = {"schemas": [{"name": "sales"}, {"name": "ops"}, {"name": "bad.name"}]}


def ok(body: object) -> tuple[int, object]:
    return 200, body


def suggester_with(answers: Mapping[tuple[str, str], Answer]) -> Suggester:
    return Suggester(Workspace(answers))


def params_of(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


def items_of(response: httpx.Response) -> list[dict[str, object]]:
    items: list[dict[str, object]] = response.json()["items"]
    return items


def levels(type_name: str) -> list[str]:
    return list(RESOURCE_TYPES[type_name].levels)


def item(type_name: str, target: str, label: str, detail: str | None) -> dict[str, object]:
    return {"target": target, "label": label, "detail": detail, "permissions": levels(type_name)}


# --- Unity Catalog tables and functions: catalog.schema.name ----------------------------------

DOTTED: Final = {
    "uc_table": ("tables", TABLES),
    "uc_function": ("functions", FUNCTIONS),
}


@pytest.mark.parametrize("type_name", DOTTED)
def test_a_query_with_no_dot_lists_the_catalogs_as_prefixes(type_name: str) -> None:
    suggester = suggester_with({("GET", CATALOGS): ok(CATALOG_LIST)})

    response = suggester.suggest(type_name)

    assert response.status_code == 200, response.text
    assert response.json()["source"] == "caller_token"
    assert items_of(response) == [
        item(type_name, "main.", "main", "catalog"),
        item(type_name, "samples.", "samples", "catalog"),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", CATALOGS)
    assert params_of(call) == {"max_results": "200"}
    assert call.headers["Authorization"] == f"Bearer {VISITOR_TOKEN}"


@pytest.mark.parametrize("type_name", DOTTED)
def test_a_catalog_name_typed_so_far_filters_the_catalogs(type_name: str) -> None:
    suggester = suggester_with({("GET", CATALOGS): ok(CATALOG_LIST)})

    response = suggester.suggest(type_name, "SAM")

    assert targets_of(response) == ["samples."]


@pytest.mark.parametrize("type_name", DOTTED)
def test_a_catalog_and_a_dot_lists_the_schemas_of_that_catalog(type_name: str) -> None:
    suggester = suggester_with({("GET", SCHEMAS): ok(SCHEMA_LIST)})

    response = suggester.suggest(type_name, "main.")

    assert items_of(response) == [
        item(type_name, "main.ops.", "ops", "schema"),
        item(type_name, "main.sales.", "sales", "schema"),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", SCHEMAS)
    assert params_of(call) == {"catalog_name": "main", "max_results": "200"}


@pytest.mark.parametrize("type_name", DOTTED)
def test_the_text_after_the_catalog_dot_filters_the_schemas_by_name_alone(type_name: str) -> None:
    suggester = suggester_with({("GET", SCHEMAS): ok(SCHEMA_LIST)})

    # "main" is in every target, so only the label may be matched: "ma" must find no schema.
    assert targets_of(suggester.suggest(type_name, "main.SAL")) == ["main.sales."]
    assert targets_of(suggester.suggest(type_name, "main.ma")) == []


def test_a_schema_and_a_dot_lists_the_tables_with_their_type() -> None:
    body = {
        "tables": [
            {"name": "orders", "table_type": "MANAGED"},
            {"name": "daily_view", "table_type": "VIEW"},
            {"name": "bare"},
            {"name": "has space", "table_type": "MANAGED"},
            {"table_type": "MANAGED"},
        ]
    }
    suggester = suggester_with({("GET", TABLES): ok(body)})

    response = suggester.suggest("uc_table", "main.sales.")

    assert items_of(response) == [
        item("uc_table", "main.sales.bare", "bare", None),
        item("uc_table", "main.sales.daily_view", "daily_view", "VIEW"),
        item("uc_table", "main.sales.orders", "orders", "MANAGED"),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", TABLES)
    # The columns and properties of every table are not needed to name it.
    assert params_of(call) == {
        "catalog_name": "main",
        "schema_name": "sales",
        "max_results": "200",
        "omit_columns": "true",
        "omit_properties": "true",
    }


def test_a_schema_and_a_dot_lists_the_functions() -> None:
    body = {"functions": [{"name": "score"}, {"name": "clean-text"}, {"name": "no way"}]}
    suggester = suggester_with({("GET", FUNCTIONS): ok(body)})

    response = suggester.suggest("uc_function", "main.sales.")

    assert items_of(response) == [
        item("uc_function", "main.sales.clean-text", "clean-text", None),
        item("uc_function", "main.sales.score", "score", None),
    ]
    [call] = suggester.workspace.requests
    assert params_of(call) == {
        "catalog_name": "main",
        "schema_name": "sales",
        "max_results": "200",
    }


@pytest.mark.parametrize("type_name", DOTTED)
def test_the_text_after_the_schema_dot_filters_the_objects_by_name_alone(type_name: str) -> None:
    key, path = DOTTED[type_name]
    suggester = suggester_with({("GET", path): ok({key: [{"name": "orders"}, {"name": "items"}]})})

    assert targets_of(suggester.suggest(type_name, "main.sales.ORD")) == ["main.sales.orders"]
    # Every target holds "sales": matching the target would offer every object for "sal".
    assert targets_of(suggester.suggest(type_name, "main.sales.sal")) == []


@pytest.mark.parametrize("type_name", DOTTED)
@pytest.mark.parametrize(
    "q", ["a.b.c.d", ".x", "main..x", "bad name.x", "main.bad name.", "main.sales.x.", ".."]
)
def test_a_query_that_cannot_be_catalog_schema_name_calls_nothing(type_name: str, q: str) -> None:
    suggester = Suggester()

    response = suggester.suggest(type_name, q)

    body = response.json()
    assert (response.status_code, body["items"], body["source"]) == (200, [], "caller_token")
    assert "catalog" in body["note"]
    assert suggester.workspace.requests == []


@pytest.mark.parametrize("type_name", DOTTED)
def test_a_prefix_is_not_a_target_the_attach_form_takes(type_name: str) -> None:
    suggester = suggester_with({("GET", CATALOGS): ok(CATALOG_LIST)})

    for suggestion in items_of(suggester.suggest(type_name)):
        assert not is_valid_target(type_name, str(suggestion["target"]))


# --- Unity Catalog volumes: /Volumes/catalog/schema/volume --------------------------------------


def test_a_volume_query_with_no_path_lists_the_catalogs_as_volume_prefixes() -> None:
    suggester = suggester_with({("GET", CATALOGS): ok(CATALOG_LIST)})

    response = suggester.suggest("uc_volume")

    assert items_of(response) == [
        item("uc_volume", "/Volumes/main/", "main", "catalog"),
        item("uc_volume", "/Volumes/samples/", "samples", "catalog"),
    ]


@pytest.mark.parametrize("q", ["main", "/Volumes/main", "/Volumes/", "/Vol", "/"])
def test_a_catalog_name_or_the_start_of_the_volumes_path_is_still_the_catalog_step(q: str) -> None:
    suggester = suggester_with({("GET", CATALOGS): ok(CATALOG_LIST)})

    response = suggester.suggest("uc_volume", q)

    expected = (
        ["/Volumes/main/"]
        if q in {"main", "/Volumes/main"}
        else ["/Volumes/main/", "/Volumes/samples/"]
    )
    assert targets_of(response) == expected
    assert [call.url.path for call in suggester.workspace.requests] == [CATALOGS]


def test_a_catalog_in_the_volumes_path_lists_its_schemas() -> None:
    suggester = suggester_with({("GET", SCHEMAS): ok(SCHEMA_LIST)})

    response = suggester.suggest("uc_volume", "/Volumes/main/")

    assert items_of(response) == [
        item("uc_volume", "/Volumes/main/ops/", "ops", "schema"),
        item("uc_volume", "/Volumes/main/sales/", "sales", "schema"),
    ]
    [call] = suggester.workspace.requests
    assert params_of(call) == {"catalog_name": "main", "max_results": "200"}


def test_a_schema_in_the_volumes_path_lists_the_volumes_with_their_type() -> None:
    body = {
        "volumes": [
            {"name": "files", "volume_type": "MANAGED"},
            {"name": "raw", "volume_type": "EXTERNAL"},
            {"name": "no way", "volume_type": "MANAGED"},
        ]
    }
    suggester = suggester_with({("GET", VOLUMES): ok(body)})

    response = suggester.suggest("uc_volume", "/Volumes/main/sales/")

    assert items_of(response) == [
        item("uc_volume", "/Volumes/main/sales/files", "files", "MANAGED"),
        item("uc_volume", "/Volumes/main/sales/raw", "raw", "EXTERNAL"),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", VOLUMES)
    assert params_of(call) == {"catalog_name": "main", "schema_name": "sales", "max_results": "200"}


def test_the_text_after_the_last_slash_filters_the_volumes_by_name_alone() -> None:
    body = {"volumes": [{"name": "files"}, {"name": "raw"}]}
    suggester = suggester_with({("GET", VOLUMES): ok(body)})

    assert targets_of(suggester.suggest("uc_volume", "/Volumes/main/sales/RA")) == [
        "/Volumes/main/sales/raw"
    ]
    assert targets_of(suggester.suggest("uc_volume", "/Volumes/main/sales/sal")) == []


@pytest.mark.parametrize(
    "q",
    ["/Volumes/a/b/c/d", "/Volumes//x", "/Volumes/bad name/", "/Volumes/main/bad name/", "a/b/"],
)
def test_a_volume_query_that_cannot_be_a_volume_path_calls_nothing(q: str) -> None:
    suggester = Suggester()

    response = suggester.suggest("uc_volume", q)

    body = response.json()
    assert (response.status_code, body["items"], body["source"]) == (200, [], "caller_token")
    assert "/Volumes/" in body["note"]
    assert suggester.workspace.requests == []


# --- the flat lists: connections, Genie spaces, apps, model services ----------------------------


def test_connections_are_listed_by_name_with_their_kind() -> None:
    body = {
        "connections": [
            {"name": "warehouse_pg", "connection_type": "POSTGRESQL"},
            {"name": "crm", "connection_type": "SALESFORCE"},
            {"name": "bad name", "connection_type": "MYSQL"},
        ]
    }
    suggester = suggester_with({("GET", CONNECTIONS): ok(body)})

    response = suggester.suggest("uc_connection")

    assert items_of(response) == [
        item("uc_connection", "crm", "crm", "SALESFORCE"),
        item("uc_connection", "warehouse_pg", "warehouse_pg", "POSTGRESQL"),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", CONNECTIONS)
    assert params_of(call) == {"max_results": "200"}
    assert call.headers["Authorization"] == f"Bearer {VISITOR_TOKEN}"


def test_genie_spaces_are_listed_by_title_with_the_space_id_as_the_target() -> None:
    body = {
        "spaces": [
            {"space_id": "01ef1234567890abcdef1234567890ab", "title": "Sales questions"},
            {"space_id": "01ef0000000000000000000000000001", "title": "Support"},
            {"space_id": "has space", "title": "Not an id"},
            {"title": "No id"},
        ]
    }
    suggester = suggester_with({("GET", SPACES): ok(body)})

    response = suggester.suggest("genie_space", "s")

    assert items_of(response) == [
        item("genie_space", "01ef1234567890abcdef1234567890ab", "Sales questions", None),
        item("genie_space", "01ef0000000000000000000000000001", "Support", None),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", SPACES)
    assert params_of(call) == {"page_size": "100"}


def test_apps_are_listed_by_name_with_their_compute_state() -> None:
    body = {
        "apps": [
            {"name": "reports", "compute_status": {"state": "ACTIVE"}},
            {"name": "etl-ui", "compute_status": {"state": "STOPPED"}},
            {"name": "bare"},
            {"description": "no name"},
        ]
    }
    suggester = suggester_with({("GET", APPS): ok(body)})

    response = suggester.suggest("databricks_app")

    assert items_of(response) == [
        item("databricks_app", "bare", "bare", None),
        item("databricks_app", "etl-ui", "etl-ui", "STOPPED"),
        item("databricks_app", "reports", "reports", "ACTIVE"),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", APPS)
    assert params_of(call) == {"page_size": "100"}


def test_model_services_are_listed_by_their_three_part_name() -> None:
    body = {
        "model_services": [
            {"name": "system.ai.embedder", "supported_api_types": ["mlflow/v1/embeddings"]},
            {
                "name": "main.sales.chat",
                "supported_api_types": ["mlflow/v1/chat", "openai/v1/chat"],
            },
            {"name": "main.sales.plain"},
            {"supported_api_types": []},
        ]
    }
    suggester = suggester_with({("GET", MODEL_SERVICES): ok(body)})

    response = suggester.suggest("gateway_model_service")

    # The label is the last part of the name, so the order is by that part: chat, embedder, plain.
    assert items_of(response) == [
        item("gateway_model_service", "main.sales.chat", "chat", "mlflow/v1/chat, openai/v1/chat"),
        item("gateway_model_service", "system.ai.embedder", "embedder", "mlflow/v1/embeddings"),
        item("gateway_model_service", "main.sales.plain", "plain", None),
    ]
    assert labels_of(response) == ["chat", "embedder", "plain"]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", MODEL_SERVICES)
    assert params_of(call) == {}


# --- vector search: the endpoint, then its indexes --------------------------------------------


def test_a_vector_search_query_with_no_slash_lists_the_endpoints_as_prefixes() -> None:
    body = {
        "endpoints": [
            {"name": "docs-endpoint", "endpoint_status": {"state": "ONLINE"}},
            {"name": "scratch"},
            {"name": "bad name"},
        ]
    }
    suggester = suggester_with({("GET", VECTOR_ENDPOINTS): ok(body)})

    response = suggester.suggest("vector_search_index")

    assert items_of(response) == [
        item("vector_search_index", "docs-endpoint/", "docs-endpoint", "endpoint, ONLINE"),
        item("vector_search_index", "scratch/", "scratch", "endpoint"),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", VECTOR_ENDPOINTS)


def test_an_endpoint_and_a_slash_lists_the_indexes_of_that_endpoint() -> None:
    body = {
        "vector_indexes": [
            {"name": "main.sales.docs_index", "index_type": "DELTA_SYNC", "primary_key": "id"},
            {"name": "main.sales.faq_index", "index_type": "DIRECT_ACCESS"},
            {"name": "main.sales.bare"},
            {"index_type": "DELTA_SYNC"},
        ]
    }
    suggester = suggester_with({("GET", VECTOR_INDEXES): ok(body)})

    response = suggester.suggest("vector_search_index", "docs-endpoint/")

    assert items_of(response) == [
        item("vector_search_index", "main.sales.bare", "bare", None),
        item("vector_search_index", "main.sales.docs_index", "docs_index", "DELTA_SYNC"),
        item("vector_search_index", "main.sales.faq_index", "faq_index", "DIRECT_ACCESS"),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", VECTOR_INDEXES)
    assert params_of(call) == {"endpoint_name": "docs-endpoint"}
    assert call.headers["Authorization"] == f"Bearer {VISITOR_TOKEN}"


def test_the_text_after_the_slash_filters_the_indexes_by_label_or_catalog_name() -> None:
    body = {"vector_indexes": [{"name": "main.sales.docs_index"}, {"name": "ml.prod.faq_index"}]}
    suggester = suggester_with({("GET", VECTOR_INDEXES): ok(body)})

    assert targets_of(suggester.suggest("vector_search_index", "ep/DOCS")) == [
        "main.sales.docs_index"
    ]
    assert targets_of(suggester.suggest("vector_search_index", "ep/prod")) == ["ml.prod.faq_index"]


@pytest.mark.parametrize("q", ["/x", "bad name/", "a/b/c"])
def test_a_vector_search_query_whose_endpoint_cannot_be_a_name_calls_nothing(q: str) -> None:
    suggester = Suggester()

    response = suggester.suggest("vector_search_index", q)

    body = response.json()
    assert (response.status_code, body["items"], body["source"]) == (200, [], "caller_token")
    assert "endpoint" in body["note"]
    assert suggester.workspace.requests == []


# --- Lakebase: projects, branches, endpoints, and Provisioned instances -----------------------

LAKEBASE_ROOT: Final = {
    ("GET", PROJECTS): ok(
        {
            "projects": [
                {"name": "projects/my-project", "status": {"display_name": "My Project"}},
                {"name": "projects/bare-project"},
                {"name": "other/not-a-project"},
            ]
        }
    ),
    ("GET", INSTANCES): ok(
        {
            "database_instances": [
                {
                    "name": "prov-1",
                    "read_write_dns": "instance-abc.database.example.invalid",
                    "state": "AVAILABLE",
                },
                {"name": "no-host"},
            ]
        }
    ),
}


def test_lakebase_lists_autoscaling_projects_as_prefixes_and_provisioned_instances_by_host() -> (
    None
):
    suggester = suggester_with(LAKEBASE_ROOT)

    response = suggester.suggest("lakebase_database")

    assert response.json()["source"] == "caller_token"
    assert items_of(response) == [
        item("lakebase_database", "projects/bare-project/", "bare-project", "Autoscaling project"),
        item("lakebase_database", "projects/my-project/", "My Project", "Autoscaling project"),
        item(
            "lakebase_database",
            "instance-abc.database.example.invalid",
            "prov-1",
            "Provisioned, AVAILABLE",
        ),
    ]
    assert suggester.workspace.calls == [("GET", PROJECTS), ("GET", INSTANCES)]
    assert [params_of(call) for call in suggester.workspace.requests] == [
        {"page_size": "100"},
        {"page_size": "100"},
    ]


def test_a_project_and_a_slash_lists_its_branches_as_prefixes() -> None:
    body = {
        "branches": [
            {"name": "projects/my-project/branches/production", "branch_id": "production"},
            {"name": "projects/my-project/branches/dev"},
            {"name": "projects/other/branches/stray"},
        ]
    }
    suggester = suggester_with({("GET", BRANCHES): ok(body)})

    response = suggester.suggest("lakebase_database", "projects/my-project/")

    assert items_of(response) == [
        item("lakebase_database", "projects/my-project/branches/dev/", "dev", "branch"),
        item(
            "lakebase_database", "projects/my-project/branches/production/", "production", "branch"
        ),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", BRANCHES)
    assert params_of(call) == {"page_size": "100"}


@pytest.mark.parametrize("q", ["projects/my-project/pro", "projects/my-project/branches/pro"])
def test_the_text_after_the_project_filters_the_branches_by_name(q: str) -> None:
    body = {
        "branches": [
            {"name": "projects/my-project/branches/production"},
            {"name": "projects/my-project/branches/dev"},
        ]
    }
    suggester = suggester_with({("GET", BRANCHES): ok(body)})

    assert targets_of(suggester.suggest("lakebase_database", q)) == [
        "projects/my-project/branches/production/"
    ]


def test_a_branch_and_a_slash_lists_its_endpoints_and_the_target_is_the_endpoint_path() -> None:
    body = {
        "endpoints": [
            {
                "name": "projects/my-project/branches/production/endpoints/primary",
                "endpoint_id": "primary",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_WRITE", "current_state": "ACTIVE"},
            },
            {
                "name": "projects/my-project/branches/production/endpoints/replica",
                "status": {"endpoint_type": "ENDPOINT_TYPE_READ_ONLY"},
            },
        ]
    }
    suggester = suggester_with({("GET", ENDPOINTS): ok(body)})

    response = suggester.suggest("lakebase_database", "projects/my-project/branches/production/")

    assert items_of(response) == [
        item(
            "lakebase_database",
            "projects/my-project/branches/production/endpoints/primary",
            "primary",
            "READ_WRITE, ACTIVE",
        ),
        item(
            "lakebase_database",
            "projects/my-project/branches/production/endpoints/replica",
            "replica",
            "READ_ONLY",
        ),
    ]
    [call] = suggester.workspace.requests
    assert (call.method, call.url.path) == ("GET", ENDPOINTS)
    assert params_of(call) == {"page_size": "100"}


@pytest.mark.parametrize("q", ["my", "projects/my", "MY-PROJ"])
def test_text_that_is_not_yet_a_project_path_filters_the_first_list(q: str) -> None:
    suggester = suggester_with(LAKEBASE_ROOT)

    response = suggester.suggest("lakebase_database", q)

    assert targets_of(response) == ["projects/my-project/"]


@pytest.mark.parametrize(
    "q",
    [
        "projects/bad name/",
        "projects//",
        "projects/a/branches/bad name/",
        "projects/a/b/c/d/e/f",
        "other/x/y",
        "projects/a/branches/b/endpoints/e/x",
    ],
)
def test_a_lakebase_query_that_cannot_be_a_project_path_calls_nothing(q: str) -> None:
    suggester = Suggester()

    response = suggester.suggest("lakebase_database", q)

    body = response.json()
    assert (response.status_code, body["items"], body["source"]) == (200, [], "caller_token")
    assert "projects/" in body["note"]
    assert suggester.workspace.requests == []


def test_a_failed_provisioned_list_keeps_the_autoscaling_projects_and_says_so() -> None:
    answers = {
        ("GET", PROJECTS): LAKEBASE_ROOT[("GET", PROJECTS)],
        ("GET", INSTANCES): (500, {"message": "boom"}),
    }
    suggester = suggester_with(answers)

    response = suggester.suggest("lakebase_database")

    body = response.json()
    assert body["source"] == "caller_token"
    assert targets_of(response) == ["projects/bare-project/", "projects/my-project/"]
    assert "Provisioned" in body["note"]


def test_a_failed_autoscaling_list_keeps_the_provisioned_instances_and_says_so() -> None:
    answers = {
        ("GET", PROJECTS): (500, {"message": "boom"}),
        ("GET", INSTANCES): LAKEBASE_ROOT[("GET", INSTANCES)],
    }
    suggester = suggester_with(answers)

    response = suggester.suggest("lakebase_database")

    body = response.json()
    assert targets_of(response) == ["instance-abc.database.example.invalid"]
    assert "Autoscaling" in body["note"]


def test_when_neither_lakebase_list_answers_the_answer_is_unavailable() -> None:
    suggester = Suggester()
    suggester.workspace.answer_everything_with(lambda request: httpx.Response(500, json={}))

    body = suggester.suggest("lakebase_database").json()

    assert (body["items"], body["source"]) == ([], "unavailable")
    assert body["note"]


# --- pages of a list ------------------------------------------------------------------------


def paged(pages: list[object]) -> Callable[[httpx.Request], httpx.Response]:
    """Answers page n for the request that carries the token ``p<n>`` (none: the first)."""

    def answer(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("page_token")
        number = 0 if token is None else int(token.removeprefix("p"))
        page = pages[number]
        assert isinstance(page, dict)
        more = {"next_page_token": f"p{number + 1}"} if number + 1 < len(pages) else {}
        return httpx.Response(200, json={**page, **more})

    return answer


def test_the_pages_of_a_list_are_followed_until_none_is_left() -> None:
    pages = [
        {"catalogs": [{"name": "alpha"}]},
        {"catalogs": []},  # a page may hold nothing and not be the last
        {"catalogs": [{"name": "gamma"}]},
    ]
    suggester = suggester_with({("GET", CATALOGS): paged(pages)})

    response = suggester.suggest("uc_table")

    body = response.json()
    assert targets_of(response) == ["alpha.", "gamma."]
    assert (body["truncated"], body["note"]) == (False, None)
    assert [params_of(call).get("page_token") for call in suggester.workspace.requests] == [
        None,
        "p1",
        "p2",
    ]
    assert all(params_of(call)["max_results"] == "200" for call in suggester.workspace.requests)


def test_a_list_with_more_pages_than_the_limit_is_cut_and_says_so() -> None:
    pages = [{"catalogs": [{"name": f"cat{n}"}]} for n in range(LIST_MAX_PAGES + 3)]
    suggester = suggester_with({("GET", CATALOGS): paged(pages)})

    response = suggester.suggest("uc_table")

    body = response.json()
    assert len(suggester.workspace.requests) == LIST_MAX_PAGES
    assert targets_of(response) == [f"cat{n}." for n in range(LIST_MAX_PAGES)]
    assert body["truncated"] is True
    assert body["note"]


def test_a_refusal_on_a_later_page_is_the_answer() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page_token") is None:
            return httpx.Response(
                200, json={"catalogs": [{"name": "alpha"}], "next_page_token": "p1"}
            )
        return httpx.Response(500, json={"message": "boom"})

    suggester = suggester_with({("GET", CATALOGS): answer})

    body = suggester.suggest("uc_table").json()

    assert (body["items"], body["source"]) == ([], "unavailable")
    assert "answered 500" in body["note"]
