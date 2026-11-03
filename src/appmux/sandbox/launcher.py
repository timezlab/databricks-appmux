"""The sandbox launcher of design 6.1: one supervisor process per child, and stage 1 inside it.

Three roles live here, because they are one protocol and change together:

* :class:`SandboxSpec` is everything the launcher needs, as data. The spec reaches the supervisor
  on stdin and stage 1 on an inherited memory file, so the child's secrets are never in a command
  line. ``omit_layers`` is the one switch that weakens a layer (the integration test of fail-closed
  self checks sets it); nothing else in appmux reads it.
* :func:`start_sandbox` starts a **supervisor** (``python -m appmux.sandbox.launcher supervise``)
  and returns a :class:`SandboxHandle`. The supervisor is a subreaper (the container's PID 1 does
  not reap, so each teardown would otherwise leave a zombie). It runs ``unshare ... stage1``,
  receives the packet socket stage 1 hands over, starts ``slirp4netns``, runs the frame pump, and
  reports ``started`` / ``failed`` / ``exited`` as JSON lines on stdout.
* **Stage 1** (``... launcher stage1``) is the first process in the new namespaces. It builds the
  child's network and root, limits itself, drops every capability and ``exec``s the child. Any
  step that fails stops it before ``exec``; the reason travels to the supervisor on the channel.

The channel between them is a unix socket that stage 1 connects to by path, not an inherited
descriptor: ``unshare --fork`` keeps a copy of every descriptor it inherits for as long as the
sandbox lives, so an inherited close-on-exec pipe would never report EOF when ``exec`` succeeds.

Everything the kernel does is behind a small seam (:class:`Stage1Ops`, the ``prctl`` and ``capset``
callables, the injectable commands of :class:`Supervisor`), so the sequence, the layers and the
fail-closed behaviour are unit-tested without namespaces; what the kernel then does is for the
container run (``tests/integration``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import errno
import json
import logging
import os
import platform
import queue
import resource
import select
import signal
import socket
import struct
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Protocol, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from appmux.sandbox.pump import (
    ATTACHED_SIGNAL,
    PUMP_INTERFACE,
    VETH_MTU,
    FramePump,
    NetworkAttachError,
    PacketPort,
    PumpStats,
    RtnetlinkSocket,
    RulesFileError,
    configure_child_network,
    disable_offloads,
    load_ruleset,
    log_tail,
    open_packet_socket,
    packet_port_for,
    receive_packet_socket,
    require_slirp_binary,
    send_packet_socket,
    slirp_binary_path,
    start_slirp,
    wait_until_attached,
    write_stats_file,
)
from appmux.sandbox.relay import bind_relay_socket, serve_relay
from appmux.sandbox.rootfs import (
    RELEASE_PATH,
    LinuxRootOps,
    RootfsError,
    RootLayout,
    RootPlan,
    build_root,
    overlay_path,
    plan_rootfs,
)
from appmux.sandbox.seccomp import SeccompError, deny_sched_setaffinity, load_filter
from appmux.sandbox.watchdog import apply_address_space_limit

logger = logging.getLogger(__name__)

LAUNCHER_MODULE: Final = "appmux.sandbox.launcher"

# ---- tunables (named so a measurement can change them in one place) ------------------------------

# Untuned defaults: spike 7 measured that a limit is counted per sandbox and stops a fork test,
# not what a real app needs. A caller with a measurement overrides them in the spec.
DEFAULT_MAX_PROCESSES: Final = 256
DEFAULT_FILE_SIZE_LIMIT_BYTES: Final = 256 * 1024 * 1024
CHILD_NICE: Final = 19
CHILD_WORKING_DIRECTORY: Final = RELEASE_PATH

# What the child's PATH ends with: the standard directories of the allow-listed root.
SYSTEM_PATH: Final = ("/usr/local/bin", "/usr/bin", "/bin")

ATTACH_TIMEOUT_S: Final = 30.0  # stage 1 waits this long for the supervisor to attach slirp4netns
HANDOFF_TIMEOUT_S: Final = (
    20.0  # the supervisor waits this long for stage 1 to connect and hand over
)
EXEC_TIMEOUT_S: Final = 20.0  # ... and this long, after attaching, for stage 1 to exec the child
STOP_GRACE_S: Final = 10.0  # between asking the init to terminate and killing it
POLL_INTERVAL_S: Final = 0.05
PUMP_STOP_WAIT_S: Final = 2.0
SLIRP_STOP_WAIT_S: Final = 2.0
STATS_POLL_INTERVAL_S: Final = 0.02
CHANNEL_READ_BYTES: Final = 4096

PIVOT_DIRECTORY_NAME: Final = "newroot"  # under the scratch directory; a tmpfs is mounted on it
LAUNCH_SOCKET_NAME: Final = "launch.sock"
STATS_FILE_NAME: Final = "pump-stats.json"
SLIRP_LOG_NAME: Final = "slirp.log"
SUPERVISOR_LOG_NAME: Final = "supervisor.log"
LOG_FILE_MODE: Final = 0o600
SCRATCH_DIRECTORY_MODE: Final = 0o700
DEFAULT_PATH_FOR_HELPERS: Final = "/usr/local/bin:/usr/bin:/bin"
MAX_DESCRIPTOR_CEILING: Final = 1 << 20

PR_SET_PDEATHSIG: Final = 1
PR_SET_CHILD_SUBREAPER: Final = 36
PR_CAPBSET_READ: Final = 23
PR_CAPBSET_DROP: Final = 24
PR_SET_NO_NEW_PRIVS: Final = 38
PR_GET_NO_NEW_PRIVS: Final = 39
PR_CAP_AMBIENT: Final = 47
PR_CAP_AMBIENT_CLEAR_ALL: Final = 4
MAX_CAPABILITY_NUMBER: Final = 64  # the bounding set has at most 64 bits
LINUX_CAPABILITY_VERSION_3: Final = 0x20080522
CAPSET_DATA_BYTES: Final = 24  # two __user_cap_data_struct of 12 bytes: no capability in any set
SYSCALL_CAPSET: Final = {"x86_64": 126, "aarch64": 91}
# memfd_create is called as a raw system call: the standalone CPython builds that uv installs are
# compiled against an older libc and have no ``os.memfd_create``; the kernel has had it since 3.17.
SYSCALL_MEMFD_CREATE: Final = {"x86_64": 319, "aarch64": 279}
MFD_CLOEXEC: Final = 1

UNSHARE_FLAGS: Final = (
    "--user",
    "--map-root-user",
    "--net",
    "--mount",
    "--pid",
    "--ipc",
    "--fork",
    "--kill-child",
)


class SandboxStartError(RuntimeError):
    """The sandbox did not start; the message says which step failed and what to check."""


class Stage1Error(RuntimeError):
    """A step of stage 1 failed; the child was not started."""


class CapabilityDropError(RuntimeError):
    """The capability drop or ``no_new_privs`` did not take effect; the child must not start."""


# ---- the spec ------------------------------------------------------------------------------------


class Layer(StrEnum):
    """A protection that ``omit_layers`` can leave out, for the integration test of SC-002."""

    PROC_OVERMOUNT = "proc-overmount"
    CAPABILITY_DROP = "capability-drop"
    PUMP_FILTER = "pump-filter"


class SandboxSpec(BaseModel):
    """What one sandbox is: the command, its environment, the mounts, the limits and the layers."""

    # hide_input_in_errors: a rejected spec must not echo the child's environment values.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    slug: str = Field(min_length=1)
    command: tuple[str, ...] = Field(min_length=1)
    environment: dict[str, str]
    app_environment: Path
    release_dir: Path
    scratch_dir: Path
    log_path: Path
    rules_path: Path
    # Where the child's stderr goes; ``None`` sends it to ``log_path`` with its stdout. The
    # reconciler keeps the two apart so a line of the app's output says which stream it came from.
    stderr_path: Path | None = None
    overlays: tuple[Path, ...] = ()
    build_output: Path | None = None
    host_run_dir: Path | None = None
    relay_socket_path: Path | None = None
    child_port: int | None = Field(default=None, ge=1, le=65535)
    max_processes: int = Field(default=DEFAULT_MAX_PROCESSES, ge=1)
    file_size_limit_bytes: int = Field(default=DEFAULT_FILE_SIZE_LIMIT_BYTES, ge=1)
    memory_budget_mb: int | None = Field(default=None, ge=1)
    pinned_cpu: int | None = Field(default=None, ge=0)
    wall_clock_limit_s: float | None = Field(default=None, gt=0)
    omit_layers: frozenset[Layer] = frozenset()

    @model_validator(mode="after")
    def _relay_needs_a_port(self) -> Self:
        if self.relay_socket_path is not None and self.child_port is None:
            raise ValueError("relay_socket_path needs child_port: the relay forwards to that port")
        return self

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str) -> Self:
        return cls.model_validate_json(text)

    def __repr__(self) -> str:
        names = ", ".join(sorted(self.environment))
        return (
            f"SandboxSpec(slug={self.slug!r}, command={list(self.command)!r}, "
            f"environment=<{len(self.environment)} variables: {names}>)"
        )

    def __str__(self) -> str:
        return repr(self)


def unshare_command(stage1: Sequence[str]) -> list[str]:
    """The ``unshare`` command line that runs ``stage1`` in the new namespaces."""
    return ["unshare", *UNSHARE_FLAGS, "--", *stage1]


def root_layout_for(spec: SandboxSpec) -> RootLayout:
    return RootLayout(
        app_environment=spec.app_environment,
        release=spec.release_dir,
        overlays=spec.overlays,
        build_output=spec.build_output,
        host_run_dir=spec.host_run_dir,
        overmount_proc_files=Layer.PROC_OVERMOUNT not in spec.omit_layers,
    )


def child_python_path(spec: SandboxSpec) -> str | None:
    """Overlays first, in the order given, so a pinned package wins over the app's own."""
    if not spec.overlays:
        return None
    return ":".join(overlay_path(index) for index in range(len(spec.overlays)))


