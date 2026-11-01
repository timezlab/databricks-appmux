"""The child's root, built from an allow-list (design 6.1 step 5, spike 2, FR-016).

The root is never made by hiding paths one by one. A fresh ``tmpfs`` holds a subdirectory
(``rootfs``) into which only what the child may see is mounted; then ``pivot_root`` into the
``tmpfs``, detach the old root, and ``chroot`` into the subdirectory. The ``chroot`` after the
pivot is what makes a nested ``unshare(CLONE_NEWUSER)`` fail with ``EPERM`` (spike 2).

Two halves, so that the decisions can be tested without the kernel:

* :func:`plan_rootfs` turns a :class:`RootLayout` into an ordered :class:`RootPlan` of steps, as
  plain data: which host paths are bound (read-only unless the step says otherwise), which
  ``tmpfs`` are mounted and how large, which links and files the launcher writes. Nothing the
  platform owns (``/databricks``, ``/sys``, ``/mnt``, appmux's own directory) is in any step, so it
  does not exist in the child's root: absence is stronger than a ``tmpfs`` laid over it.
* :func:`build_root` runs a plan through :class:`RootOps`, the system calls, in the measured
  order. Any step that fails raises :class:`RootfsError` and the child is never started on a
  half-built root.

The shared package environment and every overlay are bound read-only (FR-016). A read-only bind
is a bind followed by a remount with ``MS_RDONLY`` *and the source mount's locked flags* (nosuid,
nodev, noexec, relatime); without them the remount fails in an unprivileged mount namespace.

Ported from ``specs/001-appmux-v1/spikes/scripts/spike-02/sandbox_fs.py``.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from appmux.sandbox.run_dir import RUN_DIR_MOUNT_PATH, RUN_DIR_SIZE_MB

MS_RDONLY: Final = 1
MS_NOSUID: Final = 2
MS_NODEV: Final = 4
MS_NOEXEC: Final = 8
MS_REMOUNT: Final = 32
MS_NOATIME: Final = 1024
MS_NODIRATIME: Final = 2048
MS_BIND: Final = 4096
MS_REC: Final = 16384
MS_PRIVATE: Final = 1 << 18
MS_RELATIME: Final = 1 << 21
MNT_DETACH: Final = 2
SYS_PIVOT_ROOT_X86_64: Final = 155

_MOUNT_OPTION_FLAGS: Final = {
    "nosuid": MS_NOSUID,
    "nodev": MS_NODEV,
    "noexec": MS_NOEXEC,
    "noatime": MS_NOATIME,
    "nodiratime": MS_NODIRATIME,
    "relatime": MS_RELATIME,
}

ROOT_SUBDIRECTORY: Final = "rootfs"
OLD_ROOT_NAME: Final = ".old"
PIVOT_TMPFS_SIZE: Final = "16m"

BUILD_OUTPUT_PATH: Final = "/srv/build"
RELEASE_PATH: Final = "/srv/app"
HOME_PATH: Final = "/home/app"
# Mount targets in the child's own root, each a private tmpfs: no file is made in the host's
# shared temporary directories, which is what the S108 rule watches for.
TMP_PATH: Final = "/tmp"  # noqa: S108 - a mount target in the child's root
VAR_TMP_PATH: Final = "/var/tmp"  # noqa: S108 - a mount target in the child's root
SHM_PATH: Final = "/dev/shm"  # noqa: S108 - a mount target in the child's root
_OVERLAY_PARENT: Final = "/srv/overlay"

TMP_SIZE_MB: Final = 64
VAR_TMP_SIZE_MB: Final = 16
SHM_SIZE_MB: Final = 64
HOME_SIZE_MB: Final = 64
SKELETON_SIZE_MB: Final = 1

ETC_FILES: Final = (
    "ld.so.cache",
    "ld.so.conf",
    "ld.so.conf.d",
    "ssl",
    "ca-certificates",
    "nsswitch.conf",
    "passwd",
    "group",
    "localtime",
    "os-release",
)
# slirp's resolver: none of the ten files holds it, and without it a name lookup fails (spike 7).
RESOLV_CONF: Final = "nameserver 10.0.2.3\n"
# A constant line with no information about the host. Spike 2 wrote it so that the name
# "localhost" resolves for apps that use it; design 6.1 lists no /etc/hosts (see the hand-back).
HOSTS: Final = "127.0.0.1 localhost\n"
DEVICE_NODES: Final = ("null", "zero", "full", "random", "urandom")
USR_SYMLINKS: Final = {"bin": "usr/bin", "sbin": "usr/sbin", "lib": "usr/lib", "lib64": "usr/lib64"}
DESCRIPTOR_LINKS: Final = {
    "stdin": "/proc/self/fd/0",
    "stdout": "/proc/self/fd/1",
    "stderr": "/proc/self/fd/2",
    "fd": "/proc/self/fd",
}
# /proc/self files that name the pod and the host's paths.
LEAKY_PROC_SELF_FILES: Final = ("cgroup", "mountinfo", "mounts", "mountstats")


def overlay_path(index: int) -> str:
    """Where the ``index``-th overlay (first wins) appears in the child's root."""
    return f"{_OVERLAY_PARENT}/{index}"


