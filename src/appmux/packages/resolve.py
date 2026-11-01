"""Resolve a child's requirements: the environment, then wheels, then an index (FR-031, T134).

For each requirement of the app's ``requirements.txt`` (design 5.3), in this order:

1. **The environment satisfies it**: use the environment's copy. Nothing is fetched or installed.
   "Satisfies" is ``environment.check_requirements``, called per line, so there is one definition.
2. **Otherwise a wheel is needed**, and it overwrites whatever the environment has under that name
   in this release's overlay. A requirement can use a wheel only if it is pinned (``==``) with a
   ``--hash=sha256:...``; the wheel is found by that hash (``wheels.fetch_wheels``).
3. **If no wheel has it**, an index mode takes it from the index. In ``wheelhouse`` the Update fails
   and the reason lists *every* requirement that could not be resolved, in line order.

``package_mode`` decides which of 2 and 3 exist (``modes.PackageMode``). A line a build can never
install (``-r``, ``-e``, a direct URL, text that is not a requirement) fails in every mode, because
resolving it would mean following a path or a URL the child chose.

Options such as ``--index-url`` are noted and dropped: the build is handed only the requirements
this module produces, never a source the child named. In an index mode the hashes of an index
requirement are not passed on either (pip would then insist on hashes for the whole closure).

``recheck_environment`` is the check at every start: the environment can change under a release (a
redeploy of appmux), so the requirements it was satisfying are judged again before the child starts.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Final

from packaging.requirements import InvalidRequirement, Requirement
from packaging.tags import Tag
from packaging.version import InvalidVersion, Version

from appmux.packages.environment import Environment, check_requirements
from appmux.packages.modes import PackageMode
from appmux.packages.wheels import (
    FetchResult,
    ResolvedWheel,
    WheelRequest,
    WheelSource,
    WheelStore,
    fetch_wheels,
)

# `pip` ends a comment at a '#' that starts the line or follows whitespace.
_INLINE_COMMENT: Final = re.compile(r"(^|\s)#.*$")
_HASH_OPTION: Final = re.compile(r"\s--hash(?:=|\s+)(\S+)")
_SHA256_VALUE: Final = re.compile(r"sha256:([0-9a-fA-F]{64})")
_LINE_CONTINUATION: Final = "\\"
_NEEDS_PIN_AND_HASH: Final = "a wheel needs the requirement pinned with == and a --hash=sha256:..."


# --- reading the lock ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LockLine:
    number: int  # the physical line the logical line starts on
    text: str  # the requirement as written, without its ``--hash`` options
    sha256s: tuple[str, ...]  # lower-case hex; other hash algorithms are not wheel hashes


def _lock_lines(text: str) -> list[_LockLine]:
    """Logical lines with continuations joined and comments removed, ``--hash`` options kept apart.

    This reads a file the way ``environment._logical_lines`` does; that one drops the hashes, this
    one needs them.
    """
    lines: list[_LockLine] = []
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
        cleaned = _INLINE_COMMENT.sub("", joined.strip()).strip()
        if not cleaned:
            continue
        sha256s = tuple(
            match.group(1).lower()
            for value in _HASH_OPTION.findall(cleaned)
            if (match := _SHA256_VALUE.fullmatch(value)) is not None
        )
        lines.append(_LockLine(first_number, _HASH_OPTION.sub("", cleaned).strip(), sha256s))
    return lines


class _Verdict(Enum):
    IGNORED = auto()  # an option that selects a source: noted, not a requirement
    SKIPPED = auto()  # its marker is false on this platform
    ENVIRONMENT = auto()  # the environment satisfies it
    INSTALLABLE = auto()  # a wheel or the index could supply it
    UNSUPPORTED = auto()  # a path, a URL or text no build can install


@dataclass(frozen=True, slots=True)
class _Judged:
    line: _LockLine
    verdict: _Verdict
    requirement: Requirement | None = None
    problems: tuple[str, ...] = ()  # why the environment does not satisfy it
    notes: tuple[str, ...] = ()


def _judge(line: _LockLine, environment: Environment) -> _Judged:
    # The text is padded with blank lines so the report's "line N" is the line of the lock file.
    report = check_requirements("\n" * (line.number - 1) + line.text, environment)
    if report.total_count == 0 and report.skipped_count == 0:
        return _Judged(line, _Verdict.IGNORED, notes=report.notes)
    if report.skipped_count:
        return _Judged(line, _Verdict.SKIPPED)
    if not report.unresolved:
        return _Judged(line, _Verdict.ENVIRONMENT)
    requirement = _installable_requirement(line.text)
    verdict = _Verdict.UNSUPPORTED if requirement is None else _Verdict.INSTALLABLE
    return _Judged(line, verdict, requirement, problems=report.unresolved)


def _installable_requirement(text: str) -> Requirement | None:
    """The requirement in ``text``, or ``None`` if it is an option, a URL or not a requirement."""
    if text.startswith("-"):
        return None
    try:
        requirement = Requirement(text)
    except InvalidRequirement:
        return None
    return None if requirement.url is not None else requirement


def _requirement_text(requirement: Requirement) -> str:
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    return f"{requirement.name}{extras}{requirement.specifier}"


def _pinned_version(requirement: Requirement) -> Version | None:
    """The version of an exact ``==`` pin, or ``None`` for any other specifier."""
    specifiers = list(requirement.specifier)
    if len(specifiers) != 1 or specifiers[0].operator != "==":
        return None
    try:
        return Version(specifiers[0].version)
    except InvalidVersion:  # a wildcard such as "==2.*" is not a version, so not a pin
        return None


# --- the result ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    """How one release's requirements are met, and what is left over."""

    mode: PackageMode
    total_count: int
    from_environment: int
    from_wheels: int
    wheels: tuple[ResolvedWheel, ...]  # the release's wheel list, in the order of the lock
    index_requirements: tuple[str, ...]
    unresolved: tuple[str, ...]
    replaced: tuple[str, ...]  # a wheel replaces another version the environment holds
    notes: tuple[str, ...]

    @property
    def from_index(self) -> int:
        return len(self.index_requirements)

    @property
    def needs_overlay(self) -> bool:
        """A release that needs no wheel and no index package has no overlay (design 5.3)."""
        return bool(self.wheels or self.index_requirements)

    def failure_reason(self) -> str | None:
        return "; ".join(self.unresolved) if self.unresolved else None

    def summary(self) -> str:
        text = (
            f"{self.from_environment} of {self.total_count} from the environment, "
            f"{self.from_wheels} from wheels"
        )
        return f"{text}, {self.from_index} from the index" if self.index_requirements else text


