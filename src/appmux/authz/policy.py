"""The pure authorization decision (design 9.1 and 9.2, ADR 0010, FR-022 to FR-026).

``authorize`` answers one question from plain values: may this actor do this action on this app?
It takes an ``AuthzData`` that another module builds from a registry snapshot and the actor's
Databricks groups, and it imports no registry module, no settings and no I/O library, so the whole
decision is a table that tests can run (``tests/unit/test_authorize_table.py``).

Rules that hold everywhere:

* The default is deny. An unknown action, an actor without an email, and an action on an app that
  is not in the data are denied. Platform-level actions (``Requirement.PLATFORM_CAN_MANAGE``) never
  read the app, so a platform admin is allowed them whatever slug is passed; the caller answers
  ``404`` for a slug that does not exist after authorizing.
* ``groups`` is ``None`` when the lookup failed and nothing usable is cached. Then no group grant
  and no group entry of ``platform_admins`` matches: only the owner, a user grant, an ``everyone``
  grant and a ``user`` entry of ``platform_admins`` can. The same holds while the ``user
  authorization`` platform setting is off, because without the visitor's token there are no groups.
* A platform admin holds ``CAN_MANAGE`` on every app with no row in ``app_grants`` (rule 17).
* ``everyone`` is ``CAN_USE`` at most: a stored ``CAN_MANAGE`` on it counts as ``CAN_USE``, because
  the shared-SP token can write ``app_grants`` (data model section 6).
* A ``Decision`` holds a code and a level, never text taken from the request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from appmux.authz.actions import ACTION_REQUIREMENTS, Action, Requirement
from appmux.identity.actor import Actor


class Permission(StrEnum):
    """The two permission levels; ``CAN_MANAGE`` includes ``CAN_USE``."""

    CAN_USE = "CAN_USE"
    CAN_MANAGE = "CAN_MANAGE"


_RANK: Final = {Permission.CAN_USE: 1, Permission.CAN_MANAGE: 2}


class Reason(StrEnum):
    """Why a decision came out as it did; a short machine code, safe to log.

    Do not return it to a visitor: ``UNKNOWN_APP`` and ``NO_ACCESS`` differ, and the contract
    answers both with the same ``404`` so that slugs cannot be enumerated (edge-http.md).
    """

    # Allowed.
    OWNER = "owner"
    PLATFORM_ADMIN = "platform_admin"
    USER_GRANT = "user_grant"
    GROUP_GRANT = "group_grant"
    EVERYONE_GRANT = "everyone_grant"
    ANY_VISITOR = "any_visitor"
    # Denied by the default or by missing standing.
    UNKNOWN_ACTION = "unknown_action"
    UNKNOWN_APP = "unknown_app"
    NO_IDENTITY = "no_identity"
    NO_ACCESS = "no_access"
    INSUFFICIENT_LEVEL = "insufficient_level"
    NOT_PLATFORM_ADMIN = "not_platform_admin"
    NO_MANAGED_APP = "no_managed_app"
    # Denied by a rule of the data model.
    NOT_OBO_APP = "not_obo_app"
    ALREADY_HAS_ACCESS = "already_has_access"
    GROUPS_UNKNOWN = "groups_unknown"
    OWNER_PROTECTED = "owner_protected"
    EVERYONE_CAN_USE_ONLY = "everyone_can_use_only"
    LAST_USER_ADMIN = "last_user_admin"
    UNEXPECTED_CHANGE = "unexpected_change"


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of ``authorize``.

    ``level`` is the permission the actor held on the resource that decided it (``None`` when they
    held none, or when the action needs no level); ``reason`` is a code, never request text.
    """

    allowed: bool
    reason: Reason
    level: Permission | None


@dataclass(frozen=True, slots=True)
class Grant:
    """One row of ``app_grants``: ``type`` is user, group or everyone (``name`` is ``*``)."""

    type: Literal["user", "group", "everyone"]
    name: str
    level: Permission


@dataclass(frozen=True, slots=True)
class AppAccess:
    """What authorization reads of one ``apps`` row and its grants."""

    slug: str
    owner: str
    grants: tuple[Grant, ...] = ()
    obo_delegated: bool = False


@dataclass(frozen=True, slots=True)
class AdminEntry:
    """One entry of the ``platform_admins`` setting: a user's email or a Databricks group name."""

    type: Literal["user", "group"]
    name: str


@dataclass(frozen=True, slots=True)
class AuthzData:
    """Everything ``authorize`` reads, taken from one registry snapshot."""

    apps: Mapping[str, AppAccess]
    platform_admins: tuple[AdminEntry, ...]
    user_authorization: bool


@dataclass(frozen=True, slots=True)
class GrantChange:
    """A proposed ``grant.app`` write: set ``level`` for a principal, or remove it (``None``)."""

    principal_type: Literal["user", "group", "everyone"]
    principal: str
    level: Permission | None


