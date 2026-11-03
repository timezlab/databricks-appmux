"""``GET /apps/{app}/environment``: what an app's child is started with, for its app admins (T191).

The variables are the layering of ``sandbox/environment.py`` read back without resolving anything:
an entry of the release or its ``app.yaml`` with its value, a ``valueFrom`` by its resource key
(the target for a non-secret, never anything for a secret), and the variables appmux sets for
every child. The packages are what the release declares in its built directory, the wheels it
recorded and, for a Python app, what the platform's own environment provides.
"""

from __future__ import annotations

import json
import platform
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Final

import pytest
from edge_harness import ALICE, BOB, CAROL, DANA, FRANK, NOW, AdminApi, app_row, seeded_registry
from stub_registry import StubRegistry

from appmux.admin.environment import MAX_MANIFEST_BYTES, ChildSetup, EnvironmentRoutes
from appmux.admin.schemas import AppEnvironment, EnvironmentVariable
from appmux.reconciler.child_env import PlatformIdentity

DIGEST: Final = "sha256:" + "ab" * 32
ENVIRONMENT: Final = "/apps/sales/environment"
SECRET_TARGET: Final = "billing-scope/db-password"  # noqa: S105 - a scope/key reference, not a value
WAREHOUSE_ID: Final = "abc123def456"
PLATFORM: Final = PlatformIdentity(
    workspace_host="https://workspace.example.com",
    workspace_id="1234",
    parent_url="https://appmux.example.com",
)
REQUIREMENTS: Final = """\
# the app's own pins
fastapi==0.115.0
pandas>=2.0  # any recent one
httpx[http2] \\
    --hash=sha256:00ff
--index-url https://user:hunter2@pypi.example.com/simple
-r more.txt
private @ https://token-in-url@example.com/private.whl
"""
PACKAGE_JSON: Final = {
    "name": "front",
    "dependencies": {"next": "^15.0.0", "react": "19.0.0"},
    "devDependencies": {"typescript": "~5.6.0"},
}


async def node_22() -> str | None:
    return "22.22.1"


def resource(key: str, *, type_: str, target: str) -> dict[str, object]:
    is_secret = type_ == "secret"
    return {
        "app_slug": "sales",
        "key": key,
        "type": type_,
        "target": target,
        "permission": "READ" if is_secret else "CAN_USE",
        "access_as": None if is_secret else "obo",
        "check_result": "ok",
        "attached_by": ALICE,
        "attached_at": NOW,
    }


def seed_release(registry: StubRegistry, *, runtime: str = "python", **columns: object) -> None:
    registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": 4,
            "snapshot_digest": DIGEST,
            "detected": {
                "runtime": runtime,
                "command": ["python", "app.py"],
                "env": [
                    {"name": "LOG_LEVEL", "value": "debug"},
                    {"name": "DB_PASSWORD", "value_from": "db-password"},
                    {"name": "WAREHOUSE", "value_from": "warehouse"},
                    {"name": "PORT", "value": "9999"},
                ],
            },
            "config": {
                "env": [
                    {"name": "LOG_LEVEL", "value": "info"},
                    {"name": "MISSING", "value_from": "not-attached"},
                ]
            },
            "wheels": [
                {
                    "sha256": "cd" * 32,
                    "filename": "acme_tools-1.2.0-py3-none-any.whl",
                    "source": "store",
                },
            ],
            "status": "active",
            "status_at": NOW,
            "created_by": CAROL,
            "created_at": NOW,
            **columns,
        },
    )


class Environment:
    def __init__(self, releases_dir: Path, *, runtime: str = "python") -> None:
        self.registry: StubRegistry = seeded_registry()
        seed_release(self.registry, runtime=runtime)
        self.registry.seed(
            "app_resources", resource("db-password", type_="secret", target=SECRET_TARGET)
        )
        self.registry.seed(
            "app_resources", resource("warehouse", type_="sql_warehouse", target=WAREHOUSE_ID)
        )
        self.releases_dir = releases_dir
        self.built = releases_dir / "sales" / "4"
        self.built.mkdir(parents=True)
        setup = ChildSetup(platform=PLATFORM, releases_dir=releases_dir)
        self.api = AdminApi(
            self.registry,
            lambda authorizer: [EnvironmentRoutes(setup, node_version=node_22)],
        )

    def get(self, email: str = CAROL) -> AppEnvironment:
        reply = self.api.call("GET", ENVIRONMENT, email)
        assert reply.status_code == 200, reply.text
        return AppEnvironment.model_validate_json(reply.content)

    def variables(self, email: str = CAROL) -> Mapping[str, EnvironmentVariable]:
        rows = self.get(email).variables
        names = [row.name for row in rows]
        assert len(names) == len(set(names)), "a variable is listed twice"
        return {row.name: row for row in rows}


@pytest.fixture
def env(tmp_path: Path) -> Environment:
    return Environment(tmp_path / "releases")


def test_the_runtime_is_the_releases_with_the_platforms_versions(env: Environment) -> None:
    body = env.get()

    assert body.release == 4
    assert body.runtime.kind == "python"
    assert body.runtime.python_version == platform.python_version()
    assert body.runtime.node_version == "22.22.1"


