"""Which releases an app keeps: the protection rules, the cap and the hand locks (US5).

Data model rules 8 to 10 and 13 are the source of the rules below.

An app keeps at most ``max_releases_per_app`` releases (10 by default). A release is **protected**,
and never deleted by the cap, while it is

* the **active** release (the route points at it);
* the app's **target** (``app_config.target_release_no``), whatever its status;
* **in progress** (``pending``, ``building``, ``starting`` or ``healthy``: a build or a swap is
  using it);
* **locked** by hand (at most ``max_locked_releases``, 3 by default).

A tag or a name protects nothing. A deleted release is not counted and is protected from nothing.

Design 5.2 puts the clean-up *before* the new release is created, so the old target is still the
target (and so protected) while the oldest unprotected release goes. A full cap with every release
protected refuses the Update instead of deleting something a person chose to keep.

Everything here is pure: it reads a ``RegistrySnapshot`` and returns the ``Change`` values to write
and the audit entries that go with them. The callers write them: the Update flow
(``reconciler/update.py``, as the ``appmux`` actor) and the release routes (``admin/releases.py``,
as the person).

Counting is only safe if two people cannot count at once. Every plan that depends on a count (a
lock, a delete, the clean-up) starts with ``config_guard_change``, a write of the app's
``app_config`` row at the version the plan read. Two plans made from the same snapshot cannot both
commit: the second one gets ``ChangedByOthers`` (``409``) and reads again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from appmux.registry.audit import AuditDetail, AuditEntry
from appmux.registry.models import AppConfig, Release
from appmux.registry.protocol import Change
from appmux.registry.releases import (
    config_guard_change,
    lock_change,
    soft_delete_change,
    unlock_change,
)
from appmux.registry.snapshot import RegistrySnapshot
from appmux.settings import RELEASES_KEPT_FREE, PlatformSettings

IN_PROGRESS_STATUSES: Final = frozenset({"pending", "building", "starting", "healthy"})
# The actor of a deletion nobody asked for: the audit model's "appmux for automatic clean-up".
RETENTION_ACTOR: Final = "appmux"
_RETENTION_REASON: Final = "retention"
_MAX_RELEASES_KEY: Final = "max_releases_per_app"
_MAX_LOCKED_KEY: Final = "max_locked_releases"

# The ``code`` of a refusal, which a route puts in its ``409`` for the page to branch on.
PROTECTED: Final = "release_protected"
LOCK_LIMIT_REACHED: Final = "lock_limit_reached"
CAP_FULL: Final = "release_cap_full"


class RetentionRefused(Exception):
    """The action would break a retention rule; the message tells the person what to do next."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RetentionLimits:
    max_releases: int
    max_locked: int


@dataclass(frozen=True, slots=True)
class LockPlan:
    """The writes of a lock or an unlock and how many hand locks the app holds afterwards."""

    changes: tuple[Change, ...]
    locks_after: int
    max_locked: int

    def locks_text(self) -> str:
        """``"2 of 3"``: the audit detail's form."""
        return f"{self.locks_after} of {self.max_locked}"


# --- reading the rules --------------------------------------------------------------------------


def retention_limits(snapshot: RegistrySnapshot) -> RetentionLimits:
    """The cap and the lock limit in force: the stored setting, else the default.

    A stored value that is not a whole number in its allowed range counts as unset. The registry
    does not let the admin API store one (``PlatformSettings`` refuses it), so this only guards
    against a row edited by hand; a bad value must neither stop every Update nor lift a limit.
    """
    defaults = PlatformSettings()
    max_releases = _whole_setting(
        snapshot,
        _MAX_RELEASES_KEY,
        minimum=RELEASES_KEPT_FREE,
        default=defaults.max_releases_per_app,
    )
    max_locked = _whole_setting(
        snapshot, _MAX_LOCKED_KEY, minimum=0, default=defaults.max_locked_releases
    )
    # The rule between the two settings: a cap always leaves room to clean up.
    return RetentionLimits(
        max_releases=max_releases, max_locked=min(max_locked, max_releases - RELEASES_KEPT_FREE)
    )


def _whole_setting(snapshot: RegistrySnapshot, key: str, *, minimum: int, default: int) -> int:
    row = snapshot.setting(key)
    value = row.value if row is not None else None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return default
    return value


def live_releases(snapshot: RegistrySnapshot, app_slug: str) -> tuple[Release, ...]:
    """The app's releases that are not deleted, oldest first."""
    return tuple(r for r in snapshot.releases_for(app_slug) if r.deleted_at is None)


def hand_locks(releases: tuple[Release, ...]) -> int:
    """How many of ``releases`` are locked by hand (the active release holds no lock of its own)."""
    return sum(1 for release in releases if release.locked_at is not None)


def protections(release: Release, *, target_release_no: int | None) -> tuple[str, ...]:
    """Why ``release`` may not be deleted; empty when nothing protects it."""
    reasons: list[str] = []
    if release.locked_at is not None:
        reasons.append("locked")
    if release.status == "active":
        reasons.append("active")
    if release.status in IN_PROGRESS_STATUSES:
        reasons.append("in progress")
    if release.release_no == target_release_no:
        reasons.append("target")
    return tuple(reasons)


def _target_of(snapshot: RegistrySnapshot, app_slug: str) -> int | None:
    config = snapshot.app_config(app_slug)
    return config.target_release_no if config is not None else None


def _config_of(snapshot: RegistrySnapshot, app_slug: str) -> AppConfig:
    config = snapshot.app_config(app_slug)
    if config is None:
        # Every app has its settings row from the moment it exists; callers check the app first.
        raise ValueError(f"app '{app_slug}' has no settings row to guard a retention change with")
    return config


