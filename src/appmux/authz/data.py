"""The platform admins in force: the setting, or the bootstrap list while the setting is unset.

``APPMUX_BOOTSTRAP_ADMINS`` is how the first platform admin exists (design 9.1, FR-026). It is
honoured only while the ``platform_admins`` setting is unset (``None``). Once the setting exists the
list is ignored, including when the setting is an empty list: an empty list is a value somebody set
(the data model's guard refuses it through the API, so it only appears after a break-glass edit),
and refilling it from a deploy-time variable would undo that edit. With no setting and no bootstrap
list nobody is a platform admin.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal, Protocol

from appmux.authz.policy import AdminEntry


class ConfiguredAdmin(Protocol):
    """The shape of one stored entry (``settings.PlatformAdmin`` satisfies it)."""

    @property
    def type(self) -> Literal["user", "group"]: ...

    @property
    def name(self) -> str: ...


def effective_platform_admins(
    configured: Sequence[ConfiguredAdmin] | None, bootstrap: Iterable[str]
) -> tuple[AdminEntry, ...]:
    """The ``platform_admins`` entries in force, de-duplicated, in their given order.

    ``configured`` is the setting's value, or ``None`` when the setting is unset. Emails are
    compared in lower case; a group name keeps its case and is compared in lower case by
    ``authorize``.
    """
    if configured is not None:
        entries = (AdminEntry(entry.type, entry.name) for entry in configured)
    else:
        entries = (AdminEntry("user", email) for email in bootstrap)

    seen: set[tuple[str, str]] = set()
    admins: list[AdminEntry] = []
    for entry in entries:
        name = entry.name.strip()
        if not name:
            continue
        if entry.type == "user":
            name = name.lower()
        key = (entry.type, name.lower())
        if key in seen:
            continue
        seen.add(key)
        admins.append(AdminEntry(entry.type, name))
    return tuple(admins)
