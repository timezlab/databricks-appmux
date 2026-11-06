"""Hostile fixture disk_filler: writes into its run directory until the filesystem refuses.

NEVER run this outside an appmux sandbox. It exists to prove that the child's run directory is
capped (design 6.1 and 6.4, TD-004, T060): a ``tmpfs`` of ``RUN_DIR_SIZE_MB`` inside the sandbox
must answer ``ENOSPC`` at its size, where a host-backed directory let a child write 1 GiB in 1.5 s
(spike 7). It is a standalone Databricks-style app: ``python app.py`` and the port in
``DATABRICKS_APP_PORT``.

HTTP paths
    GET /          200 ``{"fixture": "disk_filler"}``, so the health check passes (writes nothing).
    GET /report    200 JSON. Each call (serialised, so two callers cannot add their writes together)
                   truncates the fill file, writes 1 MiB at a time until the filesystem refuses or
                   ``CEILING_MB`` is reached, and *keeps* the file so a test can read what the data
                   costs elsewhere (memory) before it asks for ``/clear``.
    GET /clear     200 ``{"removed": true|false}``. Deletes the fill file.

    GET /files?directory=D
                   200 JSON. Makes empty files in a folder of ``D/inode_filler`` until the
                   filesystem refuses or ``CEILING_FILES`` is reached, and keeps them. ``D`` is one
                   of the five writable mounts (``WRITABLE_DIRECTORIES``); another value is a 400.
                   An empty file costs nothing against the byte cap, so only the file limit of the
                   mount (``nr_inodes``, T219) stops this.
    GET /files/clear?directory=D
                   200 ``{"removed": true|false}``. Deletes what ``/files`` made in ``D``.

Safety stops, so a sandbox that failed to cap the directory costs a bounded write and not the
host's disk or memory: the byte loop stops at ``CEILING_MB`` and the file loop at
``CEILING_FILES``.

``/report`` keys
    written_bytes        bytes the file holds when the loop ends (a short write at the cap counts).
    written_mb           ``written_bytes`` in whole MiB, rounded down.
    refused_with         the errno name of the refusal (``"ENOSPC"`` when the size cap stopped it),
                         or null when the loop reached the ceiling without a refusal.
    stopped_by_ceiling   true when the loop stopped at ``CEILING_MB`` (no cap was enforced).
    ceiling_mb           ``CEILING_MB``.
    mount_size_mb        the size of the filesystem under the run directory (``statvfs``), in MiB.
    elapsed_s            seconds the loop took.

``/files`` keys
    directory            the mount that was filled.
    created              files made (the folder they live in is not counted).
    refused_with         the errno name of the refusal (``"ENOSPC"`` when the file limit stopped
                         it), or null when the loop reached the ceiling without a refusal.
    stopped_by_ceiling   true when the loop stopped at ``CEILING_FILES`` (no limit was enforced).
    ceiling_files        ``CEILING_FILES``.
    mount_file_limit     the file limit of the filesystem (``statvfs`` ``f_files``).
    free_files_before    files the filesystem could still take when the loop began.
    mem_free_kb_before / mem_free_kb_after / slab_kb_before / slab_kb_after
                         ``MemFree`` and ``Slab`` of ``/proc/meminfo`` around the loop: evidence of
                         what the files cost the container, read as printed, not asserted.
    elapsed_s            seconds the loop took.
"""

from __future__ import annotations

import errno
import os
import shutil
import threading
import time

import uvicorn
from fastapi import FastAPI, HTTPException

RUN_DIR = "/run/appmux"
FILL_FILE = os.path.join(RUN_DIR, "disk_filler.bin")
MIB = 1024 * 1024
CEILING_MB = 256
# The five mounts a child can write to (design 6.1); the fixture refuses any other folder.
WRITABLE_DIRECTORIES = (RUN_DIR, "/tmp", "/var/tmp", "/dev/shm", "/home/app")
FILES_FOLDER = "inode_filler"
# More than the largest limit (16 384 files) so a missing limit is seen, and few enough that it
# costs a bounded amount of kernel memory (about 100 MB at 1 KB a file, from memory, not measured).
CEILING_FILES = 100_000

app = FastAPI()
fill_lock = threading.Lock()


# Fixtures are standalone apps and cannot share code, so the hostile ones repeat this helper.
def errno_name(exc: OSError) -> str:
    """The errno name of a failed system call, or the exception type when it carries no errno."""
    if exc.errno is None:
        return type(exc).__name__
    return errno.errorcode.get(exc.errno, type(exc).__name__)