def child_path(spec: SandboxSpec) -> str:
    overlay_bins = [f"{overlay_path(index)}/bin" for index in range(len(spec.overlays))]
    return ":".join([*overlay_bins, f"{spec.app_environment}/bin", *SYSTEM_PATH])


def child_environment(spec: SandboxSpec) -> dict[str, str]:
    """The spec's environment with the launcher's ``PATH`` and ``PYTHONPATH`` on top.

    ``PATH`` is the launcher's alone. An app's own ``PYTHONPATH`` is kept behind the overlays
    instead of being dropped: a standard Databricks App may depend on it (constitution I), and
    the overlays still come first, which is what lets them win.
    """
    environment = dict(spec.environment)
    environment["PATH"] = child_path(spec)
    own_python_path = environment.pop("PYTHONPATH", None)
    entries = [entry for entry in (child_python_path(spec), own_python_path) if entry]
    if entries:
        environment["PYTHONPATH"] = ":".join(entries)
    return environment


def stats_path_for(spec: SandboxSpec) -> Path:
    """Where the supervisor writes the pump counters when it gets ``SIGUSR1``."""
    return spec.scratch_dir / STATS_FILE_NAME


def choose_pinned_cpu(allowed_cpus: Collection[int]) -> int:
    """The CPU to pin a child to: the highest allowed one, away from where work usually starts."""
    if not allowed_cpus:
        raise ValueError("no CPU is allowed for this process, so there is nothing to pin to")
    return max(allowed_cpus)