def test_an_entry_of_the_release_overrides_the_one_of_app_yaml(env: Environment) -> None:
    log_level = env.variables()["LOG_LEVEL"]

    assert (log_level.value, log_level.source, log_level.is_secret) == ("info", "config", False)


def test_a_resource_is_shown_by_its_key_and_a_non_secret_by_its_target(env: Environment) -> None:
    warehouse = env.variables()["WAREHOUSE"]

    assert (warehouse.source, warehouse.resource_key, warehouse.value) == (
        "resource",
        "warehouse",
        WAREHOUSE_ID,
    )
    assert warehouse.is_secret is False


def test_a_secret_is_never_returned(env: Environment) -> None:
    reply = env.api.call("GET", ENVIRONMENT, CAROL)

    password = env.variables()["DB_PASSWORD"]
    assert (password.source, password.resource_key) == ("resource", "db-password")
    assert password.is_secret is True
    assert password.value is None
    assert SECRET_TARGET not in reply.text


def test_a_key_the_app_does_not_have_is_shown_without_a_value(env: Environment) -> None:
    missing = env.variables()["MISSING"]

    assert (missing.source, missing.resource_key, missing.value) == (
        "resource",
        "not-attached",
        None,
    )


def test_the_platforms_variables_are_listed_and_win_over_the_releases(env: Environment) -> None:
    variables = env.variables()

    assert variables["PORT"].value == "8000"
    assert variables["PORT"].source == "platform"
    assert variables["APPMUX_BASE_PATH"].value == "/sales"
    assert variables["DATABRICKS_APP_URL"].value == "https://appmux.example.com/sales"
    assert {row.source for name, row in variables.items() if name.startswith("STREAMLIT_")} == {
        "platform"
    }


def test_no_credential_variable_is_listed_even_for_a_shared_sp_app(env: Environment) -> None:
    env.registry.seed("apps", app_row("sales", owner=ALICE) | {"shared_sp_delegated": True})

    names = set(env.variables())

    assert not names & {
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_TOKEN",
        "DATABRICKS_AUTH_TYPE",
        "DATABRICKS_CLI_PATH",
    }


def test_the_packages_are_the_releases_the_wheels_and_the_platforms(env: Environment) -> None:
    (env.built / "requirements.txt").write_text(REQUIREMENTS)
    (env.built / "package.json").write_text(json.dumps(PACKAGE_JSON))

    reply = env.api.call("GET", ENVIRONMENT, CAROL)
    packages = AppEnvironment.model_validate_json(reply.content).packages

    declared = [(p.name, p.version) for p in packages if p.source == "release"]
    assert declared == [
        ("fastapi", "==0.115.0"),
        ("pandas", ">=2.0"),
        ("httpx", None),
        ("next", "^15.0.0"),
        ("react", "19.0.0"),
        ("typescript", "~5.6.0"),
    ]
    assert [(p.name, p.version) for p in packages if p.source == "wheel"] == [
        ("acme-tools", "1.2.0")
    ]
    platform_rows = {p.name: p.version for p in packages if p.source == "platform"}
    assert platform_rows["pydantic"] == metadata.version("pydantic")
    for leaked in ("hunter2", "token-in-url", "private", "more.txt"):
        assert leaked not in reply.text


def test_a_release_not_built_here_lists_only_its_wheels_and_the_platform(
    tmp_path: Path,
) -> None:
    env = Environment(tmp_path / "releases")
    env.built.rmdir()

    sources = {p.source for p in env.get().packages}

    assert sources == {"wheel", "platform"}


def test_a_manifest_larger_than_the_cap_is_not_read(env: Environment) -> None:
    (env.built / "requirements.txt").write_text("fastapi\n" + "#" * MAX_MANIFEST_BYTES)

    assert [p for p in env.get().packages if p.source == "release"] == []


def test_a_node_app_lists_no_platform_python_packages(tmp_path: Path) -> None:
    env = Environment(tmp_path / "releases", runtime="node")
    (env.built / "package.json").write_text(json.dumps(PACKAGE_JSON))

    body = env.get()

    assert body.runtime.kind == "node"
    assert {p.source for p in body.packages} == {"release", "wheel"}


def test_an_app_with_no_active_release_has_nothing_to_describe(tmp_path: Path) -> None:
    env = Environment(tmp_path / "releases")
    seed_release(env.registry, status="failed")  # the same row, no longer active

    body = env.get()

    assert (body.release, body.variables, body.packages) == (None, [], [])
    assert body.runtime.kind == "unknown"
    assert body.runtime.python_version == platform.python_version()


def test_a_platform_admin_reads_it_and_nothing_is_recorded(env: Environment) -> None:
    assert env.get(DANA).release == 4
    assert env.registry.audit_rows == ()


@pytest.mark.parametrize(
    ("email", "path", "status"),
    [
        (BOB, ENVIRONMENT, 403),
        (FRANK, ENVIRONMENT, 404),
        (CAROL, "/apps/nope/environment", 404),
    ],
    ids=["can-use-is-forbidden", "a-stranger-sees-no-such-app", "a-missing-app"],
)
def test_who_may_not_read_the_environment(
    env: Environment, email: str, path: str, status: int
) -> None:
    reply = env.api.call("GET", path, email)

    assert reply.status_code == status
    assert WAREHOUSE_ID not in reply.text
