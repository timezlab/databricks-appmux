"""Row builders for the request tests: apps, requests and a snapshot to hold them.

Lane-local on purpose: ``tests/contract/stub_registry.py`` (a hot file) seeds rows for the contract
test; the unit tests of the catalogue and of approval only need a ``RegistrySnapshot``.
Every e-mail is a placeholder.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Final

from appmux.registry.models import App, PermissionRequest, PlatformSetting
from appmux.registry.snapshot import RegistrySnapshot, build_snapshot

NOW: Final = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
ALICE: Final = "alice@example.com"  # owner of sales
CAROL: Final = "carol@example.com"  # CAN_MANAGE on sales
DANA: Final = "dana@example.com"  # platform admin
GRACE: Final = "grace@example.com"  # a visitor with no app


def make_app(
    slug: str = "sales",
    *,
    owner: str = ALICE,
    display_name: str = "Sales",
    source_path: str | None = None,
    memory_budget_mb: int = 256,
    obo: bool = False,
    shared_sp: bool = False,
    version: int = 1,
) -> App:
    return App(
        slug=slug,
        display_name=display_name,
        owner=owner,
        source_path=source_path or f"/Workspace/Shared/apps/{slug}",
        memory_budget_mb=memory_budget_mb,
        obo_delegated=obo,
        shared_sp_delegated=shared_sp,
        version=version,
        created_by=DANA,
        created_at=NOW,
        updated_by=DANA,
        updated_at=NOW,
    )


def make_request(
    request_id: int = 1,
    *,
    kind: str,
    payload: Mapping[str, object],
    app_slug: str | None = "sales",
    requested_by: str = CAROL,
    reason: str = "the team needs it",
    status: str = "pending",
) -> PermissionRequest:
    closed: dict[str, object] = {}
    if status != "pending":
        closed = {"closed_by": DANA, "closed_at": NOW}
    return PermissionRequest.model_validate(
        {
            "request_id": request_id,
            "app_slug": app_slug,
            "kind": kind,
            "payload": dict(payload),
            "reason": reason,
            "requested_by": requested_by,
            "requested_at": NOW,
            "status": status,
            **closed,
        }
    )


def make_snapshot(
    *,
    apps: Iterable[App] = (),
    requests: Iterable[PermissionRequest] = (),
    shared_sp_allowed: bool = True,
) -> RegistrySnapshot:
    settings = PlatformSetting(
        key="shared_sp_delegation_allowed", value=shared_sp_allowed, updated_by=DANA, updated_at=NOW
    )
    return build_snapshot(
        {
            "apps": list(apps),
            "permission_requests": list(requests),
            "platform_settings": [settings],
        },
        loaded_at=NOW,
    )