class RootfsError(Exception):
    """The child's root could not be built; the child must not start."""


# ---- the plan: what the root holds, as data -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class BindMount:
    """A host directory or file bound into the root; read-only unless ``writable``."""

    source: Path
    target: str
    writable: bool = False
    optional: bool = False  # skipped, not an error, when the source does not exist


@dataclass(frozen=True, slots=True)
class HostFile:
    """A single host file bound as it is (a device node, a ``/proc`` file), no read-only remount."""

    source: Path
    target: str


@dataclass(frozen=True, slots=True)
class TmpfsMount:
    """A fresh ``tmpfs``. ``writable=False`` marks a skeleton, remounted read-only at the end."""

    target: str
    size_mb: int
    writable: bool = True
    mode: int = 0o1777
    noexec: bool = False


@dataclass(frozen=True, slots=True)
class Symlink:
    target: str
    destination: str


@dataclass(frozen=True, slots=True)
class WrittenFile:
    """A file the launcher writes (into a ``tmpfs`` that is read-only afterwards)."""

    target: str
    content: str


@dataclass(frozen=True, slots=True)
class ProcSelf:
    """``/proc/self`` as the entry of the process that becomes the child."""

    target: str


@dataclass(frozen=True, slots=True)
class Overmount:
    """``/dev/null`` bound over a file that would name the host."""

    target: str


RootStep = BindMount | HostFile | TmpfsMount | Symlink | WrittenFile | ProcSelf | Overmount


@dataclass(frozen=True, slots=True)
class RootPlan:
    steps: tuple[RootStep, ...]

    def writable_directories(self) -> set[str]:
        """Targets where the child can create files (the skeletons are read-only)."""
        return {
            step.target
            for step in self.steps
            if (isinstance(step, TmpfsMount) and step.writable)
            or (isinstance(step, BindMount) and step.writable)
        }


@dataclass(frozen=True, slots=True)
class RootLayout:
    """What differs between children: the rest of the root is the same for every one."""

    app_environment: Path
    release: Path
    overlays: tuple[Path, ...] = ()
    build_output: Path | None = None
    host_run_dir: Path | None = None
    overmount_proc_files: bool = True


def plan_rootfs(layout: RootLayout) -> RootPlan:
    """The ordered mounts of the child's root: a parent always comes before what is inside it."""
    steps: list[RootStep] = [
        Symlink(f"/{name}", destination) for name, destination in USR_SYMLINKS.items()
    ]
    steps.append(BindMount(Path("/usr"), "/usr"))
    steps.append(BindMount(layout.app_environment, str(layout.app_environment)))
    steps.extend(
        BindMount(overlay, overlay_path(index)) for index, overlay in enumerate(layout.overlays)
    )
    steps.append(BindMount(layout.release, RELEASE_PATH))
    if layout.build_output is not None:
        steps.append(BindMount(layout.build_output, BUILD_OUTPUT_PATH, writable=True))
    if layout.host_run_dir is not None:
        steps.append(BindMount(layout.host_run_dir, RUN_DIR_MOUNT_PATH, writable=True))
    else:
        steps.append(TmpfsMount(RUN_DIR_MOUNT_PATH, RUN_DIR_SIZE_MB))
    steps += [
        TmpfsMount(TMP_PATH, TMP_SIZE_MB),
        TmpfsMount(VAR_TMP_PATH, VAR_TMP_SIZE_MB),
        TmpfsMount(HOME_PATH, HOME_SIZE_MB),
    ]
    steps.append(TmpfsMount("/etc", SKELETON_SIZE_MB, writable=False, mode=0o755))
    steps.extend(
        BindMount(Path("/etc") / name, f"/etc/{name}", optional=True) for name in ETC_FILES
    )
    steps += [WrittenFile("/etc/hosts", HOSTS), WrittenFile("/etc/resolv.conf", RESOLV_CONF)]
    steps.append(TmpfsMount("/dev", SKELETON_SIZE_MB, writable=False, mode=0o755))
    steps.append(TmpfsMount(SHM_PATH, SHM_SIZE_MB))
    steps.extend(HostFile(Path("/dev") / name, f"/dev/{name}") for name in DEVICE_NODES)
    steps.extend(
        Symlink(f"/dev/{name}", destination) for name, destination in DESCRIPTOR_LINKS.items()
    )
    steps.append(TmpfsMount("/proc", SKELETON_SIZE_MB, writable=False, mode=0o755, noexec=True))
    steps += [
        HostFile(Path("/proc/cpuinfo"), "/proc/cpuinfo"),
        HostFile(Path("/proc/meminfo"), "/proc/meminfo"),
        ProcSelf("/proc/self"),
    ]
    if layout.overmount_proc_files:
        steps.extend(Overmount(f"/proc/self/{name}") for name in LEAKY_PROC_SELF_FILES)
    return RootPlan(steps=tuple(steps))


