"""The child's run directory: a size-capped tmpfs, with a size watchdog as the fallback.

Design 6.4 ("Disk") and TD-004. The run directory is the child's writable ``/run/appmux``. Spike 7
measured the problem with a host-backed one: a child wrote 1 GiB into it in 1.5 s, and
``RLIMIT_FSIZE`` bounds only each file. A ``tmpfs`` mount does give ``ENOSPC`` at its size (a
64 MiB one stopped at 64 MiB), so the plan is that the run directory *is* a ``tmpfs`` inside the
sandbox. That only works if nothing outside the sandbox needs a path in it, so the relay's
listening socket is bound by the launcher before the sandbox starts and inherited by the relay
(``relay.bind_relay_socket``) instead of living in the run directory.

Measured in a container (dev App, 2026-11-05, ``tests/integration/test_run_dir_cap.py``, T060):

* the ``tmpfs`` inside the unprivileged mount namespace, with the inherited socket, holds its cap
  *in bytes*: a writer was refused with ``ENOSPC`` at exactly ``RUN_DIR_SIZE_MB`` and the child
  went on answering. The mount also carries ``nr_inodes`` (one file per 4 KiB page of its size,
  ``rootfs.tmpfs_mount_data``), because ``size=`` caps bytes and not files: with it, an
  empty-file loop was refused with ``ENOSPC`` at 16 384 files, in this mount and in the four
  other writable ones (``test_tmpfs_file_limit.py``, T219, 2026-11-06). A file costs the
  container about 1 KB of kernel memory that neither the byte cap nor the child's resident
  figure shows (TD-004);
* its pages are memory, and in one run they were counted in the container and not in the child's
  resident figure (the container's rose by 68 MiB for 64 MiB written, the child's by 1 MiB), so
  the memory watchdog, which reads that figure, does not see them. ``RLIMIT_AS`` bounds a
  process's address space and file pages it has not mapped are not in it; that is how the kernel
  defines it, and it was not measured here. That is why TD-004 stays open.

Not run in a container: :func:`watch_run_dir` below stopping a child that overfills a host-backed
directory (the directory itself has carried a shared-service-principal child's token file).

The second layer here is the fallback should the ``tmpfs`` fail, and the way of a child that must
keep files the host reads: the launcher is given a host directory (``SandboxSpec.host_run_dir``),
binds it read-write, and :func:`watch_run_dir` measures it and stops the child that passes
``RUN_DIR_LIMIT_BYTES``. Like the memory watchdog it polls, so a fast writer gets some way past
the limit between two readings: bounded by ``WATCH_INTERVAL_S`` times the write rate, not
prevented. That residue is the other half of TD-004.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

RUN_DIR_MOUNT_PATH: Final = "/run/appmux"
RUN_DIR_SIZE_MB: Final = 64
RUN_DIR_LIMIT_BYTES: Final = RUN_DIR_SIZE_MB * 1024 * 1024
# One second bounds how far a steady writer gets past the limit between two readings; the walk of
# a small run directory costs far less than that.
WATCH_INTERVAL_S: Final = 1.0

_BLOCK_BYTES: Final = 512  # st_blocks counts in 512-byte units on Linux, whatever the filesystem


@dataclass(frozen=True, slots=True)
class RunDirOffence:
    """A run directory that holds more than its limit."""

    path: Path
    size_bytes: int
    limit_bytes: int

    @property
    def reason(self) -> str:
        return (
            f"run directory holds {self.size_bytes // 1024} KiB, over its limit of "
            f"{self.limit_bytes // 1024} KiB"
        )


def _stat_without_following(entry: os.DirEntry[str]) -> os.stat_result:
    return entry.stat(follow_symlinks=False)


def directory_size_bytes(
    directory: Path,
    *,
    stat_entry: Callable[[os.DirEntry[str]], os.stat_result] = _stat_without_following,
) -> int:
    """Disk allocated under ``directory``, symlinks not followed; 0 when it does not exist.

    Allocated blocks, not file lengths: a sparse file claims a length it does not use, and the
    point is how much disk a child can take. An entry that vanishes while the tree is walked (the
    child is still running) is skipped.
    """
    total_bytes = 0
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            try:
                status = stat_entry(entry)
                is_directory = entry.is_dir(follow_symlinks=False)
            except FileNotFoundError:
                continue  # removed between the listing and the stat
            total_bytes += status.st_blocks * _BLOCK_BYTES
            if is_directory:
                pending.append(Path(entry.path))
    return total_bytes


def check_run_dir(
    directory: Path,
    *,
    limit_bytes: int = RUN_DIR_LIMIT_BYTES,
    measure_bytes: Callable[[Path], int] = directory_size_bytes,
) -> RunDirOffence | None:
    """The offence if ``directory`` holds more than ``limit_bytes``, else ``None``."""
    size_bytes = measure_bytes(directory)
    if size_bytes > limit_bytes:
        return RunDirOffence(path=directory, size_bytes=size_bytes, limit_bytes=limit_bytes)
    return None


async def watch_run_dir(
    directory: Path,
    limit_bytes: int,
    stop: Callable[[RunDirOffence], Awaitable[None]],
    *,
    interval_s: float = WATCH_INTERVAL_S,
    measure_bytes: Callable[[Path], int] = directory_size_bytes,
) -> None:
    """Measure every ``interval_s`` and ``await stop(offence)`` once the directory is over.

    Returns after one successful stop (the child is gone, so there is nothing left to watch);
    runs until cancelled otherwise. A ``stop`` that raises is retried at the next round, and a
    measurement that fails is logged and retried: a watchdog that quietly ended is worse than a
    noisy one.
    """
    while True:
        try:
            offence = await asyncio.to_thread(
                check_run_dir, directory, limit_bytes=limit_bytes, measure_bytes=measure_bytes
            )
        except OSError:
            logger.exception("run directory watchdog could not measure %s; will retry", directory)
        else:
            if offence is not None:
                logger.warning("stopping the child: %s", offence.reason)
                try:
                    await stop(offence)
                except Exception:
                    # Same boundary as the memory watchdog: the directory stays over its limit,
                    # so the next round stops it again.
                    logger.exception("could not stop the child (%s); will retry", offence.reason)
                else:
                    return
        await asyncio.sleep(interval_s)