def _live_release(snapshot: RegistrySnapshot, app_slug: str, release_no: int, verb: str) -> Release:
    release = snapshot.release(app_slug, release_no)
    if release is None:
        raise ValueError(f"app '{app_slug}' has no release {release_no}")
    if release.deleted_at is not None:
        raise RetentionRefused(
            f"release {release_no} was deleted; a deleted release cannot be {verb}", code=PROTECTED
        )
    return release


# --- the cap ------------------------------------------------------------------------------------


def releases_to_delete(snapshot: RegistrySnapshot, app_slug: str) -> tuple[Release, ...]:
    """The oldest unprotected releases to delete to make room for one more; ``()`` when it fits.

    Normally one release, at the cap. After the cap was lowered, enough of them to bring the app
    to the new cap with the new release counted. ``RetentionRefused`` (and nothing deleted) when
    the unprotected releases are too few.
    """
    limits = retention_limits(snapshot)
    live = live_releases(snapshot, app_slug)
    missing_room = len(live) + 1 - limits.max_releases
    if missing_room <= 0:
        return ()
    target = _target_of(snapshot, app_slug)
    unprotected = [r for r in live if not protections(r, target_release_no=target)]
    if len(unprotected) >= missing_room:
        return tuple(unprotected[:missing_room])
    if not unprotected:
        raise RetentionRefused(
            f"app '{app_slug}' has {len(live)} releases and all are protected "
            "(active, target or locked); unlock one",
            code=CAP_FULL,
        )
    raise RetentionRefused(
        f"app '{app_slug}' has {len(live)} releases, over its limit of {limits.max_releases}, "
        f"and only {len(unprotected)} of them are unprotected "
        "(not active, target, in progress or locked); unlock some",
        code=CAP_FULL,
    )


def cleanup_changes(
    snapshot: RegistrySnapshot,
    app_slug: str,
    doomed: tuple[Release, ...],
    *,
    deleted_by: str,
    now: datetime,
) -> list[Change]:
    """One transaction that deletes ``doomed``, behind the guard of the app's settings row."""
    return [
        config_guard_change(_config_of(snapshot, app_slug)),
        *(soft_delete_change(r, deleted_by=deleted_by, deleted_at=now) for r in doomed),
    ]


def cleanup_audit(
    app_slug: str, doomed: tuple[Release, ...], *, max_releases: int
) -> list[AuditEntry]:
    """One ``release.delete`` by ``appmux`` for each release the cap removed."""
    return [
        AuditEntry(
            actor=RETENTION_ACTOR,
            action="release.delete",
            app_slug=app_slug,
            detail=AuditDetail(release=r.release_no, by=_RETENTION_REASON, cap=max_releases),
        )
        for r in doomed
    ]


# --- deleting by hand ---------------------------------------------------------------------------


def delete_changes(
    snapshot: RegistrySnapshot, app_slug: str, release_no: int, *, deleted_by: str, now: datetime
) -> list[Change]:
    """The writes that delete one release by hand; ``RetentionRefused`` when it is protected."""
    release = _live_release(snapshot, app_slug, release_no, "deleted again")
    reasons = protections(release, target_release_no=_target_of(snapshot, app_slug))
    if reasons:
        raise RetentionRefused(_protected_message(release, reasons[0]), code=PROTECTED)
    return cleanup_changes(snapshot, app_slug, (release,), deleted_by=deleted_by, now=now)


def _protected_message(release: Release, reason: str) -> str:
    number = release.release_no
    if reason == "locked":
        return f"release {number} is locked; unlock it first"
    if reason == "active":
        return f"release {number} is the active release; activate another release first"
    if reason == "in progress":
        return f"release {number} is in progress ({release.status}); wait for it to finish"
    return f"release {number} is the target of the app; activate another release first"


# --- hand locks ---------------------------------------------------------------------------------


def lock_changes(
    snapshot: RegistrySnapshot, app_slug: str, release_no: int, *, locked_by: str, now: datetime
) -> LockPlan:
    """The writes that lock a release by hand; ``RetentionRefused`` at the lock limit.

    The release must not be locked already (the caller treats that as done).
    """
    release = _live_release(snapshot, app_slug, release_no, "locked")
    if release.locked_at is not None:
        raise ValueError(f"release {release_no} is locked already; there is nothing to plan")
    limits = retention_limits(snapshot)
    held = hand_locks(live_releases(snapshot, app_slug))
    if limits.max_locked == 0:
        raise RetentionRefused(
            f"app '{app_slug}' cannot lock releases: {_MAX_LOCKED_KEY} is 0; ask a platform admin",
            code=LOCK_LIMIT_REACHED,
        )
    if held >= limits.max_locked:
        noun = "release" if held == 1 else "releases"
        raise RetentionRefused(
            f"app '{app_slug}' already has {held} locked {noun}; unlock one first",
            code=LOCK_LIMIT_REACHED,
        )
    return LockPlan(
        changes=(
            config_guard_change(_config_of(snapshot, app_slug)),
            lock_change(release, locked_by=locked_by, locked_at=now),
        ),
        locks_after=held + 1,
        max_locked=limits.max_locked,
    )


def unlock_changes(snapshot: RegistrySnapshot, app_slug: str, release_no: int) -> LockPlan:
    """The writes that clear a hand lock; the release must be locked.

    No guard: removing a lock can only leave more room, so two people unlocking at once need no
    order, and a delete that read the release as locked is refused, never misled.
    """
    release = _live_release(snapshot, app_slug, release_no, "unlocked")
    if release.locked_at is None:
        raise ValueError(f"release {release_no} is not locked; there is nothing to plan")
    held = hand_locks(live_releases(snapshot, app_slug))
    return LockPlan(
        changes=(unlock_change(release),),
        locks_after=held - 1,
        max_locked=retention_limits(snapshot).max_locked,
    )