def mount_size_mb() -> int | None:
    try:
        stats = os.statvfs(RUN_DIR)
    except OSError:
        # No run directory (the fixture is not inside a sandbox): the report says so by its
        # refusal, and the size is unknown.
        return None
    return stats.f_blocks * stats.f_frsize // MIB


def fill_until_refused() -> dict[str, object]:
    chunk = bytes(MIB)
    written_bytes = 0
    refused_with: str | None = None
    started = time.monotonic()
    try:
        fd = os.open(FILL_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError as exc:
        refused_with = errno_name(exc)
    else:
        try:
            while written_bytes < CEILING_MB * MIB:
                try:
                    # A write that meets the cap may be short; the next one then raises ENOSPC.
                    written_bytes += os.write(fd, chunk)
                except OSError as exc:
                    refused_with = errno_name(exc)
                    break
        finally:
            os.close(fd)
    return {
        "fixture": "disk_filler",
        "written_bytes": written_bytes,
        "written_mb": written_bytes // MIB,
        "refused_with": refused_with,
        "stopped_by_ceiling": refused_with is None,
        "ceiling_mb": CEILING_MB,
        "mount_size_mb": mount_size_mb(),
        "elapsed_s": round(time.monotonic() - started, 3),
    }


def meminfo_kb(name: str) -> int | None:
    """One figure of ``/proc/meminfo`` in kB, or None when the line is not there."""
    try:
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                key, _, rest = line.partition(":")
                if key == name:
                    return int(rest.split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return None


def files_folder(directory: str) -> str:
    if directory not in WRITABLE_DIRECTORIES:
        raise HTTPException(status_code=400, detail="not one of the writable mounts")
    return os.path.join(directory, FILES_FOLDER)


def create_files_until_refused(directory: str) -> dict[str, object]:
    folder = files_folder(directory)
    refused_with: str | None = None
    created = 0
    mem_free_before, slab_before = meminfo_kb("MemFree"), meminfo_kb("Slab")
    started = time.monotonic()
    try:
        stats = os.statvfs(directory)
        limit, free_before = stats.f_files, stats.f_ffree
    except OSError as exc:
        limit, free_before, refused_with = None, None, errno_name(exc)
    if refused_with is None:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            refused_with = errno_name(exc)
    if refused_with is None:
        while created < CEILING_FILES:
            try:
                # O_EXCL: a name that is taken is an error, not a file that was already counted.
                os.close(
                    os.open(
                        os.path.join(folder, f"f{created}"),
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                )
            except OSError as exc:
                refused_with = errno_name(exc)
                break
            created += 1
    return {
        "fixture": "disk_filler",
        "directory": directory,
        "created": created,
        "refused_with": refused_with,
        "stopped_by_ceiling": refused_with is None,
        "ceiling_files": CEILING_FILES,
        "mount_file_limit": limit,
        "free_files_before": free_before,
        "mem_free_kb_before": mem_free_before,
        "mem_free_kb_after": meminfo_kb("MemFree"),
        "slab_kb_before": slab_before,
        "slab_kb_after": meminfo_kb("Slab"),
        "elapsed_s": round(time.monotonic() - started, 3),
    }


@app.get("/")
def health() -> dict[str, str]:
    return {"fixture": "disk_filler"}


@app.get("/report")
def report() -> dict[str, object]:
    with fill_lock:
        return fill_until_refused()


@app.get("/files")
def files(directory: str) -> dict[str, object]:
    with fill_lock:
        return create_files_until_refused(directory)


@app.get("/files/clear")
def clear_files(directory: str) -> dict[str, bool]:
    folder = files_folder(directory)
    with fill_lock:
        if not os.path.isdir(folder):
            return {"removed": False}
        shutil.rmtree(folder)
        return {"removed": True}


@app.get("/clear")
def clear() -> dict[str, bool]:
    with fill_lock:
        try:
            os.remove(FILL_FILE)
        except FileNotFoundError:
            return {"removed": False}
        return {"removed": True}


if __name__ == "__main__":
    uvicorn.run(
        app,
        # appmux and the platform both set UVICORN_HOST=0.0.0.0 (design 5.4). Run without it, the
        # hostile /report must not be open on every interface, so the default is loopback.
        host=os.environ.get("UVICORN_HOST", "127.0.0.1"),
        port=int(os.environ["DATABRICKS_APP_PORT"]),
    )
