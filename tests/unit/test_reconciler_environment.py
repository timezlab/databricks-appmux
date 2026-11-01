"""The environment a release's child starts with, as the reconciler builds it (design 5.4).

``sandbox.environment`` already proves how entries are layered and checked; here the reconciler's
part is tested: reading the entries from the release row, resolving the resources they name
through a seam, and turning every refusal into a reason a person can act on.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import pytest
from reconciler_fakes import seed_app, seed_release, stub_registry_class

from appmux.reconciler.child_env import (
    EnvironmentRefused,
    NoResources,
    PlatformIdentity,
    ReleaseEnvironment,
    ResourcesMissing,
)
from appmux.registry.models import Release
from appmux.registry.protocol import Change
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio

PLATFORM = PlatformIdentity(
    workspace_host="https://workspace.example.invalid",
    workspace_id="1234567890",
    parent_url="https://appmux.example.invalid",
)


class FakeResources:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.asked: list[tuple[str, frozenset[str]]] = []

    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]:
        self.asked.append((app_slug, frozenset(keys)))
        missing = sorted(key for key in keys if key not in self.values)
        if missing:
            raise ResourcesMissing(missing)
        return {key: self.values[key] for key in keys}


async def release_with(
    detected_env: list[dict[str, str]], config_env: list[dict[str, str]] | None = None
) -> Release:
    registry = stub_registry_class()()
    seed_app(registry)
    seed_release(registry, "sales", 1, "pending")
    await registry.write(
        [
            Change(
                WriteKind.RELEASE_UPDATE,
                key={"app_slug": "sales", "release_no": 1},
                values={
                    "detected": {
                        "runtime": "python",
                        "command": ["python", "a.py"],
                        "env": detected_env,
                    },
                    "config": {"env": config_env or [], "wheel_sources": []},
                },
            )
        ]
    )
    found = registry.snapshot.release("sales", 1)
    assert found is not None
    return found


async def test_literal_entries_and_the_variables_appmux_sets_make_the_environment() -> None:
    release = await release_with([{"name": "MODE", "value": "demo"}])
    factory = ReleaseEnvironment(platform=PLATFORM, resources=FakeResources())

    environment = await factory.environment_for(release, port=8000)

    assert environment["MODE"] == "demo"
    assert environment["DATABRICKS_APP_PORT"] == "8000"
    assert environment["DATABRICKS_APP_NAME"] == "sales"
    assert environment["APPMUX_BASE_PATH"] == "/sales"
    assert environment["DATABRICKS_HOST"] == "https://workspace.example.invalid"
    assert environment["DATABRICKS_APP_URL"] == "https://appmux.example.invalid/sales"


async def test_the_release_config_wins_over_the_app_yaml_and_appmux_wins_over_both() -> None:
    release = await release_with(
        [{"name": "MODE", "value": "yaml"}, {"name": "PORT", "value": "1"}],
        [{"name": "MODE", "value": "config"}],
    )
    factory = ReleaseEnvironment(platform=PLATFORM, resources=FakeResources())

    environment = await factory.environment_for(release, port=8000)

    assert environment["MODE"] == "config"
    assert environment["PORT"] == "8000"


async def test_a_value_from_entry_is_resolved_through_the_resources_of_the_app() -> None:
    release = await release_with(
        [{"name": "API_KEY", "value_from": "openai-api-key"}],
        [{"name": "WAREHOUSE", "value_from": "sql-warehouse"}],
    )
    resources = FakeResources({"openai-api-key": "sk-test-value", "sql-warehouse": "wh-1"})
    factory = ReleaseEnvironment(platform=PLATFORM, resources=resources)

    environment = await factory.environment_for(release, port=8000)

    assert environment["API_KEY"] == "sk-test-value"
    assert environment["WAREHOUSE"] == "wh-1"
    assert resources.asked == [("sales", frozenset({"openai-api-key", "sql-warehouse"}))]


async def test_no_resource_is_asked_for_when_the_release_names_none() -> None:
    release = await release_with([{"name": "MODE", "value": "demo"}])
    resources = FakeResources()
    factory = ReleaseEnvironment(platform=PLATFORM, resources=resources)

    await factory.environment_for(release, port=8000)

    assert resources.asked == []


async def test_missing_resources_are_named_together_in_the_reason() -> None:
    release = await release_with(
        [
            {"name": "A", "value_from": "sql-warehouse"},
            {"name": "B", "value_from": "openai-api-key"},
        ]
    )
    factory = ReleaseEnvironment(platform=PLATFORM, resources=FakeResources())

    with pytest.raises(ResourcesMissing) as refusal:
        await factory.environment_for(release, port=8000)

    assert str(refusal.value) == "missing resources: openai-api-key, sql-warehouse"
    assert isinstance(refusal.value, EnvironmentRefused)


async def test_without_a_resources_source_every_named_resource_is_missing() -> None:
    release = await release_with([{"name": "A", "value_from": "sql-warehouse"}])
    factory = ReleaseEnvironment(platform=PLATFORM, resources=NoResources())

    with pytest.raises(ResourcesMissing, match="missing resources: sql-warehouse"):
        await factory.environment_for(release, port=8000)


async def test_an_entry_that_cannot_be_exported_is_refused_without_its_value() -> None:
    release = await release_with([{"name": "MODE", "value": "sk-secret\x00tail"}])
    factory = ReleaseEnvironment(platform=PLATFORM, resources=FakeResources())

    with pytest.raises(EnvironmentRefused) as refusal:
        await factory.environment_for(release, port=8000)

    assert "MODE" in str(refusal.value)
    assert "sk-secret" not in str(refusal.value)
