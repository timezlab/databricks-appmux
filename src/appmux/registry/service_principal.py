"""Stop every running app when appmux's own service principal is not the one it last ran as.

A Databricks App has one service principal (SP), made with the App. Delete the App and create it
again and the new SP has none of the old grants (a secret scope, a warehouse, a Unity Catalog
privilege), yet the registry survives and says every app is ``running``. Starting them all would
run them under an SP that has none of their access, and an app that only uses the shared-SP token
at run time would pass its health check and then fail with 403s nobody sees (ADR 0022, FR-055).

So, before any app starts, appmux compares the SP it runs as now with the one in its newest
``platform.service_principal`` audit row. A different one stops every running app in one
transaction; the apps' admins give the new SP what their app needs and press Start. appmux grants
nothing (design 8.2). No earlier row stops nothing, so a first install, or the first start after
upgrading to this release, only gets the row written.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Final

from pydantic import TypeAdapter, ValidationError

from appmux.registry.audit import AuditDetail, AuditEntry, ClientId
from appmux.registry.models import AppConfig, AuditLogRow
from appmux.registry.operations import desired_state_change
from appmux.registry.protocol import Registry

logger = logging.getLogger(__name__)

SERVICE_PRINCIPAL_ACTION: Final = "platform.service_principal"
# The same actor as the retention clean-up: appmux acting on its own, with nobody signed in.
SERVICE_PRINCIPAL_ACTOR: Final = "appmux"
STOP_REASON: Final = "the appmux service principal changed"

# How many of the newest rows are read. A row that is not usable (a forged or malformed one) is
# skipped, so a few are asked for and the first usable one is taken.
_ROWS_READ: Final = 5
_CLIENT_ID: Final = TypeAdapter(ClientId)


async def stop_apps_when_the_service_principal_changed(
    registry: Registry, *, client_id: str
) -> tuple[str, ...]:
    """Record ``client_id`` and, if it is not the one recorded last, stop every running app.

    Returns the slugs it stopped. Raises when the read or the write fails, or when the stop was
    committed but the registry's snapshot still shows an app running: the start-up then stops and
    the gate stays shut, because starting the apps without having decided would run them under an
    SP that has none of their access.
    """
    rows = await registry.read_audit_of(
        actor=SERVICE_PRINCIPAL_ACTOR, action=SERVICE_PRINCIPAL_ACTION, limit=_ROWS_READ
    )
    remembered = _newest_usable_client_id(rows)
    if remembered == client_id:
        return ()

    running = _running_apps(registry) if remembered is not None else []
    changes = [
        desired_state_change(config, "stopped", updated_by=SERVICE_PRINCIPAL_ACTOR)
        for config in running
    ]
    audit = [
        AuditEntry(
            actor=SERVICE_PRINCIPAL_ACTOR,
            action="app.stop",
            app_slug=config.app_slug,
            reason=STOP_REASON,
            automatic=True,
        )
        for config in running
    ]
    audit.append(
        AuditEntry(
            actor=SERVICE_PRINCIPAL_ACTOR,
            action="platform.service_principal",
            detail=AuditDetail(client_id=client_id, previous_client_id=remembered),
            automatic=True,
        )
    )
    await registry.write(changes, audit)

    stopped = tuple(config.app_slug for config in running)
    if stopped:
        logger.warning(
            "the appmux service principal changed, so %d running app(s) were stopped: %s",
            len(stopped),
            ", ".join(stopped),
        )
        _require_stopped(registry, stopped)
    return stopped


def _newest_usable_client_id(rows: Iterable[AuditLogRow]) -> str | None:
    """The ``client_id`` of the first row (newest first) that has one in the shape of an id."""
    for row in rows:
        try:
            return _CLIENT_ID.validate_python(row.detail.get("client_id"))
        except ValidationError:
            continue
    return None


def _running_apps(registry: Registry) -> list[AppConfig]:
    return [config for config in registry.snapshot.app_configs if config.desired_state == "running"]


def _require_stopped(registry: Registry, slugs: Iterable[str]) -> None:
    """Fail the start if the snapshot still shows an app as running after the stop was written.

    A write is committed even when the reload after it fails (``RegistryClient.write``), and the
    reconciler reads the snapshot, so a stale one would start the apps this step just stopped.
    """
    snapshot = registry.snapshot
    still_running = [
        slug
        for slug in slugs
        if (config := snapshot.app_config(slug)) is not None and config.desired_state == "running"
    ]
    if still_running:
        raise RuntimeError(
            "the apps were stopped in the registry, but the snapshot still shows "
            f"{', '.join(still_running)} as running; retry the start so no app starts under the "
            "new service principal"
        )
