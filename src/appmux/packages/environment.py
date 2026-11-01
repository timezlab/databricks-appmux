"""Do the parent's installed packages satisfy a child's ``requirements.txt``? (design 5.3, T090).

The first of the three ways a requirement is resolved is the environment appmux itself runs in: a
requirement it already satisfies needs no download, no index and no wheel. This module is that
check and nothing else; ``resolve.py`` calls it for each line and decides what to do with the lines
it cannot satisfy (a hashed wheel, the index, or a failure that lists every one of them).

``check_requirements`` reads a requirements file as text and returns a ``RequirementsReport``:
what the environment satisfies and, in ``unresolved``, one sentence for each line it does not.

What counts as satisfied: the package is installed and its version is inside the specifier, with
pre-releases allowed (an installed ``1.0rc1`` meets ``==1.0rc1``). A pin to a local version
(``==1.2.3+acme1``) is not met by plain ``1.2.3``: that pin is how an app forces its own build. A
plain pin is met by a local version, as PEP 440 says. An extra (``fastapi[standard]``) is met when
the installed package declares what the extra needs and the environment has it, one level deep.

What can never be satisfied from the environment: a direct URL, a path, a ``-r``/``-c``/``-e`` line
and a line that is not a requirement. Those are reported as unresolved, without echoing the line,
because a URL can hold a credential. Index options are noted and ignored: they select a source, and
this check has none.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import metadata
from typing import Final

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

# `pip` ends a comment at a '#' that starts the line or follows whitespace; '#' inside a URL
# fragment (`#egg=`) is not a comment.
_INLINE_COMMENT: Final = re.compile(r"(^|\s)#.*$")
_HASH_OPTION: Final = re.compile(r"\s--hash(?:=|\s+)\S+")
_UNSUPPORTED_OPTIONS: Final = frozenset(
    {"-r", "--requirement", "-c", "--constraint", "-e", "--editable"}
)
_LINE_CONTINUATION: Final = "\\"


@dataclass(frozen=True, slots=True)
class _Installed:
    version: Version
    requires: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Environment:
    """The installed distributions, by canonical name."""

    installed: Mapping[str, _Installed]

    def version_of(self, name: str) -> Version | None:
        entry = self.installed.get(canonicalize_name(name))
        return entry.version if entry is not None else None

    def requires_of(self, name: str) -> tuple[str, ...]:
        entry = self.installed.get(canonicalize_name(name))
        return entry.requires if entry is not None else ()


@dataclass(frozen=True, slots=True)
class RequirementsReport:
    satisfied_count: int
    total_count: int
    skipped_count: int
    unresolved: tuple[str, ...]
    notes: tuple[str, ...]

    def summary(self) -> str:
        return f"{self.satisfied_count} of {self.total_count} from the environment"


def read_environment(
    distributions: Iterable[metadata.Distribution] | None = None,
) -> Environment:
    """The environment of this process, or of the ``distributions`` given (tests)."""
    installed: dict[str, _Installed] = {}
    for distribution in metadata.distributions() if distributions is None else distributions:
        info = distribution.metadata
        name = info["Name"]
        raw_version = info["Version"]
        if not name or not raw_version:
            continue
        key = canonicalize_name(name)
        if key in installed:
            continue  # an earlier path entry shadows this copy, as it does for `import`
        try:
            version = Version(raw_version)
        except InvalidVersion:
            continue  # a legacy version no specifier can judge; it cannot satisfy anything
        installed[key] = _Installed(version, tuple(info.get_all("Requires-Dist") or ()))
    return Environment(installed)


def check_requirements(text: str, environment: Environment) -> RequirementsReport:
    """Check each requirement line of ``text`` against ``environment``."""
    satisfied = 0
    total = 0
    skipped = 0
    unresolved: list[str] = []
    notes: dict[str, None] = {}

    for line_number, line in _logical_lines(text):
        if line.startswith("-"):
            option = _option_name(line)
            if option in _UNSUPPORTED_OPTIONS:
                total += 1
                unresolved.append(
                    f"no wheel for line {line_number} (option {option} names another file or a "
                    "source tree, which the environment cannot satisfy)"
                )
            else:
                notes[f"ignored option: {option}"] = None
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            total += 1
            unresolved.append(
                f"no wheel for line {line_number} (it is not a package requirement the "
                "environment can satisfy)"
            )
            continue
        if requirement.marker is not None and not requirement.marker.evaluate():
            skipped += 1
            continue
        total += 1
        problems = _problems(requirement, environment)
        if problems:
            unresolved.extend(problems)
        else:
            satisfied += 1

    return RequirementsReport(satisfied, total, skipped, tuple(unresolved), tuple(notes))


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Requirement lines with continuations joined, comments and ``--hash`` options removed."""
    logical: list[tuple[int, str]] = []
    pending = ""
    first_number = 0
    # The trailing empty line flushes a file that ends in a continuation backslash.
    for number, physical in enumerate([*text.splitlines(), ""], start=1):
        if not pending:
            first_number = number
            if physical.lstrip().startswith("#"):
                continue
        if physical.endswith(_LINE_CONTINUATION):
            pending += physical[:-1] + " "
            continue
        joined, pending = pending + physical, ""
        cleaned = _HASH_OPTION.sub("", _INLINE_COMMENT.sub("", joined.strip())).strip()
        if cleaned:
            logical.append((first_number, cleaned))
    return logical


def _option_name(line: str) -> str:
    """``--index-url x`` -> ``--index-url``; ``-rfile`` -> ``-r``; never the option's argument."""
    head = re.split(r"[\s=]", line, maxsplit=1)[0]
    return head if head.startswith("--") else head[:2]


def _problems(requirement: Requirement, environment: Environment) -> list[str]:
    if requirement.url is not None:
        return [
            f"no wheel for '{requirement.name}' (a direct URL is not read from the environment)"
        ]
    version = environment.version_of(requirement.name)
    if version is None or not requirement.specifier.contains(version, prereleases=True):
        return [_unresolved_text(requirement, version)]
    return [
        f"{_unresolved_text(needed, environment.version_of(needed.name))}, "
        f"needed by extra '{extra}' of '{requirement.name}'"
        for extra in sorted(requirement.extras)
        for needed in _needed_by_extra(environment, requirement.name, extra)
        if not _is_met(needed, environment)
    ]


def _needed_by_extra(environment: Environment, package: str, extra: str) -> list[Requirement]:
    needed: list[Requirement] = []
    for raw in environment.requires_of(package):
        try:
            candidate = Requirement(raw)
        except InvalidRequirement:
            continue  # the installed package's own metadata is odd; the child cannot fix that
        marker = candidate.marker
        if marker is not None and marker.evaluate({"extra": extra}) and not marker.evaluate():
            needed.append(candidate)
    return needed


def _is_met(requirement: Requirement, environment: Environment) -> bool:
    version = environment.version_of(requirement.name)
    return version is not None and requirement.specifier.contains(version, prereleases=True)


def _unresolved_text(requirement: Requirement, installed: Version | None) -> str:
    has = f"{requirement.name} {installed}" if installed is not None else f"no {requirement.name}"
    return f"no wheel for '{_without_marker(requirement)}' (the environment has {has})"


def _without_marker(requirement: Requirement) -> str:
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    return f"{requirement.name}{extras}{requirement.specifier}"
