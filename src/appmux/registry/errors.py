"""The errors the registry raises, each with a stable machine code.

The admin API turns a code into a status and into the ``error.code`` of its one error body
(contracts/admin-api.md, Conventions), so a message can be reworded without breaking a client. A
message says what failed and what to do next, and never repeats a driver message, a host name or a
row value: the detail goes to the log, not to the person.

Writes raise ``ChangedByOthers`` for a stale ``version`` (design 4.3), ``RegistryRoleMissing`` for a
platform write by someone whose database role is not a member of ``appmux_platform_writers``
(data model 6), and ``RegistryUnavailable`` when Lakebase cannot be reached. ``RowAlreadyExists``,
``RowMissing`` and ``InvalidRow`` stand for the three kinds of constraint the database enforces
(a key or unique index, a foreign key or a missing row, a ``CHECK`` or a column type), so a route
can answer them without knowing a driver's error classes.
"""

from __future__ import annotations

from typing import ClassVar


class RegistryError(Exception):
    """Base of the registry's errors: a ``code`` that does not change and a message for people."""

    code: ClassVar[str] = "registry_error"
    default_message: ClassVar[str] = "the registry failed; try again"

    def __init__(self, message: str | None = None) -> None:
        self.message = message if message is not None else self.default_message
        super().__init__(self.message)


class RegistryUnavailable(RegistryError):
    """Lakebase cannot be reached or did not answer; the caller serves what it has or says so."""

    code = "registry_unavailable"
    default_message = "the registry is not reachable; try again in a moment"


class RegistryRoleMissing(RegistryError):
    """A platform write was attempted without a database role that may write platform data."""

    code = "registry_role_missing"
    default_message = (
        "your database role cannot write platform data; ask the Lakebase project owner"
    )


class ChangedByOthers(RegistryError):
    """The ``version`` the caller read is no longer the row's version."""

    code = "changed_by_others"
    default_message = "changed by someone else, reload and retry"


class RowAlreadyExists(RegistryError):
    """An insert or a rename collided with a key or a unique index (a taken slug, for example)."""

    code = "row_exists"
    default_message = "that already exists"


class RowMissing(RegistryError):
    """An update or delete found no row, or a row refers to a parent that does not exist."""

    code = "row_missing"
    default_message = "that does not exist"


class InvalidRow(RegistryError):
    """A row would break a constraint of its table; the message names fields, never values."""

    code = "invalid_row"
    default_message = "the values do not fit the table"
