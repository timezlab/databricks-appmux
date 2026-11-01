"""The bundle file ``databricks.yml``: offers for the Requirements panel, never a grant (FR-034).

A child may ship the file ``databricks bundle`` uses. appmux reads one place in it,
``resources.apps.<app>.resources[]``, and turns each entry into an *offer* (``BundleResource``): a
suggested type, target and permission that the Requirements panel pre-fills for an app admin. An
offer attaches nothing, grants nothing and is not required by anything; the admin confirms it with
the same attach check as a typed resource (rule 12). Everything else in the file is ignored, with a
warning on the manifest, and a file that cannot be used never refuses an Update: it gives warnings
and no offers.

The module is pure like ``appyaml.appyaml``: it is given the root file names and a way to read one
file. Three choices keep it safe on a hostile file.

* ``yaml.safe_load`` only: no tag builds an object and no code runs. ``${var.x}`` interpolations
  are never resolved; a target that holds one is offered with no target, and the admin types it.
* Work is bounded: the file size, the entries scanned, the offers and the warnings each have a cap.
* A warning names keys and levels but never repeats a value of the file, and the parser's own
  message (which quotes the offending line) is dropped for the line number alone.

Which app of the file to read is an argument: a file may declare several apps under
``resources.apps`` and appmux cannot guess which one is the child. With one app it is chosen; with
several and no choice, nothing is offered and the warning lists the names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final

import yaml

from appmux.registry.models import BundleResource
from appmux.registry.resources import is_resource_key, levels_of

BUNDLE_FILE_NAMES: Final = ("databricks.yml", "databricks.yaml")
# A bundle file also holds targets and variables, so its bound is wider than the app.yaml one.
MAX_BUNDLE_BYTES: Final = 256 * 1024
MAX_OFFERS: Final = 100

# Aliases in YAML let a small file describe a huge list; the scan stops long before that matters.
_MAX_ENTRIES_SCANNED: Final = 500
_MAX_WARNINGS: Final = 50
_MAX_TEXT_LENGTH: Final = 256
_MAX_LABEL_LENGTH: Final = 64
_MAX_LISTED_NAMES: Final = 10

_INTERPOLATION: Final = "${"
_KNOWN_TOP_LEVEL_KEYS: Final = frozenset({"bundle", "resources"})
_HARMLESS_APP_KEYS: Final = frozenset({"name", "description", "source_code_path", "resources"})
_ENTRY_META_KEYS: Final = frozenset({"name", "description"})

# bundle type key -> appmux type.
_TYPES_BY_KEY: Final = {
    "sql_warehouse": "sql_warehouse",
    "experiment": "mlflow_experiment",
    "secret": "secret",
    "serving_endpoint": "serving_endpoint",
    "job": "job",
    "genie_space": "genie_space",
    "app": "databricks_app",
    "database": "lakebase_database",
    "postgres": "lakebase_database",
}
# bundle type key -> the field of its body that is the target (``secret`` and the Lakebase keys are
# special cases of ``_target_of``).
_TARGET_FIELDS: Final = {
    "sql_warehouse": "id",
    "experiment": "experiment_id",
    "serving_endpoint": "name",
    "job": "id",
    "genie_space": "space_id",
    "app": "name",
}
_SECURABLE_TYPES: Final = {
    "TABLE": "uc_table",
    "VOLUME": "uc_volume",
    "FUNCTION": "uc_function",
    "CONNECTION": "uc_connection",
    "MODEL_SERVICE": "gateway_model_service",
}
_UC_SECURABLE_KEY: Final = "uc_securable"
_KNOWN_TYPE_KEYS: Final = frozenset(_TYPES_BY_KEY) | {_UC_SECURABLE_KEY}


@dataclass(frozen=True, slots=True)
class BundleReading:
    """What the bundle file gave: offers, warnings, and which app of the file was read."""

    offers: tuple[BundleResource, ...] = ()
    warnings: list[str] = field(default_factory=list)
    chosen_app: str | None = None
    app_names: tuple[str, ...] = ()


class _Warnings:
    """Warnings of one file, prefixed with its name and capped so a hostile file stays small."""

    def __init__(self, file_name: str) -> None:
        self._file_name = file_name
        self._items: list[str] = []
        self._dropped = 0

    def add(self, text: str) -> None:
        if len(self._items) < _MAX_WARNINGS:
            self._items.append(f"{self._file_name}: {text}")
        else:
            self._dropped += 1

    def result(self) -> list[str]:
        if self._dropped:
            return [*self._items, f"{self._file_name}: {self._dropped} more warnings are not shown"]
        return list(self._items)


def read_bundle(
    root_files: Iterable[str],
    read_file: Callable[[str], bytes],
    *,
    app_name: str | None = None,
) -> BundleReading:
    """Read the offers of a snapshot's bundle file.

    ``app_name`` is the key of the child under ``resources.apps``; see the module docstring.
    """
    names = set(root_files)
    present = [name for name in BUNDLE_FILE_NAMES if name in names]
    if not present:
        return BundleReading()
    if len(present) == 2:
        both = " and ".join(BUNDLE_FILE_NAMES)
        return BundleReading(
            warnings=[f"{both} both exist; appmux reads neither, so keep one of them"]
        )

    file_name = present[0]
    warnings = _Warnings(file_name)
    document = _load(file_name, read_file, warnings)
    if document is None:
        return BundleReading(warnings=warnings.result())
    return _read_document(document, app_name, warnings)


def _load(
    file_name: str, read_file: Callable[[str], bytes], warnings: _Warnings
) -> dict[Any, Any] | None:
    """The parsed mapping, ``{}`` for an empty file, or ``None`` after a warning."""
    try:
        data = read_file(file_name)
    except OSError:
        warnings.add("could not be read from the snapshot; no resource is offered")
        return None
    if len(data) > MAX_BUNDLE_BYTES:
        warnings.add(f"is larger than {MAX_BUNDLE_BYTES} bytes; no resource is offered")
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        warnings.add("is not UTF-8 text; no resource is offered")
        return None
    try:
        document = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as exc:
        # RecursionError: a deeply nested flow sequence can exhaust the parser's stack.
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        warnings.add(f"is not valid YAML{where}; no resource is offered")
        return None
    if document is None:
        return {}
    if not isinstance(document, dict):
        warnings.add("must be a mapping with resources.apps; no resource is offered")
        return None
    return document


def _read_document(
    document: dict[Any, Any], app_name: str | None, warnings: _Warnings
) -> BundleReading:
    for key in document:
        if key not in _KNOWN_TOP_LEVEL_KEYS:
            warnings.add(
                f"top-level key '{_label(key)}' is ignored; appmux reads only"
                " resources.apps.<app>.resources"
            )

    resources = document.get("resources")
    if resources is not None and not isinstance(resources, dict):
        warnings.add("'resources' must be a mapping that holds resources.apps; nothing is offered")
        return BundleReading(warnings=warnings.result())
    for key in resources or {}:
        if key != "apps":
            warnings.add(f"resources key '{_label(key)}' is ignored; appmux reads only the apps")

    apps = (resources or {}).get("apps")
    if not isinstance(apps, dict) or not apps:
        warnings.add("declares no app under resources.apps; nothing is offered")
        return BundleReading(warnings=warnings.result())

    by_name = {str(key): value for key, value in apps.items()}
    app_names = tuple(sorted(by_name))
    listed = _label_list(app_names)
    if app_name is None:
        if len(by_name) != 1:
            warnings.add(
                f"declares {len(by_name)} apps under resources.apps ({listed}); choose the one"
                " to read, so nothing is offered"
            )
            return BundleReading(warnings=warnings.result(), app_names=app_names)
        chosen = app_names[0]
    elif app_name in by_name:
        chosen = app_name
    else:
        warnings.add(
            f"has no app '{_label(app_name)}' under resources.apps ({listed}); nothing is offered"
        )
        return BundleReading(warnings=warnings.result(), app_names=app_names)

    offers = _read_app(chosen, by_name[chosen], warnings)
    return BundleReading(
        offers=tuple(offers), warnings=warnings.result(), chosen_app=chosen, app_names=app_names
    )


def _read_app(app_key: str, app: object, warnings: _Warnings) -> list[BundleResource]:
    shown = _label(app_key)
    if not isinstance(app, dict):
        warnings.add(f"app '{shown}' must be a mapping; nothing is offered")
        return []
    for key in app:
        if key in _HARMLESS_APP_KEYS:
            continue
        if key == "permissions":
            warnings.add(
                f"app '{shown}': key 'permissions' is ignored; appmux never applies a grant"
                " from a bundle file"
            )
        else:
            warnings.add(f"app '{shown}': key '{_label(key)}' is ignored; appmux does not apply it")

    entries = app.get("resources")
    if entries is None:
        return []
    if not isinstance(entries, list):
        warnings.add(f"app '{shown}': 'resources' must be a list; nothing is offered")
        return []

    offers: list[BundleResource] = []
    names: set[str] = set()
    for position, raw in enumerate(entries, start=1):
        if position > _MAX_ENTRIES_SCANNED:
            warnings.add(f"only the first {_MAX_ENTRIES_SCANNED} entries are read")
            break
        if len(offers) >= MAX_OFFERS:
            warnings.add(f"only the first {MAX_OFFERS} resources are offered; the rest are ignored")
            break
        offer = _read_entry(position, raw, names, warnings)
        if offer is not None:
            names.add(offer.key)
            offers.append(offer)
    return offers


def _read_entry(
    position: int, raw: object, names: set[str], warnings: _Warnings
) -> BundleResource | None:
    if not isinstance(raw, dict):
        warnings.add(f"entry {position} is not a mapping; skipped")
        return None
    name = _text(raw.get("name"))
    if name is None:
        warnings.add(f"entry {position} needs a 'name' of short printable text; skipped")
        return None
    shown = _label(name)
    if name in names:
        warnings.add(f"resource '{shown}' appears more than once; only the first is offered")
        return None

    type_keys = [key for key in raw if key not in _ENTRY_META_KEYS]
    if not type_keys:
        warnings.add(f"resource '{shown}' has no resource type key; skipped")
        return None
    if len(type_keys) > 1:
        warnings.add(f"resource '{shown}' has more than one resource type key; skipped")
        return None
    type_key = type_keys[0]
    if type_key not in _KNOWN_TYPE_KEYS:
        warnings.add(f"resource '{shown}' has the unknown type '{_label(type_key)}'; skipped")
        return None
    body = raw[type_key]
    if not isinstance(body, dict):
        warnings.add(f"resource '{shown}': '{type_key}' must be a mapping; skipped")
        return None
    permission = _text(body.get("permission"))
    if permission is None:
        warnings.add(f"resource '{shown}': '{type_key}' needs a 'permission'; skipped")
        return None

    resolved = _resolve(type_key, body)
    if isinstance(resolved, str):
        warnings.add(f"resource '{shown}': {resolved}; skipped")
        return None
    resource_type, target, target_note = resolved

    if target_note is not None:
        warnings.add(f"resource '{shown}': {target_note}; type the target when you attach it")
    levels = levels_of(resource_type)
    if levels is not None and permission not in levels:
        warnings.add(
            f"resource '{shown}': permission '{_label(permission)}' is not a level of"
            f" {resource_type} ({', '.join(levels)}); pick one when you attach it"
        )
    if not is_resource_key(name):
        warnings.add(
            f"resource '{shown}': the name is not in the form of a resource key, so no valueFrom"
            " can name it"
        )
    return BundleResource(key=name, type=resource_type, target=target, permission=permission)


def _resolve(type_key: str, body: dict[Any, Any]) -> tuple[str, str | None, str | None] | str:
    """``(appmux type, target, why there is no target)``, or the reason the entry is skipped."""
    if type_key == _UC_SECURABLE_KEY:
        securable = _text(body.get("securable_type"))
        resource_type = _SECURABLE_TYPES.get(securable) if securable is not None else None
        if resource_type is None:
            kind = (
                "no securable_type"
                if securable is None
                else f"securable type '{_label(securable)}'"
            )
            return f"{kind} that appmux can attach"
        target, note = _securable_target(resource_type, body)
        return resource_type, target, note
    target, note = _target_of(type_key, body)
    return _TYPES_BY_KEY[type_key], target, note


def _target_of(type_key: str, body: dict[Any, Any]) -> tuple[str | None, str | None]:
    """The target text and, when there is none and it is not an interpolation, why."""
    if type_key == "secret":
        scope = _text(body.get("scope"))
        key = _text(body.get("key"))
        if scope is None or key is None:
            return None, "the file holds no usable scope and key"
        if _INTERPOLATION in scope or _INTERPOLATION in key:
            return None, None
        return f"{scope}/{key}", None
    field_name = _TARGET_FIELDS.get(type_key)
    if field_name is None:
        return None, "the file holds no target that appmux can use for this type"
    return _plain_target(body, field_name)


def _securable_target(resource_type: str, body: dict[Any, Any]) -> tuple[str | None, str | None]:
    target, note = _plain_target(body, "securable_full_name")
    if target is None or resource_type != "uc_volume":
        return target, note
    parts = target.split(".")
    if len(parts) != 3:
        return None, "the volume name is not catalog.schema.volume"
    return "/Volumes/" + "/".join(parts), None


def _plain_target(body: dict[Any, Any], field_name: str) -> tuple[str | None, str | None]:
    value = _text(body.get(field_name))
    if value is None:
        return None, f"the file holds no usable '{field_name}'"
    if _INTERPOLATION in value:
        return None, None
    return value, None


def _text(value: object) -> str | None:
    """Short printable text, or ``None``. A number is read as its digits (an id written bare).

    Control characters are refused: the result is stored as JSON in Postgres, which rejects NUL.
    """
    if isinstance(value, bool):
        return None
    text = str(value) if isinstance(value, int) else value
    if not isinstance(text, str) or text == "" or len(text) > _MAX_TEXT_LENGTH:
        return None
    return text if text.isprintable() else None


def _label(value: object) -> str:
    """A key or value of the file made safe to show: printable ASCII, bounded."""
    text = str(value)
    shown = "".join(
        c if c.isascii() and c.isprintable() else "?" for c in text[: _MAX_LABEL_LENGTH + 1]
    )
    return shown[:_MAX_LABEL_LENGTH] + ("..." if len(text) > _MAX_LABEL_LENGTH else "")


def _label_list(names: tuple[str, ...]) -> str:
    shown = ", ".join(_label(name) for name in names[:_MAX_LISTED_NAMES])
    hidden = len(names) - _MAX_LISTED_NAMES
    return f"{shown}, and {hidden} more" if hidden > 0 else shown
