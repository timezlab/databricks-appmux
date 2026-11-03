"""The baseline migration against data model section 4, and the row models against the migration.

No database is needed: the SQL text is parsed. Three things are checked.

* every table and column that section 4 of the data model names exists in
  ``migrations/0001_baseline.sql``, and the file holds no table or column beyond them;
* the field names of each row model in ``appmux.registry.models`` equal the column names of
  its table;
* a row model accepts the example rows of the data model and refuses what the SQL ``CHECK``
  constraints refuse.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from appmux.registry import models
from appmux.sandbox import rules
from appmux.settings import PlatformAdmin

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPOSITORY_ROOT / "migrations" / "0001_baseline.sql"
DATA_MODEL_PATH = REPOSITORY_ROOT / "docs" / "product" / "v1-data-model.md"

NINE_TABLES = (
    "platform_settings",
    "apps",
    "permission_requests",
    "app_config",
    "app_grants",
    "releases",
    "app_resources",
    "metrics",
    "audit_log",
)
CONSTRAINT_START = re.compile(r"(CHECK|PRIMARY KEY|UNIQUE|FOREIGN KEY|CONSTRAINT)\b", re.IGNORECASE)


def _strip_sql_comments(sql_text: str) -> str:
    # No string literal of the baseline holds "--", so a line-wise cut is exact.
    return "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on ``separator`` outside parentheses and single-quoted strings."""
    parts: list[str] = []
    depth = 0
    in_quote = False
    current: list[str] = []
    for character in text:
        if character == "'":
            in_quote = not in_quote
        elif not in_quote and character == "(":
            depth += 1
        elif not in_quote and character == ")":
            depth -= 1
        if character == separator and depth == 0 and not in_quote:
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _statements(sql_text: str) -> list[str]:
    return _split_top_level(_strip_sql_comments(sql_text), ";")


def _columns_by_table(sql_text: str) -> dict[str, list[str]]:
    columns_by_table: dict[str, list[str]] = {}
    for statement in _statements(sql_text):
        match = re.match(r"CREATE TABLE (\w+) \((.*)\)$", statement, re.DOTALL)
        if match is None:
            continue
        table, body = match.groups()
        definitions = _split_top_level(body, ",")
        columns_by_table[table] = [
            definition.split()[0]
            for definition in definitions
            if not CONSTRAINT_START.match(definition)
        ]
    return columns_by_table


def _documented_columns_by_table(markdown: str) -> dict[str, list[str]]:
    """Read the ``**Columns.**`` table of every ``### 4.n `table``` section of the data model."""
    section_four = markdown.split("\n## 4. Tables", 1)[1].split("\n## 5. Walkthroughs", 1)[0]
    documented: dict[str, list[str]] = {}
    for section in re.split(r"\n### 4\.\d+ ", section_four)[1:]:
        table = re.match(r"`(\w+)`", section)
        assert table is not None, section[:60]
        after_heading = section.split("**Columns.**", 1)[1]
        names: list[str] = []
        for line in after_heading.splitlines():
            if not line.startswith("|"):
                if names:
                    break
                continue
            first_cell = line.split("|")[1]
            names.extend(re.findall(r"`(\w+)`", first_cell))
        documented[table.group(1)] = names[1:] if names[:1] == ["Column"] else names
    return documented


@pytest.fixture(scope="module")
def baseline_text() -> str:
    return BASELINE_PATH.read_text()


@pytest.fixture(scope="module")
def sql_columns(baseline_text: str) -> dict[str, list[str]]:
    return _columns_by_table(baseline_text)


@pytest.fixture(scope="module")
def documented_columns() -> dict[str, list[str]]:
    return _documented_columns_by_table(DATA_MODEL_PATH.read_text())


def test_baseline_holds_exactly_the_nine_tables(sql_columns: dict[str, list[str]]) -> None:
    assert sorted(sql_columns) == sorted(NINE_TABLES)


def test_documented_tables_are_the_nine(documented_columns: dict[str, list[str]]) -> None:
    assert sorted(documented_columns) == sorted(NINE_TABLES)


@pytest.mark.parametrize("table", NINE_TABLES)
def test_every_documented_column_exists_and_none_is_added(
    table: str,
    sql_columns: dict[str, list[str]],
    documented_columns: dict[str, list[str]],
) -> None:
    assert sorted(sql_columns[table]) == sorted(documented_columns[table])


def test_column_names_are_not_repeated_in_a_table(sql_columns: dict[str, list[str]]) -> None:
    for table, columns in sql_columns.items():
        assert len(columns) == len(set(columns)), table


