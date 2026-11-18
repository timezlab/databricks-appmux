"""What credential a child gets (T142, T233; FR-036, FR-039, FR-056, design 8.5, ADR 0023).

A child has no Databricks credential unless one was delegated or attached. An own service principal
sets the two standard variables from its ``secret`` references. The shared service principal, when
a platform admin delegated it and the master switch is on, sets the same two variables from appmux's
own pair, exactly as if the child were deployed alone. OBO is a header at the edge that is never
stored here. An own service principal wins over a delegation.

Every client id and secret here is a fake placeholder.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import SecretStr

from appmux.reconciler.child_env import (
    EnvironmentRefused,
    PlatformIdentity,
    ReleaseEnvironment,
    ResourcesMissing,
)
from appmux.registry.models import App, AppConfig, AppResource, PlatformSetting, Release
from appmux.registry.snapshot import SNAPSHOT_TABLES, RegistrySnapshot, build_snapshot
from appmux.secrets.credentials import ChildCredentials, CredentialsRefused
from appmux.secrets.resolver import MissingResourceKeys, SecretUnreadable, UnreadableSecret

pytestmark = pytest.mark.anyio

NOW: Final = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
CLIENT_ID: Final = "client-id-placeholder"
CLIENT_SECRET: Final = "client-secret-placeholder"  # noqa: S105 - a placeholder, not a credential
APPMUX_CLIENT_ID: Final = "appmux-client-id-placeholder"
APPMUX_CLIENT_SECRET: Final = "appmux-client-secret-placeholder"  # noqa: S105 - a placeholder, not a credential
USER: Final = "alice@example.com"


# --- which credential a child gets ---------------------------------------------------------------


def app_row(*, obo: bool = False, shared_sp: bool = False, slug: str = "sales") -> App:
    return App(
        slug=slug,
        display_name="Sales",
        owner=USER,
        source_path=f"/Workspace/Users/{USER}/sales",
        memory_budget_mb=256,
        obo_delegated=obo,
        shared_sp_delegated=shared_sp,
        created_by="dana@example.com",
        created_at=NOW,
        updated_by="dana@example.com",
        updated_at=NOW,
    )


def config_row(
    *, id_key: str | None = None, secret_key: str | None = None, slug: str = "sales"
) -> AppConfig:
    return AppConfig(
        app_slug=slug,
        desired_state="running",
        own_sp_client_id_key=id_key,
        own_sp_client_secret_key=secret_key,
        updated_by="dana@example.com",
        updated_at=NOW,
    )


def secret_resource(key: str, target: str, *, slug: str = "sales") -> AppResource:
    return AppResource(
        app_slug=slug,
        key=key,
        type="secret",
        target=target,
        permission="READ",
        access_as=None,
        check_result="ok",
        attached_by=USER,
        attached_at=NOW,
    )


def warehouse_resource(
    key: str, *, access_as: str, check_result: str = "ok", slug: str = "sales"
) -> AppResource:
    fields: dict[str, Any] = {
        "app_slug": slug,
        "key": key,
        "type": "sql_warehouse",
        "target": "a1b2c3d4e5f67890",
        "permission": "CAN_USE",
        "access_as": access_as,
        "check_result": check_result,
        "attached_by": USER,
        "attached_at": NOW,
    }
    if access_as == "shared_sp" and check_result != "ok":
        # The model refuses this pair, so no registry read can produce it. The start still checks
        # (defence in depth on the confused-deputy path), and this row proves it by skipping the
        # model's validation.
        return AppResource.model_construct(**fields)
    return AppResource(**fields)


def snapshot_of(
    app: App,
    config: AppConfig,
    *resources: AppResource,
    shared_sp_allowed: bool | None = None,
) -> RegistrySnapshot:
    rows: dict[str, list[Any]] = {table: [] for table in SNAPSHOT_TABLES}
    rows["apps"] = [app]
    rows["app_config"] = [config]
    rows["app_resources"] = list(resources)
    if shared_sp_allowed is not None:
        rows["platform_settings"] = [
            PlatformSetting(
                key="shared_sp_delegation_allowed",
                value=shared_sp_allowed,
                updated_by="dana@example.com",
                updated_at=NOW,
            )
        ]
    return build_snapshot(rows, loaded_at=NOW)


class FakeResources:
    """The resolver seam: answers from a table of resource key to value, and remembers asks."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = dict(values)
        self.asked: list[frozenset[str]] = []
        self.failure: Exception | None = None

    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]:
        self.asked.append(frozenset(keys))
        if self.failure is not None:
            raise self.failure
        missing = [key for key in keys if key not in self.values]
        if missing:
            raise MissingResourceKeys(missing)
        return {key: self.values[key] for key in keys}


