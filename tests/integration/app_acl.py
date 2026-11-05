"""Read, change and put back the direct permissions of a Databricks App with the CLI.

``databricks apps update-permissions`` adds or raises an entry; ``set-permissions`` replaces the
whole *direct* list and there is no call that removes one entry. A run that gives a principal
``CAN_USE`` on the appmux App (``test_service_principal_grant.py``, ADR 0020) therefore takes a
snapshot of the direct list first and puts it back with ``set-permissions`` afterwards. The pure
part (what a snapshot keeps, whether two lists agree) is checked by ``tests/unit/test_app_acl.py``.

Nothing here prints a principal: the list holds the owner's address, so a caller reports counts and
booleans only, and the CLI's own output is dropped.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Final

CLI_TIMEOUT_S: Final = 60.0
PRINCIPAL_KEYS: Final = ("user_name", "group_name", "service_principal_name")
Entry = dict[str, str]


class AclError(RuntimeError):
    """A CLI call about an App's permissions failed; the message holds no principal or address."""


def direct_entries(listing: dict[str, Any]) -> list[Entry]:
    """The entries ``set-permissions`` needs to keep the direct grants as they are.

    One entry per principal and direct level. An inherited level is left out: sending it back would
    turn it into a direct one that outlives the parent's.
    """
    kept: list[Entry] = []
    for item in listing.get("access_control_list", []):
        key = next((candidate for candidate in PRINCIPAL_KEYS if item.get(candidate)), None)
        if key is None:
            continue
        kept.extend(
            {key: str(item[key]), "permission_level": str(level["permission_level"])}
            for level in item.get("all_permissions", [])
            if not level.get("inherited")
        )
    return kept


def same_entries(first: list[Entry], second: list[Entry]) -> bool:
    """Whether two lists hold the same principals at the same levels, in any order."""

    def normal(entries: list[Entry]) -> list[tuple[tuple[str, str], ...]]:
        return sorted(tuple(sorted(item.items())) for item in entries)

    return normal(first) == normal(second)


def permissions_body(entries: list[Entry]) -> dict[str, list[Entry]]:
    return {"access_control_list": entries}


class AppAcl:
    """The direct permissions of one App, through the CLI profile of a person who manages it."""

    def __init__(self, profile: str, app_name: str) -> None:
        self._profile = profile
        self._app_name = app_name

    def read(self) -> list[Entry]:
        done = self._run("get-permissions", self._app_name, "-o", "json")
        if done.returncode != 0:
            raise AclError(f"get-permissions failed (exit code {done.returncode})")
        try:
            return direct_entries(json.loads(done.stdout))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AclError("get-permissions gave an answer that is not a permissions list") from exc

    def give_can_use(self, application_id: str) -> None:
        """Add ``CAN_USE`` for a service principal; every other direct entry is left alone."""
        entry = {"service_principal_name": application_id, "permission_level": "CAN_USE"}
        self._write("update-permissions", [entry])

    def put_back(self, entries: list[Entry]) -> None:
        """Replace the direct list with ``entries`` and check that the App now has exactly them."""
        self._write("set-permissions", entries)
        now = self.read()
        if not same_entries(now, entries):
            raise AclError(
                f"the direct permissions differ from the snapshot after set-permissions "
                f"({len(now)} entries now, {len(entries)} in the snapshot)"
            )

    def _write(self, command: str, entries: list[Entry]) -> None:
        # The body holds addresses: it goes through a private file, not the command line.
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory, "permissions.json")
            body.write_text(json.dumps(permissions_body(entries)), encoding="utf-8")
            body.chmod(0o600)
            done = self._run(command, self._app_name, "--json", f"@{body}")
        if done.returncode != 0:
            raise AclError(f"{command} failed (exit code {done.returncode})")

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["databricks", "apps", *arguments, "--profile", self._profile],
            capture_output=True,
            check=False,
            timeout=CLI_TIMEOUT_S,
        )