def test_baseline_creates_the_two_partial_unique_indexes_and_no_other_object(
    baseline_text: str,
) -> None:
    statements = _statements(baseline_text)
    unique_indexes = {
        match.group(1): statement
        for statement in statements
        if (match := re.match(r"CREATE UNIQUE INDEX (\w+) ON ", statement))
    }
    assert sorted(unique_indexes) == ["one_active_release", "one_pending_request"]
    pending_request = " ".join(unique_indexes["one_pending_request"].split())
    assert pending_request == (
        "CREATE UNIQUE INDEX one_pending_request ON permission_requests "
        "(coalesce(app_slug, payload ->> 'slug'), kind) WHERE status = 'pending'"
    )
    other_statements = [
        statement
        for statement in statements
        if not statement.startswith(("CREATE TABLE", "CREATE UNIQUE INDEX", "CREATE INDEX"))
    ]
    assert [statement.split("\n", 1)[0] for statement in other_statements] == [
        "ALTER TABLE app_config"
    ]


def test_app_config_composite_keys_come_after_the_tables_they_refer_to(baseline_text: str) -> None:
    statements = _statements(baseline_text)
    positions = {
        statement.split("\n", 1)[0].split("(", 1)[0].strip(): index
        for index, statement in enumerate(statements)
    }
    alter_index = positions["ALTER TABLE app_config"]
    assert alter_index > positions["CREATE TABLE releases"]
    assert alter_index > positions["CREATE TABLE app_resources"]
    alter = statements[alter_index]
    assert alter.count("DEFERRABLE INITIALLY DEFERRED") == 3


def test_baseline_has_no_transaction_control_because_the_loader_wraps_it(
    baseline_text: str,
) -> None:
    words = {statement.split()[0].upper() for statement in _statements(baseline_text)}
    assert words.isdisjoint({"BEGIN", "COMMIT", "ROLLBACK", "START"})


@pytest.mark.parametrize("table", NINE_TABLES)
def test_row_model_field_names_equal_the_table_columns(
    table: str, sql_columns: dict[str, list[str]]
) -> None:
    model = models.ROW_MODELS[table]
    assert sorted(model.model_fields) == sorted(sql_columns[table])


def test_row_models_are_one_per_table_and_named_after_it() -> None:
    assert set(models.ROW_MODELS) == set(NINE_TABLES)
    assert {table: model.__name__ for table, model in models.ROW_MODELS.items()} == {
        "platform_settings": "PlatformSetting",
        "apps": "App",
        "permission_requests": "PermissionRequest",
        "app_config": "AppConfig",
        "app_grants": "AppGrant",
        "releases": "Release",
        "app_resources": "AppResource",
        "metrics": "MetricRow",
        "audit_log": "AuditLogRow",
    }


def test_row_models_are_frozen() -> None:
    for model in models.ROW_MODELS.values():
        assert model.model_config.get("frozen") is True, model.__name__
        assert model.model_config.get("extra") == "forbid", model.__name__


