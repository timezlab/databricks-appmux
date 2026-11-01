"""Build a release's overlay inside the sandbox, cache it, and publish it atomically (T135).

The overlay is a directory of packages that sits in front of the app environment (design 5.3). It
is built from the release's own wheel list, so another app's wheel never reaches it, and it is built
*inside the sandbox* because installing a wheel or building a source distribution runs untrusted
code. This module decides what to run and in what order; the sandbox itself is behind the
``SandboxRunner`` seam, because ``packages`` starts no processes (the module-boundary test forbids
``subprocess`` here) and the launcher lives in ``sandbox``.

For an overlay folder ``O`` and a directory ``W`` holding exactly the release's wheels, the steps
are, in order, each with its own egress list (``modes.build_egress``) and all inside one time budget
(``build_timeout_s``):

* ``wheels``: ``python -m pip install --no-index --no-deps --require-hashes --find-links W
  --target O -r wheels-needed.txt``. Offline, so its egress list is all-deny.
* ``index`` (index modes only): ``pip install --target O -r index-needed.txt`` with the app's own
  egress rules, and ``--only-binary=:all:`` unless the mode builds source distributions.
* ``check``: ``pip check`` with ``PYTHONPATH=O``. Its output is a warning on the release, never a
  failure (research D6): an overlay that conflicts with an environment package may still work, and
  the health check is the gate.

The directory ``W`` appears in the sandbox where a release does (``/srv/app``, read-only) and ``O``
at the build-output path (``/srv/build``, writable). A finished overlay is renamed into the cache in
one step, so a reader never sees a half-built one; the cache is a directory under ``/tmp`` and is
keyed by ``overlay_key``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Final, Literal, Protocol
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement

from appmux.packages.modes import PackageMode, build_egress
from appmux.packages.wheels import ResolvedWheel, WheelStore, parse_wheel_file, stage_wheels
from appmux.sandbox.launcher import (
    LAUNCH_SOCKET_NAME,
    STOP_GRACE_S,
    SUPERVISOR_LOG_NAME,
    SandboxExit,
    SandboxSpec,
    SandboxStartError,
    start_sandbox,
)
from appmux.sandbox.pump import write_rules_file
from appmux.sandbox.rootfs import BUILD_OUTPUT_PATH, RELEASE_PATH
from appmux.sandbox.rules import EgressRule

# The wheel directory is mounted read-only where a release is, the overlay under construction
# read-write at the build-output path (``rootfs.plan_rootfs``); the commands name those places.
WHEEL_DIR_IN_SANDBOX: Final = RELEASE_PATH
OVERLAY_DIR_IN_SANDBOX: Final = BUILD_OUTPUT_PATH
WHEEL_REQUIREMENTS_FILE: Final = "wheels-needed.txt"
INDEX_REQUIREMENTS_FILE: Final = "index-needed.txt"

_KEY_FORMAT_VERSION: Final = 1
_PIP_CHECK_NOTE_SUFFIX: Final = ".pip-check.txt"
_SCRATCH_PREFIX: Final = ".build-"
_LOG_TAIL_LINES: Final = 20
_LOG_LINE_CHARS: Final = 300
_MAX_WARNING_LINES: Final = 20
_BASE_ENVIRONMENT: Final = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/app",
    "PIP_NO_CACHE_DIR": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
}

# The sandbox that runs a step (``SandboxStepRunner``).
BUILD_SLUG: Final = "appmux-build"
BUILD_TMPDIR_NAME: Final = ".build-tmp"  # inside the overlay under construction; removed after
BUILD_LOG_READ_BYTES: Final = 64 * 1024  # the end of a step's output that the builder gets
_BUILD_LOG_NAME: Final = "build.log"
_BUILD_RULES_NAME: Final = "rules.json"
_SANDBOX_START_TIMEOUT_S: Final = 60.0  # at most; a short build budget shortens it
_WALL_CLOCK_MARGIN_S: Final = STOP_GRACE_S + 5.0  # the launcher's own limit is a backstop
_WALL_CLOCK_REASON: Final = "wall-clock limit"  # the start of the reason ``SandboxExit`` carries
_MAX_SOCKET_PATH_BYTES: Final = 107  # sockaddr_un.sun_path is 108 bytes including the NUL
_INSTALL_LABELS: Final = frozenset({"wheels", "index"})
_REDACTED: Final = "***"
_MIN_HIDDEN_CHARS: Final = 4  # a shorter string would blank out ordinary words
# The only variables of this process's own environment that a build may take for the package index.
INDEX_ENVIRONMENT_NAMES: Final = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST")

StepLabel = Literal["wheels", "index", "check"]


# --- errors -------------------------------------------------------------------------------------


class OverlayError(Exception):
    """The overlay could not be planned or built; the message says what to change."""


class OverlayBuildFailed(OverlayError):
    """A build step failed. ``reason`` is the sentence for the release; ``log_tail`` its log end."""

    def __init__(self, reason: str, *, log_tail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.log_tail = log_tail


# --- the sandbox seam ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BuildStep:
    """One command to run in the build sandbox, as data."""

    label: StepLabel
    argv: tuple[str, ...]  # the interpreter and its arguments: never a shell string
    environment: Mapping[str, str]
    egress: tuple[EgressRule, ...]


@dataclass(frozen=True, slots=True)
class StepResult:
    exit_code: int | None  # ``None``: the command did not exit on its own (killed or timed out)
    timed_out: bool
    output: str  # stdout and stderr together


class SandboxRunner(Protocol):
    """Runs one step in a fresh sandbox: ``wheel_dir`` read-only, ``overlay_dir`` writable."""

    async def run(
        self, step: BuildStep, *, wheel_dir: Path, overlay_dir: Path, timeout_s: float
    ) -> StepResult: ...


class BuildSandbox(Protocol):
    """The part of ``sandbox.launcher.SandboxHandle`` a build step uses."""

    async def wait_started(self, *, timeout_s: float) -> None: ...

    async def wait(self) -> SandboxExit: ...

    async def stop(self, *, grace_s: float) -> SandboxExit: ...


StartSandbox = Callable[[SandboxSpec], Awaitable[BuildSandbox]]


class SandboxStepRunner:
    """Runs a build step in a sandbox of its own, through the launcher and nothing else.

    The wheel directory is the sandbox's "release" (read-only at ``/srv/app``) and the overlay
    under construction its build output (writable at ``/srv/build``); the step's egress list is
    the rules file the pump enforces, with the platform's global rules behind it. A step starts
    with no overlay and no other mount: it sees the app environment and the system directories, as
    a child does, and nothing of any release.

    ``/tmp`` in the sandbox is a small tmpfs, and pip unpacks a wheel into a temporary directory
    before it moves the files into ``--target``: an install step therefore gets its ``TMPDIR`` in
    the build output, on the disk that holds the overlay, and the directory is removed afterwards
    so it never ships. Everything else the step leaves (its log, the rules file, the launcher's
    sockets) is in a scratch directory that is removed whatever the outcome; the failures the
    launcher explains by pointing at its log carry the end of that log instead.
    """

    def __init__(
        self,
        *,
        run_base: Path,
        app_environment: Path,
        container_ip: IPv4Address,
        appmux_port: int,
        global_rules: Callable[[], Sequence[EgressRule]] = tuple,
        start: StartSandbox = start_sandbox,
        memory_budget_mb: int | None = None,
    ) -> None:
        self._run_base = run_base
        self._app_environment = app_environment
        self._container_ip = container_ip
        self._appmux_port = appmux_port
        self._global_rules = global_rules
        self._start = start
        self._memory_budget_mb = memory_budget_mb

    async def run(
        self, step: BuildStep, *, wheel_dir: Path, overlay_dir: Path, timeout_s: float
    ) -> StepResult:
        scratch = await self._make_scratch()
        try:
            return await self._run_in(
                scratch, step, wheel_dir=wheel_dir, overlay_dir=overlay_dir, timeout_s=timeout_s
            )
        finally:
            # Best effort and shielded: a cancelled build still cleans up, and a leftover
            # directory only wastes /tmp, which a restart empties.
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, scratch, True))
            await asyncio.shield(
                asyncio.to_thread(shutil.rmtree, overlay_dir / BUILD_TMPDIR_NAME, True)
            )

    async def _make_scratch(self) -> Path:
        try:
            scratch = await asyncio.to_thread(self._make_scratch_directory)
        except OSError as exc:
            raise OverlayBuildFailed(
                f"the run directory of the build sandbox could not be prepared "
                f"({exc.strerror or 'I/O error'}); check the free space and permissions "
                f"of {self._run_base}"
            ) from exc
        size = len(os.fsencode(scratch / LAUNCH_SOCKET_NAME))
        if size > _MAX_SOCKET_PATH_BYTES:
            await asyncio.to_thread(shutil.rmtree, scratch, True)
            raise OverlayBuildFailed(
                f"the run directory {self._run_base} is too long for a unix socket "
                f"({size} bytes, at most {_MAX_SOCKET_PATH_BYTES}); use a shorter run directory"
            )
        return scratch

    def _make_scratch_directory(self) -> Path:
        self._run_base.mkdir(parents=True, exist_ok=True)
        # ``mkdtemp`` creates it private (0o700): the supervisor's log may hold a pip error text.
        return Path(tempfile.mkdtemp(prefix="build-", dir=self._run_base))

    async def _run_in(
        self,
        scratch: Path,
        step: BuildStep,
        *,
        wheel_dir: Path,
        overlay_dir: Path,
        timeout_s: float,
    ) -> StepResult:
        log_path = scratch / _BUILD_LOG_NAME
        spec = SandboxSpec(
            slug=BUILD_SLUG,
            command=step.argv,
            environment=self._environment_for(step),
            app_environment=self._app_environment,
            release_dir=wheel_dir,
            build_output=overlay_dir,
            scratch_dir=scratch,
            log_path=log_path,
            rules_path=scratch / _BUILD_RULES_NAME,
            memory_budget_mb=self._memory_budget_mb,
            wall_clock_limit_s=timeout_s + _WALL_CLOCK_MARGIN_S,
        )
        try:
            await asyncio.to_thread(self._prepare, spec, step, overlay_dir)
        except OSError as exc:
            raise OverlayBuildFailed(
                f"the build sandbox could not be prepared ({exc.strerror or 'I/O error'}); "
                "check the free space and permissions of the build directories"
            ) from exc
        try:
            process = await self._start(spec)
        except SandboxStartError as exc:
            raise await self._start_failure(exc, scratch, log_path) from exc
        outcome, timed_out = await self._wait_for_end(process, timeout_s, scratch, log_path)
        output = await asyncio.to_thread(_read_tail_bytes, log_path, BUILD_LOG_READ_BYTES)
        killed = timed_out or outcome.signal is not None
        return StepResult(
            exit_code=None if killed else outcome.status,
            timed_out=timed_out or outcome.reason.startswith(_WALL_CLOCK_REASON),
            output=output,
        )

    def _environment_for(self, step: BuildStep) -> dict[str, str]:
        environment = dict(step.environment)
        if step.label in _INSTALL_LABELS:
            environment["TMPDIR"] = f"{OVERLAY_DIR_IN_SANDBOX}/{BUILD_TMPDIR_NAME}"
        return environment

    def _prepare(self, spec: SandboxSpec, step: BuildStep, overlay_dir: Path) -> None:
        write_rules_file(
            spec.rules_path,
            app_rules=step.egress,
            global_rules=self._global_rules(),
            container_ip=self._container_ip,
            appmux_port=self._appmux_port,
        )
        if step.label in _INSTALL_LABELS:
            (overlay_dir / BUILD_TMPDIR_NAME).mkdir(exist_ok=True)

    async def _wait_for_end(
        self, process: BuildSandbox, timeout_s: float, scratch: Path, log_path: Path
    ) -> tuple[SandboxExit, bool]:
        """How the sandbox ended, and whether the budget ran out first; it never outlives a call."""
        ended = False
        try:
            try:
                async with asyncio.timeout(timeout_s):
                    await process.wait_started(timeout_s=min(timeout_s, _SANDBOX_START_TIMEOUT_S))
                    outcome = await process.wait()
                ended = True
                return outcome, False
            except TimeoutError:
                outcome = await process.stop(grace_s=STOP_GRACE_S)
                ended = True
                return outcome, True
            except SandboxStartError as exc:
                raise await self._start_failure(exc, scratch, log_path) from exc
        finally:
            if not ended:
                # An error or a cancellation: no sandbox is left running behind it.
                await asyncio.shield(process.stop(grace_s=STOP_GRACE_S))

    async def _start_failure(
        self, exc: SandboxStartError, scratch: Path, log_path: Path
    ) -> OverlayBuildFailed:
        # The launcher's messages point at files in the scratch directory, which is about to go.
        tail = await asyncio.to_thread(_read_tail_bytes, log_path, BUILD_LOG_READ_BYTES)
        if not tail.strip():
            tail = await asyncio.to_thread(
                _read_tail_bytes, scratch / SUPERVISOR_LOG_NAME, BUILD_LOG_READ_BYTES
            )
        return OverlayBuildFailed(f"the build sandbox could not start: {exc}", log_tail=_tail(tail))


def _read_tail_bytes(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def pip_index_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The package-index settings of ``environ`` (pip's own variables), for ``index_environment``.

    Only these reach the index step, and only that step: a wheel install and ``pip check`` need no
    index, and a child never gets them. They may carry a credential (``https://user:token@...``),
    which is why the step is the one place a build reads them.
    """
    return {name: environ[name] for name in INDEX_ENVIRONMENT_NAMES if environ.get(name)}


