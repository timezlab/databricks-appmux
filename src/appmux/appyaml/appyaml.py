"""The child's ``app.yaml``: only ``command`` and ``env`` are read (FR-033).

appmux follows the contract a standalone Databricks App has, so no appmux-specific file exists
(design 5.4). From the snapshot's root files this module builds the ``DetectedManifest`` stored with
the release; it is pure, because the caller passes the root file names and a way to read one file.

The parse, as the design's table states it:

* both ``app.yaml`` and ``app.yml`` present: the Update is refused;
* an ``env`` entry with both or neither of ``value`` and ``valueFrom``: refused, naming the entry;
* any other key: a warning on the manifest, and the key is ignored.

Two choices keep the manifest honest. Every scalar is read as the text written (a custom loader
without implicit typing), so ``value: 8080`` is ``"8080"`` and ``value: no`` is ``"no"``, not an
integer or the YAML 1.1 boolean ``False``; an environment variable is text. And a duplicate key is
refused, because PyYAML would silently keep the last one.

``$NAME`` substitution is a separate function because it needs the child's environment, which only
the start path knows. Measured in spike 17: Databricks replaces ``$NAME`` in a ``command`` argument
but not ``${NAME}`` and not inside an ``env`` value; this module does the same and runs no shell.

A refusal message names the file, the key or the entry and the rule; it never carries a value, since
an ``env`` value can be a secret someone pasted by mistake.

The snapshot's bundle file ``databricks.yml`` is read by ``appyaml.bundle`` into offers and warnings
that join the manifest; a file that cannot be used never refuses the Update (FR-034).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

import yaml
from pydantic import ValidationError

from appmux.appyaml.bundle import read_bundle
from appmux.registry.models import DetectedManifest, EnvEntry
from appmux.registry.resources import is_resource_key

APP_YAML_NAMES: Final = ("app.yaml", "app.yml")
# An app.yaml is a handful of lines; a bound keeps a hostile snapshot from costing the parser time.
MAX_APP_YAML_BYTES: Final = 64 * 1024

_KNOWN_TOP_LEVEL_KEYS: Final = frozenset({"command", "env"})
_KNOWN_ENV_KEYS: Final = frozenset({"name", "value", "valueFrom"})
_DOLLAR_NAME: Final = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_NUL: Final = "\x00"


class AppYamlInvalid(ValueError):
    """The snapshot's ``app.yaml`` cannot be used; the Update is refused with this text."""