# Example rows of the data model, section 4, as a database driver returns them.
WHEN = datetime(2026, 10, 30, 8, 41, tzinfo=UTC)
APP_ROW: dict[str, Any] = {
    "slug": "sales",
    "display_name": "Sales dashboard",
    "owner": "alice@example.com",
    "source_path": "/Workspace/Shared/apps/sales",
    "memory_budget_mb": 512,
    "obo_delegated": True,
    "shared_sp_delegated": False,
    "egress": [
        {"action": "allow", "cidr": "10.20.0.0/16", "port": "443", "comment": "warehouse subnet"},
        {"action": "deny", "cidr": "0.0.0.0/0", "port": "any", "comment": ""},
    ],
    "version": 3,
    "created_by": "dana@example.com",
    "created_at": WHEN,
    "updated_by": "dana@example.com",
    "updated_at": WHEN,
}
REQUEST_ROW: dict[str, Any] = {
    "request_id": 6,
    "app_slug": None,
    "kind": "register",
    "payload": {
        "slug": "wiki",
        "display_name": "Team wiki",
        "source_path": "/Workspace/Shared/apps/wiki",
        "memory_budget_mb": 128,
    },
    "reason": "the team wiki, a small Flask app",
    "requested_by": "frank@example.com",
    "requested_at": WHEN,
    "status": "pending",
    "closed_by": None,
    "closed_at": None,
    "closed_note": None,
}
RELEASE_ROW: dict[str, Any] = {
    "app_slug": "sales",
    "release_no": 7,
    "snapshot_digest": "sha256:" + "9f2c" * 16,
    "detected": {
        "runtime": "python",
        "framework": "fastapi",
        "command": ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"],  # noqa: S104 - sample command text
        "env": [
            {"name": "LOG_LEVEL", "value": "info"},
            {"name": "WAREHOUSE_ID", "value_from": "sql-warehouse"},
        ],
        "bundle_resources": [
            {
                "key": "sql-warehouse",
                "type": "sql_warehouse",
                "target": "a1b2c3d4e5f67890",
                "permission": "CAN_USE",
            },
            {
                "key": "forecast-model",
                "type": "serving_endpoint",
                "target": None,
                "permission": "CAN_QUERY",
            },
        ],
        "warnings": ["app.yaml: unknown key 'resources' ignored"],
    },
    "config": {
        "env": [{"name": "WAREHOUSE", "value_from": "sql-warehouse"}],
        "wheel_sources": ["/Workspace/Users/alice@example.com/wheels/sales"],
    },
    "wheels": [
        {
            "sha256": "3b1e" * 16,
            "filename": "fastapi-0.115.0-py3-none-any.whl",
            "source": "/Workspace/Users/alice@example.com/wheels/sales",
        }
    ],
    "status": "active",
    "status_reason": None,
    "build_log_tail": None,
    "status_at": WHEN,
    "label": "fix pagination",
    "tags": ["stable"],
    "locked_by": None,
    "locked_at": None,
    "created_by": "alice@example.com",
    "created_at": WHEN,
    "deleted_by": None,
    "deleted_at": None,
}
METRIC_ROW: dict[str, Any] = {
    "app_slug": "sales",
    "minute": WHEN,
    "requests": 412,
    "r2xx": 389,
    "r3xx": 11,
    "r4xx": 9,
    "r5xx": 3,
    "upstream_errors": 2,
    "limited": 0,
    "lat_hist": [18, 61, 140, 88, 52, 29, 14, 6, 3, 1, 0],
    "bytes_in": 184320,
    "bytes_out": 5242880,
    "in_flight_max": 7,
    "ws_opened": 0,
    "ws_open": 0,
    "rss_mb_max": 341,
    "restarts": 0,
    "egress_bytes_out": 91234,
    "egress_bytes_in": 2310456,
    "egress_dropped": 0,
    "egress_dropped_by_rule": {},
    "mem_used_mb": None,
    "mem_limit_mb": None,
    "children_running": None,
    "children_registered": None,
    "admission_rejections": None,
    "registry_errors": None,
    "registry_ms_max": None,
    "edge_rss_mb": None,
}


@pytest.mark.parametrize(
    ("model", "row"),
    [
        (
            models.PlatformSetting,
            {
                "key": "platform_admins",
                "value": [
                    {"type": "user", "name": "dana@example.com"},
                    {"type": "group", "name": "data-platform"},
                ],
                "updated_by": "dana@example.com",
                "updated_at": WHEN,
            },
        ),
        (
            models.PlatformSetting,
            {
                "key": "max_releases_per_app",
                "value": 10,
                "updated_by": "db:ops",
                "updated_at": WHEN,
            },
        ),
        (models.App, APP_ROW),
        (models.PermissionRequest, REQUEST_ROW),
        (
            models.AppConfig,
            {
                "app_slug": "docs",
                "desired_state": "stopped",
                "target_release_no": None,
                "health_path": "/healthz",
                "health_deadline_s": 120,
                "own_sp_client_id_key": None,
                "own_sp_client_secret_key": None,
                "version": 1,
                "updated_by": "frank@example.com",
                "updated_at": WHEN,
            },
        ),
        (
            models.AppGrant,
            {
                "app_slug": "docs",
                "principal_type": "everyone",
                "principal": "*",
                "permission": "CAN_USE",
                "granted_by": "frank@example.com",
                "granted_at": WHEN,
            },
        ),
        (models.Release, RELEASE_ROW),
        (
            models.AppResource,
            {
                "app_slug": "sales",
                "key": "openai-api-key",
                "type": "secret",
                "target": "sales-secrets/openai-api-key",
                "permission": "READ",
                "access_as": None,
                "check_result": "ok",
                "attached_by": "alice@example.com",
                "attached_at": WHEN,
            },
        ),
        (models.MetricRow, METRIC_ROW),
        (
            models.AuditLogRow,
            {
                "id": 414,
                "ts": WHEN,
                "actor": "dana@example.com",
                "action": "request.approve",
                "app_slug": "sales",
                "detail": {"request": 1, "kind": "delegation", "after": {"obo": True}},
                "reason": "the app reads per-viewer data only",
            },
        ),
    ],
)
def test_row_models_accept_the_example_rows_of_the_data_model(
    model: type[models.RowModel], row: dict[str, Any]
) -> None:
    parsed = model.model_validate(row)
    assert set(parsed.model_dump()) == set(row)