# ---- kernel calls --------------------------------------------------------------------------------


class Prctl(Protocol):
    def __call__(
        self, option: int, arg2: int = 0, arg3: int = 0, arg4: int = 0, arg5: int = 0, /
    ) -> int: ...


def _prctl(option: int, arg2: int = 0, arg3: int = 0, arg4: int = 0, arg5: int = 0, /) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = int(libc.prctl(option, arg2, arg3, arg4, arg5))
    if result == -1:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def _capset(header: bytes, data: bytes) -> None:
    number = SYSCALL_CAPSET.get(platform.machine())
    if number is None:
        raise OSError(errno.ENOSYS, f"capset is not known for {platform.machine()}")
    libc = ctypes.CDLL(None, use_errno=True)
    header_buffer = ctypes.create_string_buffer(header, len(header))
    data_buffer = ctypes.create_string_buffer(data, len(data))
    if libc.syscall(number, header_buffer, data_buffer) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def create_memory_file(name: str) -> int:
    """An anonymous in-memory file (``memfd_create``): the spec's secrets never touch disk."""
    number = SYSCALL_MEMFD_CREATE.get(platform.machine())
    if number is None:
        raise OSError(errno.ENOSYS, f"memfd_create is not known for {platform.machine()}")
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = int(libc.syscall(number, name.encode("ascii"), MFD_CLOEXEC))
    if descriptor == -1:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return descriptor


def become_subreaper() -> None:
    """Make this process the reaper of its orphaned descendants (``PR_SET_CHILD_SUBREAPER``)."""
    _prctl(PR_SET_CHILD_SUBREAPER, 1)


def _die_with_parent() -> None:
    """``preexec_fn`` of the sandbox process: SIGKILL it if the supervisor goes away.

    It runs before the supervisor has started any thread, which is what makes ``preexec_fn``
    safe here. With ``unshare --kill-child`` this chains: supervisor dies, ``unshare`` dies, the
    sandbox's init dies, and the kernel ends every process of the pid namespace.
    """
    _prctl(PR_SET_PDEATHSIG, signal.SIGKILL)


def drop_capabilities(
    *, prctl: Prctl = _prctl, capset: Callable[[bytes, bytes], None] = _capset
) -> None:
    """Remove every capability: bounding set, ambient set, then the permitted/effective sets.

    The spike's version ignored any error other than ``EINVAL`` (past the last capability). Here
    every other error raises, and the bounding set is read back, so a drop that silently did
    nothing cannot go on to ``exec`` the child.
    """
    dropped = 0
    for capability in range(MAX_CAPABILITY_NUMBER):
        try:
            prctl(PR_CAPBSET_DROP, capability)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                break  # past the last capability this kernel knows
            raise CapabilityDropError(
                f"cannot drop capability {capability} from the bounding set: {exc}"
            ) from exc
        dropped += 1
    if dropped == 0:
        raise CapabilityDropError("the kernel accepted no capability in the bounding set drop")
    try:
        prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL)
        capset(struct.pack("Ii", LINUX_CAPABILITY_VERSION_3, 0), bytes(CAPSET_DATA_BYTES))
    except OSError as exc:
        raise CapabilityDropError(f"cannot clear the capability sets: {exc}") from exc
    for capability in range(dropped):
        if prctl(PR_CAPBSET_READ, capability):
            raise CapabilityDropError(
                f"the bounding set still holds capability {capability} after the drop"
            )


def forbid_new_privileges(*, prctl: Prctl = _prctl) -> None:
    """``no_new_privs``: no ``exec`` in this process tree can gain privileges. Read back."""
    try:
        prctl(PR_SET_NO_NEW_PRIVS, 1)
        enabled = prctl(PR_GET_NO_NEW_PRIVS)
    except OSError as exc:
        raise CapabilityDropError(f"cannot set no_new_privs: {exc}") from exc
    if enabled != 1:
        raise CapabilityDropError("no_new_privs did not take effect after it was set")


def apply_resource_limits(
    spec: SandboxSpec,
    *,
    setrlimit: Callable[[int, tuple[int, int]], None] = resource.setrlimit,
) -> None:
    """``RLIMIT_NPROC`` and ``RLIMIT_FSIZE`` from the spec, ``RLIMIT_AS`` from the memory budget.

    Soft and hard are set together so the child cannot lift its own limit. ``RLIMIT_NPROC`` is
    counted per sandbox (spike 7).
    """
    setrlimit(resource.RLIMIT_NPROC, (spec.max_processes, spec.max_processes))
    setrlimit(resource.RLIMIT_FSIZE, (spec.file_size_limit_bytes, spec.file_size_limit_bytes))
    if spec.memory_budget_mb is not None:
        apply_address_space_limit(spec.memory_budget_mb, setrlimit=setrlimit)


def _set_affinity(cpus: Collection[int]) -> None:
    os.sched_setaffinity(0, cpus)


def _set_nice(nice: int) -> None:
    os.setpriority(os.PRIO_PROCESS, 0, nice)


def apply_cpu_limits(
    pinned_cpu: int,
    *,
    set_affinity: Callable[[Collection[int]], None] = _set_affinity,
    set_priority: Callable[[int], None] = _set_nice,
) -> None:
    """Pin this process (and what it starts) to one CPU at the lowest scheduling priority."""
    set_priority(CHILD_NICE)
    set_affinity({pinned_cpu})


