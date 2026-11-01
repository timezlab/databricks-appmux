"""Starting data for the retention tests: an app with many releases, some locked, named or deleted.

Not a test module. ``reconciler_fakes.seed_release`` makes a release with neither a lock, a name
nor tags; the retention rules turn on exactly those, so this helper seeds them. All people are
placeholders.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from reconciler_fakes import NOW, digest_of

LOCKER = "alice@example.com"


def seed_numbered_release(
    registry: Any,
    release_no: int,
    status: str = "superseded",
    *,
    slug: str = "sales",
    locked: bool = False,
    label: str | None = None,
    tags: Sequence[str] = (),
    deleted: bool = False,
) -> None:
    """One release of ``slug``; a deleted one has no snapshot, as the table requires."""
    fields: dict[str, Any] = {
        "app_slug": slug,
        "release_no": release_no,
        "snapshot_digest": None if deleted else digest_of(release_no),
        "detected": {"runtime": "python", "command": ["python", "app.py"]},
        "wheels": []
        if deleted
        else [{"sha256": "c" * 64, "filename": "tool-1.0.whl", "source": "/w"}],
        "status": status,
        "status_at": NOW,
        "label": label,
        "tags": list(tags),
        "created_by": "alice@example.com",
        "created_at": NOW,
    }
    if locked:
        fields.update(locked_by=LOCKER, locked_at=NOW)
    if deleted:
        fields.update(deleted_by="alice@example.com", deleted_at=NOW)
    registry.seed("releases", fields)


def seed_releases(registry: Any, statuses: dict[int, str], *, locked: Sequence[int] = ()) -> None:
    """Several releases at once: number to status, with ``locked`` naming the hand-locked ones."""
    for release_no, status in statuses.items():
        seed_numbered_release(registry, release_no, status, locked=release_no in locked)


def seed_setting(registry: Any, key: str, value: object) -> None:
    registry.seed(
        "platform_settings",
        {"key": key, "value": value, "updated_by": "dana@example.com", "updated_at": NOW},
    )


def point_target_at(registry: Any, release_no: int | None, *, slug: str = "sales") -> None:
    """Replace the app's ``app_config`` row with one that targets ``release_no``."""
    registry.seed(
        "app_config",
        {
            "app_slug": slug,
            "desired_state": "running",
            "target_release_no": release_no,
            "updated_by": "dana@example.com",
            "updated_at": NOW,
        },
    )


def full_app_statuses(count: int = 10) -> dict[int, str]:
    """``count`` releases: the newest is active, the others are superseded (so unprotected)."""
    statuses = dict.fromkeys(range(1, count), "superseded")
    statuses[count] = "active"
    return statuses