@dataclass(frozen=True, slots=True)
class AdminChange:
    """A proposed ``platform_admins`` list, as it would be after the write."""

    resulting_admins: tuple[AdminEntry, ...]


Change = GrantChange | AdminChange


def check_grant_change(owner: str, change: GrantChange) -> Reason | None:
    """The reason a grant write breaks a rule of the model, or ``None`` when it is acceptable.

    Rule 1: the owner is always ``CAN_MANAGE``, so a user entry naming the owner may neither be
    removed nor set lower. ``everyone`` is ``CAN_USE`` only (FR-022).
    """
    if change.principal_type == "everyone" and change.level is Permission.CAN_MANAGE:
        return Reason.EVERYONE_CAN_USE_ONLY
    if (
        change.principal_type == "user"
        and _fold(change.principal) == _fold(owner)
        and change.level is not Permission.CAN_MANAGE
    ):
        return Reason.OWNER_PROTECTED
    return None


def check_admin_change(change: AdminChange) -> Reason | None:
    """Rule 2: ``None`` when the resulting list keeps a ``user`` entry, else ``LAST_USER_ADMIN``.

    appmux cannot count the members of a Databricks group, so a list of groups alone could lock
    everyone out.
    """
    if any(entry.type == "user" and _fold(entry.name) for entry in change.resulting_admins):
        return None
    return Reason.LAST_USER_ADMIN


def authorize(
    actor: Actor,
    action: Action | str,
    app: str | None,
    data: AuthzData,
    *,
    groups: frozenset[str] | None,
    change: Change | None = None,
) -> Decision:
    """Decide whether ``actor`` may perform ``action`` on the app with slug ``app``.

    ``groups`` is the actor's lower-cased Databricks group names, or ``None`` when unknown. For
    ``request.create`` and ``request.withdraw`` pass ``app=None`` for a ``register`` request (any
    visitor may ask) and the app's slug for every other kind (``CAN_MANAGE`` on it); the caller
    keeps the business rules that need the request row, such as "only the asker withdraws".

    ``change`` is optional. Given with ``grant.app`` (a ``GrantChange``) it also applies rule 1 and
    the ``everyone`` cap; given with ``platform_admin.add`` or ``platform_admin.remove`` (an
    ``AdminChange``) it also applies rule 2. A change of any other kind is denied. The same checks
    are available as ``check_grant_change`` and ``check_admin_change`` for a handler that has
    parsed the body after the route-level check.
    """
    email = _fold(actor.email)
    if not email:
        return Decision(False, Reason.NO_IDENTITY, None)
    try:
        known_action = Action(action)
    except ValueError:
        return Decision(False, Reason.UNKNOWN_ACTION, None)

    decision = _decide(email, known_action, app, data, groups)
    if not decision.allowed:
        return decision
    return _apply_change(known_action, app, data, change, decision)


def effective_permission(
    email: str, app: AppAccess, data: AuthzData, groups: frozenset[str] | None
) -> tuple[Permission | None, Reason | None]:
    """The actor's level on ``app`` and what gave it: owner, platform admin, else best grant."""
    folded = _fold(email)
    if folded == _fold(app.owner):
        return Permission.CAN_MANAGE, Reason.OWNER
    if is_platform_admin(folded, data, groups):
        return Permission.CAN_MANAGE, Reason.PLATFORM_ADMIN
    return _best_grant(folded, app, data, groups)


def is_platform_admin(email: str, data: AuthzData, groups: frozenset[str] | None) -> bool:
    """True when a ``user`` entry names the email or a ``group`` entry names one of the groups."""
    folded = _fold(email)
    names = _usable_groups(data, groups)
    return any(
        (entry.type == "user" and _fold(entry.name) == folded)
        or (entry.type == "group" and _fold(entry.name) in names)
        for entry in data.platform_admins
    )


def _decide(
    email: str,
    action: Action,
    app: str | None,
    data: AuthzData,
    groups: frozenset[str] | None,
) -> Decision:
    requirement = ACTION_REQUIREMENTS.get(action)
    if requirement is None:  # an Action added without a requirement: deny, never guess
        return Decision(False, Reason.UNKNOWN_ACTION, None)

    if requirement is Requirement.PLATFORM_CAN_MANAGE:
        return _platform_decision(email, data, groups)
    if requirement is Requirement.ANY_VISITOR:
        return Decision(True, Reason.ANY_VISITOR, None)
    if requirement is Requirement.ANY_APP_MANAGER:
        return _any_app_manager_decision(email, data, groups)
    if requirement is Requirement.APP_CAN_MANAGE_OR_NO_APP and app is None:
        return Decision(True, Reason.ANY_VISITOR, None)

    app_access = data.apps.get(app) if app is not None else None
    if app_access is None:
        return Decision(False, Reason.UNKNOWN_APP, None)
    if requirement is Requirement.PLATFORM_ADMIN_WITHOUT_GRANT_ON_OBO_APP:
        return _view_as_user_decision(email, app_access, data, groups)

    needed = Permission.CAN_USE if requirement is Requirement.APP_CAN_USE else Permission.CAN_MANAGE
    level, source = effective_permission(email, app_access, data, groups)
    if level is None or source is None:
        return Decision(False, Reason.NO_ACCESS, None)
    if _RANK[level] < _RANK[needed]:
        return Decision(False, Reason.INSUFFICIENT_LEVEL, level)
    return Decision(True, source, level)


