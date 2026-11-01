"""The canary of design 6.4: a short program that tries to do what a sandboxed child must not.

Two halves live in this file because they share one contract (the plan and the list of checks):

* **The canary** (``python /srv/app/canary.py <plan json>``) runs *inside* a real sandbox. It is
  standard library only and imports nothing of appmux, because the sandbox has no appmux package.
  It runs every check of ``self_check.CHECKS``, never stops at a failure, and prints ONE JSON
  object, ``{"checks": [{"id": ..., "passed": ..., "detail": ...}, ...]}``, which is what
  ``self_check.parse_canary_output`` reads. It fails closed: a check that raises, returns the
  wrong shape, or has nothing to try is a failed check, never a pass.
* **The host side** (``run_canary``) prepares what the canary needs to be tested against (a decoy
  listener, marker files, a SysV segment and queue, the ports the container listens on), starts the
  canary through the launcher in a real sandbox, and returns the JSON line it printed. It imports
  appmux lazily, inside the function, for the reason above.

A passing check proves a child cannot do something only if the thing was really tried and really
refused for the expected reason, so each check accepts one specific refusal (``EROFS`` for the
read-only mounts, ``EPERM`` for a missing capability), and a plan with nothing to try fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import json
import logging
import os
import resource
import secrets
import selectors
import shutil
import socket
import struct
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, fields
from ipaddress import IPv4Address
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, TextIO

if TYPE_CHECKING:
    from appmux.sandbox.launcher import SandboxSpec
    from appmux.sandbox.rules import EgressRule

logger = logging.getLogger(__name__)

Outcome = tuple[bool, str]
Check = Callable[["Plan"], Outcome]

# The ids and their order are those of ``self_check.CHECKS``; a test holds the two lists equal.
# This copy exists because the canary cannot import self_check.
CHECK_IDS: Final = (
    "environ_isolation",
    "pid_isolation",
    "loopback_and_container_ip",
    "metadata_address",
    "platform_files",
    "capabilities",
    "read_only_environment",
    "sibling_sockets",
    "appmux_directories",
    "sysv_ipc",
    "nested_user_namespace",
    "inherited_descriptors",
    "cpu_pin",
)

# What slirp4netns calls the host and its DNS; with ``--disable-host-loopback`` the first must not
# reach appmux's loopback.
SLIRP_GATEWAY: Final = "10.0.2.2"
METADATA_ADDRESS: Final = ("169.254.169.254", 80)
CHILD_NICE: Final = 19  # the same value as launcher.CHILD_NICE, which the canary cannot import
DESCRIPTOR_SCAN_CEILING: Final = 65536
PROBE_FILE_NAME: Final = ".appmux-canary-probe"
DIRECTORY_MARKER: Final = b"<directory>"
LINK_NAME: Final = b"canary0"
NETLINK_RECEIVE_TIMEOUT_S: Final = 5.0

# Files and directories of the platform that must be absent (or empty, for the four /proc files
# the launcher overmounts with /dev/null). The platform's runtime.cfg and customer log live at
# paths only the host knows, so they arrive in the plan.
PLATFORM_PATHS: Final = (
    "/databricks",
    "/dev/termination-log",
    "/sys/class/dmi/id",
    "/etc/hostname",
    "/mnt",
    "/proc/self/cgroup",
    "/proc/self/mountinfo",
    "/proc/self/mounts",
    "/proc/self/mountstats",
)

CLONE_NEWUSER: Final = 0x10000000
IPC_CREAT: Final = 0o1000
IPC_EXCL: Final = 0o2000
IPC_RMID: Final = 0
IPC_KEY_ATTEMPTS: Final = 5
IPC_OBJECT_MODE: Final = 0o600
IPC_SEGMENT_BYTES: Final = 4096

_NETLINK_ROUTE: Final = 0
_NLMSG_ERROR: Final = 2
_RTM_NEWLINK: Final = 16
_NLM_F_REQUEST_ACK_CREATE_EXCL: Final = 0x1 | 0x4 | 0x400 | 0x200
_IFLA_IFNAME: Final = 3
_IFLA_LINKINFO: Final = 18
_IFLA_INFO_KIND: Final = 1
_NETLINK_HEADER_BYTES: Final = 16

_PERMISSION_ERRNOS: Final = frozenset({errno.EPERM, errno.EACCES})
_ABSENT_OR_DENIED_ERRNOS: Final = frozenset({errno.ENOENT, errno.EACCES, errno.EPERM, errno.ESRCH})


def errno_name(code: int) -> str:
    return errno.errorcode.get(code, str(code))


# ---- the plan: what the host tells the canary ------------------------------------------------


class PlanError(ValueError):
    """The plan is not what the canary expects; the message names the field, never its value."""


@dataclass(frozen=True, slots=True)
class Plan:
    """What only the host knows: the decoys it planted and the paths that must be invisible."""

    container_ip: str
    container_ports: tuple[int, ...]
    loopback_decoy_port: int
    connect_timeout_s: float
    host_pids: tuple[int, ...]
    allowed_environment_names: frozenset[str]
    platform_paths: tuple[str, ...]
    sibling_paths: tuple[str, ...]
    appmux_paths: tuple[str, ...]
    write_probe_dirs: tuple[str, ...]
    ipc_key: int
    pinned: bool

    def to_json(self) -> str:
        document = {field.name: getattr(self, field.name) for field in fields(self)}
        document["allowed_environment_names"] = sorted(self.allowed_environment_names)
        return json.dumps(document, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Plan:
        try:
            document = json.loads(text)
        except ValueError:
            raise PlanError("the plan is not JSON") from None
        names = {field.name for field in fields(cls)}
        if not isinstance(document, dict) or set(document) != names:
            raise PlanError("the plan does not have exactly the expected fields")
        return cls(
            container_ip=_text(document, "container_ip"),
            container_ports=_integers(document, "container_ports", minimum=1, maximum=65535),
            loopback_decoy_port=_integer(document, "loopback_decoy_port", minimum=1, maximum=65535),
            connect_timeout_s=_positive_number(document, "connect_timeout_s"),
            host_pids=_integers(document, "host_pids", minimum=1),
            allowed_environment_names=frozenset(_texts(document, "allowed_environment_names")),
            platform_paths=_texts(document, "platform_paths"),
            sibling_paths=_texts(document, "sibling_paths"),
            appmux_paths=_texts(document, "appmux_paths"),
            write_probe_dirs=_texts(document, "write_probe_dirs"),
            ipc_key=_ipc_key(document),
            pinned=_boolean(document, "pinned"),
        )


def _text(document: dict[str, Any], name: str) -> str:
    value = document[name]
    if not isinstance(value, str):
        raise PlanError(f"plan field {name} must be a string")
    return value


def _texts(document: dict[str, Any], name: str) -> tuple[str, ...]:
    value = document[name]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PlanError(f"plan field {name} must be a list of strings")
    return tuple(value)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _integer(
    document: dict[str, Any], name: str, *, minimum: int, maximum: int | None = None
) -> int:
    value = document[name]
    if not _is_integer(value) or value < minimum or (maximum is not None and value > maximum):
        raise PlanError(f"plan field {name} must be an integer in range")
    return int(value)


def _integers(
    document: dict[str, Any], name: str, *, minimum: int, maximum: int | None = None
) -> tuple[int, ...]:
    value = document[name]
    if not isinstance(value, list):
        raise PlanError(f"plan field {name} must be a list of integers")
    return tuple(_integer({name: item}, name, minimum=minimum, maximum=maximum) for item in value)


def _positive_number(document: dict[str, Any], name: str) -> float:
    value = document[name]
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise PlanError(f"plan field {name} must be a positive number")
    return float(value)


def _boolean(document: dict[str, Any], name: str) -> bool:
    value = document[name]
    if not isinstance(value, bool):
        raise PlanError(f"plan field {name} must be true or false")
    return value


def _ipc_key(document: dict[str, Any]) -> int:
    # 0 is IPC_PRIVATE, which never names an existing object, so a lookup of it proves nothing.
    return _integer(document, "ipc_key", minimum=1, maximum=2**31 - 1)


# ---- OS actions the checks ask through (each is the seam a test replaces) ---------------------


def _descriptor_is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as exc:
        return exc.errno != errno.EBADF  # anything but "not open" is treated as open
    return True


def _descriptor_ceiling() -> int:
    soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    if soft_limit == resource.RLIM_INFINITY:
        return DESCRIPTOR_SCAN_CEILING
    return min(soft_limit, DESCRIPTOR_SCAN_CEILING)


def _own_environment_names() -> Iterable[str]:
    return os.environ.keys()


def _read_environ(pid: int) -> bytes:
    with open(f"/proc/{pid}/environ", "rb") as handle:
        return handle.read(1)


def _list_proc() -> list[str]:
    return os.listdir("/proc")


def reachable_targets(
    targets: Sequence[tuple[str, int]], timeout_s: float
) -> list[tuple[str, int]]:
    """The targets a TCP connection to completes within ``timeout_s``; all are tried at once."""
    reached: list[tuple[str, int]] = []
    with selectors.DefaultSelector() as selector:
        for host, port in targets:
            attempt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            attempt.setblocking(False)
            code = attempt.connect_ex((host, port))
            if code == errno.EINPROGRESS:
                selector.register(attempt, selectors.EVENT_WRITE, (attempt, (host, port)))
                continue
            if code == 0:
                reached.append((host, port))
            attempt.close()  # refused, or no route, at once
        deadline = time.monotonic() + timeout_s
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for key, _events in selector.select(remaining):
                attempt, target = key.data
                if attempt.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR) == 0:
                    reached.append(target)
                selector.unregister(attempt)
                attempt.close()
        for key in list(selector.get_map().values()):
            key.data[0].close()
    return reached


def visible_content(path: str) -> bytes | None:
    """``None`` if ``path`` does not exist, else its first byte (empty for an empty file)."""
    try:
        with open(path, "rb") as handle:
            return handle.read(1)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except IsADirectoryError:
        return DIRECTORY_MARKER


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _errno_of(result: int) -> int:
    return ctypes.get_errno() if result == -1 else 0


def _mount_errno() -> int:
    ctypes.set_errno(0)
    return _errno_of(_libc().mount(b"tmpfs", b"/tmp", b"tmpfs", 0, None))


def _packet_socket_errno() -> int:
    try:
        socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)).close()
    except OSError as exc:
        return exc.errno or -1
    return 0


def _attribute(kind: int, payload: bytes) -> bytes:
    return struct.pack("HH", 4 + len(payload), kind) + payload + bytes(-len(payload) % 4)


def _create_link_errno() -> int:
    """Ask the kernel to create a ``dummy`` link (``ip link add``); the errno it answers with."""
    kind = _attribute(_IFLA_INFO_KIND, b"dummy\0")
    body = struct.pack("BxHiII", socket.AF_UNSPEC, 0, 0, 0, 0)
    body += _attribute(_IFLA_IFNAME, LINK_NAME + b"\0") + _attribute(_IFLA_LINKINFO, kind)
    header = struct.pack(
        "IHHII",
        _NETLINK_HEADER_BYTES + len(body),
        _RTM_NEWLINK,
        _NLM_F_REQUEST_ACK_CREATE_EXCL,
        1,
        0,
    )
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_ROUTE) as netlink:
        netlink.settimeout(NETLINK_RECEIVE_TIMEOUT_S)
        netlink.bind((0, 0))
        netlink.send(header + body)
        reply = netlink.recv(4096)
    _length, message_type, _flags, _sequence, _pid = struct.unpack_from("IHHII", reply)
    if message_type != _NLMSG_ERROR:
        raise OSError(errno.EPROTO, "the kernel answered with something other than an ack")
    (error_number,) = struct.unpack_from("i", reply, _NETLINK_HEADER_BYTES)
    return -int(error_number)


def parse_capability_sets(status: str) -> tuple[int, int]:
    """``(CapEff, CapBnd)`` from the text of ``/proc/self/status``."""
    values: dict[str, int] = {}
    for line in status.splitlines():
        name, _, value = line.partition(":")
        if name in ("CapEff", "CapBnd"):
            values[name] = int(value.strip(), 16)
    for name in ("CapEff", "CapBnd"):
        if name not in values:
            raise ValueError(f"/proc/self/status has no {name} line")
    return values["CapEff"], values["CapBnd"]


def _capability_sets() -> tuple[int, int]:
    with open("/proc/self/status", encoding="ascii") as handle:
        return parse_capability_sets(handle.read())


def create_probe_file(directory: str) -> int:
    """Try to create a file in ``directory`` and remove it again; 0, or the errno that refused."""
    path = os.path.join(directory, PROBE_FILE_NAME)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return exc.errno or -1
    os.close(descriptor)
    os.unlink(path)
    return 0


def sysv_ipc_errnos(key: int) -> tuple[int, int]:
    """``(shmget, msgget)`` on an existing key: 0 if the object is found, else the errno."""
    libc = _libc()
    ctypes.set_errno(0)
    shared_memory = _errno_of(libc.shmget(key, 0, 0))
    ctypes.set_errno(0)
    message_queue = _errno_of(libc.msgget(key, 0))
    return shared_memory, message_queue


def _unshare_user_errno() -> int:
    """``unshare(CLONE_NEWUSER)`` in a forked process (a success must not change this one)."""
    pid = os.fork()
    if pid == 0:
        try:
            ctypes.set_errno(0)
            succeeded = _libc().unshare(CLONE_NEWUSER) == 0
            os._exit(0 if succeeded else min(ctypes.get_errno(), 255))
        finally:
            os._exit(255)  # only reached if the lines above raised
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


def _widen_affinity_errno() -> int:
    try:
        os.sched_setaffinity(0, range(os.cpu_count() or 1))
    except OSError as exc:
        return exc.errno or -1
    return 0


def _get_affinity() -> set[int]:
    return os.sched_getaffinity(0)


def _get_priority() -> int:
    return os.getpriority(os.PRIO_PROCESS, 0)


# ---- the checks ----------------------------------------------------------------------------------


def check_inherited_descriptors(
    plan: Plan,
    *,
    is_open: Callable[[int], bool] = _descriptor_is_open,
    limit: int | None = None,
) -> Outcome:
    ceiling = limit if limit is not None else _descriptor_ceiling()
    leaked = [descriptor for descriptor in range(3, ceiling) if is_open(descriptor)]
    if leaked:
        return False, f"descriptors open beyond 2: {leaked[:8]}"
    return True, ""


def check_environ_isolation(
    plan: Plan,
    *,
    environment_names: Callable[[], Iterable[str]] = _own_environment_names,
    read_environ: Callable[[int], bytes] = _read_environ,
) -> Outcome:
    problems: list[str] = []
    extra = sorted(set(environment_names()) - plan.allowed_environment_names)
    if extra:
        problems.append("variables the child was not given: " + ", ".join(extra))
    for pid in sorted({1, *plan.host_pids}):
        try:
            read_environ(pid)
        except OSError as exc:
            if exc.errno not in _ABSENT_OR_DENIED_ERRNOS:
                problems.append(
                    f"/proc/{pid}/environ failed unexpectedly ({errno_name(exc.errno or -1)})"
                )
        else:
            problems.append(f"/proc/{pid}/environ is readable")
    return (False, "; ".join(problems)) if problems else (True, "")


def check_pid_isolation(
    plan: Plan,
    *,
    list_proc: Callable[[], Iterable[str]] = _list_proc,
    own_pid: int | None = None,
) -> Outcome:
    own = own_pid if own_pid is not None else os.getpid()
    others = sorted(int(name) for name in list_proc() if name.isdigit() and int(name) != own)
    if others:
        return False, f"other pids are visible: {others[:8]}"
    return True, ""


def check_loopback_and_container_ip(
    plan: Plan,
    *,
    reachable: Callable[[list[tuple[str, int]], float], list[tuple[str, int]]] = reachable_targets,
) -> Outcome:
    if not plan.container_ports:
        # appmux itself listens on the container address, so an empty list means the host could
        # not read it; passing would prove nothing.
        return (
            False,
            "the plan lists no listening port of the container address, so nothing was tried",
        )
    targets = [
        ("127.0.0.1", plan.loopback_decoy_port),
        (SLIRP_GATEWAY, plan.loopback_decoy_port),
        *((plan.container_ip, port) for port in plan.container_ports),
    ]
    reached = reachable(targets, plan.connect_timeout_s)
    if reached:
        return False, "connected to " + ", ".join(f"{host}:{port}" for host, port in reached)
    return True, f"{len(targets)} targets tried, none reachable"


def check_metadata_address(
    plan: Plan,
    *,
    reachable: Callable[[list[tuple[str, int]], float], list[tuple[str, int]]] = reachable_targets,
) -> Outcome:
    if reachable([METADATA_ADDRESS], plan.connect_timeout_s):
        return False, "connected to the metadata address"
    return True, ""


def check_platform_files(
    plan: Plan, *, content_of: Callable[[str], bytes | None] = visible_content
) -> Outcome:
    paths = (*PLATFORM_PATHS, *plan.platform_paths)
    visible = [path for path in paths if content_of(path)]
    if visible:
        return False, "visible: " + ", ".join(visible)
    return True, f"{len(paths)} paths absent or empty"


def check_capabilities(
    plan: Plan,
    *,
    try_mount: Callable[[], int] = _mount_errno,
    try_packet_socket: Callable[[], int] = _packet_socket_errno,
    try_create_link: Callable[[], int] = _create_link_errno,
    capability_sets: Callable[[], tuple[int, int]] = _capability_sets,
) -> Outcome:
    problems = [
        f"{name} {'succeeded' if code == 0 else 'failed with ' + errno_name(code)}"
        for name, code in (
            ("mount", try_mount()),
            ("AF_PACKET socket", try_packet_socket()),
            ("link creation", try_create_link()),
        )
        if code not in _PERMISSION_ERRNOS  # any other refusal may not be about a capability
    ]
    effective, bounding = capability_sets()
    if effective or bounding:
        problems.append(
            f"capabilities are still held (effective {effective:#x}, bounding {bounding:#x})"
        )
    return (False, "; ".join(problems)) if problems else (True, "")


def check_read_only_environment(
    plan: Plan, *, create_probe_file: Callable[[str], int] = create_probe_file
) -> Outcome:
    if not plan.write_probe_dirs:
        return False, "the plan lists no directory to write to, so nothing was tried"
    problems = []
    for directory in plan.write_probe_dirs:
        code = create_probe_file(directory)
        if code != errno.EROFS:  # permissions alone are not the layer the design relies on
            problems.append(
                f"{directory}: " + ("a file could be created" if code == 0 else errno_name(code))
            )
    return (False, "; ".join(problems)) if problems else (True, "")


def _is_absent(path: str) -> bool:
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False  # a permission error or a loop does not say the path is absent
    return False


def _check_paths_absent(paths: tuple[str, ...], kind: str) -> Outcome:
    if not paths:
        return False, f"the plan lists no {kind}, so nothing was tried"
    visible = [path for path in paths if not _is_absent(path)]
    if visible:
        return False, "reachable: " + ", ".join(visible)
    return True, f"{len(paths)} paths absent"


def check_sibling_sockets(plan: Plan) -> Outcome:
    return _check_paths_absent(plan.sibling_paths, "path of a sibling")


def check_appmux_directories(plan: Plan) -> Outcome:
    return _check_paths_absent(plan.appmux_paths, "path of appmux")


def check_sysv_ipc(
    plan: Plan, *, ipc_errnos: Callable[[int], tuple[int, int]] = sysv_ipc_errnos
) -> Outcome:
    shared_memory, message_queue = ipc_errnos(plan.ipc_key)
    problems = []
    if shared_memory != errno.ENOENT:  # even EACCES shows that the segment exists
        problems.append(f"the shared memory segment answered {errno_name(shared_memory)}")
    if message_queue != errno.ENOENT:
        problems.append(f"the message queue answered {errno_name(message_queue)}")
    return (False, "; ".join(problems)) if problems else (True, "")


def check_nested_user_namespace(
    plan: Plan, *, unshare_user: Callable[[], int] = _unshare_user_errno
) -> Outcome:
    code = unshare_user()
    if code == errno.EPERM:  # what a process in a chroot gets
        return True, ""
    return False, "unshare(CLONE_NEWUSER) " + (
        "succeeded" if code == 0 else f"failed with {errno_name(code)}"
    )


def check_cpu_pin(
    plan: Plan,
    *,
    get_affinity: Callable[[], set[int]] = _get_affinity,
    get_priority: Callable[[], int] = _get_priority,
    widen_affinity: Callable[[], int] = _widen_affinity_errno,
) -> Outcome:
    if not plan.pinned:
        return True, "child_cpu_pin is off, so there is no pin to prove"
    problems = []
    cpus = get_affinity()
    if len(cpus) != 1:
        problems.append(f"{len(cpus)} CPUs are allowed, expected 1")
    nice = get_priority()
    if nice != CHILD_NICE:
        problems.append(f"nice is {nice}, expected {CHILD_NICE}")
    code = widen_affinity()
    if code != errno.EPERM:  # the seccomp filter answers EPERM
        problems.append(
            "widening the affinity "
            + ("worked" if code == 0 else f"failed with {errno_name(code)}")
        )
    return (False, "; ".join(problems)) if problems else (True, "")


# In the order they run: the descriptor check first, before anything else opens a file.
CHECKS: Final[tuple[tuple[str, Check], ...]] = (
    ("inherited_descriptors", check_inherited_descriptors),
    ("environ_isolation", check_environ_isolation),
    ("pid_isolation", check_pid_isolation),
    ("loopback_and_container_ip", check_loopback_and_container_ip),
    ("metadata_address", check_metadata_address),
    ("platform_files", check_platform_files),
    ("capabilities", check_capabilities),
    ("read_only_environment", check_read_only_environment),
    ("sibling_sockets", check_sibling_sockets),
    ("appmux_directories", check_appmux_directories),
    ("sysv_ipc", check_sysv_ipc),
    ("nested_user_namespace", check_nested_user_namespace),
    ("cpu_pin", check_cpu_pin),
)


def run_checks(plan: Plan, checks: Sequence[tuple[str, Check]] = CHECKS) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name, check in checks:
        try:
            passed, detail = check(plan)
            if not isinstance(passed, bool) or not isinstance(detail, str):
                raise TypeError("a check returns (bool, str)")
        except Exception as exc:
            # Broad on purpose: whatever goes wrong inside one check is that check's failure, and
            # the others must still run and report. The message is not repeated, only the class
            # (and errno), because it can quote a path.
            passed = False
            code = f" ({errno_name(exc.errno)})" if isinstance(exc, OSError) and exc.errno else ""
            detail = f"the check raised {type(exc).__name__}{code}"
        results.append({"id": name, "passed": passed, "detail": detail})
    return results


def render_output(results: list[dict[str, Any]]) -> str:
    def position(result: dict[str, Any]) -> int:
        name = result["id"]
        return CHECK_IDS.index(name) if name in CHECK_IDS else len(CHECK_IDS)

    return json.dumps({"checks": sorted(results, key=position)}, separators=(",", ":"))


def main(
    argv: Sequence[str],
    *,
    out: TextIO | None = None,
    checks: Sequence[tuple[str, Check]] = CHECKS,
) -> int:
    """Run the checks of the plan in ``argv[1]``; print one JSON line; ``0`` if all passed."""
    stream = out if out is not None else sys.stdout
    try:
        plan = Plan.from_json(argv[1])
    except (IndexError, PlanError):
        results = [
            {"id": name, "passed": False, "detail": "the plan is missing or invalid"}
            for name in CHECK_IDS
        ]
    else:
        results = run_checks(plan, checks)
    stream.write(render_output(results) + "\n")
    stream.flush()
    return 0 if all(result["passed"] for result in results) else 1


# ---- the host side: prepare, run in a real sandbox, collect ------------------------------------

START_TIMEOUT_S: Final = 30.0
CONNECT_TIMEOUT_S: Final = 3.0  # per target; all targets of a check are tried at once
EXIT_MARGIN_S: Final = 1.0  # past the sandbox's own wall-clock limit, for the exit to be reported
STOP_GRACE_S: Final = 2.0
LOG_TAIL_BYTES: Final = 1024 * 1024
LOGGED_TAIL_CHARS: Final = 2000
RESULT_LINE_PREFIX: Final = '{"checks"'
CANARY_SLUG: Final = "canary"
CANARY_ENVIRONMENT: Final = {
    "HOME": "/home/app",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",  # /srv/app is read-only
}


# The host directories a sandbox must not see into: appmux plants a marker in each, and the canary
# looks for it. They are the subject of the check, so S108 (a hardcoded temp directory) is moot.
MARKER_DIRECTORIES: Final = (
    Path("/tmp"),  # noqa: S108 - the host's directory is what the canary checks is unreachable
    Path("/var/tmp"),  # noqa: S108 - same
    Path("/dev/shm"),  # noqa: S108 - same
    Path("/home/app"),
)


@dataclass(frozen=True, slots=True)
class CanaryConfig:
    """What the host needs to run the canary: its scratch space and the facts of this container."""

    work_dir: Path  # created, used and removed by run_canary
    app_environment: Path
    container_ip: IPv4Address
    appmux_port: int
    # The platform's default rules: self_check runs with them.
    global_rules: tuple[EgressRule, ...] = ()
    pin_cpu: bool = True  # the platform setting child_cpu_pin
    timeout_s: float = 90.0
    marker_directories: tuple[Path, ...] = MARKER_DIRECTORIES
    proc_net_tcp_paths: tuple[Path, ...] = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
    platform_paths: tuple[str, ...] = ()  # runtime.cfg, the customer log: paths only the host knows


class _Handle(Protocol):
    async def wait_started(self, *, timeout_s: float) -> None: ...
    async def wait(self) -> object: ...
    async def stop(self, *, grace_s: float) -> object: ...


StartSandbox = Callable[["SandboxSpec"], Awaitable[_Handle]]


def container_listening_ports(tables: Iterable[str], container_ip: IPv4Address) -> tuple[int, ...]:
    """The TCP ports that accept a connection on ``container_ip``, from ``/proc/net/tcp[6]``."""
    ip_hex = socket.inet_aton(str(container_ip))[::-1].hex().upper()
    addresses = {ip_hex, "00000000", "0" * 32, "0000000000000000FFFF0000" + ip_hex}
    ports: set[int] = set()
    for table in tables:
        for line in table.splitlines()[1:]:
            fields_ = line.split()
            if len(fields_) < 4 or fields_[3] != "0A":  # 0A is LISTEN
                continue
            address, _, port_hex = fields_[1].partition(":")
            if address in addresses:
                ports.add(int(port_hex, 16))
    return tuple(sorted(ports))


def plant_sysv_ipc() -> tuple[int, Callable[[], None]]:
    """Create a shared memory segment and a message queue under one new key: ``(key, remove)``."""
    libc = _libc()
    for _attempt in range(IPC_KEY_ATTEMPTS):
        key = secrets.randbelow(2**31 - 1) + 1
        flags = IPC_CREAT | IPC_EXCL | IPC_OBJECT_MODE
        segment = libc.shmget(key, IPC_SEGMENT_BYTES, flags)
        if segment == -1:
            continue
        queue_id = libc.msgget(key, flags)
        if queue_id == -1:
            libc.shmctl(segment, IPC_RMID, None)
            continue

        def remove(segment: int = segment, queue_id: int = queue_id) -> None:
            libc.shmctl(segment, IPC_RMID, None)
            libc.msgctl(queue_id, IPC_RMID, None)

        return key, remove
    raise OSError(errno.EAGAIN, "cannot create a SysV shared memory segment and message queue")


def _read_if_present(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii")
    except FileNotFoundError:
        return ""  # tcp6 is absent on a host without IPv6


def _plant_markers(
    directories: Iterable[Path], nonce: str, cleanup: contextlib.ExitStack
) -> list[Path]:
    planted = []
    for directory in directories:
        marker = directory / f"appmux-canary-{nonce}"
        try:
            marker.write_text("planted by appmux; a sandbox must not see this\n", encoding="utf-8")
        except OSError:
            continue  # best effort: this host has no such directory, or it is not writable
        cleanup.callback(marker.unlink, missing_ok=True)
        planted.append(marker)
    return planted


def _extract_result(log_path: Path) -> str:
    try:
        tail = log_path.read_bytes()[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace")
    except OSError as exc:
        from appmux.sandbox.self_check import CanaryUnavailable

        raise CanaryUnavailable("the log of the canary cannot be read") from exc
    for line in reversed(tail.splitlines()):
        if line.startswith(RESULT_LINE_PREFIX):
            return line
    from appmux.sandbox.self_check import CanaryUnavailable

    # The server log gets the end of the canary's output; the exception, which reaches the
    # platform page, gets none of it.
    logger.warning(
        "the canary printed no result; the end of its log: %s", tail[-LOGGED_TAIL_CHARS:]
    )
    raise CanaryUnavailable("the canary printed no result")


def _prepare(config: CanaryConfig, cleanup: contextlib.ExitStack) -> SandboxSpec:
    """Make everything the canary is tested against; each thing made is undone by ``cleanup``."""
    # Lazy: the part of this file that runs inside the sandbox must import nothing of appmux.
    from appmux.sandbox.launcher import SandboxSpec, choose_pinned_cpu
    from appmux.sandbox.pump import write_rules_file
    from appmux.sandbox.rootfs import RELEASE_PATH, overlay_path
    from appmux.sandbox.self_check import CanaryUnavailable

    cleanup.callback(shutil.rmtree, config.work_dir, True)  # ignore_errors: only a temp dir
    release_dir = config.work_dir / "release"
    overlay_dir = config.work_dir / "overlay"
    run_dir = config.work_dir / "run"
    for directory in (release_dir, overlay_dir, run_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__), release_dir / "canary.py")
    write_rules_file(
        run_dir / "rules.json",
        app_rules=[],
        global_rules=list(config.global_rules),
        container_ip=config.container_ip,
        appmux_port=config.appmux_port,
    )

    decoy = socket.socket()
    cleanup.callback(decoy.close)
    decoy.bind(("127.0.0.1", 0))
    decoy.listen(8)
    try:
        ipc_key, remove_ipc = plant_sysv_ipc()
    except OSError as exc:
        raise CanaryUnavailable("cannot plant the SysV IPC objects of the canary") from exc
    cleanup.callback(remove_ipc)
    markers = _plant_markers(config.marker_directories, secrets.token_hex(8), cleanup)
    site_packages = next(
        iter(sorted(config.app_environment.glob("lib/python*/site-packages"))),
        config.app_environment,
    )

    plan = Plan(
        container_ip=str(config.container_ip),
        container_ports=container_listening_ports(
            [_read_if_present(path) for path in config.proc_net_tcp_paths], config.container_ip
        ),
        loopback_decoy_port=decoy.getsockname()[1],
        connect_timeout_s=CONNECT_TIMEOUT_S,
        host_pids=(1, os.getpid()),
        allowed_environment_names=frozenset({*CANARY_ENVIRONMENT, "PATH", "PYTHONPATH"}),
        platform_paths=config.platform_paths,
        # Stand-ins for a sibling's socket, run directory and release: this canary's own, whose
        # host paths the sandbox must not see. No second sandbox is started.
        sibling_paths=(str(run_dir / "launch.sock"), str(run_dir / "rules.json"), str(release_dir)),
        appmux_paths=(str(Path(__file__).resolve()), *map(str, markers)),
        write_probe_dirs=(str(site_packages), overlay_path(0)),
        ipc_key=ipc_key,
        pinned=config.pin_cpu,
    )
    return SandboxSpec(
        slug=CANARY_SLUG,
        command=("python", f"{RELEASE_PATH}/canary.py", plan.to_json()),
        environment=dict(CANARY_ENVIRONMENT),
        app_environment=config.app_environment,
        release_dir=release_dir,
        overlays=(overlay_dir,),
        scratch_dir=run_dir,
        log_path=run_dir / "canary.log",
        rules_path=run_dir / "rules.json",
        pinned_cpu=choose_pinned_cpu(os.sched_getaffinity(0)) if config.pin_cpu else None,
        wall_clock_limit_s=config.timeout_s,
    )


async def run_canary(config: CanaryConfig, *, start: StartSandbox | None = None) -> str:
    """Run the canary in a real sandbox and return the JSON text it printed.

    Raises ``CanaryUnavailable`` if the sandbox cannot start, the canary does not finish in
    ``config.timeout_s`` or prints no result; ``self_check`` turns that into a failed result.
    Everything this prepared (listener, markers, SysV objects, the work directory) is removed.
    """
    from appmux.sandbox.launcher import SandboxStartError, start_sandbox
    from appmux.sandbox.self_check import CanaryUnavailable

    with contextlib.ExitStack() as cleanup:
        spec = _prepare(config, cleanup)
        handle: _Handle | None = None
        try:
            try:
                handle = await (start or start_sandbox)(spec)
                await handle.wait_started(timeout_s=START_TIMEOUT_S)
            except (SandboxStartError, OSError) as exc:
                raise CanaryUnavailable(f"the canary sandbox did not start: {exc}") from exc
            try:
                await asyncio.wait_for(handle.wait(), config.timeout_s + EXIT_MARGIN_S)
            except TimeoutError as exc:
                raise CanaryUnavailable(
                    f"the canary did not finish within {config.timeout_s:g} seconds"
                ) from exc
        finally:
            if handle is not None:
                await handle.stop(grace_s=STOP_GRACE_S)
        return _extract_result(spec.log_path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