def close_all_descriptors_except(keep: Iterable[int]) -> None:
    """Close every descriptor above 2 except ``keep``: the child inherits only 0, 1 and 2."""
    soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    ceiling = MAX_DESCRIPTOR_CEILING if soft_limit == resource.RLIM_INFINITY else soft_limit
    low = 3
    for descriptor in sorted(set(keep)):
        if descriptor >= low:
            os.closerange(low, descriptor)
            low = descriptor + 1
    os.closerange(low, ceiling)


# ---- stage 1: the sequence -----------------------------------------------------------------------


class Stage1Ops(Protocol):
    """The OS actions of stage 1, in the groups :func:`run_stage1` sequences."""

    def configure_network(self) -> None: ...
    def hand_over_packet_socket(self) -> None: ...
    def build_root(self, plan: RootPlan) -> None: ...
    def start_relay(self, child_port: int) -> None: ...
    def limit_resources(self, spec: SandboxSpec) -> None: ...
    def limit_cpu(self, pinned_cpu: int) -> None: ...
    def drop_capabilities(self) -> None: ...
    def forbid_new_privileges(self) -> None: ...
    def install_seccomp(self) -> None: ...
    def close_descriptors(self) -> None: ...
    def exec_child(
        self, command: tuple[str, ...], environment: Mapping[str, str], working_directory: str
    ) -> None: ...


# Everything a step can raise. A step that fails for any other reason is a bug, and stage 1 ends
# with its traceback in the child's log, which is also before ``exec``.
_STEP_FAILURES: Final = (OSError, RuntimeError, ValueError, RootfsError)


def run_stage1(spec: SandboxSpec, ops: Stage1Ops) -> None:
    """Run the steps of design 6.1 in order; the first failure raises and nothing later runs.

    The order matters: the network exists before the root hides the host; the relay is forked
    before the limits and the capability drop so it is not held to the child's budget (it drops
    its own capabilities); the capabilities are dropped before ``no_new_privs`` and the seccomp
    filter (which needs it); descriptors are closed last, so nothing the child must not hold
    survives into ``exec``.
    """
    steps: list[tuple[str, Callable[[], None]]] = [
        ("configure_network", ops.configure_network),
        ("hand_over_packet_socket", ops.hand_over_packet_socket),
        ("build_root", lambda: ops.build_root(plan_rootfs(root_layout_for(spec)))),
    ]
    child_port = spec.child_port
    if spec.relay_socket_path is not None and child_port is not None:
        steps.append(("start_relay", lambda: ops.start_relay(child_port)))
    steps.append(("limit_resources", lambda: ops.limit_resources(spec)))
    pinned_cpu = spec.pinned_cpu
    if pinned_cpu is not None:
        steps.append(("limit_cpu", lambda: ops.limit_cpu(pinned_cpu)))
    if Layer.CAPABILITY_DROP not in spec.omit_layers:
        steps.append(("drop_capabilities", ops.drop_capabilities))
    steps.append(("forbid_new_privileges", ops.forbid_new_privileges))
    if pinned_cpu is not None:
        # The filter keeps the child from widening its own pin (spike 7); no pin, nothing to keep.
        steps.append(("install_seccomp", ops.install_seccomp))
    steps.append(("close_descriptors", ops.close_descriptors))
    steps.append(
        (
            "exec_child",
            lambda: ops.exec_child(spec.command, child_environment(spec), CHILD_WORKING_DIRECTORY),
        )
    )
    for name, action in steps:
        try:
            action()
        except (*_STEP_FAILURES, CapabilityDropError, SeccompError, NetworkAttachError) as exc:
            raise Stage1Error(
                f"stage 1 step {name!r} failed: {exc}; the sandbox was not started"
            ) from exc


class LinuxStage1Ops:
    """The real stage-1 actions. Container-only: they need the new namespaces."""

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        channel: socket.socket,
        relay_listener: socket.socket | None,
    ) -> None:
        self._spec = spec
        self._channel = channel
        self._relay_listener = relay_listener

    def configure_network(self) -> None:
        netlink = RtnetlinkSocket()
        try:
            configure_child_network(
                netlink, index_of=socket.if_nametoindex, disable_offloads=disable_offloads
            )
        finally:
            netlink.close()

    def hand_over_packet_socket(self) -> None:
        packet_socket = open_packet_socket(PUMP_INTERFACE)
        try:
            send_packet_socket(self._channel, packet_socket)
        finally:
            packet_socket.close()
        wait_until_attached(self._channel, timeout_s=ATTACH_TIMEOUT_S)

    def build_root(self, plan: RootPlan) -> None:
        pivot_dir = str(self._spec.scratch_dir / PIVOT_DIRECTORY_NAME)
        build_root(plan, LinuxRootOps(), pivot_dir=pivot_dir)

    def start_relay(self, child_port: int) -> None:
        listener = self._relay_listener
        if listener is None:
            raise Stage1Error("the spec asks for a relay but the supervisor passed no socket")
        if os.fork() != 0:
            return
        # The forked relay. Nothing below may return into stage 1: that would run the child's
        # setup twice, and the relay must not outlive its own failure silently.
        try:
            # The channel is the supervisor's signal that exec succeeded (EOF); a relay that kept
            # a copy open would hold that signal back forever.
            close_all_descriptors_except([listener.fileno()])
            if Layer.CAPABILITY_DROP not in self._spec.omit_layers:
                drop_capabilities()
            forbid_new_privileges()
            asyncio.run(serve_relay(listener, child_port))
        except BaseException:  # forked child: report, then leave through os._exit below
            traceback.print_exc()
        finally:
            os._exit(1)

    def limit_resources(self, spec: SandboxSpec) -> None:
        apply_resource_limits(spec)

    def limit_cpu(self, pinned_cpu: int) -> None:
        apply_cpu_limits(pinned_cpu)

    def drop_capabilities(self) -> None:
        drop_capabilities()

    def forbid_new_privileges(self) -> None:
        forbid_new_privileges()

    def install_seccomp(self) -> None:
        load_filter(deny_sched_setaffinity())

    def close_descriptors(self) -> None:
        # The channel stays open until exec closes it (it is close-on-exec): its EOF is how the
        # supervisor learns that the child started.
        close_all_descriptors_except([self._channel.fileno()])

    def exec_child(
        self, command: tuple[str, ...], environment: Mapping[str, str], working_directory: str
    ) -> None:
        os.chdir(working_directory)
        # S606: running the child without a shell is the point; command is an argument list.
        os.execvpe(command[0], list(command), dict(environment))  # noqa: S606