def test_egress_rules_and_request_payload_are_typed_models_not_dicts() -> None:
    app = models.App.model_validate(APP_ROW)
    assert all(isinstance(rule, models.EgressRule) for rule in app.egress)
    request = models.PermissionRequest.model_validate(REQUEST_ROW)
    assert isinstance(request.payload, models.RequestPayload)
    assert request.payload.slug == "wiki"
    release = models.Release.model_validate(RELEASE_ROW)
    assert isinstance(release.detected, models.DetectedManifest)
    assert isinstance(release.config, models.ReleaseConfig)
    assert all(isinstance(wheel, models.WheelEntry) for wheel in release.wheels)


def test_the_stored_shapes_are_defined_once() -> None:
    # The egress rule is the sandbox's (egress semantics), the platform admin the settings'.
    assert models.EgressRule is rules.EgressRule
    assert models.PlatformAdmin is PlatformAdmin
    assert not hasattr(models, "PlatformAdminEntry")


def test_platform_setting_value_picks_the_matching_json_shape() -> None:
    admins = models.PlatformSetting.model_validate(
        {
            "key": "platform_admins",
            "value": [{"type": "user", "name": "dana@example.com"}],
            "updated_by": "dana@example.com",
            "updated_at": WHEN,
        }
    )
    assert isinstance(admins.value, list)
    assert isinstance(admins.value[0], models.PlatformAdmin)
    rules = models.PlatformSetting.model_validate(
        {
            "key": "egress_global",
            "value": [{"action": "deny", "cidr": "169.254.0.0/16", "port": "any", "comment": ""}],
            "updated_by": "dana@example.com",
            "updated_at": WHEN,
        }
    )
    assert isinstance(rules.value, list)
    assert isinstance(rules.value[0], models.EgressRule)
    flag = models.PlatformSetting.model_validate(
        {"key": "child_cpu_pin", "value": True, "updated_by": "x", "updated_at": WHEN}
    )
    count = models.PlatformSetting.model_validate(
        {"key": "request_timeout_s", "value": 1, "updated_by": "x", "updated_at": WHEN}
    )
    assert flag.value is True
    assert count.value == 1
    assert count.value is not True


def _with(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**row, **changes}


CONFIG_ROW: dict[str, Any] = {
    "app_slug": "sales",
    "desired_state": "running",
    "target_release_no": 7,
    "health_path": "/",
    "health_deadline_s": 60,
    "own_sp_client_id_key": "sp-client-id",
    "own_sp_client_secret_key": "sp-client-secret",
    "version": 1,
    "updated_by": "alice@example.com",
    "updated_at": WHEN,
}
GRANT_ROW: dict[str, Any] = {
    "app_slug": "docs",
    "principal_type": "group",
    "principal": "sales-team",
    "permission": "CAN_USE",
    "granted_by": "frank@example.com",
    "granted_at": WHEN,
}
SECRET_RESOURCE_ROW: dict[str, Any] = {
    "app_slug": "sales",
    "key": "openai-api-key",
    "type": "secret",
    "target": "sales-secrets/openai-api-key",
    "permission": "READ",
    "access_as": None,
    "check_result": "ok",
    "attached_by": "alice@example.com",
    "attached_at": WHEN,
}
ENDPOINT_RESOURCE_ROW: dict[str, Any] = {
    "app_slug": "sales",
    "key": "forecast-model",
    "type": "serving_endpoint",
    "target": "sales-forecast",
    "permission": "CAN_QUERY",
    "access_as": "obo",
    "check_result": "ok",
    "attached_by": "alice@example.com",
    "attached_at": WHEN,
}
DELETED_RELEASE_ROW: dict[str, Any] = _with(
    RELEASE_ROW, snapshot_digest=None, deleted_at=WHEN, deleted_by="appmux", wheels=[], tags=[]
)

Case = tuple[type[models.RowModel], dict[str, Any]]