# --- planning the commands ----------------------------------------------------------------------


def plan_build(
    mode: PackageMode,
    *,
    python: str,
    has_wheels: bool,
    has_index_requirements: bool,
    app_rules: Sequence[EgressRule],
    index_environment: Mapping[str, str] | None = None,
) -> tuple[BuildStep, ...]:
    """The steps that build an overlay for ``mode``; ``python`` is the sandbox's own path to it."""
    if not python.startswith("/"):
        raise OverlayError(
            "the build's Python interpreter must be an absolute path inside the sandbox"
        )
    if has_wheels and not mode.uses_wheels:
        raise OverlayError(f"the package mode '{mode.value}' does not install wheels")
    if has_index_requirements and not mode.uses_index:
        raise OverlayError(f"the package mode '{mode.value}' has no package index to install from")

    steps: list[BuildStep] = []
    if has_wheels:
        steps.append(
            BuildStep(
                label="wheels",
                argv=(
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--require-hashes",
                    "--find-links",
                    WHEEL_DIR_IN_SANDBOX,
                    "--target",
                    OVERLAY_DIR_IN_SANDBOX,
                    "-r",
                    f"{WHEEL_DIR_IN_SANDBOX}/{WHEEL_REQUIREMENTS_FILE}",
                ),
                environment=dict(_BASE_ENVIRONMENT),
                egress=build_egress(mode, app_rules, needs_index=False),
            )
        )
    if has_index_requirements:
        only_binary = () if mode.builds_source_distributions else ("--only-binary=:all:",)
        steps.append(
            BuildStep(
                label="index",
                argv=(
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--target",
                    OVERLAY_DIR_IN_SANDBOX,
                    "-r",
                    f"{WHEEL_DIR_IN_SANDBOX}/{INDEX_REQUIREMENTS_FILE}",
                    *only_binary,
                ),
                environment={**_BASE_ENVIRONMENT, **(index_environment or {})},
                egress=build_egress(mode, app_rules, needs_index=True),
            )
        )
    steps.append(
        BuildStep(
            label="check",
            argv=(python, "-m", "pip", "check"),
            environment={**_BASE_ENVIRONMENT, "PYTHONPATH": OVERLAY_DIR_IN_SANDBOX},
            egress=build_egress(mode, app_rules, needs_index=False),
        )
    )
    return tuple(steps)