def _report_stage1_failure(channel: socket.socket | None, reason: str) -> int:
    sys.stderr.write(f"{reason}\n")
    sys.stderr.flush()
    if channel is not None:
        # The supervisor may already have given up and closed its end; stderr has the reason.
        with contextlib.suppress(OSError):
            channel.sendall(reason.encode("utf-8"))
    return 1


def stage1_main(spec_fd: int, channel_path: Path, relay_fd: int | None) -> int:
    """Entry point of stage 1. Returns only on failure; success is an ``exec``."""
    try:
        with os.fdopen(spec_fd, encoding="utf-8") as spec_file:
            spec = SandboxSpec.from_json(spec_file.read())
    except (OSError, ValueError) as exc:
        return _report_stage1_failure(None, f"cannot read the sandbox spec: {exc}")
    channel = socket.socket(socket.AF_UNIX)
    try:
        channel.connect(str(channel_path))
    except OSError as exc:
        return _report_stage1_failure(None, f"cannot reach the supervisor at {channel_path}: {exc}")
    try:
        listener = None if relay_fd is None else socket.socket(fileno=relay_fd)
        run_stage1(spec, LinuxStage1Ops(spec, channel=channel, relay_listener=listener))
    except (Stage1Error, OSError, ValueError) as exc:
        return _report_stage1_failure(channel, str(exc))
    return _report_stage1_failure(channel, "stage 1 returned without starting the child")


# ---- how a sandbox ended -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxExit:
    """How the sandbox ended: an exit status or the signal that killed it, and why we stopped it."""

    status: int
    signal: int | None
    reason: str = ""

    @classmethod
    def from_returncode(cls, returncode: int, *, reason: str = "") -> SandboxExit:
        if returncode < 0:
            return cls(status=128 - returncode, signal=-returncode, reason=reason)
        return cls(status=returncode, signal=None, reason=reason)

    def describe(self) -> str:
        if self.signal is not None:
            what = f"was killed by {signal.Signals(self.signal).name}"
        else:
            what = f"exited with status {self.status}"
        return f"{what} ({self.reason})" if self.reason else what


def _open_log(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, LOG_FILE_MODE)


def _read_children(pid: int) -> str:
    return Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")


def find_init_pid(
    unshare_pid: int, *, read_children: Callable[[int], str] = _read_children
) -> int | None:
    """The sandbox's init: the only child of ``unshare``. ``None`` if it cannot be told for sure."""
    try:
        children = read_children(unshare_pid).split()
    except OSError:
        return None  # no /proc/<pid>/children (kernel option) or the process is gone
    if len(children) != 1:
        return None
    return int(children[0])


# ---- the supervisor ------------------------------------------------------------------------------

_Request = Literal["stop", "reload", "stats"]
_START_FAILURES: Final = (SandboxStartError, NetworkAttachError, RulesFileError, OSError)


def install_signal_handlers(handlers: Mapping[int, Callable[[], None]]) -> Callable[[], None]:
    """Run ``handlers`` on their signals; return a function that puts the old handlers back."""
    previous: dict[int, Any] = {}

    def make_handler(handler: Callable[[], None]) -> Callable[[int, Any], None]:
        return lambda _signal_number, _frame: handler()

    for number, handler in handlers.items():
        previous[number] = signal.signal(number, make_handler(handler))

    def restore() -> None:
        for number, old in previous.items():
            signal.signal(number, old)

    return restore