# Every CHECK of migrations/0001_baseline.sql (whitespace collapsed) with rows it refuses. The
# test below compares the keys with the CHECKs found in the SQL, so a CHECK added to the SQL, or
# one dropped from it, fails until this table and the models follow.
CHECK_REFUSALS: dict[str, tuple[Case, ...]] = {
    # apps
    "slug ~ '^[a-z0-9][a-z0-9-]{0,30}$'": (
        (models.App, _with(APP_ROW, slug="_appmux")),
        (models.App, _with(APP_ROW, slug="a" * 32)),
        (models.App, _with(APP_ROW, slug="-sales")),
        (models.App, _with(APP_ROW, slug="Sales")),
    ),
    "btrim(display_name) <> ''": (
        (models.App, _with(APP_ROW, display_name="")),
        (models.App, _with(APP_ROW, display_name="   ")),
    ),
    "owner = lower(owner)": ((models.App, _with(APP_ROW, owner="Alice@example.com")),),
    "source_path LIKE '/Workspace/%'": (
        (models.App, _with(APP_ROW, source_path="/Users/alice")),
        (models.App, _with(APP_ROW, source_path="/Workspace")),
    ),
    "memory_budget_mb BETWEEN 64 AND 16384": (
        (models.App, _with(APP_ROW, memory_budget_mb=63)),
        (models.App, _with(APP_ROW, memory_budget_mb=16385)),
    ),
    # permission_requests
    "kind ~ '^[a-z][a-z_]{0,31}$'": (
        (models.PermissionRequest, _with(REQUEST_ROW, kind="Register")),
        (models.PermissionRequest, _with(REQUEST_ROW, kind="1register")),
        (models.PermissionRequest, _with(REQUEST_ROW, kind="r" * 33)),
    ),
    "jsonb_typeof(payload) = 'object'": (
        (models.PermissionRequest, _with(REQUEST_ROW, payload=["slug"])),
        (models.PermissionRequest, _with(REQUEST_ROW, payload="slug")),
    ),
    "btrim(reason) <> '' AND char_length(reason) <= 500": (
        (models.PermissionRequest, _with(REQUEST_ROW, reason="")),
        (models.PermissionRequest, _with(REQUEST_ROW, reason="   ")),
        (models.PermissionRequest, _with(REQUEST_ROW, reason="x" * 501)),
    ),
    "status IN ('pending', 'approved', 'denied', 'withdrawn')": (
        (models.PermissionRequest, _with(REQUEST_ROW, status="maybe")),
    ),
    "app_slug IS NOT NULL OR payload ? 'slug'": (
        (models.PermissionRequest, _with(REQUEST_ROW, payload={})),
        (models.PermissionRequest, _with(REQUEST_ROW, payload={"display_name": "Team wiki"})),
    ),
    "(status = 'pending') = (closed_at IS NULL)": (
        (models.PermissionRequest, _with(REQUEST_ROW, status="approved")),
        (
            models.PermissionRequest,
            _with(REQUEST_ROW, closed_at=WHEN, closed_by="dana@example.com"),
        ),
    ),
    "(closed_at IS NULL) = (closed_by IS NULL)": (
        (
            models.PermissionRequest,
            _with(REQUEST_ROW, closed_at=WHEN, closed_by=None, status="denied"),
        ),
        (
            models.PermissionRequest,
            _with(REQUEST_ROW, closed_by="dana@example.com", status="denied"),
        ),
    ),
    # app_config
    "desired_state IN ('running', 'stopped')": (
        (models.AppConfig, _with(CONFIG_ROW, desired_state="paused")),
        (models.AppConfig, _with(CONFIG_ROW, desired_state="Running")),
    ),
    "health_path LIKE '/%'": (
        (models.AppConfig, _with(CONFIG_ROW, health_path="healthz")),
        (models.AppConfig, _with(CONFIG_ROW, health_path="")),
    ),
    "health_deadline_s BETWEEN 5 AND 600": (
        (models.AppConfig, _with(CONFIG_ROW, health_deadline_s=4)),
        (models.AppConfig, _with(CONFIG_ROW, health_deadline_s=601)),
    ),
    "(own_sp_client_id_key IS NULL) = (own_sp_client_secret_key IS NULL)": (
        (models.AppConfig, _with(CONFIG_ROW, own_sp_client_secret_key=None)),
        (models.AppConfig, _with(CONFIG_ROW, own_sp_client_id_key=None)),
    ),
    # app_grants
    "principal_type IN ('user', 'group', 'everyone')": (
        (models.AppGrant, _with(GRANT_ROW, principal_type="role")),
        (models.AppGrant, _with(GRANT_ROW, principal_type="USER")),
    ),
    "permission IN ('CAN_USE', 'CAN_MANAGE')": (
        (models.AppGrant, _with(GRANT_ROW, permission="ADMIN")),
        (models.AppGrant, _with(GRANT_ROW, permission="can_use")),
    ),
    "(principal_type = 'everyone') = (principal = '*')": (
        (models.AppGrant, _with(GRANT_ROW, principal_type="everyone", principal="all")),
        (models.AppGrant, _with(GRANT_ROW, principal="*")),
    ),
    "principal_type <> 'user' OR principal = lower(principal)": (
        (models.AppGrant, _with(GRANT_ROW, principal_type="user", principal="Bob@example.com")),
    ),
    # releases
    "release_no > 0": (
        (models.Release, _with(RELEASE_ROW, release_no=0)),
        (models.Release, _with(RELEASE_ROW, release_no=-1)),
    ),
    "snapshot_digest ~ '^sha256:[0-9a-f]{64}$'": (
        (models.Release, _with(RELEASE_ROW, snapshot_digest="sha256:9f2c")),
        (models.Release, _with(RELEASE_ROW, snapshot_digest="sha256:" + "9F2C" * 16)),
        (models.Release, _with(RELEASE_ROW, snapshot_digest="md5:" + "9f2c" * 16)),
    ),
    "status IN ('pending', 'building', 'starting', 'healthy', 'active', 'failed', 'superseded')": (
        (models.Release, _with(RELEASE_ROW, status="done")),
    ),
    "label IS NULL OR char_length(label) BETWEEN 1 AND 80": (
        (models.Release, _with(RELEASE_ROW, label="x" * 81)),
        (models.Release, _with(RELEASE_ROW, label="")),
    ),
    "(deleted_at IS NULL) = (snapshot_digest IS NOT NULL)": (
        (models.Release, _with(RELEASE_ROW, snapshot_digest=None)),
        (
            models.Release,
            _with(DELETED_RELEASE_ROW, snapshot_digest=RELEASE_ROW["snapshot_digest"]),
        ),
    ),
    "(deleted_at IS NULL) = (deleted_by IS NULL)": (
        (models.Release, _with(DELETED_RELEASE_ROW, deleted_by=None)),
        (models.Release, _with(RELEASE_ROW, deleted_by="appmux")),
    ),
    "(locked_at IS NULL) = (locked_by IS NULL)": (
        (models.Release, _with(RELEASE_ROW, locked_by="alice@example.com")),
        (models.Release, _with(RELEASE_ROW, locked_at=WHEN)),
    ),
    "deleted_at IS NULL OR locked_at IS NULL": (
        (models.Release, _with(DELETED_RELEASE_ROW, locked_by="alice@example.com", locked_at=WHEN)),
    ),
    # app_resources
    "key ~ '^[a-z0-9][a-z0-9._-]{0,62}$'": (
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, key="Forecast")),
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, key="")),
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, key="-forecast")),
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, key="k" * 64)),
    ),
    "btrim(target) <> ''": (
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, target="")),
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, target="   ")),
    ),
    "access_as IN ('obo', 'own_sp', 'shared_sp')": (
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, access_as="sp")),
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, access_as="OBO")),
    ),
    "check_result IN ('ok', 'unverified')": (
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, check_result="failed")),
    ),
    "(type = 'secret') = (access_as IS NULL)": (
        (models.AppResource, _with(SECRET_RESOURCE_ROW, access_as="obo")),
        (models.AppResource, _with(ENDPOINT_RESOURCE_ROW, access_as=None)),
    ),
    "type <> 'secret' OR target ~ '^[^/]+/[^/]+$'": (
        (models.AppResource, _with(SECRET_RESOURCE_ROW, target="no-slash-here")),
        (models.AppResource, _with(SECRET_RESOURCE_ROW, target="scope/")),
        (models.AppResource, _with(SECRET_RESOURCE_ROW, target="a/b/c")),
    ),
    "NOT (access_as = 'shared_sp' AND check_result <> 'ok')": (
        (
            models.AppResource,
            _with(ENDPOINT_RESOURCE_ROW, access_as="shared_sp", check_result="unverified"),
        ),
    ),
    # metrics
    "cardinality(lat_hist) = 11": (
        (models.MetricRow, _with(METRIC_ROW, lat_hist=[1, 2, 3])),
        (models.MetricRow, _with(METRIC_ROW, lat_hist=[0] * 12)),
    ),
    "(app_slug = '_platform') = (mem_used_mb IS NOT NULL)": (
        (models.MetricRow, _with(METRIC_ROW, mem_used_mb=1500)),
        (models.MetricRow, _with(METRIC_ROW, app_slug="_platform")),
    ),
}


