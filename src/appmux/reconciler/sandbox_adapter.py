"""The reconciler's ``ChildLauncher`` over ``sandbox.launcher.start_sandbox``.

Children start only through the sandbox launcher; this adapter is the one place the reconciler's
vocabulary (``ChildStart``, ``RunningChild``) meets the launcher's (``SandboxSpec``,
``SandboxHandle``). It decides the spec and prepares the files the launcher expects, and nothing
else: no policy about when to start, restart or stop a child belongs here.

Each start gets a scratch directory of its own, ``<run_base>/<slug>/<release_no>-<n>``, emptied
before use. It holds the supervisor's log, the child's two output files (``child.log`` for its
standard output and the supervisor's own lines, ``child.err`` for its standard error), the egress
rules file and the unix socket the edge connects to; none of it is visible inside the sandbox
(design 6.1, 6.4). Directories of finished children stay until the same name is used again.

The reconciler reads a child's output as it comes (``read_output``) into the app's output ring
(``output.py``): each file is followed from where the last read stopped, a line is returned only
once it is whole (or the child has ended), and a flood between two reads of more than
``MAX_OUTPUT_READ_BYTES`` keeps its end and says how many bytes it skipped. One read hands over no
more lines than the ring keeps (``OUTPUT_MAX_LINES``), both files together: a file that needs more
than its share keeps its newest lines and a note before them of what it skipped. Each line read
is masked in the event loop that serves every app. These caps bound how much text one read hands
over, and so the time it holds the loop, because masking takes time linear in that text: each
credential shape runs in linear time (``registry.audit``), and a long line is masked only as far
as its cut (``output.py``).

A child whose environment names the shared service principal's token command also gets a *host*
run directory, ``<scratch>/run``, bound read-write at ``/run/appmux`` instead of the tmpfs (the
launcher's ``SandboxSpec.host_run_dir``): appmux has to put the token files where the child sees
them, and a tmpfs inside the sandbox cannot be reached from outside it. The cap of the tmpfs is
then gone, so the size watchdog of ``sandbox.run_dir`` (the fallback TD-004 names) runs for that
child and stops it when the directory passes its limit. The files and their renewal are the
``RunDirFiles`` seam; the keeper it returns is stopped when the child ends.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import shutil
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Final, Protocol

from appmux.reconciler.children import ChildExit, ChildStart, ChildStartError, OutputStream
from appmux.reconciler.output import OUTPUT_MAX_LINES
from appmux.sandbox.launcher import (
    LAUNCH_SOCKET_NAME,
    SCRATCH_DIRECTORY_MODE,
    SandboxExit,
    SandboxSpec,
    SandboxStartError,
    start_sandbox,
)
from appmux.sandbox.pump import PumpStats, write_rules_file
from appmux.sandbox.rules import EgressRule
from appmux.sandbox.run_dir import (
    RUN_DIR_LIMIT_BYTES,
    WATCH_INTERVAL_S,
    RunDirOffence,
    watch_run_dir,
)

# sockaddr_un.sun_path is 108 bytes including the terminating NUL.
MAX_SOCKET_PATH_BYTES: Final = 107
RELAY_SOCKET_NAME: Final = "relay.sock"
CHILD_LOG_NAME: Final = "child.log"
CHILD_ERR_NAME: Final = "child.err"
RULES_FILE_NAME: Final = "rules.json"
HOST_RUN_DIR_NAME: Final = "run"
# The SIGTERM grace of a child stopped for filling its run directory.
RUN_DIR_STOP_GRACE_S: Final = 5.0
# The SIGTERM grace of a child stopped for holding more memory than its limit. Shorter than the
# run directory's: a child that is still allocating is the container's out-of-memory kill away
# from taking the other apps with it, and nothing it would flush in a few seconds is worth that.
MEMORY_STOP_GRACE_S: Final = 2.0
# Enough for the last 40 lines of a chatty child; the registry keeps fewer (``clean_log_tail``).
LOG_READ_BYTES: Final = 16 * 1024
# How long the metrics sampler waits for a pump's counters; under its own 3 s probe timeout.
PUMP_STATS_TIMEOUT_S: Final = 2.0
# The most of one output file read at once: the size of the app's output ring. The masks take time
# linear in the text they get, so a flood between two reads costs the loop one ring's worth of
# masking, whatever its size.
MAX_OUTPUT_READ_BYTES: Final = 256 * 1024
# A line still without its newline past this size is returned as it is (the ring cuts it further).
MAX_PARTIAL_LINE_BYTES: Final = 16 * 1024

logger = logging.getLogger(__name__)


class SandboxProcess(Protocol):
    """The part of ``SandboxHandle`` the adapter uses."""

    async def wait_started(self, *, timeout_s: float) -> None: ...

    async def wait(self) -> SandboxExit: ...

    async def stop(self, *, grace_s: float) -> SandboxExit: ...

    @property
    def sandbox_pid(self) -> int | None: ...

    async def read_stats(self, *, timeout_s: float) -> PumpStats | None: ...


StartSandbox = Callable[[SandboxSpec], Awaitable[SandboxProcess]]


class RunDirKeeper(Protocol):
    """Whatever keeps files in a child's host run directory current; ``stop`` ends it."""

    async def stop(self) -> None: ...


