"""Registry data for releases: the ``Change`` values the Update flow and the reconciler write.

The appmux service principal writes releases (data model 6), so none of these needs an admin
token. The table, the key and the columns of ``releases`` are named here once, the way
``grants.py`` names them for ``app_grants``.

Four rules the builders keep for their callers:

* A status step goes through ``reconciler.states.check_transition``, so a step the state machine
  forbids never reaches the registry. ``by_hand`` is the person re-activating a ``failed`` or
  ``superseded`` release.
* Every status step writes ``status_reason`` and ``build_log_tail`` both, ``NULL`` when the step
  has none. A reason belongs to the step that gave it: a release that goes back to ``pending``
  must not keep the reason of the failure it is retrying.
* A deleted release (data model rule 8: the row stays, its pointers go) takes no status step: it
  has no snapshot to run, so it cannot be activated or retried.
* Text is cleaned before it is written: a NUL byte makes the registry refuse the row
  (``InvalidRow``), and a child can print anything, so a log tail is cut to the last
  ``LOG_TAIL_LINES`` lines (``START_LOG_TAIL_LINES`` for a failed start) and
  ``MAX_LOG_TAIL_CHARS`` characters, and the credential shapes of the audit log are masked in it
  (``registry.audit.mask_credentials``). Removing the child's own secret values from a log is the
  caller's job: only the caller knows the values.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Final

from appmux.reconciler.states import IllegalTransition, check_transition
from appmux.registry.audit import mask_credentials
from appmux.registry.models import (
    AppConfig,
    DetectedManifest,
    Release,
    ReleaseConfig,
    WheelEntry,
)
from appmux.registry.protocol import Change
from appmux.registry.snapshot import RegistrySnapshot
from appmux.registry.writers import WriteKind

# "The last 40 lines of the build, kept for failed releases" (data model 4.6).
LOG_TAIL_LINES: Final = 40
# A failed start keeps the child's last 50 lines of output (contracts/admin-api.md, addendum).
START_LOG_TAIL_LINES: Final = 50
MAX_LOG_TAIL_CHARS: Final = 8000
MAX_REASON_CHARS: Final = 500
_ELLIPSIS: Final = "..."
_NUL: Final = "\x00"


def next_release_no(snapshot: RegistrySnapshot, app_slug: str) -> int:
    """One past the highest number the app ever used; a deleted release keeps its number.

    A clash with a concurrent Update is a unique violation of the table, which the caller
    answers by reading the snapshot again and asking for the number once more.
    """
    return max((release.release_no for release in snapshot.releases_for(app_slug)), default=0) + 1


def insert_release_change(
    *,
    app_slug: str,
    release_no: int,
    snapshot_digest: str,
    detected: DetectedManifest,
    config: ReleaseConfig,
    created_by: str,
    failed_reason: str | None = None,
    wheels: Sequence[WheelEntry] = (),
) -> Change:
    """The write that creates a release; the database fills the rest.

    The release is ``pending`` unless ``failed_reason`` is given: then it is stored ``failed`` with
    that reason, so the reconciler (which only picks up ``pending`` releases) never builds it. The
    requirements check uses that for an Update whose ``valueFrom`` keys the app lacks.

    ``wheels`` is the release's wheel list, written once here (data model 4.6: "set once, by the
    install") and read by every later build, restart and rollback. It is sent as plain JSON data,
    the way ``plan_config_edit`` copies it, and left out when empty so the column keeps its default.
    """
    values: dict[str, object] = {
        "app_slug": app_slug,
        "release_no": release_no,
        "snapshot_digest": snapshot_digest,
        "detected": detected,
        "config": config,
        "created_by": created_by,
    }
    if wheels:
        values["wheels"] = [wheel.model_dump(mode="json") for wheel in wheels]
    if failed_reason is not None:
        values["status"] = "failed"
        values["status_reason"] = clean_reason(failed_reason)
    return Change(WriteKind.RELEASE_INSERT, values=values)


def release_status_change(
    release: Release,
    new_status: str,
    *,
    reason: str | None = None,
    log_tail: str | None = None,
    log_tail_lines: int = LOG_TAIL_LINES,
    by_hand: bool = False,
) -> Change:
    """The write that moves ``release`` to ``new_status`` (``IllegalTransition`` if forbidden).

    ``reason`` and ``log_tail`` replace the stored ones (``None`` clears them), the tail cut to its
    last ``log_tail_lines`` lines; the registry stamps ``status_at``. A deleted release takes no
    step (``IllegalTransition``).
    """
    if release.deleted_at is not None:
        raise IllegalTransition(
            f"release {release.release_no} was deleted; a deleted release cannot be activated"
        )
    check_transition(release.status, new_status, by_hand=by_hand)
    return Change(
        WriteKind.RELEASE_UPDATE,
        key={"app_slug": release.app_slug, "release_no": release.release_no},
        values={
            "status": new_status,
            "status_reason": clean_reason(reason) if reason else None,
            "build_log_tail": clean_log_tail(log_tail, lines=log_tail_lines) if log_tail else None,
        },
    )


def activation_changes(new: Release, old: Release | None) -> list[Change]:
    """The swap's transaction: ``new`` (healthy) becomes active and ``old`` (active) superseded.

    The old release goes first. The table allows one ``active`` release per app after every
    statement (the partial unique index ``one_active_release`` is not deferrable), so activating
    the new release while the old one is still active would be refused.
    """
    changes: list[Change] = []
    if old is not None:
        changes.append(release_status_change(old, "superseded"))
    changes.append(release_status_change(new, "active"))
    return changes


def _release_key(release: Release) -> dict[str, object]:
    return {"app_slug": release.app_slug, "release_no": release.release_no}


def config_guard_change(config: AppConfig) -> Change:
    """A write of the app's ``app_config`` row that changes nothing but its version.

    Placed first in a transaction, it makes the version check and the write one step: the row lock
    it takes holds every other writer of the same guard until this transaction ends, and a writer
    that read an older version gets ``ChangedByOthers``. That is how counting the locks of an app
    (and the releases a cap may delete) is made safe against a second person doing the same at
    the same moment; the ``Registry`` protocol has no ``SELECT ... FOR UPDATE`` of its own. The
    columns are written with the values they already have, so ``updated_by`` keeps naming the last
    person who really changed the settings (``updated_at`` and ``version`` move, as for any update).
    """
    return Change(
        WriteKind.APP_CONFIG_UPDATE,
        key={"app_slug": config.app_slug},
        values={"updated_by": config.updated_by},
        expected_version=config.version,
    )


def label_and_tags_change(
    release: Release, *, name: str | None, tags: Sequence[str]
) -> Change | None:
    """The write that gives ``release`` this name and these tags; ``None`` when both are as stored.

    Tags are a set: the same tags in another order change nothing, so the stored order stays.
    """
    values: dict[str, object] = {}
    if name != release.label:
        values["label"] = name
    if set(tags) != set(release.tags):
        values["tags"] = list(tags)
    if not values:
        return None
    return Change(WriteKind.RELEASE_UPDATE, key=_release_key(release), values=values)


def lock_change(release: Release, *, locked_by: str, locked_at: datetime) -> Change:
    """The write that records who locked ``release`` by hand, and when."""
    return Change(
        WriteKind.RELEASE_UPDATE,
        key=_release_key(release),
        values={"locked_by": locked_by, "locked_at": locked_at},
    )


def unlock_change(release: Release) -> Change:
    """The write that clears the hand lock of ``release``."""
    return Change(
        WriteKind.RELEASE_UPDATE,
        key=_release_key(release),
        values={"locked_by": None, "locked_at": None},
    )


def soft_delete_change(release: Release, *, deleted_by: str, deleted_at: datetime) -> Change:
    """The write that deletes ``release``: its pointers go and the row stays (data model rule 8).

    The snapshot digest, the wheel list and the tags are cleared, so the content-store clean-up
    finds the files unreferenced; the name and the status stay, and so does the number, which is
    never given again (``next_release_no`` counts deleted rows). The table requires a deleted row
    to have no snapshot digest and a deleter, and a release to be unlocked before it is deleted.
    """
    return Change(
        WriteKind.RELEASE_UPDATE,
        key=_release_key(release),
        values={
            "deleted_by": deleted_by,
            "deleted_at": deleted_at,
            "snapshot_digest": None,
            "wheels": [],
            "tags": [],
        },
    )


def clean_reason(text: str) -> str:
    """``text`` without NUL bytes, cut to ``MAX_REASON_CHARS`` characters."""
    cleaned = text.replace(_NUL, "")
    if len(cleaned) <= MAX_REASON_CHARS:
        return cleaned
    return cleaned[: MAX_REASON_CHARS - len(_ELLIPSIS)] + _ELLIPSIS


def clean_log_tail(text: str, *, lines: int = LOG_TAIL_LINES) -> str | None:
    """The last ``lines`` lines of ``text``, masked, for ``build_log_tail``; ``None`` if empty."""
    kept = mask_credentials(text.replace(_NUL, "")).strip().splitlines()
    tail = "\n".join(kept[-lines:])
    if len(tail) > MAX_LOG_TAIL_CHARS:
        tail = tail[-MAX_LOG_TAIL_CHARS:]
    return tail or None