OWN_SP_VALUES: Final = {"sp-client-id": CLIENT_ID, "sp-client-secret": CLIENT_SECRET}


def own_sp_snapshot(**kwargs: Any) -> RegistrySnapshot:
    return snapshot_of(
        app_row(**kwargs),
        config_row(id_key="sp-client-id", secret_key="sp-client-secret"),  # noqa: S106 - a resource key, not a credential
        secret_resource("sp-client-id", "sales-sp/client-id"),
        secret_resource("sp-client-secret", "sales-sp/client-secret"),
    )


def credentials_over(
    snapshot: RegistrySnapshot, *, values: Mapping[str, str] | None = None
) -> tuple[ChildCredentials, FakeResources]:
    resources = FakeResources(OWN_SP_VALUES if values is None else values)
    return ChildCredentials(
        snapshot=lambda: snapshot,
        resources=resources,
        appmux_client_id=APPMUX_CLIENT_ID,
        appmux_client_secret=SecretStr(APPMUX_CLIENT_SECRET),
    ), resources


def shared_sp_snapshot(*resources: AppResource) -> RegistrySnapshot:
    """An app delegated the shared service principal, with the platform switch on."""
    return snapshot_of(app_row(shared_sp=True), config_row(), *resources, shared_sp_allowed=True)


async def test_a_child_with_nothing_delegated_or_attached_gets_no_credential_and_no_lookup() -> (
    None
):
    credentials, resources = credentials_over(snapshot_of(app_row(), config_row()))

    variables = await credentials.credentials_for("sales")

    assert dict(variables) == {}
    assert resources.asked == []


async def test_an_obo_delegation_gives_the_child_nothing_to_store() -> None:
    # The visitor's token arrives per request in a header and is the edge's business (design 8.5).
    credentials, resources = credentials_over(snapshot_of(app_row(obo=True), config_row()))

    variables = await credentials.credentials_for("sales")

    assert dict(variables) == {}
    assert resources.asked == []


async def test_an_own_service_principal_sets_its_client_id_and_secret_from_its_references() -> None:
    credentials, resources = credentials_over(own_sp_snapshot())

    variables = await credentials.credentials_for("sales")

    assert dict(variables) == {
        "DATABRICKS_CLIENT_ID": CLIENT_ID,
        "DATABRICKS_CLIENT_SECRET": CLIENT_SECRET,
    }
    assert resources.asked == [frozenset({"sp-client-id", "sp-client-secret"})]


async def test_the_variables_it_returns_show_names_and_never_values_when_printed() -> None:
    credentials, _ = credentials_over(own_sp_snapshot())

    variables = await credentials.credentials_for("sales")

    for text in (repr(variables), str(variables), f"{variables}", f"{variables!r}"):
        assert CLIENT_SECRET not in text
        assert CLIENT_ID not in text


async def test_an_own_service_principal_with_one_key_missing_is_refused_naming_the_field() -> None:
    snapshot = snapshot_of(
        app_row(),
        config_row(id_key="sp-client-id", secret_key="sp-client-secret"),  # noqa: S106 - a resource key, not a credential
        secret_resource("sp-client-id", "sales-sp/client-id"),
    )
    credentials, resources = credentials_over(snapshot)

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "own_sp_client_secret_key" in refusal.value.message
    assert resources.asked == []  # nothing is read for a pair that cannot stand


async def test_an_own_service_principal_whose_secret_cannot_be_read_is_refused_unvalued() -> None:
    credentials, resources = credentials_over(own_sp_snapshot())
    resources.failure = SecretUnreadable(
        [UnreadableSecret("secret scope 'sales-sp'", "sales-sp", "denied")]
    )

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "sales-sp" in refusal.value.message
    assert CLIENT_SECRET not in refusal.value.message


async def test_a_personal_access_token_is_not_accepted_as_an_own_service_principal() -> None:
    pat = "dapi" + "0123456789abcdef" * 2
    credentials, _ = credentials_over(
        own_sp_snapshot(), values={"sp-client-id": CLIENT_ID, "sp-client-secret": pat}
    )

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "personal access token" in refusal.value.message
    assert pat not in refusal.value.message
    assert pat not in str(refusal.value)


async def test_no_mode_ever_produces_a_personal_access_token_variable() -> None:
    names: set[str] = set()
    for snapshot in (own_sp_snapshot(), shared_sp_snapshot()):
        credentials, _ = credentials_over(snapshot)
        names |= set(await credentials.credentials_for("sales"))

    assert "DATABRICKS_TOKEN" not in names
    assert names == {"DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET"}