class RunDirFiles(Protocol):
    """Files appmux puts in the host run directory of the children that need them."""

    def needed_by(self, environment: Mapping[str, str]) -> bool:
        """Whether a child with this environment needs its run directory on the host."""
        ...

    async def attach(self, directory: Path) -> RunDirKeeper:
        """Put the files in ``directory``, keep them current; ``ChildStartError`` if it cannot."""
        ...


class _HostRunDir:
    """What runs beside a child whose run directory is on the host: the keeper and the watchdog."""

    def __init__(self, keeper: RunDirKeeper) -> None:
        self.keeper = keeper
        self.offence: RunDirOffence | None = None
        self.watchdog: asyncio.Task[None] | None = None

    async def release(self) -> None:
        """End both; a watchdog that is stopping the child is waited for, not cancelled."""
        watchdog, self.watchdog = self.watchdog, None
        if watchdog is not None:
            if self.offence is None:
                watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        await self.keeper.stop()


class _OutputFile:
    """One output file of a child, followed from where the last read stopped."""

    def __init__(self, path: Path, stream: OutputStream) -> None:
        self._path = path
        self._stream = stream
        self._offset = 0
        self._partial = b""

    def read(self, *, final: bool) -> _NewOutput:
        try:
            data, skipped = self._read_new()
        except FileNotFoundError:
            return _NewOutput(self._stream, [], 0)  # the child has not written to it yet
        except OSError as exc:
            # Best effort: the output ring is for a person to read; a file that cannot be read now
            # is tried again at the next read, and the child runs on either way.
            logger.warning("could not read %s: %s", self._path, exc.strerror)
            return _NewOutput(self._stream, [], 0)
        *whole, self._partial = (self._partial + data).split(b"\n")
        if final or len(self._partial) > MAX_PARTIAL_LINE_BYTES:
            if self._partial:
                whole.append(self._partial)
            self._partial = b""
        return _NewOutput(self._stream, whole, skipped)

    def _read_new(self) -> tuple[bytes, int]:
        with self._path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size < self._offset:  # cut or replaced: start again from its beginning
                self._offset, self._partial = 0, b""
            skipped = 0
            if size - self._offset > MAX_OUTPUT_READ_BYTES:
                skipped = size - MAX_OUTPUT_READ_BYTES - self._offset
                self._offset, self._partial = size - MAX_OUTPUT_READ_BYTES, b""
            handle.seek(self._offset)
            data = handle.read(size - self._offset)
        self._offset += len(data)
        if skipped:
            # The read starts inside a line: that piece is part of what was skipped.
            first_newline = data.find(b"\n")
            cut = first_newline + 1 if first_newline >= 0 else len(data)
            data, skipped = data[cut:], skipped + cut
        return data, skipped


@dataclass(frozen=True, slots=True)
class _NewOutput:
    """What one read found in one output file: its whole lines, and the bytes it skipped first."""

    stream: OutputStream
    whole: list[bytes]
    skipped: int

    @property
    def lines_needed(self) -> int:
        """How many lines of the ring it takes, the note of what was skipped included."""
        return len(self.whole) + (1 if self.skipped else 0)

    def lines(self, *, room: int) -> list[tuple[OutputStream, str]]:
        """Its newest lines that fit in ``room``; what does not fit is counted in a note first."""
        whole, skipped = self.whole, self.skipped
        if self.lines_needed > room:
            # The note takes a line of the room too: with it, one read fits the ring's line cap.
            kept = whole[len(whole) - max(room - 1, 0) :]
            dropped = len(whole) - len(kept)
            skipped += sum(map(len, whole[:dropped])) + dropped  # each with its newline
            whole = kept
        lines: list[tuple[OutputStream, str]] = []
        if skipped:
            lines.append(
                ("appmux", f"skipped {skipped} bytes of {self.stream} written faster than read")
            )
        lines.extend((self.stream, _decode(line)) for line in whole)
        return lines