async def resolve_requirements(
    requirements: str,
    *,
    mode: PackageMode,
    environment: Environment,
    store: WheelStore,
    sources: Sequence[WheelSource],
    stored_filenames: Mapping[str, str],
    work_dir: Path,
    supported_tags: Sequence[Tag] | None = None,
) -> Resolution:
    """Decide, for every requirement, whether the environment, a wheel or the index supplies it."""
    judged = [_judge(line, environment) for line in _lock_lines(requirements)]
    countable = [
        item for item in judged if item.verdict not in {_Verdict.IGNORED, _Verdict.SKIPPED}
    ]
    notes = tuple(dict.fromkeys(note for item in judged for note in item.notes))

    wheel_requests = [
        request for item in judged if (request := _wheel_request(item, mode)) is not None
    ]
    fetched = (
        await fetch_wheels(
            wheel_requests,
            store=store,
            sources=sources,
            stored_filenames=stored_filenames,
            work_dir=work_dir,
            supported_tags=supported_tags,
        )
        if wheel_requests
        else FetchResult((), ())
    )
    wheel_by_line = {match.request.line_number: match.wheel for match in fetched.matches}
    reason_by_line = {missing.request.line_number: missing.reason for missing in fetched.missing}

    problems: list[str] = []  # in line order: the loop walks the lock top to bottom
    index_requirements: list[str] = []
    replaced: list[str] = []
    from_environment = from_wheels = 0
    for item in countable:
        number = item.line.number
        requirement = item.requirement
        if item.verdict is _Verdict.ENVIRONMENT:
            from_environment += 1
        elif requirement is None:  # a path, a URL or text no build can install
            problems.extend(item.problems)
        elif number in wheel_by_line:
            from_wheels += 1
            replaced.extend(_replacement_note(requirement, environment))
        elif mode.uses_index:
            index_requirements.append(_requirement_text(requirement))
        elif number in reason_by_line:
            problems.append(reason_by_line[number])
        else:
            problems.append("; ".join((*item.problems, _NEEDS_PIN_AND_HASH)))

    return Resolution(
        mode=mode,
        total_count=len(countable),
        from_environment=from_environment,
        from_wheels=from_wheels,
        wheels=fetched.wheels,
        index_requirements=tuple(index_requirements),
        unresolved=tuple(problems),
        replaced=tuple(replaced),
        notes=notes,
    )