def wheel_requirements_text(wheels: Sequence[ResolvedWheel]) -> str:
    """``wheels-needed.txt``: each wheel pinned to its name, version and hash (--require-hashes).

    The name and version come from the file name, which ``parse_wheel_file`` has checked; pip
    matches them against the wheels in the directory and the hash against their bytes.
    """
    lines: dict[str, str] = {}
    for wheel in wheels:
        parsed = parse_wheel_file(wheel.filename)
        lines.setdefault(
            wheel.sha256, f"{parsed.name}=={parsed.version} --hash=sha256:{wheel.sha256}"
        )
    return "".join(f"{line}\n" for line in lines.values())


def index_requirements_text(requirements: Sequence[str]) -> str:
    """``index-needed.txt``: plain requirements only, so no option or second line can ride along."""
    for text in requirements:
        try:
            requirement = Requirement(text)
        except InvalidRequirement as exc:
            raise OverlayError(
                "an index requirement must be a plain package requirement (name, extras, version)"
            ) from exc
        if requirement.url is not None:
            raise OverlayError("an index requirement must not be a direct URL")
    return "".join(f"{text}\n" for text in requirements)


# --- the cache key ------------------------------------------------------------------------------


def running_python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def running_platform() -> str:
    """The most specific tag of this interpreter, e.g. ``cp311-cp311-manylinux_2_39_x86_64``."""
    from appmux.packages.wheels import running_tags

    return str(running_tags()[0])


