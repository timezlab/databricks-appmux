"""The requirements check: every ``valueFrom`` key is a resource of the app (FR-035, design 5.4).

The keys a release needs are every ``value_from`` of its detected manifest plus every one the app
admin added to the release ``env``. The reconciler runs the check first, before any build; if some
key is not a resource of the app the release becomes ``failed`` and its ``status_reason`` is
``check.message()``, which lists **all** the missing keys at once. After the keys are attached the
admin presses Activate, which runs the same check again over the *stored* release row: nothing here
reads a source file or a token, so the check works from the snapshot alone.

A key of the release config counts even when it overrides a name of ``app.yaml``: the environment
builder (``sandbox/environment.py``) refuses a ``value_from`` with no resolved value in either
layer, so a key that only a later layer hides would still stop the start.

Pure: no I/O. The keys are compared as written; the key form is ``registry.resources``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Final

from appmux.registry.models import EnvEntry, Release

MISSING_RESOURCES_PREFIX: Final = "missing resources: "

# A key reaches a status text that the app page shows. It is cut and made printable first, because
# app.yaml is the child's own file and a key there can be any text.
_MAX_SHOWN_KEY_LENGTH: Final = 64
_MAX_SHOWN_KEYS: Final = 20

# The first word that matches decides. A hint from the key's name, never a gate: the admin may pick
# any type for any key.
_TYPE_HINTS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("warehouse",), "sql_warehouse"),
    (("secret", "password", "passwd", "token", "key"), "secret"),
    (("experiment",), "mlflow_experiment"),
    (("endpoint", "model"), "serving_endpoint"),
    (("genie",), "genie_space"),
    (("lakebase", "postgres", "database"), "lakebase_database"),
    (("job",), "job"),
    (("volume",), "uc_volume"),
    (("table",), "uc_table"),
    (("function",), "uc_function"),
    (("connection",), "uc_connection"),
    (("index",), "vector_search_index"),
)


@dataclass(frozen=True, slots=True)
class RequirementCheck:
    """The outcome of the check: the keys a release needs and those with no resource.

    Both tuples are sorted, so the message and the panel do not depend on the order of the file.
    """

    required: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def is_met(self) -> bool:
        return not self.missing

    def message(self) -> str:
        """The ``status_reason`` of a release that cannot start; empty when nothing is missing."""
        if not self.missing:
            return ""
        shown = [_shown(key) for key in self.missing[:_MAX_SHOWN_KEYS]]
        hidden = len(self.missing) - len(shown)
        more = f", and {hidden} more" if hidden > 0 else ""
        return f"{MISSING_RESOURCES_PREFIX}{', '.join(shown)}{more}"


def required_keys(
    detected_env: Iterable[EnvEntry], config_env: Iterable[EnvEntry]
) -> tuple[str, ...]:
    """Every distinct ``value_from`` of the two layers, sorted."""
    return tuple(
        sorted(
            {
                entry.value_from
                for entry in (*detected_env, *config_env)
                if entry.value_from is not None
            }
        )
    )


def check_requirements(
    detected_env: Iterable[EnvEntry],
    config_env: Iterable[EnvEntry],
    *,
    resource_keys: Collection[str],
) -> RequirementCheck:
    """Compare the keys the env entries need with the keys of the app's resources."""
    required = required_keys(detected_env, config_env)
    attached = set(resource_keys)
    return RequirementCheck(
        required=required, missing=tuple(key for key in required if key not in attached)
    )


def check_release_requirements(
    release: Release, *, resource_keys: Collection[str]
) -> RequirementCheck:
    """``check_requirements`` over a stored release row: its manifest and its own config."""
    return check_requirements(release.detected.env, release.config.env, resource_keys=resource_keys)


def suggest_resource_type(key: str) -> str | None:
    """A type hint from the name of a missing key, or ``None``; the Attach form pre-selects it."""
    lowered = key.lower()
    for words, type_name in _TYPE_HINTS:
        if any(word in lowered for word in words):
            return type_name
    return None


def _shown(key: str) -> str:
    printable = "".join(c if c.isascii() and c.isprintable() else "?" for c in key)
    if len(printable) > _MAX_SHOWN_KEY_LENGTH:
        return printable[:_MAX_SHOWN_KEY_LENGTH] + "..."
    return printable