# ---- running the plan ---------------------------------------------------------------------------


class RootOps(Protocol):
    """The system calls the build makes: the seam a test replaces."""

    def exists(self, path: str) -> bool: ...
    def is_directory(self, path: str) -> bool: ...
    def makedirs(self, path: str) -> None: ...
    def touch(self, path: str) -> None: ...
    def symlink(self, destination: str, link: str) -> None: ...
    def write_text(self, path: str, content: str) -> None: ...
    def mount(
        self, source: str | None, target: str, fstype: str | None, flags: int, data: str | None
    ) -> None: ...
    def mount_flags_of(self, path: str) -> int: ...
    def process_proc_entry(self) -> str: ...
    def chdir(self, path: str) -> None: ...
    def pivot_root(self, new_root: str, put_old: str) -> None: ...
    def umount_detach(self, path: str) -> None: ...
    def rmdir(self, path: str) -> None: ...
    def chroot(self, path: str) -> None: ...


def build_root(plan: RootPlan, ops: RootOps, *, pivot_dir: str) -> None:
    """Make the plan the root of this process. ``pivot_dir`` is an existing empty directory.

    After it returns, the working directory is ``/`` of the new root, the old root is detached,
    and nothing that was not in the plan is reachable. Raises :class:`RootfsError` at the first
    step that fails; the caller must not go on to start the child.
    """
    root = f"{pivot_dir}/{ROOT_SUBDIRECTORY}"
    step_name = "make mounts private"
    try:
        ops.mount(None, "/", None, MS_REC | MS_PRIVATE, None)
        step_name = f"mount the new root at {pivot_dir}"
        ops.makedirs(pivot_dir)
        ops.mount(
            "tmpfs", pivot_dir, "tmpfs", MS_NOSUID | MS_NODEV, f"size={PIVOT_TMPFS_SIZE},mode=0755"
        )
        ops.makedirs(root)
        for step in plan.steps:
            step_name = f"{type(step).__name__} {step.target}"
            _apply_step(step, ops, root)
        for step in plan.steps:
            if isinstance(step, TmpfsMount) and not step.writable:
                step_name = f"make {step.target} read-only"
                flags = MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV
                ops.mount(
                    None, root + step.target, None, flags | (MS_NOEXEC if step.noexec else 0), None
                )
        step_name = "pivot_root"
        ops.makedirs(f"{pivot_dir}/{OLD_ROOT_NAME}")
        ops.chdir(pivot_dir)
        ops.pivot_root(".", OLD_ROOT_NAME)
        ops.chdir("/")
        step_name = "detach the old root"
        ops.umount_detach(f"/{OLD_ROOT_NAME}")
        ops.rmdir(f"/{OLD_ROOT_NAME}")
        step_name = "make the pivot root read-only"
        ops.mount(None, "/", None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV, None)
        step_name = "chroot into the root"
        ops.chroot(f"/{ROOT_SUBDIRECTORY}")
        ops.chdir("/")
    except OSError as exc:
        raise RootfsError(
            f"cannot build the child's root: {step_name} failed ({exc}); "
            "the sandbox was not started"
        ) from exc


