"""Which Databricks credential a child gets, and the variables that carry it (T142; design 8.5).

A child has no Databricks credential unless one was delegated or attached. This module turns the
registry's answer for one app into the environment variables of its credential, and refuses when the
chosen credential cannot be given:

* **none** (the default): no variable. Also what an OBO delegation gives, because the visitor's
  token arrives per request in a header at the edge and is never stored here;
* **its own service principal**: ``app_config.own_sp_client_id_key`` and
  ``own_sp_client_secret_key`` name two ``secret`` resources, and their values become
  ``DATABRICKS_CLIENT_ID`` and ``DATABRICKS_CLIENT_SECRET``. The child can reach only what that
  service principal was granted, so it wins over a delegation: its reach is smaller;
* **the shared service principal** (delegated by a platform admin, and only while the platform
  master switch is on): the same two variables, set to the pair appmux itself runs as, so the child
  is as if deployed alone and its SDK signs in and renews by itself (ADR 0023). The child holds
  appmux's client secret: revoking the delegation takes it away from the next start, and does not
  take back a copy the child kept. An own service principal wins over this.

A personal access token is not a mode: none of the variables here is ``DATABRICKS_TOKEN``, and a
secret that looks like one is refused where it would be used as a client secret.

The returned mapping is a ``ChildVariables``: its ``repr`` lists names only, so a log line that
formats it holds no value. Every message of ``CredentialsRefused`` says what failed and what to do
next and holds no secret value; the reconciler stores it as the release's ``status_reason``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from typing import Final, Protocol

from pydantic import SecretStr

from appmux.registry.models import App, AppConfig
from appmux.registry.resources import SECRET_TYPE
from appmux.registry.snapshot import RegistrySnapshot
from appmux.sandbox.environment import ChildVariables
from appmux.secrets.resolver import MissingResourceKeys, SecretUnreadable
from appmux.settings import PlatformSettings

_SHARED_SP_SWITCH_KEY: Final = "shared_sp_delegation_allowed"
_CLIENT_ID_VARIABLE: Final = "DATABRICKS_CLIENT_ID"
_CLIENT_SECRET_VARIABLE: Final = "DATABRICKS_CLIENT_SECRET"  # noqa: S105 - a variable name
# A Databricks personal access token. The audit model refuses the same shape.
_PERSONAL_ACCESS_TOKEN: Final = re.compile(r"dapi[0-9a-fA-F]{16,}")


class CredentialsRefused(Exception):
    """The chosen credential cannot be given; ``message`` is for a person and holds no value."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ResourceReader(Protocol):
    """Resolves resource keys to values; ``secrets.resolver.ResourceValueResolver`` is one."""

    async def resolve(self, app_slug: str, keys: Collection[str]) -> Mapping[str, str]:
        """A value for each of ``keys``; raises ``MissingResourceKeys`` or ``SecretUnreadable``."""
        ...


def _shared_sp_allowed(snapshot: RegistrySnapshot) -> bool:
    """The master switch (data model 4.1); never set means the default, which is off."""
    row = snapshot.setting(_SHARED_SP_SWITCH_KEY)
    if row is not None and isinstance(row.value, bool):
        return row.value
    return PlatformSettings().shared_sp_delegation_allowed


class ChildCredentials:
    """The credential variables of the child of an app, read from the registry when it starts.

    ``snapshot`` is called on each ``credentials_for`` so a change since the last start is seen.
    ``appmux_client_id`` and ``appmux_client_secret`` are the pair appmux itself runs as (the
    Databricks Apps platform sets them for the App); only a shared-SP child is given them.
    """

    def __init__(
        self,
        *,
        snapshot: Callable[[], RegistrySnapshot],
        resources: ResourceReader,
        appmux_client_id: str,
        appmux_client_secret: SecretStr,
    ) -> None:
        self._snapshot = snapshot
        self._resources = resources
        self._appmux_client_id = appmux_client_id
        self._appmux_client_secret = appmux_client_secret

    async def credentials_for(self, app_slug: str) -> Mapping[str, str]:
        """The variables for ``app_slug``'s mode; empty when it has no credential or is unknown."""
        snapshot = self._snapshot()
        app, config = snapshot.app(app_slug), snapshot.app_config(app_slug)
        if app is None or config is None:
            return ChildVariables()
        if config.own_sp_client_id_key is not None or config.own_sp_client_secret_key is not None:
            return await self._own_service_principal(snapshot, app, config)
        if app.shared_sp_delegated and _shared_sp_allowed(snapshot):
            return self._shared_service_principal(snapshot, app)
        return ChildVariables()

    async def _own_service_principal(
        self, snapshot: RegistrySnapshot, app: App, config: AppConfig
    ) -> Mapping[str, str]:
        keys = (
            ("own_sp_client_id_key", config.own_sp_client_id_key),
            ("own_sp_client_secret_key", config.own_sp_client_secret_key),
        )
        for field, key in keys:
            if key is None:
                raise CredentialsRefused(
                    f"{field} is not set; the app's own service principal needs both "
                    "own_sp_client_id_key and own_sp_client_secret_key, so set the other one or "
                    "clear both, then start the release again"
                )
            resource = snapshot.resource(app.slug, key)
            if resource is None:
                raise CredentialsRefused(
                    f"{field} names resource '{key}', which the app does not have; attach the "
                    "service principal's client id and secret as secret resources, then start "
                    "the release again"
                )
            if resource.type != SECRET_TYPE:
                raise CredentialsRefused(
                    f"{field} names resource '{key}', which is not a secret; attach the "
                    "service principal's client id and secret as secret resources, then start "
                    "the release again"
                )
        client_id_key, client_secret_key = (key for _, key in keys if key is not None)
        try:
            values = await self._resources.resolve(app.slug, {client_id_key, client_secret_key})
        except MissingResourceKeys as exc:
            raise CredentialsRefused(
                "the app's own service principal names resources the app does not have: "
                f"{', '.join(exc.keys)}; attach them or change the names, then start the "
                "release again"
            ) from exc
        except SecretUnreadable as exc:
            raise CredentialsRefused(exc.message) from exc
        client_id, client_secret = values[client_id_key], values[client_secret_key]
        if _PERSONAL_ACCESS_TOKEN.fullmatch(client_id) or _PERSONAL_ACCESS_TOKEN.fullmatch(
            client_secret
        ):
            raise CredentialsRefused(
                "a personal access token is not accepted as the app's own service principal; "
                "use the service principal's OAuth client id and client secret, then start the "
                "release again"
            )
        return ChildVariables(
            {_CLIENT_ID_VARIABLE: client_id, _CLIENT_SECRET_VARIABLE: client_secret}
        )

    def _shared_service_principal(self, snapshot: RegistrySnapshot, app: App) -> Mapping[str, str]:
        for resource in snapshot.resources_for(app.slug):
            if resource.access_as == "shared_sp" and resource.check_result != "ok":
                raise CredentialsRefused(
                    f"resource '{resource.key}' is to be used as the shared service principal, "
                    f"but its access check is {resource.check_result}; a platform admin should "
                    "give the shared service principal access to it and attach it again, then "
                    "start the release again"
                )
        return ChildVariables(
            {
                _CLIENT_ID_VARIABLE: self._appmux_client_id,
                _CLIENT_SECRET_VARIABLE: self._appmux_client_secret.get_secret_value(),
            }
        )