def _check_constraints_of(sql_text: str) -> list[str]:
    """Every ``CHECK (...)`` of the file, whitespace collapsed; quotes hide their parentheses."""
    text = _strip_sql_comments(sql_text)
    found: list[str] = []
    for start in re.finditer(r"\bCHECK\s*\(", text):
        depth, in_quote, end = 1, False, start.end()
        while depth:
            character = text[end]
            if character == "'":
                in_quote = not in_quote
            elif not in_quote and character == "(":
                depth += 1
            elif not in_quote and character == ")":
                depth -= 1
            end += 1
        found.append(" ".join(text[start.end() : end - 1].split()))
    return found


def test_every_check_of_the_baseline_has_refusal_rows_and_none_is_stale(baseline_text: str) -> None:
    in_sql = _check_constraints_of(baseline_text)

    assert len(in_sql) == len(set(in_sql)), "two CHECKs read the same; key them apart here"
    assert sorted(CHECK_REFUSALS) == sorted(in_sql)
    assert all(CHECK_REFUSALS[check] for check in in_sql)


@pytest.mark.parametrize(
    ("model", "row"),
    [
        pytest.param(model, row, id=f"{check} #{number}")
        for check, cases in CHECK_REFUSALS.items()
        for number, (model, row) in enumerate(cases, start=1)
    ],
)
def test_row_models_refuse_what_the_check_constraints_refuse(
    model: type[models.RowModel], row: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(row)


# --- migrations after the baseline -----------------------------------------------------------

SERVICE_PRINCIPAL_MIGRATION_PATH = (
    REPOSITORY_ROOT / "migrations" / "0003_service_principal_grants.sql"
)
APPLICATION_ID = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab"
SERVICE_PRINCIPAL_GRANT_ROW: dict[str, Any] = _with(
    GRANT_ROW, principal_type="service_principal", principal=APPLICATION_ID
)

# Every CHECK of 0003 (ADR 0020), with rows it refuses, kept as CHECK_REFUSALS is for the baseline.
SERVICE_PRINCIPAL_CHECK_REFUSALS: dict[str, tuple[Case, ...]] = {
    "principal_type IN ('user', 'group', 'everyone', 'service_principal')": (
        (models.AppGrant, _with(GRANT_ROW, principal_type="role")),
        (models.AppGrant, _with(GRANT_ROW, principal_type="Service_Principal")),
    ),
    "principal_type <> 'service_principal' OR principal ~ "
    "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'": (
        (models.AppGrant, _with(SERVICE_PRINCIPAL_GRANT_ROW, principal="sales-bot")),
        (models.AppGrant, _with(SERVICE_PRINCIPAL_GRANT_ROW, principal=APPLICATION_ID.upper())),
        (
            models.AppGrant,
            _with(SERVICE_PRINCIPAL_GRANT_ROW, principal=APPLICATION_ID.replace("-", "")),
        ),
        (models.AppGrant, _with(SERVICE_PRINCIPAL_GRANT_ROW, principal=APPLICATION_ID + "\n")),
    ),
}


def test_every_check_of_the_service_principal_migration_has_refusal_rows() -> None:
    in_sql = _check_constraints_of(SERVICE_PRINCIPAL_MIGRATION_PATH.read_text())

    assert sorted(SERVICE_PRINCIPAL_CHECK_REFUSALS) == sorted(in_sql)


def test_the_service_principal_migration_replaces_the_type_check_of_the_baseline() -> None:
    # PostgreSQL names a column's CHECK `<table>_<column>_check`, so the baseline's list of types
    # is `app_grants_principal_type_check`; the migration drops it before it adds the longer one.
    statements = [
        " ".join(statement.split())
        for statement in _statements(SERVICE_PRINCIPAL_MIGRATION_PATH.read_text())
    ]
    assert statements[0] == "ALTER TABLE app_grants DROP CONSTRAINT app_grants_principal_type_check"
    assert all(statement.startswith("ALTER TABLE app_grants ") for statement in statements)
    assert sum("ADD CONSTRAINT app_grants_principal_type_check " in s for s in statements) == 1


@pytest.mark.parametrize(
    ("model", "row"),
    [
        pytest.param(model, row, id=f"{check} #{number}")
        for check, cases in SERVICE_PRINCIPAL_CHECK_REFUSALS.items()
        for number, (model, row) in enumerate(cases, start=1)
    ],
)
def test_the_grant_row_model_refuses_what_the_service_principal_checks_refuse(
    model: type[models.RowModel], row: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(row)


def test_the_grant_row_model_accepts_a_service_principal_named_by_its_application_id() -> None:
    grant = models.AppGrant.model_validate(SERVICE_PRINCIPAL_GRANT_ROW)
    assert grant.principal_type == "service_principal"
    # The level is the policy's to cap, as for everyone: the database keeps either level.
    models.AppGrant.model_validate(_with(SERVICE_PRINCIPAL_GRANT_ROW, permission="CAN_MANAGE"))


# Rows the database accepts although a catalogue of the code would not: the SQL leaves these
# lists to the code, so a row model that refused one would fail a whole read on a forged or a
# future row (data model 4.3 and the residual of section 6).
SQL_ACCEPTS: dict[str, Case] = {
    "a request of a kind the code does not know, with payload keys it does not know": (
        models.PermissionRequest,
        _with(
            REQUEST_ROW,
            kind="future_kind",
            app_slug="sales",
            payload={"new_field": 1, "slug": 7, "obo": "yes", "nested": {"a": [1, None]}},
        ),
    ),
    "a request to register whose payload names the slug as null": (
        models.PermissionRequest,
        _with(REQUEST_ROW, payload={"slug": None}),
    ),
    "an audit row of an action that is not in the list": (
        models.AuditLogRow,
        {
            "id": 1,
            "ts": WHEN,
            "actor": "mallory@example.com",
            "action": "x.y",
            "app_slug": None,
            "detail": {"retention_days": 7, "nested": {"a": [1, 2]}},
            "reason": None,
        },
    ),
    "an audit row with an empty detail": (
        models.AuditLogRow,
        {
            "id": 2,
            "ts": WHEN,
            "actor": "appmux",
            "action": "app.start",
            "app_slug": "sales",
            "detail": {},
            "reason": None,
        },
    ),
    "a release whose detected manifest is empty": (
        models.Release,
        _with(RELEASE_ROW, detected={}),
    ),
    "a release whose detected manifest has keys of a later version": (
        models.Release,
        _with(RELEASE_ROW, detected={"runtime": "node", "toolchain": {"node": "22"}}),
    ),
    "a release whose config and wheels have keys of a later version": (
        models.Release,
        _with(
            RELEASE_ROW,
            config={"env": [], "limits": {"cpu": 1}},
            wheels=[{"sha256": "3b1e" * 16, "filename": "a.whl", "source": "x", "size": 1}],
        ),
    ),
    "a release with eleven tags, one in upper case, one empty and one long": (
        models.Release,
        _with(RELEASE_ROW, tags=["Stable", "", "t" * 40, *[f"t{n}" for n in range(8)]]),
    ),
    "a display name of tabs only (btrim trims spaces only)": (
        models.App,
        _with(APP_ROW, display_name="\t"),
    ),
    "a group principal that is empty": (models.AppGrant, _with(GRANT_ROW, principal="")),
    "a resource of a type and a permission the catalogue does not know": (
        models.AppResource,
        _with(ENDPOINT_RESOURCE_ROW, type="", permission=""),
    ),
    "a setting under a key the code does not know": (
        models.PlatformSetting,
        {"key": "", "value": 3, "updated_by": "", "updated_at": WHEN},
    ),
}


@pytest.mark.parametrize(
    ("model", "row"), [pytest.param(*case, id=name) for name, case in SQL_ACCEPTS.items()]
)
def test_row_models_accept_what_the_sql_accepts(
    model: type[models.RowModel], row: dict[str, Any]
) -> None:
    assert model.model_validate(row) is not None


def test_a_forged_request_loads_with_its_keys_and_stays_pending() -> None:
    model, row = SQL_ACCEPTS[
        "a request of a kind the code does not know, with payload keys it does not know"
    ]
    request = model.model_validate(row)

    assert isinstance(request, models.PermissionRequest)
    assert request.status == "pending"
    assert request.kind == "future_kind"
    assert request.payload.model_dump()["new_field"] == 1


def test_a_forged_audit_row_keeps_its_action_and_detail() -> None:
    model, row = SQL_ACCEPTS["an audit row of an action that is not in the list"]
    entry = model.model_validate(row)

    assert isinstance(entry, models.AuditLogRow)
    assert entry.action == "x.y"
    assert entry.detail == {"retention_days": 7, "nested": {"a": [1, 2]}}


def test_a_release_with_an_empty_detected_manifest_loads_unchanged_otherwise() -> None:
    model, row = SQL_ACCEPTS["a release whose detected manifest is empty"]
    release = model.model_validate(row)

    assert isinstance(release, models.Release)
    assert release.detected.runtime is None
    assert release.detected.env == []
    assert release.status == "active"


def test_a_snapshot_of_every_table_loads_although_one_row_is_forged() -> None:
    # What the snapshot does with ROW_MODELS: one forged row must not fail the read of the table.
    rows = [
        _with(REQUEST_ROW, request_id=1),
        _with(
            REQUEST_ROW,
            request_id=2,
            kind="future_kind",
            payload={"slug": "x", "new_field": 1},
        ),
    ]

    loaded = [models.ROW_MODELS["permission_requests"].model_validate(row) for row in rows]

    assert [row.status for row in loaded if isinstance(row, models.PermissionRequest)] == [
        "pending",
        "pending",
    ]


def test_models_refuse_a_naive_timestamp_and_an_unknown_column() -> None:
    with pytest.raises(ValidationError):
        models.App.model_validate(_with(APP_ROW, created_at=datetime(2026, 10, 30, 8, 41)))
    with pytest.raises(ValidationError):
        models.App.model_validate(_with(APP_ROW, is_admin=True))


def test_a_deleted_release_keeps_its_row_without_snapshot_wheels_or_tags() -> None:
    deleted = _with(
        RELEASE_ROW,
        status="superseded",
        snapshot_digest=None,
        wheels=[],
        tags=[],
        deleted_at=WHEN,
        deleted_by="appmux",
    )
    assert models.Release.model_validate(deleted).deleted_by == "appmux"
