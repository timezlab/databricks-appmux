"""The adapter from ``authz.authorize`` to the admin API's ``Authorizer`` protocol (T042).

``RegistryAuthorizer`` answers one question, "may this actor do this action on this app?", from
three inputs it reads at the moment of the question: the registry's *current* snapshot (so a grant
that was removed denies on the next call, not after a restart), the actor's Databricks groups from
the group lookup (``None`` when the lookup failed and nothing usable is cached), and the bootstrap
list of platform admins. It then calls the pure ``authz.authorize`` and returns ``allowed``.

It fails closed. Any exception raised while answering (a snapshot that was never loaded, a lookup
that broke, a defect here) is logged once and answered with ``False``. The log line holds the action
and the app slug, never the actor, a token or the exception's arguments beyond the traceback.

``access_for`` exposes the same inputs to routes that must compute a decision for many apps at once
(``GET /apps``), so that they apply exactly the data the authorizer applies.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from appmux.authz.policy import AuthzData, authorize
from appmux.identity.actor import Actor
from appmux.registry.grants import build_authz_data
from appmux.registry.protocol import Registry
from appmux.registry.snapshot import RegistrySnapshot

logger = logging.getLogger(__name__)


class GroupSource(Protocol):
    """Where an actor's groups come from; ``identity.groups.GroupLookup`` satisfies it.

    ``None`` means unknown. A source must never raise for a failed lookup, and a caller must never
    turn ``None`` into an empty set: both deny, but only ``None`` says the answer is not known.
    """

    async def groups_for(self, actor: Actor) -> frozenset[str] | None: ...


@dataclass(frozen=True, slots=True)
class Access:
    """What a decision for one actor reads: one snapshot, its authorization data, the groups."""

    snapshot: RegistrySnapshot
    data: AuthzData
    groups: frozenset[str] | None


class RegistryAuthorizer:
    """``Authorizer`` over the registry's snapshot and the group lookup."""

    def __init__(
        self,
        registry: Registry,
        group_lookup: GroupSource,
        *,
        bootstrap_admins: Iterable[str] = (),
    ) -> None:
        self._registry = registry
        self._group_lookup = group_lookup
        self._bootstrap_admins = tuple(bootstrap_admins)
        # One snapshot object is immutable, so the data built from it can be reused until the
        # registry serves another one; the identity check is what keeps a removed grant from
        # lingering.
        self._built: tuple[RegistrySnapshot, AuthzData] | None = None

    async def __call__(self, actor: Actor, action: str, app_slug: str | None) -> bool:
        try:
            access = await self.access_for(actor)
            decision = authorize(actor, action, app_slug, access.data, groups=access.groups)
        except Exception:
            # An operational boundary of an auth check: whatever went wrong must deny, not raise.
            logger.exception(
                "authorization failed; the request is denied (action %r, app %r)",
                _printable(action),
                _printable(app_slug),
            )
            return False
        return decision.allowed is True

    async def access_for(self, actor: Actor) -> Access:
        """The data of the current snapshot and the actor's groups.

        A group source that raises counts as an unknown answer (``groups`` is ``None``). A snapshot
        that cannot be read raises: ``__call__`` turns that into a denial, and a route lets it
        become a ``503`` or ``500`` rather than guessing an answer.
        """
        snapshot = self._registry.snapshot
        data = self._data_of(snapshot)
        return Access(snapshot=snapshot, data=data, groups=await self._groups_of(actor))

    async def _groups_of(self, actor: Actor) -> frozenset[str] | None:
        try:
            return await self._group_lookup.groups_for(actor)
        except Exception:
            # The lookup is specified never to raise; if it does, the groups are unknown, which
            # denies every group-based grant and leaves the owner, user and everyone paths alone.
            logger.exception("the group lookup failed; group grants will not match")
            return None

    def _data_of(self, snapshot: RegistrySnapshot) -> AuthzData:
        built = self._built
        if built is not None and built[0] is snapshot:
            return built[1]
        data = build_authz_data(snapshot, bootstrap_admins=self._bootstrap_admins)
        self._built = (snapshot, data)
        return data


def make_authorizer(
    registry: Registry,
    group_lookup: GroupSource,
    *,
    bootstrap_admins: Iterable[str] = (),
) -> RegistryAuthorizer:
    """The authorizer to hand to ``create_admin_app``.

    ``bootstrap_admins`` is ``EnvironmentSettings.appmux_bootstrap_admins``; it counts only while
    the ``platform_admins`` setting is unset (FR-026).
    """
    return RegistryAuthorizer(registry, group_lookup, bootstrap_admins=bootstrap_admins)


def _printable(value: str | None) -> str | None:
    # The action and the slug come from a request path or an internal caller; keep a log line short
    # and free of control characters.
    if value is None:
        return None
    return "".join(c if c.isprintable() else "?" for c in value)[:64]
