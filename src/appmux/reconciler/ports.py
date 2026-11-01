"""The seam between the admin routes and the reconciler's Update flow (design 5.2).

The admin module asks for an Update through ``ReleaseUpdater`` and maps ``UpdateRefused`` to an
error body; the reconciler implements the protocol (``update.py``) and raises the refusal. Both
sides import this file and nothing else of each other, so the dependency direction of design 3.4
holds and each side is tested against a stub of the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol, get_args

from pydantic import SecretStr

from appmux.identity.actor import Actor
from appmux.registry.models import RowModel
from appmux.registry.protocol import Change

RefusalKind = Literal["not_found", "refused", "conflict", "unavailable"]
REFUSAL_KINDS: Final[tuple[str, ...]] = get_args(RefusalKind)


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """What a finished request for an Update produced.

    ``is_up_to_date`` is true when the folder's digest equals a live release's: no release was
    created and ``release_no`` names that live release. Otherwise ``release_no`` is the new
    release, in status ``pending``; its progress is read from the registry snapshot.
    """

    release_no: int
    is_up_to_date: bool


class UpdateRefused(Exception):
    """An Update stopped before a release was created, so nothing changed.

    ``kind`` says how the caller answers; ``message`` is for a person and says why and what to do:

    * ``not_found``: the app does not exist;
    * ``refused``: the person must change something (the folder cannot be read or is empty, the
      ``app.yaml`` is invalid, the folder is too large);
    * ``conflict``: the app's state forbids it (for example every release is protected and the cap
      is full, rule 13);
    * ``unavailable``: a dependency failed and the same request may work later (the workspace,
      the content store, the registry).

    The message never carries a token or any value read from the folder's files.
    """

    def __init__(self, kind: RefusalKind, message: str) -> None:
        super().__init__(message)
        self.kind: RefusalKind = kind
        self.message = message


class ChangeWriter(Protocol):
    """Writes the changes of an Update together with the audit entry that records it.

    The admin route supplies it (it owns the audit entry, so the rule "a mutating route records
    what it changed" stays in one place); the reconciler calls it once, with the new release's row
    and ``release_no`` for the entry's detail. A write that raises (``RegistryError``) propagates.
    """

    async def __call__(
        self, changes: Sequence[Change], *, release_no: int
    ) -> tuple[RowModel | None, ...]: ...


class ReleaseUpdater(Protocol):
    """Starts an Update of one app with the presser's own token."""

    async def update(
        self,
        *,
        actor: Actor,
        app_slug: str,
        user_token: SecretStr,
        write: ChangeWriter,
    ) -> UpdateResult:
        """Read the app's ``source_path`` with ``user_token``, snapshot it, create the release.

        The release row is written by the appmux service principal (data model section 6), so the
        token is used only to read the folder. A release is created by calling ``write`` once;
        when the folder equals a live release, ``write`` is not called. Raises ``UpdateRefused``.
        """
        ...