class _TextLoader(yaml.SafeLoader):
    """``SafeLoader`` that keeps plain scalars as text and refuses duplicate mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, "a mapping key appears twice", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


# No implicit resolvers: 8080, true, no, null and 0.10 stay the text that was written.
_TextLoader.yaml_implicit_resolvers = {}


def read_manifest(
    root_files: Iterable[str],
    read_file: Callable[[str], bytes],
    *,
    bundle_app: str | None = None,
) -> DetectedManifest:
    """Build the manifest of a snapshot from its root file names and a reader for one of them.

    ``bundle_app`` is the key under ``resources.apps`` of ``databricks.yml`` that is the child, for
    a file that declares several apps; the offers of the file join the manifest.
    """
    names = set(root_files)
    present = [name for name in APP_YAML_NAMES if name in names]
    if len(present) == 2:
        raise AppYamlInvalid(
            "the snapshot has both app.yaml and app.yml; keep one of them and Update again"
        )
    runtime = _detect_runtime(names)
    if not present:
        bundle = read_bundle(names, read_file, app_name=bundle_app)
        return DetectedManifest(
            runtime=runtime, bundle_resources=list(bundle.offers), warnings=bundle.warnings
        )

    file_name = present[0]
    document = _load_document(file_name, read_file(file_name))
    warnings = [
        f"{file_name}: unknown key '{key}' ignored"
        for key in document
        if key not in _KNOWN_TOP_LEVEL_KEYS
    ]
    command = _read_command(file_name, document.get("command"))
    env = _read_env(file_name, document.get("env"), warnings)
    bundle = read_bundle(names, read_file, app_name=bundle_app)
    return DetectedManifest(
        runtime=runtime,
        command=command,
        env=env,
        bundle_resources=list(bundle.offers),
        warnings=[*warnings, *bundle.warnings],
    )


def substitute_command_arguments(
    command: Iterable[str], environment: Mapping[str, str]
) -> list[str]:
    """Replace ``$NAME`` in each argument with its value from ``environment``.

    ``${NAME}`` and a name the environment does not have are left as written, and a replaced value
    is not expanded again.
    """

    def replace(match: re.Match[str]) -> str:
        return environment.get(match.group(1), match.group(0))

    return [_DOLLAR_NAME.sub(replace, argument) for argument in command]


def _detect_runtime(root_files: set[str]) -> str | None:
    """As Databricks does: Python markers first, then ``package.json``."""
    if "requirements.txt" in root_files or {"pyproject.toml", "uv.lock"} <= root_files:
        return "python"
    if "package.json" in root_files:
        return "node"
    return None


def _load_document(file_name: str, data: bytes) -> dict[str, Any]:
    if len(data) > MAX_APP_YAML_BYTES:
        raise AppYamlInvalid(f"{file_name} is larger than {MAX_APP_YAML_BYTES} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AppYamlInvalid(f"{file_name} is not UTF-8 text") from exc
    try:
        document = yaml.load(text, Loader=_TextLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise AppYamlInvalid(_yaml_error_text(file_name, exc)) from exc
    except RecursionError as exc:
        # A deeply nested flow sequence under the size limit exhausts the parser's stack; without
        # this the Update of the app's own manager would answer 500 (T182).
        raise AppYamlInvalid(f"{file_name} is nested too deeply to read") from exc
    if document is None or document == "":
        return {}
    if not isinstance(document, dict):
        raise AppYamlInvalid(f"{file_name} must be a mapping with 'command' and 'env' keys")
    return document


def _yaml_error_text(file_name: str, exc: yaml.YAMLError) -> str:
    # The parser's own message quotes the offending line, which could hold a pasted secret; only
    # the line number and the rule it broke are kept.
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    where = f" (line {mark.line + 1})" if mark is not None else ""
    rule = f": {problem}" if isinstance(problem, str) else ""
    return f"{file_name} is not valid YAML{where}{rule}"


def _read_command(file_name: str, value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise AppYamlInvalid(
            f"{file_name}: 'command' must be a non-empty list of arguments (appmux runs no shell)"
        )
    if not all(isinstance(argument, str) for argument in value):
        raise AppYamlInvalid(f"{file_name}: every 'command' argument must be text")
    if value[0] == "":
        raise AppYamlInvalid(f"{file_name}: the first 'command' argument is empty")
    if any(_NUL in argument for argument in value):
        raise AppYamlInvalid(f"{file_name}: a 'command' argument holds a NUL character")
    return list(value)


def _read_env(file_name: str, value: object, warnings: list[str]) -> list[EnvEntry]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AppYamlInvalid(f"{file_name}: 'env' must be a list of entries")
    entries: list[EnvEntry] = []
    for position, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise AppYamlInvalid(f"{file_name}: env entry {position} is not a mapping")
        entries.append(_read_env_entry(file_name, position, raw, warnings))
    return entries


def _read_env_entry(
    file_name: str, position: int, raw: dict[str, Any], warnings: list[str]
) -> EnvEntry:
    name = raw.get("name")
    if not isinstance(name, str) or name == "" or "=" in name or _NUL in name:
        raise AppYamlInvalid(
            f"{file_name}: env entry {position} needs a 'name' that is text without '=' or NUL"
        )
    for key in raw:
        if key not in _KNOWN_ENV_KEYS:
            warnings.append(f"{file_name}: env entry '{name}' has unknown key '{key}' ignored")
    has_value = "value" in raw
    has_value_from = "valueFrom" in raw
    if has_value and has_value_from:
        raise AppYamlInvalid(
            f"{file_name}: env entry '{name}' sets both value and valueFrom; set exactly one"
        )
    if not has_value and not has_value_from:
        raise AppYamlInvalid(
            f"{file_name}: env entry '{name}' sets neither value nor valueFrom; set exactly one"
        )
    literal = raw.get("value")
    resource_key = raw.get("valueFrom")
    if has_value and (not isinstance(literal, str) or _NUL in literal):
        raise AppYamlInvalid(f"{file_name}: the value of env entry '{name}' must be text")
    if has_value_from and (not isinstance(resource_key, str) or resource_key == ""):
        raise AppYamlInvalid(
            f"{file_name}: the valueFrom of env entry '{name}' must be a non-empty resource key"
        )
    if isinstance(resource_key, str) and not is_resource_key(resource_key):
        # Not a refusal: the Update keeps working, and the release then fails the requirements
        # check with a clear list. The key itself is not repeated: it is the child's own text.
        warnings.append(
            f"{file_name}: env entry '{name}' has a valueFrom that is not in the form of a resource"
            " key (lowercase letters, digits, '.', '_' and '-', at most 63 characters), so no"
            " resource can match it"
        )
    try:
        return EnvEntry(name=name, value=literal, value_from=resource_key)
    except ValidationError as exc:  # pragma: no cover - the checks above cover every rule
        raise AppYamlInvalid(f"{file_name}: env entry '{name}' is invalid") from exc
