"""The action catalogue and the minimum level each action needs (design 9.2).

``Action`` values are the strings the admin API contract names and the audit log reuses.
``ACTION_REQUIREMENTS`` maps every action to the one rule that grants it; the table test in
``tests/unit/test_authorize_table.py`` fails when an action has no entry, so a new action cannot be
added without deciding who may do it. The mapping is read-only, so no importer can widen who may do
what at run time. Nothing here does I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Action(StrEnum):
    """Everything ``authorize`` is asked about, one value per row of design 9.2."""

    # Apps: opening and viewing.
    APP_OPEN = "app.open"
    APP_VIEW = "app.view"
    APP_VIEW_AS_USER = "app.view_as_user"
    APP_VIEW_LOGS = "app.view_logs"
    APP_VIEW_ENVIRONMENT = "app.view_environment"
    # Apps: lifecycle, by an app admin.
    APP_UPDATE = "app.update"
    APP_RESTART = "app.restart"
    APP_STOP = "app.stop"
    APP_START = "app.start"
    # Apps: platform-controlled.
    APP_REGISTER = "app.register"
    APP_DELETE = "app.delete"
    APP_RENAME = "app.rename"
    APP_SET_OWNER = "app.set_owner"
    APP_EDIT_PLATFORM_FIELDS = "app.edit_platform_fields"
    # Releases.
    RELEASE_ACTIVATE = "release.activate"
    RELEASE_LABEL = "release.label"
    RELEASE_TAG = "release.tag"
    RELEASE_UNTAG = "release.untag"
    RELEASE_LOCK = "release.lock"
    RELEASE_UNLOCK = "release.unlock"
    RELEASE_DELETE = "release.delete"
    RELEASE_VIEW_SOURCE = "release.view_source"
    # Configuration, resources and sharing.
    CONFIG_EDIT = "config.edit"
    RESOURCE_ATTACH = "resource.attach"
    RESOURCE_DETACH = "resource.detach"
    GRANT_APP = "grant.app"
    # The directory the Sharing form suggests from (no app named: any app the person manages).
    PRINCIPAL_SEARCH = "principal.search"
    # Requests to a platform admin.
    REQUEST_CREATE = "request.create"
    REQUEST_WITHDRAW = "request.withdraw"
    REQUEST_DECIDE = "request.decide"
    DELEGATION_GRANT = "delegation.grant"
    DELEGATION_REVOKE = "delegation.revoke"
    # The platform itself.
    PLATFORM_VIEW = "platform.view"
    PLATFORM_ADMIN_ADD = "platform_admin.add"
    PLATFORM_ADMIN_REMOVE = "platform_admin.remove"
    SETTINGS_EDIT = "settings.edit"
    EGRESS_EDIT = "egress.edit"
    # Reads that help fill a form (admin UI overhaul, T194 and T195). Neither changes anything.
    WORKSPACE_BROWSE = "workspace.browse"
    RESOURCE_SUGGEST = "resource.suggest"


class Requirement(StrEnum):
    """The rule that grants an action; each member names the minimum level it needs."""

    # CAN_USE on the app (owner, platform admin and CAN_MANAGE holders include it).
    APP_CAN_USE = "app_can_use"
    # CAN_MANAGE on the app: the owner, a platform admin (implicit) or a CAN_MANAGE grant.
    APP_CAN_MANAGE = "app_can_manage"
    # CAN_MANAGE on the platform: a person or group listed in platform_admins.
    PLATFORM_CAN_MANAGE = "platform_can_manage"
    # A platform admin with no grant on an OBO-delegated app (the "Open as a user" mode).
    PLATFORM_ADMIN_WITHOUT_GRANT_ON_OBO_APP = "platform_admin_without_grant_on_obo_app"
    # The request kinds: CAN_MANAGE on the app named, or any visitor when no app is named
    # (a `register` request has no app yet).
    APP_CAN_MANAGE_OR_NO_APP = "app_can_manage_or_no_app"
    # Any visitor the proxy identified. A route that also needs the visitor's token checks that.
    ANY_VISITOR = "any_visitor"
    # CAN_MANAGE on at least one app (a platform admin holds it on every app): the people who can
    # attach a resource somewhere. The slug passed with the action is not read.
    ANY_APP_MANAGER = "any_app_manager"


ACTION_REQUIREMENTS: Final[Mapping[Action, Requirement]] = MappingProxyType(
    {
        Action.APP_OPEN: Requirement.APP_CAN_USE,
        Action.APP_VIEW: Requirement.APP_CAN_MANAGE,
        Action.APP_VIEW_AS_USER: Requirement.PLATFORM_ADMIN_WITHOUT_GRANT_ON_OBO_APP,
        Action.APP_VIEW_LOGS: Requirement.APP_CAN_MANAGE,
        Action.APP_VIEW_ENVIRONMENT: Requirement.APP_CAN_MANAGE,
        Action.APP_UPDATE: Requirement.APP_CAN_MANAGE,
        Action.APP_RESTART: Requirement.APP_CAN_MANAGE,
        Action.APP_STOP: Requirement.APP_CAN_MANAGE,
        Action.APP_START: Requirement.APP_CAN_MANAGE,
        Action.APP_REGISTER: Requirement.PLATFORM_CAN_MANAGE,
        Action.APP_DELETE: Requirement.PLATFORM_CAN_MANAGE,
        Action.APP_RENAME: Requirement.PLATFORM_CAN_MANAGE,
        Action.APP_SET_OWNER: Requirement.PLATFORM_CAN_MANAGE,
        Action.APP_EDIT_PLATFORM_FIELDS: Requirement.PLATFORM_CAN_MANAGE,
        Action.RELEASE_ACTIVATE: Requirement.APP_CAN_MANAGE,
        Action.RELEASE_LABEL: Requirement.APP_CAN_MANAGE,
        Action.RELEASE_TAG: Requirement.APP_CAN_MANAGE,
        Action.RELEASE_UNTAG: Requirement.APP_CAN_MANAGE,
        Action.RELEASE_LOCK: Requirement.APP_CAN_MANAGE,
        Action.RELEASE_UNLOCK: Requirement.APP_CAN_MANAGE,
        Action.RELEASE_DELETE: Requirement.APP_CAN_MANAGE,
        Action.RELEASE_VIEW_SOURCE: Requirement.APP_CAN_MANAGE,
        Action.CONFIG_EDIT: Requirement.APP_CAN_MANAGE,
        Action.RESOURCE_ATTACH: Requirement.APP_CAN_MANAGE,
        Action.RESOURCE_DETACH: Requirement.APP_CAN_MANAGE,
        Action.GRANT_APP: Requirement.APP_CAN_MANAGE,
        Action.PRINCIPAL_SEARCH: Requirement.ANY_APP_MANAGER,
        Action.REQUEST_CREATE: Requirement.APP_CAN_MANAGE_OR_NO_APP,
        Action.REQUEST_WITHDRAW: Requirement.APP_CAN_MANAGE_OR_NO_APP,
        Action.REQUEST_DECIDE: Requirement.PLATFORM_CAN_MANAGE,
        Action.DELEGATION_GRANT: Requirement.PLATFORM_CAN_MANAGE,
        Action.DELEGATION_REVOKE: Requirement.PLATFORM_CAN_MANAGE,
        Action.PLATFORM_VIEW: Requirement.PLATFORM_CAN_MANAGE,
        Action.PLATFORM_ADMIN_ADD: Requirement.PLATFORM_CAN_MANAGE,
        Action.PLATFORM_ADMIN_REMOVE: Requirement.PLATFORM_CAN_MANAGE,
        Action.SETTINGS_EDIT: Requirement.PLATFORM_CAN_MANAGE,
        Action.EGRESS_EDIT: Requirement.PLATFORM_CAN_MANAGE,
        Action.WORKSPACE_BROWSE: Requirement.ANY_VISITOR,
        Action.RESOURCE_SUGGEST: Requirement.ANY_APP_MANAGER,
    }
)