async def test_the_shared_service_principal_gives_the_pair_appmux_runs_as_and_no_more() -> None:
    # ADR 0023: the child is deployed as if alone, so the SDK in it finds the two standard
    # variables and signs in by itself. No auth type, no token command, no static token.
    credentials, resources = credentials_over(shared_sp_snapshot())

    variables = await credentials.credentials_for("sales")

    assert dict(variables) == {
        "DATABRICKS_CLIENT_ID": APPMUX_CLIENT_ID,
        "DATABRICKS_CLIENT_SECRET": APPMUX_CLIENT_SECRET,
    }
    assert resources.asked == []  # the pair is appmux's own, not a resource of the app


async def test_the_shared_service_principal_variables_print_names_and_never_values() -> None:
    credentials, _ = credentials_over(shared_sp_snapshot())

    variables = await credentials.credentials_for("sales")

    for text in (repr(variables), str(variables), f"{variables}", f"{variables!r}"):
        assert APPMUX_CLIENT_SECRET not in text
        assert APPMUX_CLIENT_ID not in text


async def test_a_shared_delegation_has_no_effect_while_the_platform_switch_is_off() -> None:
    for allowed in (False, None):  # off, and never set (the default is off)
        snapshot = snapshot_of(app_row(shared_sp=True), config_row(), shared_sp_allowed=allowed)
        credentials, _ = credentials_over(snapshot)

        assert dict(await credentials.credentials_for("sales")) == {}


async def test_the_platform_switch_alone_gives_an_app_nothing() -> None:
    snapshot = snapshot_of(app_row(shared_sp=False), config_row(), shared_sp_allowed=True)
    credentials, _ = credentials_over(snapshot)

    assert dict(await credentials.credentials_for("sales")) == {}


async def test_an_own_service_principal_overwrites_the_shared_one_for_its_smaller_reach() -> None:
    snapshot = snapshot_of(
        app_row(shared_sp=True),
        config_row(id_key="sp-client-id", secret_key="sp-client-secret"),  # noqa: S106 - a resource key, not a credential
        secret_resource("sp-client-id", "sales-sp/client-id"),
        secret_resource("sp-client-secret", "sales-sp/client-secret"),
        shared_sp_allowed=True,
    )
    credentials, _ = credentials_over(snapshot)

    variables = await credentials.credentials_for("sales")

    assert dict(variables) == {
        "DATABRICKS_CLIENT_ID": CLIENT_ID,
        "DATABRICKS_CLIENT_SECRET": CLIENT_SECRET,
    }
    assert APPMUX_CLIENT_SECRET not in repr(dict(variables))


async def test_an_own_service_principal_that_cannot_be_given_is_not_replaced_by_the_shared() -> (
    None
):
    # A child that named its own pair must not start under appmux's because its own one is broken:
    # the wider identity would run it with rights its admin never chose.
    snapshot = snapshot_of(
        app_row(shared_sp=True),
        config_row(id_key="sp-client-id", secret_key="sp-client-secret"),  # noqa: S106 - a resource key, not a credential
        secret_resource("sp-client-id", "sales-sp/client-id"),
        shared_sp_allowed=True,
    )
    credentials, _ = credentials_over(snapshot)

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "own_sp_client_secret_key" in refusal.value.message


async def test_a_resource_asking_for_the_shared_identity_without_a_verified_check_is_refused() -> (
    None
):
    snapshot = shared_sp_snapshot(
        warehouse_resource("wh", access_as="shared_sp", check_result="unverified")
    )
    credentials, _ = credentials_over(snapshot)

    with pytest.raises(CredentialsRefused) as refusal:
        await credentials.credentials_for("sales")

    assert "'wh'" in refusal.value.message
    assert "unverified" in refusal.value.message
    assert APPMUX_CLIENT_SECRET not in refusal.value.message


async def test_a_verified_resource_on_the_shared_identity_is_accepted() -> None:
    snapshot = shared_sp_snapshot(
        warehouse_resource("wh", access_as="shared_sp", check_result="ok")
    )
    credentials, _ = credentials_over(snapshot)

    assert "DATABRICKS_CLIENT_SECRET" in await credentials.credentials_for("sales")


async def test_an_unknown_app_gets_no_credential() -> None:
    credentials, _ = credentials_over(snapshot_of(app_row(), config_row()))

    assert dict(await credentials.credentials_for("gone")) == {}


# --- what reaches the child's environment --------------------------------------------------------

PLATFORM = PlatformIdentity(
    workspace_host="https://workspace.example.invalid",
    workspace_id="1234567890",
    parent_url="https://appmux.example.invalid",
)


class RefusingAdapter:
    """What the assembly puts between the two: a credential refusal becomes an environment one."""

    def __init__(self, source: ChildCredentials) -> None:
        self._source = source

    async def credentials_for(self, app_slug: str) -> Mapping[str, str]:
        try:
            return await self._source.credentials_for(app_slug)
        except CredentialsRefused as refusal:
            raise EnvironmentRefused(refusal.message) from refusal


