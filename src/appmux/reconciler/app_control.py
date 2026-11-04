"""What the admin routes may ask of the reconciler about one running app (T106, T107).

The admin module never imports the reconciler's loop. It asks through this protocol, which
``Reconciler`` implements: whether the app runs, restart it beside the running copy, tell it that
an app now has another slug, and stop an app that is about to be deleted. The registry stays the
source of truth for everything else: a start, a stop or an activation is a row the route writes,
and ``wake`` asks for a pass over it.
"""

from __future__ import annotations

from typing import Literal, Protocol

# The same five words as ``admin.schemas.AppState``; this module may not import the admin package.
RunState = Literal["stopped", "starting", "running", "crashed", "no_release"]


class AppControl(Protocol):
    def run_state(self, slug: str, desired_state: str) -> RunState:
        """How the app runs now, given what the registry says it should do."""
        ...

    def request_restart(self, slug: str) -> None:
        """Start a new copy of the app's active release beside the running one and swap to it.

        A copy that fails to come up leaves the running one serving; an app with no running copy
        (``crashed``) gets one attempt and a fresh restart count.
        """
        ...

    def app_renamed(self, old_slug: str, new_slug: str) -> None:
        """The registry now names the app ``new_slug``: move its running copy there and restart it.

        Call it right after the rename was written. The old copy keeps serving, under the new
        slug, until the copy started under the new name passes its health check.
        """
        ...

    async def stop_app(self, slug: str) -> None:
        """Close the app's route and stop its child; return when nothing of it runs.

        It changes no row. It is for the delete of an app: call it right before the rows go, so
        a child never outlives its registration, and ``wake`` after they went. If the rows stay,
        the next pass over the app starts it again, because the registry still says it runs. A
        call waits for the app's own task, so an update that is building or being health-checked
        finishes first. An app the reconciler does not know, or one already stopped, returns at
        once.
        """
        ...

    def wake(self, slug: str) -> None:
        """Ask for a pass over ``slug`` after a row changed."""
        ...
