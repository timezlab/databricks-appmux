"""The release state machine (FR-005, design 5.1): which status may follow which.

A release is ``pending`` until the reconciler picks it up, then ``building`` (fetch, packages, build
the environment), ``starting`` (process launched, health check running), ``healthy`` (answered the
health check, waiting for the swap) and finally ``active`` (the route points at it). It ends as
``failed`` (with a reason) or, when a newer release replaced it, ``superseded``.

The table is data so one test can walk every pair. Two rules need a reason:

* A release interrupted before it was ``active`` (a crash, a restart of appmux itself) goes back to
  ``pending``: nothing was routed to it, so starting over is safe and idempotent (constitution II).
* ``failed`` is never retried by the reconciler, because retrying a deterministic failure burns
  the container's memory budget for nothing. A person can still activate a ``failed`` or a
  ``superseded`` release, which moves it to ``pending`` ("by hand", design 5.1 and FR-005).

``active`` may fail: when appmux restarts, the environment may no longer hold a package the release
needs (design 5.3), and the release is marked failed instead of being routed to.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

ReleaseStatus = Literal[
    "pending", "building", "starting", "healthy", "active", "failed", "superseded"
]
RELEASE_STATUSES: Final[tuple[str, ...]] = get_args(ReleaseStatus)

_AUTOMATIC: Final[dict[str, frozenset[str]]] = {
    "pending": frozenset({"building", "failed"}),
    "building": frozenset({"starting", "failed", "pending"}),
    "starting": frozenset({"healthy", "failed", "pending"}),
    "healthy": frozenset({"active", "failed", "pending"}),
    "active": frozenset({"superseded", "failed"}),
    "failed": frozenset(),
    "superseded": frozenset(),
}
_BY_HAND_ONLY: Final[dict[str, frozenset[str]]] = {
    "failed": frozenset({"pending"}),
    "superseded": frozenset({"pending"}),
}


class IllegalTransition(ValueError):
    """The release cannot move from one status to the other."""


def can_transition(old: str, new: str, *, by_hand: bool = False) -> bool:
    """Whether ``old`` -> ``new`` is a legal step; ``by_hand`` adds the person's re-activation."""
    if new in _AUTOMATIC.get(old, frozenset()):
        return True
    return by_hand and new in _BY_HAND_ONLY.get(old, frozenset())


def check_transition(old: str, new: str, *, by_hand: bool = False) -> None:
    """Raise ``IllegalTransition`` unless ``old`` -> ``new`` is legal."""
    if can_transition(old, new, by_hand=by_hand):
        return
    if new in _BY_HAND_ONLY.get(old, frozenset()):
        raise IllegalTransition(
            f"a release goes from '{old}' to '{new}' only by hand (a person activating it), "
            "never automatically"
        )
    raise IllegalTransition(f"a release cannot go from '{old}' to '{new}'")


def is_picked_up_automatically(status: str) -> bool:
    """Whether the reconciler starts work on a release in this status without being asked."""
    return status == "pending"
