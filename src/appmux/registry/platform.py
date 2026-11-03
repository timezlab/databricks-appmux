"""Platform settings as the registry stores them: the values in force and the write of one.

``platform_settings`` is a key/value table (data model 4.1) and the code's list of keys, types and
the rule between keys is ``settings.PlatformSettings``. This module is the bridge: it reads the
stored rows of a snapshot into one mapping for that model, and it builds the ``Change`` that stores
one key, so that the three routes that write a setting (the global egress list, the admin list and
``PUT /platform/settings/{key}``) name the table, the key and the columns in one place.

A key is inserted the first time and updated afterwards. The choice is made from the snapshot the
route holds, which can lag a write that has just committed; a lost race ends as a duplicate-key
error from the registry (``409``), never as a silent second row. There is no ``version`` column:
two admins saving at once end as the later one (TD-013).
"""

from __future__ import annotations

import logging
from typing import Final

from pydantic import ValidationError

from appmux.registry.protocol import Change
from appmux.registry.snapshot import RegistrySnapshot
from appmux.registry.writers import WriteKind
from appmux.settings import PlatformSettings

logger = logging.getLogger(__name__)

# The two keys the rule between keys joins (``PlatformSettings.locks_leave_room_to_clean_up``).
LOCK_RULE_KEYS: Final = ("max_locked_releases", "max_releases_per_app")

_DEFAULT_BUDGET_KEY: Final = "default_memory_budget_mb"


def stored_settings(snapshot: RegistrySnapshot) -> dict[str, object]:
    """The rows of ``platform_settings`` that the code knows a key for, as ``key: value``.

    A row for a key the code does not know is left out: ``PlatformSettings`` refuses an unknown
    key, and one stray row must not make every other setting unreadable.
    """
    stored: dict[str, object] = {}
    for key in PlatformSettings.model_fields:
        row = snapshot.setting(key)
        if row is not None:
            stored[key] = row.value
    return stored


def settings_in_force(snapshot: RegistrySnapshot) -> PlatformSettings:
    """The effective settings: each stored value, else the key's default.

    Raises ``pydantic.ValidationError`` when a stored value no longer fits its key (a row edited in
    the database, since the admin API cannot write one); ``faulty_keys`` names the keys at fault.
    """
    return PlatformSettings.model_validate(stored_settings(snapshot))


def default_memory_budget_mb(snapshot: RegistrySnapshot) -> int:
    """The budget a new app starts with: the stored setting, or the code's default.

    Only this key is read, so a bad value under another key cannot stop an app from being
    registered. A stored value that no longer fits (a row edited in the database) falls back to
    the code's default rather than refusing the registration; the budget can be changed afterwards.
    """
    row = snapshot.setting(_DEFAULT_BUDGET_KEY)
    if row is not None:
        try:
            stored = PlatformSettings.model_validate({_DEFAULT_BUDGET_KEY: row.value})
        except ValidationError:
            logger.error("the stored default memory budget is unusable; the code default applies")
        else:
            return stored.default_memory_budget_mb
    return PlatformSettings().default_memory_budget_mb


def faulty_keys(error: ValidationError) -> tuple[str, ...]:
    """The setting keys ``error`` is about, in the order it lists them, each once.

    An error that belongs to no key (the model's own rule between keys) names the keys the rule
    joins. Only key names come back, never a value.
    """
    keys: list[str] = []
    for detail in error.errors(include_input=False, include_url=False, include_context=False):
        for key in [str(detail["loc"][0])] if detail["loc"] else LOCK_RULE_KEYS:
            if key not in keys:
                keys.append(key)
    return tuple(keys)


def platform_setting_change(
    snapshot: RegistrySnapshot, key: str, value: object, *, updated_by: str
) -> Change:
    """The write that stores ``value`` under ``key``: an insert while unset, else an update."""
    values = {"value": value, "updated_by": updated_by}
    if snapshot.setting(key) is None:
        return Change(WriteKind.PLATFORM_SETTING_INSERT, values={"key": key, **values})
    return Change(WriteKind.PLATFORM_SETTING_UPDATE, key={"key": key}, values=values)