class Supervisor:
    """Runs one sandbox: spawns stage 1, attaches the network, then waits for it to end.

    Signal handlers must only call :meth:`request_stop`, :meth:`request_reload` and
    :meth:`request_stats`: they put a request on a queue (``SimpleQueue.put`` is safe in a
    handler), and the loop in :meth:`run` carries it out. Locks taken in a handler could deadlock
    against the same thread's own code.
    """

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        launch_command: Sequence[str],
        slirp_binary: Path,
        announce: Callable[[dict[str, Any]], None],
        stage1_environment: Mapping[str, str] | None = None,
        port_factory: Callable[[socket.socket], PacketPort] = packet_port_for,
        set_subreaper: Callable[[], None] = become_subreaper,
        find_init: Callable[[int], int | None] = find_init_pid,
        stop_grace_s: float = STOP_GRACE_S,
        handoff_timeout_s: float = HANDOFF_TIMEOUT_S,
        exec_timeout_s: float = EXEC_TIMEOUT_S,
    ) -> None:
        self.spec = spec
        self.pump: FramePump | None = None
        self._launch_command = list(launch_command)
        self._slirp_binary = slirp_binary
        self._announce_event = announce
        self._stage1_environment = (
            dict(stage1_environment)
            if stage1_environment is not None
            else {name: os.environ[name] for name in ("PATH", "PYTHONPATH") if name in os.environ}
        )
        self._port_factory = port_factory
        self._set_subreaper = set_subreaper
        self._find_init = find_init
        self._stop_grace_s = stop_grace_s
        self._handoff_timeout_s = handoff_timeout_s
        self._exec_timeout_s = exec_timeout_s
        self._process: subprocess.Popen[bytes] | None = None
        self._requests: queue.SimpleQueue[_Request] = queue.SimpleQueue()

    # -- requests (safe from a signal handler) --

    def request_stop(self) -> None:
        self._requests.put("stop")

    def request_reload(self) -> None:
        self._requests.put("reload")

    def request_stats(self) -> None:
        self._requests.put("stats")

    # -- actions --

    def reload_rules(self) -> None:
        """Load the rules file into the running pump; a file that does not parse changes nothing."""
        if self.pump is not None:
            self.pump.reload_rules(self.spec.rules_path)

    def write_stats(self) -> None:
        if self.pump is not None:
            write_stats_file(stats_path_for(self.spec), self.pump.snapshot())

    def run(self) -> int:
        """Start the sandbox, report, wait for it to end, report. ``1`` if it did not start."""
        with contextlib.ExitStack() as cleanup:
            try:
                process = self._start(cleanup)
            except _START_FAILURES as exc:
                reason = str(exc)
                cleanup.close()  # the sandbox and the helpers are gone before anyone is told
                self._announce({"event": "failed", "reason": reason})
                return 1
            self._announce({"event": "started", "sandbox_pid": process.pid})
            outcome = self._wait_for_end(process)
            cleanup.close()
            self._announce(
                {
                    "event": "exited",
                    "status": outcome.status,
                    "signal": outcome.signal,
                    "reason": outcome.reason,
                }
            )
        return 0

    # -- starting --

    def _start(self, cleanup: contextlib.ExitStack) -> subprocess.Popen[bytes]:
        self._set_subreaper()
        ruleset = load_ruleset(self.spec.rules_path)  # before anything is spawned
        slirp_binary = require_slirp_binary(self._slirp_binary)
        launch_path = self.spec.scratch_dir / LAUNCH_SOCKET_NAME
        launch_listener = bind_relay_socket(launch_path)  # the same bind: unix, 0600, listening
        cleanup.callback(self._close_launch_socket, launch_listener, launch_path)
        relay_listener = None
        if self.spec.relay_socket_path is not None:
            # Bound only: the relay listens once the child's port is up (sandbox/relay.py).
            relay_listener = bind_relay_socket(self.spec.relay_socket_path, is_listening=False)
            cleanup.callback(relay_listener.close)
        process = self._spawn(launch_path, relay_listener)
        self._process = process
        cleanup.callback(self._kill_sandbox)
        channel = self._accept_stage1(launch_listener, process)
        cleanup.callback(channel.close)
        packet_socket = self._receive_packet_socket(channel)
        cleanup.callback(packet_socket.close)
        slirp_end, pump_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        cleanup.callback(pump_end.close)
        try:
            slirp = start_slirp(
                slirp_binary,
                slirp_end,
                mtu=VETH_MTU,
                log_path=self.spec.scratch_dir / SLIRP_LOG_NAME,
            )
        finally:
            slirp_end.close()  # slirp4netns holds its own copy
        cleanup.callback(self._stop_slirp, slirp)
        pump = FramePump(
            self._port_factory(packet_socket),
            pump_end,
            ruleset,
            filter_frames=Layer.PUMP_FILTER not in self.spec.omit_layers,
        )
        self.pump = pump
        cleanup.callback(self._stop_pump, pump)
        pump.start()
        channel.sendall(ATTACHED_SIGNAL)
        self._wait_for_exec(channel)
        return process

    def _spawn(
        self, launch_path: Path, relay_listener: socket.socket | None
    ) -> subprocess.Popen[bytes]:
        arguments = [f"--channel-path={launch_path}"]
        spec_fd = create_memory_file("appmux-sandbox-spec")
        log_fd = _open_log(self.spec.log_path)
        stderr_fd = log_fd if self.spec.stderr_path is None else _open_log(self.spec.stderr_path)
        try:
            with open(spec_fd, "wb", closefd=False) as spec_file:
                spec_file.write(self.spec.to_json().encode("utf-8"))
            os.lseek(spec_fd, 0, os.SEEK_SET)
            pass_fds = [spec_fd]
            arguments.append(f"--spec-fd={spec_fd}")
            if relay_listener is not None:
                pass_fds.append(relay_listener.fileno())
                arguments.append(f"--relay-fd={relay_listener.fileno()}")
            return subprocess.Popen(
                [*self._launch_command, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=stderr_fd,
                env=dict(self._stage1_environment),
                pass_fds=pass_fds,
                preexec_fn=_die_with_parent,  # safe: see _die_with_parent
            )
        finally:
            os.close(spec_fd)
            os.close(log_fd)
            if stderr_fd != log_fd:
                os.close(stderr_fd)

    def _accept_stage1(
        self, listener: socket.socket, process: subprocess.Popen[bytes]
    ) -> socket.socket:
        deadline = time.monotonic() + self._handoff_timeout_s
        while True:
            exited = (
                process.poll() is not None
            )  # read before the wait: a connection made first is seen
            readable, _, _ = select.select([listener], [], [], POLL_INTERVAL_S)
            if readable:
                connection, _ = listener.accept()
                connection.settimeout(self._handoff_timeout_s)
                return connection
            if exited:
                how = SandboxExit.from_returncode(process.returncode).describe()
                tail = "\n".join(
                    part for part in (log_tail(self.spec.log_path), self._stderr_tail()) if part
                )
                raise SandboxStartError(
                    f"the sandbox process {how} before it connected to the supervisor"
                    + (f"; the end of its log: {tail}" if tail else "")
                )
            if time.monotonic() >= deadline:
                raise SandboxStartError(
                    f"stage 1 did not connect to the supervisor within "
                    f"{self._handoff_timeout_s:g} s; check {self.spec.log_path}"
                )

    def _stderr_tail(self) -> str:
        return "" if self.spec.stderr_path is None else log_tail(self.spec.stderr_path)

    def _receive_packet_socket(self, channel: socket.socket) -> socket.socket:
        try:
            return receive_packet_socket(channel)
        except TimeoutError as exc:
            raise SandboxStartError(
                "stage 1 did not hand over the packet socket within "
                f"{self._handoff_timeout_s:g} s; check {self.spec.log_path}"
            ) from exc

    def _wait_for_exec(self, channel: socket.socket) -> None:
        """EOF on the channel is the close-on-exec signal; any bytes before it are the failure."""
        channel.settimeout(self._exec_timeout_s)
        reason = bytearray()
        try:
            while chunk := channel.recv(CHANNEL_READ_BYTES):
                reason += chunk
        except TimeoutError as exc:
            raise SandboxStartError(
                f"the child was not started within {self._exec_timeout_s:g} s of attaching the "
                f"network; check {self.spec.log_path}"
            ) from exc
        except ConnectionError as exc:
            raise SandboxStartError(
                f"stage 1 ended before the child started; check {self.spec.log_path}"
            ) from exc
        if reason:
            raise SandboxStartError(reason.decode("utf-8", errors="replace"))

    # -- waiting and stopping --

    def _wait_for_end(self, process: subprocess.Popen[bytes]) -> SandboxExit:
        limit_s = self.spec.wall_clock_limit_s
        deadline = None if limit_s is None else time.monotonic() + limit_s
        reason = ""
        while True:
            for request in self._drain_requests():
                if request == "reload":
                    self.reload_rules()
                elif request == "stats":
                    self.write_stats()
                else:
                    reason = "stopped"
            if reason:
                self._stop_sandbox()
                break
            if deadline is not None and time.monotonic() >= deadline:
                reason = f"wall-clock limit of {limit_s:g} s exceeded"
                self._kill_sandbox()
                break
            try:
                process.wait(timeout=POLL_INTERVAL_S)
                break
            except subprocess.TimeoutExpired:
                continue
        return SandboxExit.from_returncode(process.wait(), reason=reason)  # already ended

    def _drain_requests(self) -> Iterator[_Request]:
        while True:
            try:
                yield self._requests.get_nowait()
            except queue.Empty:
                return

    def _stop_sandbox(self) -> None:
        """Ask the sandbox's init to terminate so the app can shut down; kill it after the grace."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        init_pid = self._find_init(process.pid)
        if init_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(init_pid, signal.SIGTERM)
            try:
                process.wait(timeout=self._stop_grace_s)
                return
            except subprocess.TimeoutExpired:
                pass
        self._kill_sandbox()

    def _kill_sandbox(self) -> None:
        """SIGKILL the sandbox's init (the kernel then ends the pid namespace) and ``unshare``."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        init_pid = self._find_init(process.pid)
        if init_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(init_pid, signal.SIGKILL)
        process.kill()
        process.wait()

    @staticmethod
    def _stop_pump(pump: FramePump) -> None:
        pump.stop()
        if not pump.wait(timeout_s=PUMP_STOP_WAIT_S):
            logger.warning("the frame pump did not stop within %g s", PUMP_STOP_WAIT_S)

    @staticmethod
    def _stop_slirp(slirp: subprocess.Popen[bytes]) -> None:
        if slirp.poll() is None:
            slirp.terminate()
            try:
                slirp.wait(timeout=SLIRP_STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                slirp.kill()
                slirp.wait()

    @staticmethod
    def _close_launch_socket(listener: socket.socket, path: Path) -> None:
        listener.close()
        path.unlink(missing_ok=True)

    def _announce(self, event: dict[str, Any]) -> None:
        self._announce_event(event)


# ---- the handle appmux holds ---------------------------------------------------------------------


class _Event(BaseModel):
    """One line the supervisor prints; ``status``/``signal``/``reason`` belong to ``exited``."""

    model_config = ConfigDict(extra="ignore")

    event: Literal["started", "failed", "exited"]
    sandbox_pid: int | None = None
    reason: str = ""
    status: int = 1
    signal: int | None = None


def _supervisor_environment() -> dict[str, str]:
    """The supervisor's environment: where to find programs and the appmux package, no secrets."""
    return {
        "PATH": os.environ.get("PATH", DEFAULT_PATH_FOR_HELPERS),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_ino, status.st_mtime_ns


class SandboxHandle:
    """A running supervisor, as appmux sees it: wait for the start, signal it, stop it."""

    def __init__(self, process: asyncio.subprocess.Process, spec: SandboxSpec) -> None:
        self._process = process
        self._spec = spec
        self.sandbox_pid: int | None = None
        self._started = False
        self._failure: str | None = None
        self._exit: SandboxExit | None = None
        self._returncode = 0
        self._settled = asyncio.Event()  # started, failed, or the supervisor ended
        self._finished = asyncio.Event()  # the supervisor ended and its output is read
        self._reader = asyncio.create_task(self._read_events())

    @property
    def pid(self) -> int:
        """The supervisor's pid."""
        return self._process.pid

    async def _read_events(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            raise RuntimeError("start_sandbox asked for a stdout pipe and got none")
        async for line in stdout:
            self._handle_line(line)
        self._returncode = await self._process.wait()
        self._settled.set()
        self._finished.set()

    def _handle_line(self, line: bytes) -> None:
        try:
            event = _Event.model_validate_json(line)
        except ValidationError:
            logger.warning("ignoring a line the sandbox supervisor printed that is not an event")
            return
        if event.event == "started":
            self._started = True
            self.sandbox_pid = event.sandbox_pid
            self._settled.set()
        elif event.event == "failed":
            self._failure = event.reason
            self._settled.set()
        else:
            self._exit = SandboxExit(event.status, event.signal, event.reason)

    async def wait_started(self, *, timeout_s: float) -> None:
        """Return once the child runs; raise :class:`SandboxStartError` if it did not start."""
        try:
            await asyncio.wait_for(self._settled.wait(), timeout_s)
        except TimeoutError:
            await self.stop(grace_s=1.0)
            raise SandboxStartError(
                f"the sandbox supervisor did not report within {timeout_s:g} s; it was stopped "
                f"(see {self._spec.scratch_dir / SUPERVISOR_LOG_NAME})"
            ) from None
        if self._failure is not None:
            await self._finished.wait()
            raise SandboxStartError(self._failure)
        if not self._started:
            raise SandboxStartError(
                f"the sandbox supervisor ended with status {self._returncode} before the "
                f"sandbox started (see {self._spec.scratch_dir / SUPERVISOR_LOG_NAME})"
            )

    async def wait(self) -> SandboxExit:
        """Wait for the sandbox to end and say how."""
        await self._finished.wait()
        if self._exit is not None:
            return self._exit
        return SandboxExit.from_returncode(self._returncode)

    def _signal_supervisor(self, number: int) -> bool:
        if self._process.returncode is not None:
            return False
        try:
            self._process.send_signal(number)
        except ProcessLookupError:
            return False
        return True

    def reload_rules(self) -> bool:
        """Tell the supervisor to re-read the rules file; ``False`` if it is no longer running."""
        return self._signal_supervisor(signal.SIGHUP)

    async def read_stats(self, *, timeout_s: float) -> PumpStats | None:
        """Ask the supervisor for its pump counters; ``None`` if none arrived in time."""
        path = stats_path_for(self._spec)
        before = _file_identity(path)
        if not self._signal_supervisor(signal.SIGUSR1):
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if _file_identity(path) not in (None, before):
                try:
                    return cast("PumpStats", json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass  # caught mid-write; the next poll reads it whole
            await asyncio.sleep(STATS_POLL_INTERVAL_S)
        return None

    async def stop(self, *, grace_s: float) -> SandboxExit:
        """Ask the supervisor to stop the sandbox; kill its process group after the grace."""
        if self._process.returncode is None:
            self._signal_supervisor(signal.SIGTERM)
            try:
                await asyncio.wait_for(self._finished.wait(), grace_s)
            except TimeoutError:
                # The supervisor leads its own session, so its group holds ``unshare`` as well;
                # the sandbox's init then dies with ``unshare`` (``--kill-child``).
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self._process.pid, signal.SIGKILL)
                await self._finished.wait()
        return await self.wait()


def default_supervisor_command() -> list[str]:
    return [sys.executable, "-m", LAUNCHER_MODULE, "supervise"]


async def start_sandbox(
    spec: SandboxSpec, *, supervisor_command: Sequence[str] | None = None
) -> SandboxHandle:
    """Start the supervisor for ``spec`` and return its handle; use ``wait_started`` next."""
    command = (
        list(supervisor_command) if supervisor_command is not None else default_supervisor_command()
    )
    spec.scratch_dir.mkdir(mode=SCRATCH_DIRECTORY_MODE, parents=True, exist_ok=True)
    log_fd = os.open(
        spec.scratch_dir / SUPERVISOR_LOG_NAME,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC,
        LOG_FILE_MODE,
    )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=log_fd,
            env=_supervisor_environment(),
            start_new_session=True,
        )
    finally:
        os.close(log_fd)
    stdin = process.stdin
    if stdin is None:
        raise RuntimeError("asked for a stdin pipe and got none")
    # The spec carries the child's secrets, so it goes in on stdin, never in the command line. A
    # supervisor that already exited cannot read it; wait_started reports how it ended.
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stdin.write(spec.to_json().encode("utf-8"))
        await stdin.drain()
    stdin.close()
    return SandboxHandle(process, spec)


# ---- command line --------------------------------------------------------------------------


def supervise_main() -> int:
    """``supervise``: read the spec from stdin, run the sandbox, print events on stdout."""

    def announce(event: dict[str, Any]) -> None:
        print(json.dumps(event), flush=True)

    try:
        spec = SandboxSpec.from_json(sys.stdin.read())
    except ValueError as exc:
        announce({"event": "failed", "reason": f"the sandbox spec is not valid: {exc}"})
        return 2
    supervisor = Supervisor(
        spec,
        launch_command=unshare_command([sys.executable, "-m", LAUNCHER_MODULE, "stage1"]),
        slirp_binary=slirp_binary_path(Path(__file__)),
        announce=announce,
    )
    restore = install_signal_handlers(
        {
            signal.SIGTERM: supervisor.request_stop,
            signal.SIGINT: supervisor.request_stop,
            signal.SIGHUP: supervisor.request_reload,
            signal.SIGUSR1: supervisor.request_stats,
        }
    )
    try:
        return supervisor.run()
    finally:
        restore()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"python -m {LAUNCHER_MODULE}")
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("supervise", help="run one sandbox; the SandboxSpec JSON is read from stdin")
    stage1 = modes.add_parser("stage1", help="internal: the first process inside the namespaces")
    stage1.add_argument("--spec-fd", type=int, required=True)
    stage1.add_argument("--channel-path", type=Path, required=True)
    stage1.add_argument("--relay-fd", type=int)
    arguments = parser.parse_args(argv)
    if arguments.mode == "supervise":
        return supervise_main()
    return stage1_main(arguments.spec_fd, arguments.channel_path, arguments.relay_fd)


if __name__ == "__main__":
    raise SystemExit(main())
