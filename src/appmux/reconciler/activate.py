"""Activate any live release (T106, T128; FR-006, design 5.5): the roll back and roll forward.

Activating is a decision about rows. This module turns "make release N run" into the writes that
say so, after the checks that must come first; the reconciler then does what it does for any
``pending`` target: build from the **stored** snapshot, wheels and config, start, health check,
swap.
Nothing here reads the source folder or needs the person's token, which is why an older release
still comes back after its source was deleted or the person left (FR-006):

1. the app and the release exist and the release is live (a deleted one has no snapshot: ``404``);
2. the requirements check runs over the **stored** release row before any start or build, and
   answers with every missing resource key at once (FR-035). The keys are attached after the
   release was made, so this is where a release that once ran can be refused;
3. a release in flight (``building``, ``starting``, ``healthy``) is refused: it is on its way up;
4. a ``failed`` or ``superseded`` release moves to ``pending`` by hand (the one transition people
   may make, ``states.py``), which also clears its old reason and log; an ``active`` or ``pending``
   one is left as it is;
5. the release becomes the app's target and the app is asked to run.

A release that never passed its health check can be activated the same way: ``failed`` goes back to
``pending`` and gets a fresh attempt. Admission control (memory) is the reconciler's own, applied
when the copy starts; the second copy of an app that is already running counts like any Update's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from appmux.appyaml.requirements import check_release_requirements
from appmux.registry.models import Release
from appmux.registry.operations import target_release_change
from appmux.registry.protocol import Change
from appmux.registry.releases import release_status_change
from appmux.registry.snapshot import RegistrySnapshot

_IN_FLIGHT = frozenset({"building", "starting", "healthy"})
_REVIVABLE = frozenset({"failed", "superseded"})


class ActivationRefused(Exception):
    """Activation stopped before anything was written.

    ``kind`` says how the caller answers: ``not_found`` (no such app or live release) or
    ``conflict`` (the release cannot go live now). ``missing_keys`` lists the resource keys a
    ``requirements_missing`` refusal is about; the message names them and never any value.
    """

    def __init__(
        self,
        kind: Literal["not_found", "conflict"],
        message: str,
        *,
        code: str,
        missing_keys: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.kind: Literal["not_found", "conflict"] = kind
        self.message = message
        self.code = code
        self.missing_keys = missing_keys


@dataclass(frozen=True, slots=True)
class Activation:
    """What activating a release writes; ``is_serving``: the release is the active one already."""

    release: Release
    changes: tuple[Change, ...]
    is_serving: bool


def plan_activation(
    snapshot: RegistrySnapshot, app_slug: str, release_no: int, *, updated_by: str
) -> Activation:
    """The writes that make ``release_no`` the app's next release, or the refusal."""
    config = snapshot.app_config(app_slug)
    release = snapshot.release(app_slug, release_no)
    if config is None or release is None or release.deleted_at is not None:
        raise ActivationRefused("not_found", "no such release", code="not_found")

    if release.status != "active":
        check = check_release_requirements(
            release, resource_keys=[resource.key for resource in snapshot.resources_for(app_slug)]
        )
        if not check.is_met:
            raise ActivationRefused(
                "conflict",
                f"{check.message()}; attach them on the Requirements panel, then activate again",
                code="requirements_missing",
                missing_keys=check.missing,
            )
    if release.status in _IN_FLIGHT:
        raise ActivationRefused(
            "conflict",
            f"release {release_no} is being brought up; wait until it is active or has failed",
            code="release_in_progress",
        )

    changes: list[Change] = []
    if release.status in _REVIVABLE:
        changes.append(release_status_change(release, "pending", by_hand=True))
    changes.append(target_release_change(config, release_no, updated_by=updated_by))
    return Activation(release, tuple(changes), is_serving=release.status == "active")