def _wheel_request(item: _Judged, mode: PackageMode) -> WheelRequest | None:
    """The request for the wheel that would meet ``item``, if a wheel can: pinned and hashed."""
    requirement = item.requirement
    if not mode.uses_wheels or item.verdict is not _Verdict.INSTALLABLE or requirement is None:
        return None
    version = _pinned_version(requirement)
    if version is None or not item.line.sha256s:
        return None
    return WheelRequest(
        requirement=_requirement_text(requirement),
        name=requirement.name,
        version=version,
        sha256s=item.line.sha256s,
        line_number=item.line.number,
    )


def _replacement_note(requirement: Requirement, environment: Environment) -> list[str]:
    version = _pinned_version(requirement)
    installed = environment.version_of(requirement.name)
    if version is None or installed is None or installed == version:
        return []
    return [
        f"{requirement.name} {version} from a wheel replaces the environment's "
        f"{requirement.name} {installed} for this release only"
    ]


# --- the check at every start -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Recheck:
    """What changed under a release since it was built."""

    unmet: tuple[str, ...]  # wheel modes only: ``'uvicorn==0.30.6' (it has 0.31.0)``
    index_requirements: tuple[str, ...]  # index modes: what the index must now supply

    def failure_reason(self) -> str | None:
        if not self.unmet:
            return None
        return f"the environment no longer provides {', '.join(self.unmet)}; add a wheel and Update"


def recheck_environment(
    requirements: str,
    *,
    mode: PackageMode,
    environment: Environment,
    wheel_sha256s: Collection[str],
) -> Recheck:
    """Judge again the requirements the environment was satisfying when the release was built.

    The registry records which wheels a release installed, not which requirements the environment
    met, so the second set is what remains: a requirement none of whose hashes is in
    ``wheel_sha256s`` was met by the environment (or, in an index mode, by the index). A line a
    build cannot install never got that far, so it is not looked at again.

    In ``wheelhouse`` mode a requirement that no longer holds fails the release before it starts.
    In an index mode it is handed back as an index requirement instead: the overlay key changes with
    it and the overlay is rebuilt, which is the same thing an Update would do.
    """
    backed = set(wheel_sha256s)
    unmet: list[str] = []
    index_requirements: list[str] = []
    for line in _lock_lines(requirements):
        if backed.intersection(line.sha256s):
            continue
        requirement = _judge(line, environment).requirement
        if requirement is None:  # met by the environment, skipped, an option or unsupported
            continue
        if mode.uses_index:
            index_requirements.append(_requirement_text(requirement))
        else:
            unmet.append(
                f"'{_requirement_text(requirement)}' ({_provided(requirement, environment)})"
            )
    return Recheck(tuple(unmet), tuple(index_requirements))


def _provided(requirement: Requirement, environment: Environment) -> str:
    version = environment.version_of(requirement.name)
    if version is None:
        return f"it has no {requirement.name}"
    if requirement.specifier.contains(version, prereleases=True):
        return f"it has {version}, which lacks what an extra of {requirement.name} needs"
    return f"it has {version}"
