"""The launcher of design 6.1: stage 1, the per-child supervisor and the handle appmux holds.

Everything the kernel does in a real sandbox (the namespaces, the mounts, the capability drop,
``exec`` into the child) is for the container run. What is checked here is what the launcher
decides and the order it does it in:

* the stage-1 sequence runs against a recording stand-in for its OS actions, so the order, the
  layers that can be left out and the fail-closed behaviour are values;
* the capability calls run against a model of the kernel's bounding set;
* the supervisor runs for real (real processes, real sockets, a real fake ``slirp4netns``), with a
  small program standing in for ``unshare`` + stage 1, so the handoff, the pump start, the exit
  reporting, the wall-clock limit and the stop are exercised without a namespace;
* the handle runs against a stand-in supervisor program.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import resource
import signal
import socket
import struct
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Mapping
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest

from appmux.sandbox.launcher import (
    CHILD_NICE,
    CHILD_WORKING_DIRECTORY,
    CapabilityDropError,
    Layer,
    LinuxStage1Ops,
    SandboxExit,
    SandboxSpec,
    SandboxStartError,
    Stage1Error,
    Supervisor,
    apply_cpu_limits,
    apply_resource_limits,
    child_environment,
    child_path,
    choose_pinned_cpu,
    close_all_descriptors_except,
    drop_capabilities,
    find_init_pid,
    forbid_new_privileges,
    install_signal_handlers,
    root_layout_for,
    run_stage1,
    start_sandbox,
    stats_path_for,
)
from appmux.sandbox.pump import write_rules_file
from appmux.sandbox.rootfs import TMP_PATH, overlay_path, plan_rootfs
from appmux.sandbox.rules import EgressRule
from appmux.sandbox.watchdog import address_space_limit_bytes

pytestmark = pytest.mark.anyio

HOST_APPMUX = Path(TMP_PATH) / "appmux"
APP_ENVIRONMENT = Path("/app/python/source_code/.venv")
OVERLAY = HOST_APPMUX / "overlays/aaaa"
WAIT_S = 20
PR_CAPBSET_READ, PR_CAPBSET_DROP = 23, 24
PR_SET_NO_NEW_PRIVS, PR_GET_NO_NEW_PRIVS = 38, 39
PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL = 47, 4


def make_spec(scratch: Path | None = None, **overrides: Any) -> SandboxSpec:
    directory = scratch if scratch is not None else HOST_APPMUX / "run/shop"
    fields: dict[str, Any] = {
        "slug": "shop",
        "command": ("python", "-m", "uvicorn", "app:app"),
        "environment": {"DATABRICKS_APP_PORT": "8000", "SECRET_TOKEN": "s3cret-value"},
        "app_environment": APP_ENVIRONMENT,
        "release_dir": HOST_APPMUX / "releases/shop-1",
        "scratch_dir": directory,
        "log_path": directory / "child.log",
        "rules_path": directory / "rules.json",
    }
    fields.update(overrides)
    return SandboxSpec(**fields)


def poll_until(condition: Callable[[], object], *, what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


# ---- the environment the launcher builds --------------------------------------------------------


def test_the_launcher_sets_path_and_puts_the_overlays_first_on_pythonpath() -> None:
    spec = make_spec(
        overlays=(OVERLAY,),
        environment={"PATH": "/evil/bin", "PYTHONPATH": "/srv/app/lib", "KEEP": "1"},
    )

    environment = child_environment(spec)

    assert environment["PATH"] == child_path(spec)
    # The overlay wins (design 5.4), and the app's own entries are kept behind it.
    assert environment["PYTHONPATH"] == f"{overlay_path(0)}:/srv/app/lib"
    assert environment["KEEP"] == "1"


def test_an_apps_own_pythonpath_survives_when_there_is_no_overlay() -> None:
    with_own = child_environment(make_spec(environment={"PYTHONPATH": "/srv/app/lib"}))
    without = child_environment(make_spec(environment={}))

    assert with_own["PYTHONPATH"] == "/srv/app/lib"
    assert "PYTHONPATH" not in without


def test_nothing_of_the_launchers_own_environment_reaches_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPMUX_PLATFORM_SECRET", "must-not-reach-the-child")

    environment = child_environment(make_spec())

    assert set(environment) == {"DATABRICKS_APP_PORT", "SECRET_TOKEN", "PATH"}


def test_a_relay_socket_needs_the_port_it_forwards_to() -> None:
    with pytest.raises(ValueError, match="child_port"):
        make_spec(relay_socket_path=HOST_APPMUX / "run/shop/relay.sock")


def test_a_rejected_spec_does_not_print_the_environment_values() -> None:
    with pytest.raises(ValueError, match="child_port") as raised:
        make_spec(relay_socket_path=HOST_APPMUX / "run/shop/relay.sock")

    assert "s3cret-value" not in str(raised.value)


# ---- capabilities -------------------------------------------------------------------------------


class FakeKernel:
    """The parts of the kernel that the capability calls read and change."""

    last_capability = 40

    def __init__(
        self,
        *,
        ignore_drops: bool = False,
        refuse_drops: bool = False,
        ignore_no_new_privs: bool = False,
    ) -> None:
        self.bounding = set(range(self.last_capability + 1))
        self.ignore_drops = ignore_drops
        self.refuse_drops = refuse_drops
        self.ignore_no_new_privs = ignore_no_new_privs
        self.ambient_cleared = False
        self.no_new_privs = False
        self.capset_calls: list[tuple[bytes, bytes]] = []

    def prctl(self, option: int, arg2: int = 0, arg3: int = 0, arg4: int = 0, arg5: int = 0) -> int:
        if option in (PR_CAPBSET_DROP, PR_CAPBSET_READ) and arg2 > self.last_capability:
            raise OSError(errno.EINVAL, "Invalid argument")
        if option == PR_CAPBSET_DROP:
            if self.refuse_drops:
                raise OSError(errno.EPERM, "Operation not permitted")
            if not self.ignore_drops:
                self.bounding.discard(arg2)
            return 0
        if option == PR_CAPBSET_READ:
            return int(arg2 in self.bounding)
        if option == PR_CAP_AMBIENT and arg2 == PR_CAP_AMBIENT_CLEAR_ALL:
            self.ambient_cleared = True
            return 0
        if option == PR_SET_NO_NEW_PRIVS:
            self.no_new_privs = bool(arg2) and not self.ignore_no_new_privs
            return 0
        if option == PR_GET_NO_NEW_PRIVS:
            return int(self.no_new_privs)
        raise AssertionError(f"unexpected prctl({option}, {arg2})")

    def capset(self, header: bytes, data: bytes) -> None:
        self.capset_calls.append((header, data))


def test_dropping_capabilities_empties_the_bounding_set_the_ambient_set_and_capset() -> None:
    kernel = FakeKernel()

    drop_capabilities(prctl=kernel.prctl, capset=kernel.capset)

    assert kernel.bounding == set()
    assert kernel.ambient_cleared is True
    # Version 3 header for this process, and two all-zero data structures.
    assert kernel.capset_calls == [(struct.pack("Ii", 0x20080522, 0), b"\0" * 24)]


def test_a_refused_drop_is_an_error_and_not_a_shrug() -> None:
    kernel = FakeKernel(refuse_drops=True)

    with pytest.raises(CapabilityDropError, match="bounding"):
        drop_capabilities(prctl=kernel.prctl, capset=kernel.capset)

    assert kernel.capset_calls == []  # nothing after the failed drop was attempted


def test_a_drop_that_did_not_take_effect_is_caught_by_reading_the_set_back() -> None:
    kernel = FakeKernel(ignore_drops=True)

    with pytest.raises(CapabilityDropError, match="still holds"):
        drop_capabilities(prctl=kernel.prctl, capset=kernel.capset)


def test_no_new_privs_is_set_and_read_back() -> None:
    kernel = FakeKernel()

    forbid_new_privileges(prctl=kernel.prctl)

    assert kernel.no_new_privs is True


def test_a_refused_or_ineffective_no_new_privs_is_an_error() -> None:
    def refuse(option: int, *_: int) -> int:
        raise OSError(errno.EPERM, "Operation not permitted")

    with pytest.raises(CapabilityDropError, match="no_new_privs"):
        forbid_new_privileges(prctl=refuse)
    with pytest.raises(CapabilityDropError, match="no_new_privs"):
        forbid_new_privileges(prctl=FakeKernel(ignore_no_new_privs=True).prctl)


# ---- descriptors and exec -----------------------------------------------------------------------


def test_every_descriptor_above_two_is_closed_except_the_ones_kept() -> None:
    report_read, report_write = os.pipe()
    spare_read, spare_write = os.pipe()  # the child must close these
    pid = os.fork()
    if pid == 0:  # the forked child: report what survives, then leave without cleanup
        try:
            close_all_descriptors_except([report_write])
            survivors = []
            for descriptor in range(3, 256):
                try:
                    os.fstat(descriptor)
                except OSError:
                    continue
                survivors.append(descriptor)
            os.write(report_write, json.dumps(survivors).encode())
        finally:
            os._exit(0)
    os.close(report_write)
    survivors = json.loads(os.read(report_read, 4096))
    os.waitpid(pid, 0)
    for descriptor in (report_read, spare_read, spare_write):
        os.close(descriptor)

    assert survivors == [report_write]


def _run_exec_child(
    ops: LinuxStage1Ops, command: tuple[str, ...], directory: Path, output: Path
) -> None:
    """Fork, send stdout to ``output`` and become ``command``; no shell sits in between.

    A shell would add variables of its own (``PWD``), which hides what ``exec_child`` passed.
    """
    pid = os.fork()
    if pid == 0:  # the forked child: become the command
        try:
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT, 0o600)
            os.dup2(descriptor, 1)
            ops.exec_child(command, {"PATH": "/usr/bin:/bin", "ONLY": "this"}, str(directory))
        finally:
            os._exit(127)  # exec failed
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_the_command_is_run_from_the_given_directory(tmp_path: Path) -> None:
    ops = LinuxStage1Ops(make_spec(tmp_path), channel=socket.socket(), relay_listener=None)
    output = tmp_path / "cwd.txt"

    _run_exec_child(ops, ("pwd",), tmp_path, output)

    assert output.read_text().strip() == str(tmp_path.resolve())


def test_the_command_gets_exactly_the_given_environment(tmp_path: Path) -> None:
    ops = LinuxStage1Ops(make_spec(tmp_path), channel=socket.socket(), relay_listener=None)
    output = tmp_path / "env.txt"

    _run_exec_child(ops, ("env",), tmp_path, output)

    assert sorted(output.read_text().split()) == ["ONLY=this", "PATH=/usr/bin:/bin"]


# ---- limits -------------------------------------------------------------------------------------


def test_the_process_and_file_limits_set_soft_and_hard_together() -> None:
    calls: list[tuple[int, tuple[int, int]]] = []

    apply_resource_limits(
        make_spec(max_processes=64, file_size_limit_bytes=1_000_000),
        setrlimit=lambda which, limits: calls.append((which, limits)),
    )

    assert (resource.RLIMIT_NPROC, (64, 64)) in calls
    assert (resource.RLIMIT_FSIZE, (1_000_000, 1_000_000)) in calls
    assert resource.RLIMIT_AS not in {which for which, _ in calls}  # no budget, no address limit


def test_the_address_space_limit_comes_from_the_memory_budget() -> None:
    calls: list[tuple[int, tuple[int, int]]] = []

    apply_resource_limits(
        make_spec(memory_budget_mb=256),
        setrlimit=lambda which, limits: calls.append((which, limits)),
    )

    limit = address_space_limit_bytes(256)
    assert (resource.RLIMIT_AS, (limit, limit)) in calls


def test_the_pin_is_one_cpu_at_the_lowest_priority() -> None:
    affinity: list[set[int]] = []
    priorities: list[int] = []

    apply_cpu_limits(
        2, set_affinity=lambda cpus: affinity.append(set(cpus)), set_priority=priorities.append
    )

    assert affinity == [{2}]
    assert priorities == [CHILD_NICE] == [19]


def test_the_pinned_cpu_is_the_highest_one_the_launcher_may_use() -> None:
    assert choose_pinned_cpu({0, 1, 2, 3}) == 3
    assert choose_pinned_cpu({1}) == 1
    with pytest.raises(ValueError, match="no CPU"):
        choose_pinned_cpu(set())


# ---- the stage-1 sequence ----------------------------------------------------------------------


class RecordingOps:
    """Stands in for the OS actions of stage 1 and records the order they were asked for."""

    def __init__(self, fail_at: str | None = None) -> None:
        self.events: list[str] = []
        self.arguments: dict[str, Any] = {}
        self.fail_at = fail_at

    def _do(self, name: str, **arguments: Any) -> None:
        self.events.append(name)
        self.arguments[name] = arguments
        if name == self.fail_at:
            raise OSError(errno.EPERM, f"{name} refused")

    def configure_network(self) -> None:
        self._do("configure_network")

    def hand_over_packet_socket(self) -> None:
        self._do("hand_over_packet_socket")

    def build_root(self, plan: Any) -> None:
        self._do("build_root", plan=plan)

    def start_relay(self, child_port: int) -> None:
        self._do("start_relay", child_port=child_port)

    def limit_resources(self, spec: SandboxSpec) -> None:
        self._do("limit_resources")

    def limit_cpu(self, pinned_cpu: int) -> None:
        self._do("limit_cpu", pinned_cpu=pinned_cpu)

    def drop_capabilities(self) -> None:
        self._do("drop_capabilities")

    def forbid_new_privileges(self) -> None:
        self._do("forbid_new_privileges")

    def install_seccomp(self) -> None:
        self._do("install_seccomp")

    def close_descriptors(self) -> None:
        self._do("close_descriptors")

    def exec_child(
        self, command: tuple[str, ...], environment: Mapping[str, str], working_directory: str
    ) -> None:
        self._do(
            "exec_child",
            command=command,
            environment=dict(environment),
            working_directory=working_directory,
        )


FULL_SEQUENCE = [
    "configure_network",
    "hand_over_packet_socket",
    "build_root",
    "start_relay",
    "limit_resources",
    "limit_cpu",
    "drop_capabilities",
    "forbid_new_privileges",
    "install_seccomp",
    "close_descriptors",
    "exec_child",
]


def full_spec(**overrides: Any) -> SandboxSpec:
    fields: dict[str, Any] = {
        "relay_socket_path": HOST_APPMUX / "run/shop/relay.sock",
        "child_port": 8000,
        "pinned_cpu": 3,
    }
    fields.update(overrides)
    return make_spec(**fields)


def test_stage_one_runs_the_steps_of_design_6_1_in_order() -> None:
    ops = RecordingOps()

    run_stage1(full_spec(), ops)

    assert ops.events == FULL_SEQUENCE


def test_the_root_comes_from_the_spec_and_the_child_gets_the_launchers_environment() -> None:
    spec = full_spec(overlays=(OVERLAY,))
    ops = RecordingOps()

    run_stage1(spec, ops)

    assert ops.arguments["build_root"]["plan"] == plan_rootfs(root_layout_for(spec))
    assert ops.arguments["exec_child"] == {
        "command": spec.command,
        "environment": child_environment(spec),
        "working_directory": CHILD_WORKING_DIRECTORY,
    }
    assert ops.arguments["start_relay"] == {"child_port": 8000}
    assert ops.arguments["limit_cpu"] == {"pinned_cpu": 3}


def test_descriptors_are_closed_immediately_before_exec() -> None:
    ops = RecordingOps()

    run_stage1(full_spec(), ops)

    assert ops.events[-2:] == ["close_descriptors", "exec_child"]


def test_the_capability_drop_comes_before_no_new_privs_and_the_seccomp_filter() -> None:
    ops = RecordingOps()

    run_stage1(full_spec(), ops)

    assert ops.events.index("drop_capabilities") < ops.events.index("forbid_new_privileges")
    assert ops.events.index("forbid_new_privileges") < ops.events.index("install_seccomp")


def test_the_relay_is_started_before_the_limits_so_it_is_not_held_to_the_childs_budget() -> None:
    ops = RecordingOps()

    run_stage1(full_spec(), ops)

    assert ops.events.index("start_relay") < ops.events.index("limit_resources")


def test_without_a_relay_socket_no_relay_is_started() -> None:
    ops = RecordingOps()

    run_stage1(make_spec(pinned_cpu=3), ops)

    assert "start_relay" not in ops.events


def test_without_a_cpu_pin_there_is_no_pin_and_no_seccomp_filter() -> None:
    # The filter exists to keep the child from undoing the pin (spike 7); installing it without
    # a pin would only break a library that sets its own affinity.
    ops = RecordingOps()

    run_stage1(full_spec(pinned_cpu=None), ops)

    assert "limit_cpu" not in ops.events
    assert "install_seccomp" not in ops.events
    assert "exec_child" in ops.events


def test_omitting_the_capability_drop_leaves_everything_else_in_place() -> None:
    ops = RecordingOps()

    run_stage1(full_spec(omit_layers=frozenset({Layer.CAPABILITY_DROP})), ops)

    assert ops.events == [event for event in FULL_SEQUENCE if event != "drop_capabilities"]


@pytest.mark.parametrize("failing_step", FULL_SEQUENCE)
def test_a_step_that_fails_stops_stage_one_names_the_step_and_keeps_the_kernels_reason(
    failing_step: str,
) -> None:
    ops = RecordingOps(fail_at=failing_step)

    with pytest.raises(Stage1Error, match=failing_step) as raised:
        run_stage1(full_spec(), ops)

    assert ops.events[-1] == failing_step  # nothing ran after the failure
    assert isinstance(raised.value.__cause__, OSError)


def test_a_capability_error_stops_stage_one_too() -> None:
    class Refusing(RecordingOps):
        def drop_capabilities(self) -> None:
            raise CapabilityDropError("cannot drop capability 0 from the bounding set")

    ops = Refusing()

    with pytest.raises(Stage1Error, match="drop_capabilities"):
        run_stage1(full_spec(), ops)

    assert "exec_child" not in ops.events


# ---- how a sandbox ended ------------------------------------------------------------------------


def test_a_normal_exit_keeps_its_code_and_a_kill_becomes_a_signal() -> None:
    assert SandboxExit.from_returncode(0) == SandboxExit(status=0, signal=None)
    assert SandboxExit.from_returncode(7) == SandboxExit(status=7, signal=None)
    killed = SandboxExit.from_returncode(-signal.SIGKILL)
    assert killed == SandboxExit(status=128 + signal.SIGKILL, signal=signal.SIGKILL)
    assert "SIGKILL" in killed.describe()
    assert "status 7" in SandboxExit.from_returncode(7).describe()


def test_the_sandbox_init_is_the_only_child_of_unshare() -> None:
    assert find_init_pid(100, read_children=lambda pid: "4242 ") == 4242
    assert find_init_pid(100, read_children=lambda pid: "") is None
    # Two children: guessing which is the init could kill the wrong process.
    assert find_init_pid(100, read_children=lambda pid: "4242 4243 ") is None

    def unreadable(pid: int) -> str:
        raise FileNotFoundError(pid)

    assert find_init_pid(100, read_children=unreadable) is None


# ---- signals ------------------------------------------------------------------------------------


def test_the_supervisors_signals_call_their_handlers_and_the_old_handlers_come_back() -> None:
    calls: list[str] = []
    before = signal.getsignal(signal.SIGUSR2)

    restore = install_signal_handlers({signal.SIGUSR2: lambda: calls.append("usr2")})
    try:
        os.kill(os.getpid(), signal.SIGUSR2)
        poll_until(lambda: calls, what="the handler")
    finally:
        restore()

    assert calls == ["usr2"]
    assert signal.getsignal(signal.SIGUSR2) == before


# ---- the supervisor, with a stand-in for unshare + stage 1 --------------------------------------

FAKE_STAGE1 = textwrap.dedent(
    """\
    import argparse, json, os, signal, socket, stat, sys, time
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-fd", type=int, required=True)
    parser.add_argument("--channel-path", required=True)
    parser.add_argument("--relay-fd", type=int)
    args = parser.parse_args()

    with os.fdopen(args.spec_fd) as handle:
        spec = json.load(handle)
    environment = spec["environment"]
    mode = environment.get("FAKE_MODE", "run")
    scratch = Path(spec["scratch_dir"])
    relay_is_socket = None
    if args.relay_fd is not None:
        relay_is_socket = stat.S_ISSOCK(os.fstat(args.relay_fd).st_mode)
    (scratch / "stage1.json").write_text(json.dumps({
        "argv": sys.argv[1:],
        "environment_names": sorted(environment),
        "relay_is_socket": relay_is_socket,
        "pid": os.getpid(),
    }))
    print("hello from the child", flush=True)

    if mode == "graceful":
        def terminate(*_):
            (scratch / "terminated").write_text("1")
            os._exit(0)
        signal.signal(signal.SIGTERM, terminate)
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "exits-first":
        sys.exit(5)
    if mode == "stderr-exits-first":
        print("Error: Cannot find module 'express'", file=sys.stderr, flush=True)
        sys.exit(5)
    if mode == "never-connects":
        time.sleep(60)

    channel = socket.socket(socket.AF_UNIX)
    channel.connect(args.channel_path)
    if mode == "no-handoff":
        time.sleep(60)
    tap, far = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    socket.send_fds(channel, [b"packet-socket"], [tap.fileno()])
    if channel.recv(1) != b"A":
        os._exit(3)
    if mode == "inject":
        far.send(bytes.fromhex(environment["FAKE_FRAME_HEX"]))
    if mode == "step-fails":
        channel.sendall(b"stage 1 step 'build_root' failed: Operation not permitted")
        sys.exit(1)
    channel.close()  # what a successful exec does to the close-on-exec channel
    if mode == "exit-7":
        time.sleep(0.3)
        sys.exit(7)
    if mode == "v8-out-of-memory":
        # What node does under a too-small RLIMIT_AS: a fatal error on stderr, then a trap.
        print("# Fatal process out of memory: Failed to reserve virtual memory for CodeRange",
              file=sys.stderr, flush=True)
        os.kill(os.getpid(), signal.SIGTRAP)
    time.sleep(60)
    """
)

FAKE_SLIRP = textwrap.dedent(
    """\
    #!{python}
    import os, sys, time
    ready_fd = int(next(a for a in sys.argv[1:] if a.startswith("--ready-fd=")).split("=")[1])
    with open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "slirp.pid"), "w") as f:
        f.write(str(os.getpid()))
    os.write(ready_fd, b"1")
    time.sleep(60)
    """
)


def denied_frame_hex() -> str:
    """A TCP SYN from the child to the metadata address, which the global rules deny."""
    transport = struct.pack("!HHLLBBHHH", 40000, 80, 0, 0, 5 << 4, 0, 0, 0, 0)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(transport),
        0,
        0,
        64,
        6,
        0,
        socket.inet_aton("10.0.2.100"),
        socket.inet_aton("169.254.169.254"),
    )
    return (b"\x02" * 6 + b"\x04" * 6 + b"\x08\x00" + ip_header + transport).hex()


class SeqpacketPort:
    """A packet port on a SEQPACKET socket, so a supervisor test needs no CAP_NET_RAW."""

    def __init__(self, packet_socket: socket.socket) -> None:
        self._socket = packet_socket
        packet_socket.settimeout(0.2)

    def receive(self) -> tuple[bytes, int] | None:
        try:
            data = self._socket.recv(70000)
        except TimeoutError:
            raise
        except OSError:
            return None
        return (data, 0) if data else None

    def send(self, frame: bytes) -> None:
        self._socket.send(frame)


class SupervisorRun:
    """One supervisor running in a thread, with the events it announced."""

    def __init__(self, supervisor: Supervisor, events: list[dict[str, Any]]) -> None:
        self.supervisor = supervisor
        self.events = events
        self.result: list[int] = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.result.append(self.supervisor.run())

    def start(self) -> None:
        self.thread.start()

    def wait_for(self, event_name: str) -> dict[str, Any]:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            for event in list(self.events):
                if event["event"] == event_name:
                    return event
            time.sleep(0.01)
        raise AssertionError(f"no {event_name!r} event; saw {self.events}")

    def finish(self) -> int:
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive(), f"the supervisor did not end; events: {self.events}"
        return self.result[0]


def make_supervisor(
    tmp_path: Path,
    *,
    mode: str = "run",
    with_slirp: bool = True,
    subreaper_calls: list[str] | None = None,
    extra_environment: Mapping[str, str] | None = None,
    find_init: Callable[[int], int | None] | None = None,
    **spec_overrides: Any,
) -> SupervisorRun:
    stage1 = tmp_path / "fake_stage1.py"
    stage1.write_text(FAKE_STAGE1)
    slirp = tmp_path / "bin" / "slirp4netns"
    if with_slirp:
        slirp.parent.mkdir()
        slirp.write_text(FAKE_SLIRP.format(python=sys.executable))
        slirp.chmod(0o755)
    spec = make_spec(
        tmp_path,
        environment={
            "FAKE_MODE": mode,
            "SECRET_TOKEN": "s3cret-value",
            **(extra_environment or {}),
        },
        **spec_overrides,
    )
    if not spec.rules_path.exists():
        write_rules_file(
            spec.rules_path,
            app_rules=[],
            global_rules=[EgressRule(action="deny", cidr="169.254.0.0/16", port="any", comment="")],
            container_ip=IPv4Address("10.4.1.7"),
            appmux_port=8000,
        )
    events: list[dict[str, Any]] = []
    options: dict[str, Any] = {}
    if find_init is not None:
        options["find_init"] = find_init
    supervisor = Supervisor(
        spec,
        launch_command=[sys.executable, str(stage1)],
        slirp_binary=slirp,
        announce=events.append,
        port_factory=SeqpacketPort,
        set_subreaper=(lambda: subreaper_calls.append("subreaper"))
        if subreaper_calls is not None
        else (lambda: None),
        stop_grace_s=0.5,
        handoff_timeout_s=3,
        exec_timeout_s=5,
        **options,
    )
    return SupervisorRun(supervisor, events)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until_gone(pid: int) -> bool:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if not process_exists(pid):
            return True
        time.sleep(0.02)
    return False


def stage1_report(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "stage1.json"
    poll_until(path.exists, what="the stage-1 report")
    report: dict[str, Any] = json.loads(path.read_text())
    return report


def test_a_sandbox_that_runs_is_announced_and_its_exit_is_reported_with_its_code(
    tmp_path: Path,
) -> None:
    run = make_supervisor(tmp_path, mode="exit-7")
    run.start()

    started = run.wait_for("started")
    exited = run.wait_for("exited")

    assert started["sandbox_pid"] == stage1_report(tmp_path)["pid"]
    assert exited["status"] == 7
    assert exited["signal"] is None
    assert run.finish() == 0  # the supervisor itself did its job; the code is in the event
    assert [event["event"] for event in run.events] == ["started", "exited"]


def test_the_childs_output_goes_to_its_log_and_its_secrets_never_to_argv(tmp_path: Path) -> None:
    run = make_supervisor(tmp_path, mode="exit-7")
    run.start()
    run.wait_for("exited")
    run.finish()

    report = stage1_report(tmp_path)

    assert "hello from the child" in (tmp_path / "child.log").read_text()
    assert report["environment_names"] == [
        "FAKE_MODE",
        "SECRET_TOKEN",
    ]  # it did arrive, in the spec
    assert "s3cret-value" not in " ".join(report["argv"])  # ... but not on the command line


def test_the_childs_stderr_reaches_its_log_and_a_death_by_signal_is_reported(
    tmp_path: Path,
) -> None:
    # Regression (T190): a node child that died at start left an empty tail. The child's stderr
    # must reach the log, and the signal that ended it must be in the event.
    run = make_supervisor(tmp_path, mode="v8-out-of-memory")
    run.start()

    exited = run.wait_for("exited")
    run.finish()

    assert exited["signal"] == signal.SIGTRAP
    log = (tmp_path / "child.log").read_text()
    assert "hello from the child" in log
    assert "Failed to reserve virtual memory for CodeRange" in log


def test_with_a_stderr_path_the_two_streams_go_to_two_files(tmp_path: Path) -> None:
    run = make_supervisor(tmp_path, mode="v8-out-of-memory", stderr_path=tmp_path / "child.err")
    run.start()
    run.wait_for("exited")
    run.finish()

    assert (tmp_path / "child.log").read_text() == "hello from the child\n"
    assert "Failed to reserve virtual memory" in (tmp_path / "child.err").read_text()


def test_an_early_exit_quotes_the_end_of_both_streams(tmp_path: Path) -> None:
    run = make_supervisor(tmp_path, mode="stderr-exits-first", stderr_path=tmp_path / "child.err")
    run.start()

    failed = run.wait_for("failed")

    assert "status 5" in failed["reason"]
    assert "hello from the child" in failed["reason"]
    assert "Cannot find module 'express'" in failed["reason"]
    assert run.finish() == 1


def test_a_failed_step_is_reported_with_its_reason_and_nothing_is_left_running(
    tmp_path: Path,
) -> None:
    run = make_supervisor(tmp_path, mode="step-fails")
    run.start()

    failed = run.wait_for("failed")

    assert "stage 1 step 'build_root' failed: Operation not permitted" in failed["reason"]
    assert run.finish() == 1
    assert [event["event"] for event in run.events] == ["failed"]
    assert wait_until_gone(stage1_report(tmp_path)["pid"])


def test_a_missing_slirp_binary_refuses_the_start_before_anything_is_spawned(
    tmp_path: Path,
) -> None:
    run = make_supervisor(tmp_path, with_slirp=False)
    run.start()

    failed = run.wait_for("failed")

    assert "slirp4netns" in failed["reason"]
    assert run.finish() == 1
    assert not (tmp_path / "stage1.json").exists()


def test_an_unreadable_rules_file_refuses_the_start_before_anything_is_spawned(
    tmp_path: Path,
) -> None:
    (tmp_path / "rules.json").write_text("{ not a rules file")
    run = make_supervisor(tmp_path)
    run.start()

    failed = run.wait_for("failed")

    assert "rules.json" in failed["reason"]
    assert run.finish() == 1
    assert not (tmp_path / "stage1.json").exists()


def test_a_stage_one_that_never_hands_over_the_socket_times_out_and_is_killed(
    tmp_path: Path,
) -> None:
    run = make_supervisor(tmp_path, mode="no-handoff")
    run.start()

    failed = run.wait_for("failed")

    assert "packet socket" in failed["reason"]
    assert run.finish() == 1
    assert wait_until_gone(stage1_report(tmp_path)["pid"])


def test_a_stage_one_that_never_connects_times_out_and_is_killed(tmp_path: Path) -> None:
    run = make_supervisor(tmp_path, mode="never-connects")
    run.start()

    failed = run.wait_for("failed")

    assert "did not connect" in failed["reason"]
    assert run.finish() == 1
    assert wait_until_gone(stage1_report(tmp_path)["pid"])


def test_a_stage_one_that_exits_before_connecting_is_reported_at_once_with_its_log(
    tmp_path: Path,
) -> None:
    run = make_supervisor(tmp_path, mode="exits-first")
    started_at = time.monotonic()
    run.start()

    failed = run.wait_for("failed")

    assert "status 5" in failed["reason"]
    assert "hello from the child" in failed["reason"]  # the end of the log helps the operator
    assert time.monotonic() - started_at < 2.5  # not the 3 s connect timeout
    assert run.finish() == 1


def test_the_supervisor_marks_itself_as_the_reaper_before_it_starts_anything(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    run = make_supervisor(tmp_path, mode="exit-7", subreaper_calls=calls)
    run.start()
    run.wait_for("exited")
    run.finish()

    assert calls == ["subreaper"]


def test_a_relay_socket_is_bound_outside_and_inherited_by_stage_one(tmp_path: Path) -> None:
    run = make_supervisor(
        tmp_path,
        mode="exit-7",
        relay_socket_path=tmp_path / "relay.sock",
        child_port=8000,
    )
    run.start()
    run.wait_for("exited")
    run.finish()

    assert stage1_report(tmp_path)["relay_is_socket"] is True
    assert (tmp_path / "relay.sock").exists()


def test_the_wall_clock_limit_ends_a_sandbox_that_runs_too_long(tmp_path: Path) -> None:
    run = make_supervisor(tmp_path, wall_clock_limit_s=1.0)
    run.start()
    run.wait_for("started")

    exited = run.wait_for("exited")

    assert "wall-clock limit" in exited["reason"]
    assert exited["signal"] == signal.SIGKILL
    assert run.finish() == 0
    assert wait_until_gone(stage1_report(tmp_path)["pid"])


def test_a_slirp4netns_that_dies_ends_the_sandbox_as_that_childs_crash(tmp_path: Path) -> None:
    # TD-030, design 11 "Child's pump or slirp dies": the child has no egress and is treated as
    # crashed, so the reconciler restarts it with backoff. Before the fix the sandbox kept running.
    run = make_supervisor(tmp_path)
    run.start()
    run.wait_for("started")
    slirp_pid_file = tmp_path / "bin" / "slirp.pid"
    poll_until(slirp_pid_file.exists, what="the fake slirp4netns")
    os.kill(int(slirp_pid_file.read_text()), signal.SIGKILL)

    exited = run.wait_for("exited")

    assert "slirp4netns" in exited["reason"]
    assert exited["signal"] == signal.SIGKILL  # the sandbox was killed, not asked to leave
    assert run.finish() == 0
    assert wait_until_gone(stage1_report(tmp_path)["pid"])


def test_a_frame_pump_that_ends_ends_the_sandbox_as_that_childs_crash(tmp_path: Path) -> None:
    # The pump ends itself when either direction fails (FramePump._child_to_slirp and
    # _slirp_to_child both call stop() in their finally); calling stop() is the same event.
    run = make_supervisor(tmp_path)
    run.start()
    run.wait_for("started")
    pump = run.supervisor.pump
    assert pump is not None
    pump.stop()

    exited = run.wait_for("exited")

    assert "frame pump" in exited["reason"]
    assert exited["signal"] == signal.SIGKILL
    assert run.finish() == 0
    assert wait_until_gone(stage1_report(tmp_path)["pid"])


def test_a_stop_request_ends_the_sandbox_and_says_it_was_stopped(tmp_path: Path) -> None:
    run = make_supervisor(tmp_path)
    run.start()
    run.wait_for("started")

    run.supervisor.request_stop()
    exited = run.wait_for("exited")

    assert exited["reason"] == "stopped"
    assert run.finish() == 0
    assert wait_until_gone(stage1_report(tmp_path)["pid"])


def test_a_stop_asks_the_init_to_terminate_first_so_the_app_can_shut_down(tmp_path: Path) -> None:
    # The fake stage 1 stands in for the sandbox's init: it handles SIGTERM and leaves cleanly.
    run = make_supervisor(tmp_path, mode="graceful", find_init=lambda pid: pid)
    run.start()
    run.wait_for("started")

    run.supervisor.request_stop()
    exited = run.wait_for("exited")

    assert (tmp_path / "terminated").exists()
    assert exited["signal"] is None
    assert exited["status"] == 0
    run.finish()


def test_an_init_that_ignores_the_request_is_killed_when_the_grace_period_ends(
    tmp_path: Path,
) -> None:
    run = make_supervisor(tmp_path, mode="stubborn", find_init=lambda pid: pid)
    run.start()
    run.wait_for("started")

    run.supervisor.request_stop()
    exited = run.wait_for("exited")

    assert exited["signal"] == signal.SIGKILL
    assert exited["reason"] == "stopped"
    run.finish()
    assert wait_until_gone(stage1_report(tmp_path)["pid"])


def test_stopping_twice_is_harmless(tmp_path: Path) -> None:
    run = make_supervisor(tmp_path)
    run.start()
    run.wait_for("started")

    run.supervisor.request_stop()
    run.supervisor.request_stop()

    assert run.wait_for("exited")["reason"] == "stopped"
    assert run.finish() == 0


def test_rules_reload_into_the_running_pump_and_a_bad_file_keeps_the_old_ones(
    tmp_path: Path,
) -> None:
    run = make_supervisor(tmp_path)
    run.start()
    run.wait_for("started")
    pump = run.supervisor.pump
    assert pump is not None and pump.snapshot()["rules_generation"] == 0

    run.supervisor.reload_rules()
    assert pump.snapshot()["rules_generation"] == 1

    (tmp_path / "rules.json").write_text("{ half a file")
    run.supervisor.reload_rules()
    assert pump.snapshot()["rules_generation"] == 1

    run.supervisor.request_stop()
    run.finish()


def test_a_reload_requested_from_a_signal_handler_is_carried_out_by_the_running_loop(
    tmp_path: Path,
) -> None:
    run = make_supervisor(tmp_path)
    run.start()
    run.wait_for("started")
    pump = run.supervisor.pump
    assert pump is not None

    run.supervisor.request_reload()
    poll_until(lambda: pump.snapshot()["rules_generation"] == 1, what="the reload")

    run.supervisor.request_stop()
    run.finish()


def test_the_pump_counters_are_written_where_the_host_reads_them(tmp_path: Path) -> None:
    run = make_supervisor(tmp_path)
    run.start()
    run.wait_for("started")

    run.supervisor.write_stats()

    stats = json.loads(stats_path_for(run.supervisor.spec).read_text())
    assert stats["dropped_total"] == 0
    assert stats["rules_generation"] == 0
    run.supervisor.request_stop()
    run.finish()


def test_a_frame_to_a_denied_address_is_dropped_and_counted_by_default(tmp_path: Path) -> None:
    run = make_supervisor(
        tmp_path, mode="inject", extra_environment={"FAKE_FRAME_HEX": denied_frame_hex()}
    )
    run.start()
    run.wait_for("started")
    pump = run.supervisor.pump
    assert pump is not None

    poll_until(lambda: pump.snapshot()["dropped_total"] >= 1, what="the dropped frame")

    assert pump.snapshot()["frames_to_slirp"] == 0
    run.supervisor.request_stop()
    run.finish()


def test_omitting_the_pump_filter_lets_the_same_frame_through_uncounted(tmp_path: Path) -> None:
    run = make_supervisor(
        tmp_path,
        mode="inject",
        extra_environment={"FAKE_FRAME_HEX": denied_frame_hex()},
        omit_layers=frozenset({Layer.PUMP_FILTER}),
    )
    run.start()
    run.wait_for("started")
    pump = run.supervisor.pump
    assert pump is not None

    poll_until(lambda: pump.snapshot()["frames_to_slirp"] >= 1, what="the forwarded frame")

    assert pump.snapshot()["dropped_total"] == 0
    run.supervisor.request_stop()
    run.finish()


# ---- the handle appmux holds, with a stand-in supervisor program --------------------------------

FAKE_SUPERVISOR = textwrap.dedent(
    """\
    import json, os, signal, sys, time
    from pathlib import Path

    spec = json.load(sys.stdin)
    scratch = Path(spec["scratch_dir"])
    mode = spec["environment"].get("FAKE_MODE", "run")
    (scratch / "supervisor.json").write_text(json.dumps({"argv": sys.argv[1:], "pid": os.getpid()}))

    def say(**event):
        print(json.dumps(event), flush=True)

    def stop(*_):
        say(event="exited", status=0, reason="stopped")
        os._exit(0)

    if mode == "fails":
        say(event="failed", reason="the veth pair could not be created")
        sys.exit(1)
    if mode == "dies":
        sys.exit(9)
    signal.signal(signal.SIGTERM, signal.SIG_IGN if mode == "ignores-sigterm" else stop)
    signal.signal(signal.SIGHUP, lambda *_: (scratch / "hup").write_text("1"))
    signal.signal(
        signal.SIGUSR1,
        lambda *_: (scratch / "pump-stats.json").write_text(json.dumps({"dropped_total": 5})),
    )
    if mode != "no-start":
        say(event="started", sandbox_pid=4242)
    if mode == "exits":
        time.sleep(0.2)
        say(event="exited", status=3, reason="")
        sys.exit(0)
    time.sleep(60)
    """
)


@pytest.fixture
def supervisor_command(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_supervisor.py"
    script.write_text(FAKE_SUPERVISOR)
    return [sys.executable, str(script)]


async def test_the_spec_reaches_the_supervisor_on_stdin_and_not_on_its_command_line(
    tmp_path: Path, supervisor_command: list[str]
) -> None:
    handle = await start_sandbox(make_spec(tmp_path), supervisor_command=supervisor_command)
    try:
        await handle.wait_started(timeout_s=WAIT_S)

        report = json.loads((tmp_path / "supervisor.json").read_text())
        assert report["argv"] == []
        assert handle.pid == report["pid"]
        assert handle.sandbox_pid == 4242  # what the memory watchdog should walk from
    finally:
        await handle.stop(grace_s=1)


async def test_a_failed_start_raises_with_the_supervisors_reason(
    tmp_path: Path, supervisor_command: list[str]
) -> None:
    handle = await start_sandbox(
        make_spec(tmp_path, environment={"FAKE_MODE": "fails"}),
        supervisor_command=supervisor_command,
    )

    with pytest.raises(SandboxStartError, match="veth pair could not be created"):
        await handle.wait_started(timeout_s=WAIT_S)


async def test_a_supervisor_that_dies_without_a_word_is_a_failed_start(
    tmp_path: Path, supervisor_command: list[str]
) -> None:
    handle = await start_sandbox(
        make_spec(tmp_path, environment={"FAKE_MODE": "dies"}),
        supervisor_command=supervisor_command,
    )

    with pytest.raises(SandboxStartError, match="status 9"):
        await handle.wait_started(timeout_s=WAIT_S)


async def test_a_supervisor_that_never_reports_is_stopped_when_the_start_times_out(
    tmp_path: Path, supervisor_command: list[str]
) -> None:
    handle = await start_sandbox(
        make_spec(tmp_path, environment={"FAKE_MODE": "no-start"}),
        supervisor_command=supervisor_command,
    )

    with pytest.raises(SandboxStartError, match="did not report"):
        await handle.wait_started(timeout_s=0.5)

    assert wait_until_gone(handle.pid)


async def test_wait_returns_how_the_sandbox_ended(
    tmp_path: Path, supervisor_command: list[str]
) -> None:
    handle = await start_sandbox(
        make_spec(tmp_path, environment={"FAKE_MODE": "exits"}),
        supervisor_command=supervisor_command,
    )
    await handle.wait_started(timeout_s=WAIT_S)

    exit_report = await asyncio.wait_for(handle.wait(), WAIT_S)

    assert exit_report.status == 3
    assert exit_report.signal is None


async def test_signals_reach_the_supervisor_for_reload_and_stats(
    tmp_path: Path, supervisor_command: list[str]
) -> None:
    handle = await start_sandbox(make_spec(tmp_path), supervisor_command=supervisor_command)
    try:
        await handle.wait_started(timeout_s=WAIT_S)

        assert handle.reload_rules() is True
        stats = await handle.read_stats(timeout_s=WAIT_S)

        assert (tmp_path / "hup").exists()
        assert stats is not None and stats["dropped_total"] == 5
    finally:
        await handle.stop(grace_s=1)


async def test_a_reload_for_a_supervisor_that_is_gone_says_it_was_not_delivered(
    tmp_path: Path, supervisor_command: list[str]
) -> None:
    handle = await start_sandbox(
        make_spec(tmp_path, environment={"FAKE_MODE": "exits"}),
        supervisor_command=supervisor_command,
    )
    await handle.wait()

    assert handle.reload_rules() is False


async def test_stop_asks_first_and_kills_a_supervisor_that_does_not_answer(
    tmp_path: Path, supervisor_command: list[str]
) -> None:
    polite = await start_sandbox(make_spec(tmp_path), supervisor_command=supervisor_command)
    await polite.wait_started(timeout_s=WAIT_S)
    assert (await polite.stop(grace_s=WAIT_S)).signal is None

    stubborn_dir = tmp_path / "stubborn"
    stubborn_dir.mkdir()
    stubborn = await start_sandbox(
        make_spec(stubborn_dir, environment={"FAKE_MODE": "ignores-sigterm"}),
        supervisor_command=supervisor_command,
    )
    await stubborn.wait_started(timeout_s=WAIT_S)

    killed = await stubborn.stop(grace_s=0.3)

    assert killed.signal == signal.SIGKILL
    assert wait_until_gone(stubborn.pid)
