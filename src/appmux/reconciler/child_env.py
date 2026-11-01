"""The environment of a release's child: its entries, its resources, and what appmux adds.

The layering and the checks are ``sandbox.environment`` (pure, no I/O). This module is the part
that knows the registry: it reads the ``env`` entries of the release's manifest and of its config,
resolves the resources they name through the ``ResourceValues`` seam, and turns every refusal into
an ``EnvironmentRefused`` whose message is stored as the release's ``status_reason``.

It runs before the build, so a release that needs a resource the app does not have fails at once
with ``missing resources: <keys>`` and nothing is built (design 5.4, "Requirements"). A resolved
value is placed in the returned mapping and nowhere else: no message here names one.

The Databricks credential of the app's mode (design 8.5: none, its own service principal, or the
shared one) comes from the ``CredentialSource`` seam and goes into ``ChildIdentity.credentials``,
below the variables appmux owns, so a credential can never replace ``DATABRICKS_HOST`` and the
others. A source that refuses raises ``EnvironmentRefused``; the release then fails with that
message before anything is built.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from appmux.reconciler.children import DEFAULT_CHILD_PORT
from appmux.registry.models import EnvEntry as RegistryEnvEntry
from appmux.registry.models import Release
from appmux.sandbox.environment import (
    ChildEnvironmentError,
    ChildIdentity,
    appmux_variables,
    build_child_environment,
)
from appmux.sandbox.environment import EnvEntry as SandboxEnvEntry
from appmux.sandbox.run_dir import RUN_DIR_MOUNT_PATH

# The child's private writable directory is its run directory (design 6.4).
CHILD_HOME = f"{RUN_DIR_MOUNT_PATH}/home"


class EnvironmentRefused(Exception):
    """The environment cannot be built; the message is for a person and holds no value."""


class ResourcesMissing(EnvironmentRefused):
    """The release names resources that the app does not have, or that cannot be read."""

    def __init__(self, keys: Sequence[str]) -> None:
        self.keys = tuple(sorted(keys))
        super().__init__(f"missing resources: {', '.join(self.keys)}")


class ResourceValues(Protocol):
    """Resolves the resource keys a release names to the values its child receives."""

    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]:
        """A value for each of ``keys``; raises ``ResourcesMissing`` naming every key it lacks."""
        ...


class NoResources:
    """A source with no resources at all: any release that names one is missing it."""

    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]:
        if keys:
            raise ResourcesMissing(list(keys))
        return {}


class CredentialSource(Protocol):
    """The variables that give an app's child its Databricks credential (design 8.5)."""

    async def credentials_for(self, app_slug: str) -> Mapping[str, str]:
        """The credential variables of the app's mode; empty when it has no credential.

        Raises ``EnvironmentRefused`` when the mode is chosen but the credential cannot be given
        (a secret that cannot be read, a token that cannot be minted). The values are secrets: the
        caller puts them in the child's environment and nowhere else.
        """
        ...


class NoCredentials:
    """A source with no credential for any app: every child starts without one."""

    async def credentials_for(self, app_slug: str) -> Mapping[str, str]:
        return {}


@dataclass(frozen=True, slots=True)
class PlatformIdentity:
    """What the children are told about the workspace and about appmux's own address."""

    workspace_host: str
    workspace_id: str
    parent_url: str


class ChildEnvironmentFactory(Protocol):
    async def environment_for(self, release: Release, *, port: int) -> Mapping[str, str]:
        """The complete environment of the child of ``release``; ``EnvironmentRefused`` if none."""
        ...


class ReleaseEnvironment:
    def __init__(
        self,
        *,
        platform: PlatformIdentity,
        resources: ResourceValues,
        credentials: CredentialSource | None = None,
    ) -> None:
        self._platform = platform
        self._resources = resources
        self._credentials = credentials if credentials is not None else NoCredentials()

    async def environment_for(
        self, release: Release, *, port: int = DEFAULT_CHILD_PORT
    ) -> Mapping[str, str]:
        manifest_env = _sandbox_entries(release.detected.env)
        release_env = _sandbox_entries(release.config.env)
        keys = {entry.value_from for entry in (*manifest_env, *release_env) if entry.value_from}
        values = await self._resources.resolve(release.app_slug, keys) if keys else {}
        credentials = await self._credentials.credentials_for(release.app_slug)
        identity = ChildIdentity(
            slug=release.app_slug,
            port=port,
            workspace_host=self._platform.workspace_host,
            workspace_id=self._platform.workspace_id,
            parent_url=self._platform.parent_url,
            home_directory=CHILD_HOME,
            credentials=credentials,
        )
        try:
            return build_child_environment(
                app_yaml_env=manifest_env,
                release_env=release_env,
                appmux_variables=appmux_variables(identity),
                resource_values=values,
            )
        except ChildEnvironmentError as exc:
            raise EnvironmentRefused(str(exc)) from exc


def _sandbox_entries(entries: Sequence[RegistryEnvEntry]) -> list[SandboxEnvEntry]:
    return [
        SandboxEnvEntry(name=entry.name, value=entry.value, value_from=entry.value_from)
        for entry in entries
    ]