class NoResourceValues:
    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]:
        if keys:
            raise ResourcesMissing(list(keys))
        return {}


def release_of(slug: str = "sales") -> Release:
    return Release.model_validate(
        {
            "app_slug": slug,
            "release_no": 1,
            "snapshot_digest": "sha256:" + "0" * 64,
            "detected": {"runtime": "python", "command": ["python", "app.py"]},
            "status": "pending",
            "status_at": NOW,
            "created_by": USER,
            "created_at": NOW,
        }
    )


async def test_an_own_service_principal_child_gets_the_three_standard_variables() -> None:
    credentials, _ = credentials_over(own_sp_snapshot())
    factory = ReleaseEnvironment(
        platform=PLATFORM, resources=NoResourceValues(), credentials=RefusingAdapter(credentials)
    )

    environment = await factory.environment_for(release_of(), port=8000)

    assert environment["DATABRICKS_HOST"] == PLATFORM.workspace_host
    assert environment["DATABRICKS_CLIENT_ID"] == CLIENT_ID
    assert environment["DATABRICKS_CLIENT_SECRET"] == CLIENT_SECRET
    assert "DATABRICKS_AUTH_TYPE" not in environment
    assert CLIENT_SECRET not in repr(environment)


async def test_a_shared_service_principal_child_gets_the_variables_of_one_deployed_alone() -> None:
    credentials, _ = credentials_over(shared_sp_snapshot())
    factory = ReleaseEnvironment(
        platform=PLATFORM, resources=NoResourceValues(), credentials=RefusingAdapter(credentials)
    )

    environment = await factory.environment_for(release_of(), port=8000)

    assert environment["DATABRICKS_HOST"] == PLATFORM.workspace_host
    assert environment["DATABRICKS_CLIENT_ID"] == APPMUX_CLIENT_ID
    assert environment["DATABRICKS_CLIENT_SECRET"] == APPMUX_CLIENT_SECRET
    assert "DATABRICKS_AUTH_TYPE" not in environment
    assert "DATABRICKS_CLI_PATH" not in environment
    assert "DATABRICKS_TOKEN" not in environment
    assert APPMUX_CLIENT_SECRET not in repr(environment)


async def test_a_child_with_no_credential_has_no_databricks_credential_variable() -> None:
    credentials, _ = credentials_over(snapshot_of(app_row(), config_row()))
    factory = ReleaseEnvironment(
        platform=PLATFORM, resources=NoResourceValues(), credentials=RefusingAdapter(credentials)
    )

    environment = await factory.environment_for(release_of(), port=8000)

    for name in (
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_TOKEN",
        "DATABRICKS_AUTH_TYPE",
        "DATABRICKS_CLI_PATH",
    ):
        assert name not in environment


async def test_a_refused_credential_stops_the_environment_with_the_reason() -> None:
    snapshot = shared_sp_snapshot(
        warehouse_resource("wh", access_as="shared_sp", check_result="unverified")
    )
    credentials, _ = credentials_over(snapshot)
    factory = ReleaseEnvironment(
        platform=PLATFORM, resources=NoResourceValues(), credentials=RefusingAdapter(credentials)
    )

    with pytest.raises(EnvironmentRefused, match="shared service principal"):
        await factory.environment_for(release_of(), port=8000)


async def test_an_environment_without_a_credential_source_is_what_it_was_before() -> None:
    factory = ReleaseEnvironment(platform=PLATFORM, resources=NoResourceValues())

    environment = await factory.environment_for(release_of(), port=8000)

    assert "DATABRICKS_CLIENT_ID" not in environment
    assert environment["DATABRICKS_APP_NAME"] == "sales"


def test_a_credential_never_overrides_a_variable_appmux_owns() -> None:
    # The layering is sandbox.environment's; one pinned case here because a credential is the one
    # input this lane adds to it.
    from appmux.sandbox.environment import ChildIdentity, appmux_variables

    identity = ChildIdentity(
        slug="sales",
        port=8000,
        workspace_host=PLATFORM.workspace_host,
        workspace_id=PLATFORM.workspace_id,
        parent_url=PLATFORM.parent_url,
        home_directory="/run/appmux/home",
        credentials={
            "DATABRICKS_HOST": "https://other.example.invalid",
            "DATABRICKS_CLIENT_ID": "x",
        },
    )

    variables = appmux_variables(identity)

    assert variables["DATABRICKS_HOST"] == PLATFORM.workspace_host
    assert variables["DATABRICKS_CLIENT_ID"] == "x"
    assert "https://other.example.invalid" not in repr(identity)
