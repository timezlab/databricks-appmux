"""Building the child's root from its plan (design 6.1 step 5).

The plan itself is checked in ``test_sandbox_spec.py``. Here the plan is executed against a
recording stand-in for the system calls: the order of the pivot sequence, the flags of each
mount, and the rule that a step that fails stops the build before the child could ever see a
half-built root. That the kernel accepts the sequence is for the container run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from appmux.sandbox.rootfs import (
    MS_BIND,
    MS_NODEV,
    MS_NOEXEC,
    MS_NOSUID,
    MS_PRIVATE,
    MS_RDONLY,
    MS_REC,
    MS_RELATIME,
    MS_REMOUNT,
    TMP_PATH,
    BindMount,
    HostFile,
    LinuxRootOps,
    Overmount,
    ProcSelf,
    RootfsError,
    RootPlan,
    Symlink,
    TmpfsMount,
    WrittenFile,
    build_root,
    parse_mount_flags,
    tmpfs_mount_data,
)

PIVOT = "/work/newroot"
ROOT = PIVOT + "/rootfs"
LOCKED_FLAGS = MS_NOSUID | MS_NODEV | MS_NOEXEC | MS_RELATIME

Call = tuple[Any, ...]


class RecordingOps:
    """The system calls of the build, recorded; ``fail_on`` makes one mount target fail."""

    def __init__(
        self,
        *,
        existing: set[str] | None = None,
        directories: set[str] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self.calls: list[Call] = []
        self.existing = existing
        self.directories = directories if directories is not None else set()
        self.fail_on = fail_on

    def exists(self, path: str) -> bool:
        return self.existing is None or path in self.existing

    def is_directory(self, path: str) -> bool:
        return path in self.directories

    def makedirs(self, path: str) -> None:
        self.calls.append(("makedirs", path))

    def touch(self, path: str) -> None:
        self.calls.append(("touch", path))

    def symlink(self, destination: str, link: str) -> None:
        self.calls.append(("symlink", destination, link))

    def write_text(self, path: str, content: str) -> None:
        self.calls.append(("write_text", path, content))

    def mount(
        self, source: str | None, target: str, fstype: str | None, flags: int, data: str | None
    ) -> None:
        self.calls.append(("mount", source, target, fstype, flags, data))
        if self.fail_on is not None and target == self.fail_on:
            raise OSError(1, "Operation not permitted")

    def mount_flags_of(self, path: str) -> int:
        return LOCKED_FLAGS

    def process_proc_entry(self) -> str:
        return "/proc/4242"

    def chdir(self, path: str) -> None:
        self.calls.append(("chdir", path))

    def pivot_root(self, new_root: str, put_old: str) -> None:
        self.calls.append(("pivot_root", new_root, put_old))

    def umount_detach(self, path: str) -> None:
        self.calls.append(("umount_detach", path))

    def rmdir(self, path: str) -> None:
        self.calls.append(("rmdir", path))

    def chroot(self, path: str) -> None:
        self.calls.append(("chroot", path))

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def mounts(self) -> list[Call]:
        return [call for call in self.calls if call[0] == "mount"]


def plan_of(*steps: Any) -> RootPlan:
    return RootPlan(steps=tuple(steps))


def index_of(ops: RecordingOps, *prefix: Any) -> int:
    for position, call in enumerate(ops.calls):
        if call[: len(prefix)] == prefix:
            return position
    raise AssertionError(f"no call starting with {prefix}; calls: {ops.calls}")


# ---- binds -------------------------------------------------------------------------------------


def test_a_read_only_bind_is_remounted_with_the_locked_flags_of_its_source() -> None:
    ops = RecordingOps(directories={"/usr"})

    build_root(plan_of(BindMount(Path("/usr"), "/usr")), ops, pivot_dir=PIVOT)

    bind = ROOT + "/usr"
    assert ("mount", "/usr", bind, None, MS_BIND | MS_REC, None) in ops.calls
    # Without the source mount's nosuid, nodev, noexec and relatime the remount fails (spike 2).
    assert (
        "mount",
        None,
        bind,
        None,
        MS_BIND | MS_REMOUNT | MS_RDONLY | LOCKED_FLAGS,
        None,
    ) in ops.calls
    assert index_of(ops, "mount", "/usr", bind) < index_of(ops, "mount", None, bind)


def test_a_writable_bind_is_bound_and_left_writable() -> None:
    ops = RecordingOps(directories={"/work/out"})

    build_root(
        plan_of(BindMount(Path("/work/out"), "/work/out", writable=True)), ops, pivot_dir=PIVOT
    )

    target = ROOT + "/work/out"
    assert [m for m in ops.mounts() if m[2] == target] == [
        ("mount", "/work/out", target, None, MS_BIND | MS_REC, None)
    ]


def test_a_bind_of_a_file_creates_the_file_to_mount_over_and_is_not_recursive() -> None:
    ops = RecordingOps(directories=set())  # /etc/passwd is a file

    build_root(
        plan_of(BindMount(Path("/etc/passwd"), "/etc/passwd", optional=True)), ops, pivot_dir=PIVOT
    )

    assert ("makedirs", ROOT + "/etc") in ops.calls
    assert ("touch", ROOT + "/etc/passwd") in ops.calls
    assert ("mount", "/etc/passwd", ROOT + "/etc/passwd", None, MS_BIND, None) in ops.calls


def test_an_optional_source_that_is_missing_is_skipped_without_a_mount() -> None:
    ops = RecordingOps(existing=set())

    build_root(
        plan_of(BindMount(Path("/etc/ca-certificates"), "/etc/ca-certificates", optional=True)),
        ops,
        pivot_dir=PIVOT,
    )

    assert not [m for m in ops.mounts() if m[2] == ROOT + "/etc/ca-certificates"]


def test_a_required_source_that_is_missing_stops_the_build_before_the_pivot() -> None:
    ops = RecordingOps(existing=set())

    with pytest.raises(RootfsError, match="/srv/app"):
        build_root(plan_of(BindMount(Path("/gone/release"), "/srv/app")), ops, pivot_dir=PIVOT)

    assert "pivot_root" not in ops.names()
    assert "chroot" not in ops.names()


# ---- tmpfs, links, written files, devices ------------------------------------------------------


def test_a_private_tmpfs_is_sized_file_limited_sticky_and_holds_no_setuid_or_devices() -> None:
    ops = RecordingOps()

    build_root(plan_of(TmpfsMount(TMP_PATH, size_mb=64)), ops, pivot_dir=PIVOT)

    assert (
        "mount",
        "tmpfs",
        ROOT + TMP_PATH,
        "tmpfs",
        MS_NOSUID | MS_NODEV,
        "size=64m,nr_inodes=16384,mode=1777",
    ) in ops.calls


def test_a_writable_tmpfs_takes_one_file_for_each_page_its_size_could_hold() -> None:
    # An empty file costs nothing in "size=", so the byte cap alone lets a child make files until
    # the kernel's default (half the RAM pages) is reached, each one kernel memory that no figure
    # of the child's shows (TD-004, T219). One file per 4 KiB page is the most a full mount of
    # one-page files could hold anyway.
    assert tmpfs_mount_data(TmpfsMount("/data", size_mb=64)) == "size=64m,nr_inodes=16384,mode=1777"
    assert tmpfs_mount_data(TmpfsMount("/data", size_mb=16)) == "size=16m,nr_inodes=4096,mode=1777"


def test_a_skeleton_tmpfs_has_no_file_limit_because_nothing_can_write_to_it_afterwards() -> None:
    skeleton = TmpfsMount("/dev", size_mb=1, writable=False, mode=0o755)

    assert tmpfs_mount_data(skeleton) == "size=1m,mode=0755"


def test_a_skeleton_tmpfs_is_made_read_only_after_everything_mounted_below_it() -> None:
    ops = RecordingOps(directories={"/dev/null"})
    plan = plan_of(
        TmpfsMount("/dev", size_mb=1, writable=False, mode=0o755),
        HostFile(Path("/dev/null"), "/dev/null"),
    )

    build_root(plan, ops, pivot_dir=PIVOT)

    skeleton = ROOT + "/dev"
    remount = ("mount", None, skeleton, None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV, None)
    assert remount in ops.calls
    assert index_of(ops, "mount", "/dev/null", ROOT + "/dev/null") < ops.calls.index(remount)
    assert ops.calls.index(remount) < index_of(ops, "pivot_root")


def test_the_proc_skeleton_is_also_noexec() -> None:
    ops = RecordingOps()

    build_root(
        plan_of(TmpfsMount("/proc", size_mb=1, writable=False, mode=0o755, noexec=True)),
        ops,
        pivot_dir=PIVOT,
    )

    assert (
        "mount",
        None,
        ROOT + "/proc",
        None,
        MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC,
        None,
    ) in ops.calls


def test_symlinks_and_written_files_are_created_in_the_new_root() -> None:
    ops = RecordingOps()

    build_root(
        plan_of(
            Symlink("/bin", "usr/bin"), WrittenFile("/etc/resolv.conf", "nameserver 10.0.2.3\n")
        ),
        ops,
        pivot_dir=PIVOT,
    )

    assert ("symlink", "usr/bin", ROOT + "/bin") in ops.calls
    assert ("write_text", ROOT + "/etc/resolv.conf", "nameserver 10.0.2.3\n") in ops.calls


def test_a_device_node_is_bound_from_the_host_dev_and_stays_usable() -> None:
    ops = RecordingOps()

    build_root(plan_of(HostFile(Path("/dev/urandom"), "/dev/urandom")), ops, pivot_dir=PIVOT)

    assert ("touch", ROOT + "/dev/urandom") in ops.calls
    assert ("mount", "/dev/urandom", ROOT + "/dev/urandom", None, MS_BIND, None) in ops.calls


def test_proc_self_is_the_entry_of_the_process_that_will_become_the_child() -> None:
    ops = RecordingOps()

    build_root(
        plan_of(ProcSelf("/proc/self"), Overmount("/proc/self/cgroup")), ops, pivot_dir=PIVOT
    )

    assert ("mount", "/proc/4242", ROOT + "/proc/self", None, MS_BIND, None) in ops.calls
    # The files that name the pod and the host's paths are replaced by /dev/null.
    assert ("mount", "/dev/null", ROOT + "/proc/self/cgroup", None, MS_BIND, None) in ops.calls


# ---- the pivot sequence ------------------------------------------------------------------------


def test_the_sequence_is_private_then_fresh_root_then_pivot_then_detach_then_chroot() -> None:
    ops = RecordingOps(directories={"/usr"})

    build_root(plan_of(BindMount(Path("/usr"), "/usr")), ops, pivot_dir=PIVOT)

    first_mount = ops.mounts()[0]
    assert first_mount == ("mount", None, "/", None, MS_REC | MS_PRIVATE, None)
    order = [
        index_of(ops, "mount", None, "/", None, MS_REC | MS_PRIVATE),
        index_of(ops, "mount", "tmpfs", PIVOT),
        index_of(ops, "mount", "/usr", ROOT + "/usr"),
        index_of(ops, "chdir", PIVOT),
        index_of(ops, "pivot_root", ".", ".old"),
        index_of(ops, "umount_detach", "/.old"),
        index_of(ops, "rmdir", "/.old"),
        index_of(ops, "mount", None, "/", None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV),
        index_of(ops, "chroot", "/rootfs"),
    ]
    assert order == sorted(order)
    assert ops.calls[-1] == ("chdir", "/")  # the working directory is inside the new root


def test_the_old_root_is_detached_so_the_child_cannot_walk_back_to_it() -> None:
    ops = RecordingOps()

    build_root(plan_of(), ops, pivot_dir=PIVOT)

    assert ("makedirs", PIVOT + "/.old") in ops.calls
    assert "umount_detach" in ops.names()


def test_a_failing_mount_raises_naming_the_step_and_never_reaches_the_pivot() -> None:
    ops = RecordingOps(directories={"/usr"}, fail_on=ROOT + TMP_PATH)

    with pytest.raises(RootfsError, match=TMP_PATH) as raised:
        build_root(
            plan_of(TmpfsMount(TMP_PATH, size_mb=64), BindMount(Path("/usr"), "/usr")),
            ops,
            pivot_dir=PIVOT,
        )

    assert isinstance(raised.value.__cause__, OSError)
    assert "pivot_root" not in ops.names()
    assert "chroot" not in ops.names()
    assert not [m for m in ops.mounts() if m[2] == ROOT + "/usr"]  # nothing runs after the failure


# ---- reading the source mount's flags ----------------------------------------------------------

MOUNTINFO = """\
24 1 0:21 / / rw,relatime - overlay overlay rw,lowerdir=/x,upperdir=/y
25 24 0:22 / /usr ro,nosuid,nodev,relatime - overlay overlay ro
26 25 0:23 / /usr/local rw,nosuid,noexec,noatime - ext4 /dev/sda1 rw
27 24 0:24 / /app/python/source_code rw,nosuid,nodev,noexec,relatime - ext4 /dev/sdb rw
"""


def test_the_flags_come_from_the_longest_mount_point_that_holds_the_path() -> None:
    assert parse_mount_flags(MOUNTINFO, "/usr/bin/python3") == MS_NOSUID | MS_NODEV | MS_RELATIME
    assert (
        parse_mount_flags(MOUNTINFO, "/usr/local/bin") == MS_NOSUID | MS_NOEXEC | 0x400
    )  # noatime
    assert (
        parse_mount_flags(MOUNTINFO, "/app/python/source_code/.venv")
        == MS_NOSUID | MS_NODEV | MS_NOEXEC | MS_RELATIME
    )


def test_a_path_on_the_root_mount_takes_the_roots_flags() -> None:
    assert parse_mount_flags(MOUNTINFO, "/etc/ssl") == MS_RELATIME


def test_a_name_that_only_starts_like_a_mount_point_is_not_inside_it() -> None:
    assert parse_mount_flags(MOUNTINFO, "/usrlocal/bin") == MS_RELATIME  # not under /usr


# ---- the real file operations -------------------------------------------------------------------


def test_touching_a_file_creates_it_and_leaves_an_existing_one_as_it_was(tmp_path: Path) -> None:
    ops = LinuxRootOps()
    created = tmp_path / "new"
    existing = tmp_path / "old"
    existing.write_text("kept")

    ops.touch(str(created))
    ops.touch(str(existing))

    assert created.read_text() == ""
    assert existing.read_text() == "kept"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procfs is Linux-only")
def test_touching_a_procfs_file_that_exists_does_not_try_to_seek_it() -> None:
    # The first container start failed with "[Errno 22] Invalid argument" and no file name:
    # opening for append makes Python seek to the end, and procfs refuses that seek. The
    # overmount of /proc/self/cgroup touches a file that already exists.
    LinuxRootOps().touch("/proc/self/comm")