def _apply_step(step: RootStep, ops: RootOps, root: str) -> None:
    destination = root + step.target
    match step:
        case BindMount():
            source = str(step.source)
            if not ops.exists(source):
                if step.optional:
                    return
                raise FileNotFoundError(errno.ENOENT, f"{source} does not exist", source)
            is_directory = ops.is_directory(source)
            _prepare_mount_point(ops, destination, is_directory=is_directory)
            ops.mount(source, destination, None, MS_BIND | (MS_REC if is_directory else 0), None)
            if not step.writable:
                locked = ops.mount_flags_of(source)
                ops.mount(None, destination, None, MS_BIND | MS_REMOUNT | MS_RDONLY | locked, None)
        case HostFile():
            _prepare_mount_point(ops, destination, is_directory=False)
            ops.mount(str(step.source), destination, None, MS_BIND, None)
        case TmpfsMount():
            ops.makedirs(destination)
            data = f"size={step.size_mb}m,mode={step.mode:04o}"
            flags = MS_NOSUID | MS_NODEV | (MS_NOEXEC if step.noexec else 0)
            ops.mount("tmpfs", destination, "tmpfs", flags, data)
        case Symlink():
            ops.symlink(step.destination, destination)
        case WrittenFile():
            ops.makedirs(os.path.dirname(destination))
            ops.write_text(destination, step.content)
        case ProcSelf():
            ops.makedirs(destination)
            ops.mount(ops.process_proc_entry(), destination, None, MS_BIND, None)
        case Overmount():
            _prepare_mount_point(ops, destination, is_directory=False)
            ops.mount("/dev/null", destination, None, MS_BIND, None)


def _prepare_mount_point(ops: RootOps, destination: str, *, is_directory: bool) -> None:
    if is_directory:
        ops.makedirs(destination)
    else:
        ops.makedirs(os.path.dirname(destination))
        ops.touch(destination)


_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")


def parse_mount_flags(mountinfo: str, path: str) -> int:
    """The locked mount flags of the mount that holds ``path``, from ``/proc/self/mountinfo``.

    The longest mount point that contains the path wins; of two stacked on the same point the
    later one does.
    """
    best_mount_point, best_options = "", ""
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        mount_point = _MOUNTINFO_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4])
        contains = path == mount_point or path.startswith(mount_point.rstrip("/") + "/")
        if contains and len(mount_point) >= len(best_mount_point):
            best_mount_point, best_options = mount_point, fields[5]
    flags = 0
    for option in best_options.split(","):
        flags |= _MOUNT_OPTION_FLAGS.get(option, 0)
    return flags


class LinuxRootOps:
    """The real system calls, through ``libc``; used inside the new namespaces only."""

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def is_directory(self, path: str) -> bool:
        return os.path.isdir(path)

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def touch(self, path: str) -> None:
        # Not open(path, "a"): that seeks to the end, which procfs refuses with EINVAL for the
        # /proc/self files that are overmounted. Opening for read only creates a missing file and
        # changes nothing about an existing one.
        os.close(os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOCTTY, 0o600))

    def symlink(self, destination: str, link: str) -> None:
        os.symlink(destination, link)

    def write_text(self, path: str, content: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def mount(
        self, source: str | None, target: str, fstype: str | None, flags: int, data: str | None
    ) -> None:
        result = self._libc.mount(
            source.encode() if source else None,
            target.encode(),
            fstype.encode() if fstype else None,
            flags,
            data.encode() if data else None,
        )
        self._check(result, f"mount {fstype or 'bind'} {source or ''} -> {target}")

    def mount_flags_of(self, path: str) -> int:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            return parse_mount_flags(handle.read(), path)

    def process_proc_entry(self) -> str:
        return "/proc/" + os.readlink("/proc/self")

    def chdir(self, path: str) -> None:
        os.chdir(path)

    def pivot_root(self, new_root: str, put_old: str) -> None:
        result = self._libc.syscall(SYS_PIVOT_ROOT_X86_64, new_root.encode(), put_old.encode())
        self._check(result, f"pivot_root {new_root} {put_old}")

    def umount_detach(self, path: str) -> None:
        self._check(self._libc.umount2(path.encode(), MNT_DETACH), f"umount {path}")

    def rmdir(self, path: str) -> None:
        os.rmdir(path)

    def chroot(self, path: str) -> None:
        os.chroot(path)

    @staticmethod
    def _check(result: int, action: str) -> None:
        if result != 0:
            code = ctypes.get_errno()
            raise OSError(code, f"{action}: {os.strerror(code)}")