def _rooms(lines_needed: list[int], room: int) -> list[int]:
    """How many of ``room`` lines each file gets: all it needs while that fits an even share, and
    the room such files leave split evenly among the others."""
    rooms = [0] * len(lines_needed)
    left = room
    by_need = sorted(range(len(lines_needed)), key=lines_needed.__getitem__)
    for done, index in enumerate(by_need):
        rooms[index] = min(lines_needed[index], left // (len(by_need) - done))
        left -= rooms[index]
    return rooms


def _decode(line: bytes) -> str:
    return line.decode("utf-8", errors="replace").removesuffix("\r")


class SandboxRunningChild:
    def __init__(
        self,
        process: SandboxProcess,
        *,
        socket_path: Path,
        log_path: Path,
        stderr_path: Path,
        memory_budget_mb: int,
        host_run_dir: _HostRunDir | None = None,
    ) -> None:
        self._process = process
        self._socket_path = socket_path
        self._output = (_OutputFile(log_path, "stdout"), _OutputFile(stderr_path, "stderr"))
        self._memory_budget_mb = memory_budget_mb
        self._host_run_dir = host_run_dir
        self._over_memory_reason: str | None = None

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    async def wait_started(self, *, timeout_s: float) -> None:
        try:
            await self._process.wait_started(timeout_s=timeout_s)
        except SandboxStartError as exc:
            raise ChildStartError(str(exc)) from exc

    async def wait(self) -> ChildExit:
        # Released only once the child has ended: the reconciler cancels a ``wait`` it started
        # for the health check while the child still runs.
        exit_ = await self._process.wait()
        return await self._ended(exit_)

    async def stop(self, *, grace_s: float) -> ChildExit:
        try:
            exit_ = await self._process.stop(grace_s=grace_s)
        except BaseException:
            await self._release()
            raise
        return await self._ended(exit_)

    async def _ended(self, exit_: SandboxExit) -> ChildExit:
        host_run_dir = self._host_run_dir
        await self._release()
        description = exit_.describe()
        if host_run_dir is not None and host_run_dir.offence is not None:
            return ChildExit(f"{host_run_dir.offence.reason}; {description}")
        if self._over_memory_reason is not None:
            return ChildExit(f"{self._over_memory_reason}; {description}")
        if exit_.signal is not None:
            return ChildExit(description, signal=_signal_name(exit_.signal))
        return ChildExit(description, code=exit_.status)

    async def _release(self) -> None:
        if self._host_run_dir is not None:
            await self._host_run_dir.release()

    def read_output(self, *, final: bool = False) -> list[tuple[OutputStream, str]]:
        # Read in the event loop: a local file, at most MAX_OUTPUT_READ_BYTES of each, and no more
        # lines of both than the ring keeps, so no line of one read pushes another out of it.
        found = [output.read(final=final) for output in self._output]
        rooms = _rooms([new.lines_needed for new in found], OUTPUT_MAX_LINES)
        return [
            line for new, room in zip(found, rooms, strict=True) for line in new.lines(room=room)
        ]

    # What the memory watchdog reads and does (TD-029).

    @property
    def memory_budget_mb(self) -> int:
        """The app's memory budget this copy was started with."""
        return self._memory_budget_mb

    async def stop_over_memory(self, reason: str) -> None:
        """Stop the sandbox because its tree holds more than its limit; ``wait`` then says why.

        Only ends the process. The reconciler's own ``wait`` collects the exit and releases what
        the run directory kept, so this does not (a second release would stop the keeper twice).
        """
        self._over_memory_reason = reason
        await self._process.stop(grace_s=MEMORY_STOP_GRACE_S)

    # What the metrics sampler reads (T192): which sandbox this is, its pid, its pump's counters.

    @property
    def instance(self) -> str:
        """The name of this start's scratch directory, unique while the process runs."""
        return self._socket_path.parent.name

    @property
    def sandbox_pid(self) -> int | None:
        """The host pid at the root of the sandbox's process tree; ``None`` until it started."""
        return self._process.sandbox_pid

    async def pump_stats(self) -> PumpStats | None:
        """The egress pump's running totals, or ``None`` if the supervisor gave none in time."""
        return await self._process.read_stats(timeout_s=PUMP_STATS_TIMEOUT_S)


class SandboxChildLauncher:
    def __init__(
        self,
        *,
        run_base: Path,
        app_environment: Path,
        container_ip: IPv4Address,
        appmux_port: int,
        global_rules: Callable[[], Sequence[EgressRule]] = tuple,
        start: StartSandbox = start_sandbox,
        run_dir_files: RunDirFiles | None = None,
        run_dir_limit_bytes: int = RUN_DIR_LIMIT_BYTES,
        run_dir_watch_interval_s: float = WATCH_INTERVAL_S,
    ) -> None:
        self._run_base = run_base
        self._app_environment = app_environment
        self._container_ip = container_ip
        self._appmux_port = appmux_port
        self._global_rules = global_rules
        self._start = start
        self._run_dir_files = run_dir_files
        self._run_dir_limit_bytes = run_dir_limit_bytes
        self._run_dir_watch_interval_s = run_dir_watch_interval_s
        self._incarnations = itertools.count(1)

    async def start(self, child: ChildStart) -> SandboxRunningChild:
        scratch_dir = self._run_base / child.slug / f"{child.release_no}-{next(self._incarnations)}"
        socket_path = scratch_dir / RELAY_SOCKET_NAME
        _require_socket_path_fits(scratch_dir / LAUNCH_SOCKET_NAME)
        _require_socket_path_fits(socket_path)
        files = self._run_dir_files
        files_for_child = (
            files if files is not None and files.needed_by(child.environment) else None
        )
        spec = SandboxSpec(
            slug=child.slug,
            command=child.command,
            environment=dict(child.environment),
            app_environment=self._app_environment,
            release_dir=child.release_dir,
            overlays=tuple(child.overlays),
            scratch_dir=scratch_dir,
            log_path=scratch_dir / CHILD_LOG_NAME,
            stderr_path=scratch_dir / CHILD_ERR_NAME,
            rules_path=scratch_dir / RULES_FILE_NAME,
            relay_socket_path=socket_path,
            child_port=child.port,
            memory_budget_mb=child.memory_budget_mb,
            host_run_dir=scratch_dir / HOST_RUN_DIR_NAME if files_for_child else None,
        )
        host_run_dir: _HostRunDir | None = None
        try:
            await asyncio.to_thread(self._prepare, scratch_dir, spec.rules_path, child.app_egress)
            if files_for_child is not None and spec.host_run_dir is not None:
                host_run_dir = await _attach_files(files_for_child, spec.host_run_dir)
            process = await self._start(spec)
        except SandboxStartError as exc:
            await _release_quietly(host_run_dir)
            raise ChildStartError(str(exc)) from exc
        except OSError as exc:
            await _release_quietly(host_run_dir)
            reason = exc.strerror or "I/O error"
            raise ChildStartError(
                f"the sandbox of '{child.slug}' could not be prepared ({reason})"
            ) from exc
        except BaseException:
            await _release_quietly(host_run_dir)
            raise
        if host_run_dir is not None and spec.host_run_dir is not None:
            host_run_dir.watchdog = asyncio.create_task(
                self._watch(spec.host_run_dir, process, host_run_dir),
                name=f"run-dir-{child.slug}",
            )
        return SandboxRunningChild(
            process,
            socket_path=socket_path,
            log_path=spec.log_path,
            stderr_path=scratch_dir / CHILD_ERR_NAME,
            memory_budget_mb=child.memory_budget_mb,
            host_run_dir=host_run_dir,
        )

    async def _watch(
        self, directory: Path, process: SandboxProcess, host_run_dir: _HostRunDir
    ) -> None:
        async def stop(offence: RunDirOffence) -> None:
            host_run_dir.offence = offence
            await process.stop(grace_s=RUN_DIR_STOP_GRACE_S)

        await watch_run_dir(
            directory,
            self._run_dir_limit_bytes,
            stop,
            interval_s=self._run_dir_watch_interval_s,
        )

    def _prepare(
        self, scratch_dir: Path, rules_path: Path, app_rules: Sequence[EgressRule]
    ) -> None:
        shutil.rmtree(scratch_dir, ignore_errors=True)
        scratch_dir.mkdir(mode=SCRATCH_DIRECTORY_MODE, parents=True)
        write_rules_file(
            rules_path,
            app_rules=app_rules,
            global_rules=self._global_rules(),
            container_ip=self._container_ip,
            appmux_port=self._appmux_port,
        )


async def _attach_files(files: RunDirFiles, directory: Path) -> _HostRunDir:
    await asyncio.to_thread(directory.mkdir, mode=SCRATCH_DIRECTORY_MODE)
    return _HostRunDir(await files.attach(directory))


async def _release_quietly(host_run_dir: _HostRunDir | None) -> None:
    if host_run_dir is not None:
        await host_run_dir.release()


def _require_socket_path_fits(path: Path) -> None:
    size = len(os.fsencode(path))
    if size > MAX_SOCKET_PATH_BYTES:
        raise ChildStartError(
            f"the sandbox directory {path.parent} is too long for a unix socket "
            f"({size} bytes, at most {MAX_SOCKET_PATH_BYTES}); use a shorter run directory"
        )


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return str(number)  # a real-time signal has no name of its own