def _platform_decision(email: str, data: AuthzData, groups: frozenset[str] | None) -> Decision:
    if is_platform_admin(email, data, groups):
        return Decision(True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE)
    return Decision(False, Reason.NOT_PLATFORM_ADMIN, None)


def _any_app_manager_decision(
    email: str, data: AuthzData, groups: frozenset[str] | None
) -> Decision:
    """`resource.suggest`: a platform admin, or anyone with `CAN_MANAGE` on at least one app.

    The first app that gives `CAN_MANAGE` decides; a group grant counts only while the groups are
    known, as everywhere else, so an unknown lookup never widens who is let in.
    """
    if is_platform_admin(email, data, groups):
        return Decision(True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE)
    for app in data.apps.values():
        level, source = effective_permission(email, app, data, groups)
        if level is Permission.CAN_MANAGE and source is not None:
            return Decision(True, source, level)
    return Decision(False, Reason.NO_MANAGED_APP, None)


def _view_as_user_decision(
    email: str, app: AppAccess, data: AuthzData, groups: frozenset[str] | None
) -> Decision:
    """`app.view_as_user`: a platform admin who is neither owner nor named, on an OBO app.

    An owner or a grant holder already reaches the app as a visitor, so the mode would add nothing
    for them. "Named by no grant" cannot be shown while the group lookup failed and group grants
    count (a group grant may hide behind the unknown), so that case is refused (FR-023).
    """
    if not is_platform_admin(email, data, groups):
        return Decision(False, Reason.NOT_PLATFORM_ADMIN, None)
    if not app.obo_delegated:
        return Decision(False, Reason.NOT_OBO_APP, Permission.CAN_MANAGE)
    if groups is None and data.user_authorization:
        return Decision(False, Reason.GROUPS_UNKNOWN, Permission.CAN_MANAGE)
    if email == _fold(app.owner) or _best_grant(email, app, data, groups)[0] is not None:
        return Decision(False, Reason.ALREADY_HAS_ACCESS, Permission.CAN_MANAGE)
    return Decision(True, Reason.PLATFORM_ADMIN, Permission.CAN_MANAGE)


def _apply_change(
    action: Action, app: str | None, data: AuthzData, change: Change | None, decision: Decision
) -> Decision:
    if change is None:
        return decision
    refusal: Reason | None
    if action is Action.GRANT_APP and isinstance(change, GrantChange):
        app_access = data.apps.get(app) if app is not None else None
        # An allowed grant.app always names a known app; the check keeps that from being assumed.
        refusal = (
            Reason.UNKNOWN_APP
            if app_access is None
            else check_grant_change(app_access.owner, change)
        )
    elif action in _ADMIN_LIST_ACTIONS and isinstance(change, AdminChange):
        refusal = check_admin_change(change)
    else:
        refusal = Reason.UNEXPECTED_CHANGE
    if refusal is None:
        return decision
    return Decision(False, refusal, decision.level)


_ADMIN_LIST_ACTIONS: Final = frozenset({Action.PLATFORM_ADMIN_ADD, Action.PLATFORM_ADMIN_REMOVE})


def _best_grant(
    email: str, app: AppAccess, data: AuthzData, groups: frozenset[str] | None
) -> tuple[Permission | None, Reason | None]:
    names = _usable_groups(data, groups)
    best: tuple[Permission, Reason] | None = None
    for grant in app.grants:
        if grant.type == "user" and _fold(grant.name) == email:
            reason = Reason.USER_GRANT
            level = grant.level
        elif grant.type == "group" and _fold(grant.name) in names:
            reason = Reason.GROUP_GRANT
            level = grant.level
        elif grant.type == "everyone":
            reason = Reason.EVERYONE_GRANT
            level = Permission.CAN_USE
        else:
            continue
        # Strictly higher wins, so on a tie the first of user, group, everyone in row order stays.
        if best is None or _RANK[level] > _RANK[best[0]]:
            best = (level, reason)
    return (None, None) if best is None else best


def _usable_groups(data: AuthzData, groups: frozenset[str] | None) -> frozenset[str]:
    """The groups that may be matched: none when unknown or when user authorization is off."""
    if groups is None or not data.user_authorization:
        return frozenset()
    return frozenset(_fold(name) for name in groups)


def _fold(value: str) -> str:
    return value.strip().lower()
