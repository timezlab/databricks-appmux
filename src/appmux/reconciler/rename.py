"""A rename restarts a running app through the swap, under its new slug (T106; FR-010).

The slug is the key of the ``apps`` row and the prefix of the app's URL. A platform admin changes
it with ``PATCH /apps/{app}``; the tables that point at it follow in the same statement. What the
reconciler does about it is in ``reconciler.py`` (``app_renamed``); this module holds the rules the
route and the reconciler both rely on:

* the new slug must be free (``check_new_slug``): the registry would refuse a taken one too, but a
  route that checks first answers before it writes anything;
* the running copy is **not** stopped: its route is moved to the new slug in one step
  (``Swap.rename_route``), a second copy is started under the new name, health-checked and swapped
  in like any release, and the first copy drains and stops afterwards. If the second copy does not
  come up, the first keeps serving and Restart tries again;
* the old URL answers ``404`` from the moment the registry names the new slug: the edge looks the
  slug up in the registry before it looks at a route, so no route is left under the old name;
* a stopped app only changes its row: nothing runs, so nothing restarts.
"""

from __future__ import annotations

from appmux.registry.models import Release
from appmux.registry.snapshot import RegistrySnapshot


class SlugTaken(Exception):
    """Another app already has the slug."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"slug '{slug}' is already taken")
        self.slug = slug


def check_new_slug(snapshot: RegistrySnapshot, current_slug: str, new_slug: str) -> None:
    """Return if ``new_slug`` is the app's own or a free slug; raise ``SlugTaken`` otherwise."""
    if new_slug != current_slug and snapshot.app(new_slug) is not None:
        raise SlugTaken(new_slug)


def under_new_slug(release: Release, new_slug: str) -> Release:
    """``release`` as the registry holds it after the rename: the same row, the new ``app_slug``."""
    return release.model_copy(update={"app_slug": new_slug})