def overlay_key(
    wheels: Sequence[ResolvedWheel],
    index_requirements: Sequence[str],
    *,
    python_version: str,
    platform: str,
) -> str:
    """The digest that names an overlay: the wheel list, the index requirements, Python, platform.

    Wheels count by their bytes (sha256) and the lists as sets, so the same packages in another
    order, or a wheel named twice, give the same overlay.
    """
    material = {
        "format": _KEY_FORMAT_VERSION,
        "python": python_version,
        "platform": platform,
        "wheels": sorted({wheel.sha256 for wheel in wheels}),
        "index": sorted(set(index_requirements)),
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


# --- the builder --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Overlay:
    key: str
    path: Path  # the finished directory: mount it read-only at ``rootfs.overlay_path(0)``
    warnings: tuple[str, ...]  # ``pip check`` lines, shown on the release
    built: bool  # ``False``: an earlier build was reused


def launch_overlays(overlay: Overlay | None) -> tuple[Path, ...]:
    """The ``SandboxSpec.overlays`` of a child; the launcher puts them on PYTHONPATH and PATH.

    No overlay means the child runs on the environment alone.
    """
    return (overlay.path,) if overlay is not None else ()


class OverlayBuilder:
    def __init__(
        self,
        *,
        runner: SandboxRunner,
        store: WheelStore,
        cache_dir: Path,
        python: str,
        build_timeout_s: float,
        python_version: str | None = None,
        platform: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        index_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._runner = runner
        self._store = store
        self._cache_dir = cache_dir
        self._python = python
        self._build_timeout_s = build_timeout_s
        self._python_version = python_version or running_python_version()
        self._platform = platform or running_platform()
        self._clock = clock
        self._index_environment = index_environment
        self._hidden = _credentials_in(index_environment or {})
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure(
        self,
        *,
        mode: PackageMode,
        wheels: Sequence[ResolvedWheel],
        index_requirements: Sequence[str],
        app_rules: Sequence[EgressRule],
        build_timeout_s: float | None = None,
    ) -> Overlay | None:
        """The overlay for this wheel list, built if not cached; ``None`` if none is needed.

        ``build_timeout_s`` replaces the budget the builder was made with: it is a platform setting
        an admin can change while appmux runs, so the caller reads it again at every build.
        """
        if not wheels and not index_requirements:
            return None
        steps = plan_build(
            mode,
            python=self._python,
            has_wheels=bool(wheels),
            has_index_requirements=bool(index_requirements),
            app_rules=app_rules,
            index_environment=self._index_environment,
        )
        key = overlay_key(
            wheels,
            index_requirements,
            python_version=self._python_version,
            platform=self._platform,
        )
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = await asyncio.to_thread(self._lookup, key)
            if cached is not None:
                return cached
            budget_s = build_timeout_s if build_timeout_s is not None else self._build_timeout_s
            return await self._build(key, steps, wheels, index_requirements, budget_s)

    def _lookup(self, key: str) -> Overlay | None:
        path = self._cache_dir / key
        if not path.is_dir():
            return None
        note = self._cache_dir / f"{key}{_PIP_CHECK_NOTE_SUFFIX}"
        warnings = note.read_text(encoding="utf-8").splitlines() if note.is_file() else []
        return Overlay(key, path, tuple(warnings), built=False)

    async def _build(
        self,
        key: str,
        steps: Sequence[BuildStep],
        wheels: Sequence[ResolvedWheel],
        index_requirements: Sequence[str],
        budget_s: float,
    ) -> Overlay:
        deadline = self._clock() + budget_s
        scratch = await asyncio.to_thread(self._make_scratch, key)
        wheel_dir, overlay_dir = scratch / "wheels", scratch / "overlay"
        try:
            await asyncio.to_thread(wheel_dir.mkdir)
            await asyncio.to_thread(overlay_dir.mkdir)
            await stage_wheels(self._store, wheels, wheel_dir)
            await asyncio.to_thread(_write_requirements, wheel_dir, wheels, index_requirements)
            warnings: tuple[str, ...] = ()
            for step in steps:
                remaining = deadline - self._clock()
                if step.label == "check":
                    warnings = await self._pip_check(step, wheel_dir, overlay_dir, remaining)
                else:
                    await self._install(step, wheel_dir, overlay_dir, remaining, budget_s)
            return await asyncio.to_thread(self._publish, key, overlay_dir, warnings)
        finally:
            # Best effort: a leftover scratch directory only wastes /tmp, which a stop empties, and
            # raising here would hide the build's real outcome.
            await asyncio.to_thread(shutil.rmtree, scratch, True)

    def _make_scratch(self, key: str) -> Path:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # Inside the cache, so the final rename stays on one filesystem and is atomic.
        return Path(tempfile.mkdtemp(prefix=f"{_SCRATCH_PREFIX}{key[:12]}-", dir=self._cache_dir))

    async def _run_step(
        self, step: BuildStep, wheel_dir: Path, overlay_dir: Path, remaining_s: float
    ) -> StepResult:
        """The runner's result; a failure it raises is cleaned of the index credential too."""
        try:
            return await self._runner.run(
                step, wheel_dir=wheel_dir, overlay_dir=overlay_dir, timeout_s=remaining_s
            )
        except OverlayBuildFailed as exc:
            raise OverlayBuildFailed(
                self._hide_credentials(exc.reason), log_tail=self._hide_credentials(exc.log_tail)
            ) from exc

    def _hide_credentials(self, text: str) -> str:
        for credential in self._hidden:
            text = text.replace(credential, _REDACTED)
        return text

    async def _install(
        self,
        step: BuildStep,
        wheel_dir: Path,
        overlay_dir: Path,
        remaining_s: float,
        budget_s: float,
    ) -> None:
        if remaining_s <= 0:
            raise OverlayBuildFailed(_timeout_reason(budget_s))
        result = await self._run_step(step, wheel_dir, overlay_dir, remaining_s)
        log_tail = self._hide_credentials(_tail(result.output))
        if result.timed_out:
            raise OverlayBuildFailed(_timeout_reason(budget_s), log_tail=log_tail)
        if result.exit_code != 0:
            what = "the wheels" if step.label == "wheels" else "the packages from the index"
            ended = (
                f"exit code {result.exit_code}"
                if result.exit_code is not None
                else "no exit code (the command was killed)"
            )
            raise OverlayBuildFailed(
                f"installing {what} failed ({ended}); the end of the build log is attached",
                log_tail=log_tail,
            )

    async def _pip_check(
        self, step: BuildStep, wheel_dir: Path, overlay_dir: Path, remaining_s: float
    ) -> tuple[str, ...]:
        """The conflicts ``pip check`` reports. It never raises for them: they are warnings."""
        if remaining_s <= 0:
            return ("pip check was skipped: the install used the whole build time",)
        result = await self._run_step(step, wheel_dir, overlay_dir, remaining_s)
        if result.timed_out or result.exit_code is None:
            return ("pip check did not finish in time; conflicts, if any, are unknown",)
        if result.exit_code == 0:
            return ()
        lines = [
            self._hide_credentials(_clean(line))
            for line in result.output.splitlines()
            if line.strip()
        ]
        if not lines:
            return (f"pip check exited with code {result.exit_code} and printed nothing",)
        if len(lines) > _MAX_WARNING_LINES:
            extra = len(lines) - _MAX_WARNING_LINES
            return (*lines[:_MAX_WARNING_LINES], f"... and {extra} more lines")
        return tuple(lines)

    def _publish(self, key: str, overlay_dir: Path, warnings: tuple[str, ...]) -> Overlay:
        final = self._cache_dir / key
        # The note first: an overlay must never be visible without the warnings that belong to it.
        note = self._cache_dir / f"{key}{_PIP_CHECK_NOTE_SUFFIX}"
        note.write_text("".join(f"{line}\n" for line in warnings), encoding="utf-8")
        try:
            overlay_dir.rename(final)
        except OSError as exc:
            if not final.is_dir():
                raise OverlayError(
                    "the finished overlay could not be moved into the cache; "
                    "check the free space under the cache directory"
                ) from exc
            # Another process published the same key first; its overlay is the same packages.
            return Overlay(key, final, warnings, built=False)
        return Overlay(key, final, warnings, built=True)


def _timeout_reason(budget_s: float) -> str:
    return (
        f"the build did not finish within {budget_s:g} s; "
        "raise build_timeout_s or ask for fewer packages"
    )


def _credentials_in(index_environment: Mapping[str, str]) -> tuple[str, ...]:
    """The user name and password of every URL in the index settings: what must not reach a log.

    pip hides a URL's password in its own output, but not every message goes through that, and a
    token used as the user name is not hidden at all. The host stays visible: an unreachable index
    is the commonest reason a build fails, and the log has to say which one.
    """
    found: dict[str, None] = {}
    for value in index_environment.values():
        try:
            parts = urlsplit(value)
            userinfo = (parts.username, parts.password)
        except ValueError:  # not a URL, so it holds no user name or password to find
            continue
        for part in userinfo:
            if part and len(part) >= _MIN_HIDDEN_CHARS:
                found[part] = None
    # Longest first, so a secret that contains another is replaced whole.
    return tuple(sorted(found, key=len, reverse=True))


def _write_requirements(
    wheel_dir: Path, wheels: Sequence[ResolvedWheel], index_requirements: Sequence[str]
) -> None:
    if wheels:
        (wheel_dir / WHEEL_REQUIREMENTS_FILE).write_text(
            wheel_requirements_text(wheels), encoding="utf-8"
        )
    if index_requirements:
        (wheel_dir / INDEX_REQUIREMENTS_FILE).write_text(
            index_requirements_text(index_requirements), encoding="utf-8"
        )


def _clean(line: str) -> str:
    """One output line, cut short and with control characters (terminal escapes) made spaces."""
    printable = "".join(char if char.isprintable() else " " for char in line.strip())
    return printable[:_LOG_LINE_CHARS]


def _tail(output: str) -> str:
    lines = [_clean(line) for line in output.splitlines() if line.strip()]
    return "\n".join(lines[-_LOG_TAIL_LINES:])
