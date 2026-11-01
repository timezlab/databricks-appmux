"""The canary of design 6.4 (T063): what each check decides, and how the host runs it.

The canary is a program that runs inside a real sandbox and tries to do what a child must not be
able to do. Whether the sandbox then holds is for the container run. Here: the decision each check
makes from what the kernel tells it (through the seam that asks), that every doubtful answer is a
failed check, that the output is the one JSON object ``self_check`` accepts, and the host side
(``run_canary``) against a fake sandbox.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import io
import json
import os
import socket
import sys
from collections.abc import Callable
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest

from appmux.sandbox import canary
from appmux.sandbox.canary import (
    CHECK_IDS,
    CHECKS,
    CanaryConfig,
    Plan,
    PlanError,
    check_appmux_directories,
    check_capabilities,
    check_cpu_pin,
    check_environ_isolation,
    check_inherited_descriptors,
    check_loopback_and_container_ip,
    check_metadata_address,
    check_nested_user_namespace,
    check_pid_isolation,
    check_platform_files,
    check_read_only_environment,
    check_sibling_sockets,
    check_sysv_ipc,
    container_listening_ports,
    create_probe_file,
    main,
    parse_capability_sets,
    reachable_targets,
    render_output,
    run_canary,
    run_checks,
    visible_content,
)
from appmux.sandbox.launcher import SandboxExit, SandboxSpec, SandboxStartError
from appmux.sandbox.rootfs import TMP_PATH
from appmux.sandbox.self_check import CHECK_IDS as EXPECTED_CHECK_IDS
from appmux.sandbox.self_check import CanaryUnavailable, parse_canary_output

pytestmark = pytest.mark.anyio

CONTAINER_IP = "10.20.30.40"


def make_plan(**overrides: Any) -> Plan:
    fields: dict[str, Any] = {
        "container_ip": CONTAINER_IP,
        "container_ports": (8000, 9000),
        "loopback_decoy_port": 5555,
        "connect_timeout_s": 0.5,
        "host_pids": (4321,),
        "allowed_environment_names": frozenset({"PATH", "HOME"}),
        "platform_paths": ("/databricks/runtime.cfg",),
        "sibling_paths": (f"{TMP_PATH}/appmux/run/sibling/relay.sock",),
        "appmux_paths": ("/app/python/source_code/src/appmux/__main__.py",),
        "write_probe_dirs": ("/app/python/source_code/.venv/lib/python3.11/site-packages",),
        "ipc_key": 1234567,
        "pinned": True,
    }
    fields.update(overrides)
    return Plan(**fields)


# ---- the contract with self_check -------------------------------------------------------------


def test_the_canary_runs_exactly_the_checks_self_check_expects() -> None:
    assert CHECK_IDS == EXPECTED_CHECK_IDS
    assert sorted(name for name, _ in CHECKS) == sorted(EXPECTED_CHECK_IDS)
    assert len({name for name, _ in CHECKS}) == len(CHECKS)


def test_the_descriptor_check_runs_first_before_anything_else_opens_a_file() -> None:
    assert CHECKS[0][0] == "inherited_descriptors"


def test_the_output_is_one_json_object_that_self_check_accepts() -> None:
    results = run_checks(make_plan(), [(name, lambda _plan: (True, "")) for name, _ in CHECKS])

    text = render_output(results)

    assert "\n" not in text
    assert [check.name for check in parse_canary_output(text)] == list(EXPECTED_CHECK_IDS)


def test_a_check_that_raises_fails_and_names_the_exception_class_but_not_its_message() -> None:
    def explode(_plan: Plan) -> tuple[bool, str]:
        raise ValueError("a path the platform would rather not show")

    [result] = run_checks(make_plan(), [("pid_isolation", explode)])

    assert result["passed"] is False
    assert "ValueError" in result["detail"]
    assert "rather not show" not in result["detail"]


def test_a_check_that_raises_an_os_error_names_the_errno() -> None:
    def refused(_plan: Plan) -> tuple[bool, str]:
        raise OSError(errno.EACCES, "Permission denied")

    [result] = run_checks(make_plan(), [("pid_isolation", refused)])

    assert result["passed"] is False
    assert "EACCES" in result["detail"]


def test_a_check_that_returns_no_outcome_is_a_failure_not_a_pass() -> None:
    def breaks_its_contract(_plan: Plan) -> Any:
        return None

    [result] = run_checks(make_plan(), [("pid_isolation", breaks_its_contract)])

    assert result["passed"] is False


def test_one_failed_check_does_not_stop_the_others() -> None:
    checks: list[tuple[str, Callable[[Plan], tuple[bool, str]]]] = [
        ("pid_isolation", lambda _plan: (False, "saw pid 27")),
        ("metadata_address", lambda _plan: (True, "")),
    ]

    results = run_checks(make_plan(), checks)

    assert [(r["id"], r["passed"]) for r in results] == [
        ("pid_isolation", False),
        ("metadata_address", True),
    ]


def test_main_prints_one_line_and_exits_zero_when_every_check_passes() -> None:
    out = io.StringIO()
    checks = [(name, lambda _plan: (True, "")) for name, _ in CHECKS]

    status = main(["canary.py", make_plan().to_json()], out=out, checks=checks)

    assert status == 0
    assert len(out.getvalue().splitlines()) == 1
    assert all(entry["passed"] for entry in json.loads(out.getvalue())["checks"])


def test_main_exits_nonzero_when_a_check_fails_but_still_prints_every_check() -> None:
    out = io.StringIO()
    checks = [
        (name, lambda _plan, name=name: (name != "sysv_ipc", "")) for name, _ in CHECKS
    ]  # name=name: bind each name now, not the loop's last one

    status = main(["canary.py", make_plan().to_json()], out=out, checks=checks)

    assert status == 1
    assert len(parse_canary_output(out.getvalue())) == len(EXPECTED_CHECK_IDS)


@pytest.mark.parametrize("argv", [["canary.py"], ["canary.py", "not json"], ["canary.py", "{}"]])
def test_a_plan_that_cannot_be_read_fails_every_check_and_names_no_input(argv: list[str]) -> None:
    out = io.StringIO()

    status = main(argv, out=out)

    results = parse_canary_output(out.getvalue())
    assert status == 1
    assert [result.name for result in results] == list(EXPECTED_CHECK_IDS)
    assert not any(result.passed for result in results)


def test_the_plan_survives_a_round_trip_through_json() -> None:
    plan = make_plan()

    assert Plan.from_json(plan.to_json()) == plan


@pytest.mark.parametrize(
    "mutation",
    [
        {"container_ports": "8000"},
        {"loopback_decoy_port": "5555"},
        {"connect_timeout_s": 0},
        {"ipc_key": 0},
        {"pinned": "yes"},
        {"host_pids": [True]},
        {"unknown_field": 1},
    ],
)
def test_a_plan_with_a_wrong_field_is_refused(mutation: dict[str, Any]) -> None:
    document = json.loads(make_plan().to_json()) | mutation

    with pytest.raises(PlanError):
        Plan.from_json(json.dumps(document))


def test_the_part_that_runs_inside_the_sandbox_imports_only_the_standard_library() -> None:
    # The sandbox has no appmux package: a top-level import of one would end the canary before
    # its first check. The host side imports appmux lazily, inside the function that needs it.
    tree = ast.parse(Path(canary.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    }

    assert imported <= set(sys.stdlib_module_names)


# ---- inherited_descriptors ---------------------------------------------------------------------


def test_an_open_descriptor_beyond_two_fails_the_check_and_is_named() -> None:
    passed, detail = check_inherited_descriptors(
        make_plan(), is_open=lambda descriptor: descriptor == 7, limit=64
    )

    assert passed is False
    assert "7" in detail


def test_only_the_three_standard_descriptors_pass() -> None:
    passed, _ = check_inherited_descriptors(make_plan(), is_open=lambda _fd: False, limit=64)

    assert passed is True


def test_the_real_descriptor_probe_sees_a_descriptor_a_process_still_holds() -> None:
    def run_in_fork(keep_one_open: bool) -> int:
        pid = os.fork()
        if pid == 0:
            try:
                os.closerange(3, 4096)
                if keep_one_open:
                    os.open(os.devnull, os.O_RDONLY)
                passed, _ = check_inherited_descriptors(make_plan())
                os._exit(0 if passed else 1)
            finally:
                os._exit(2)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)

    assert run_in_fork(keep_one_open=False) == 0
    assert run_in_fork(keep_one_open=True) == 1


# ---- environ_isolation -------------------------------------------------------------------------


def refused_environ(_pid: int) -> bytes:
    raise FileNotFoundError(errno.ENOENT, "No such file")


def test_no_variable_beyond_the_ones_the_child_was_given_and_no_readable_environ_pass() -> None:
    passed, _ = check_environ_isolation(
        make_plan(), environment_names=lambda: {"PATH", "HOME"}, read_environ=refused_environ
    )

    assert passed is True


def test_a_variable_of_appmux_in_the_environment_fails_and_only_its_name_is_reported() -> None:
    passed, detail = check_environ_isolation(
        make_plan(),
        environment_names=lambda: {"PATH", "DATABRICKS_CLIENT_SECRET"},
        read_environ=refused_environ,
    )

    assert passed is False
    assert "DATABRICKS_CLIENT_SECRET" in detail


def test_a_readable_environ_of_appmux_or_pid_one_fails() -> None:
    asked: list[int] = []

    def readable(pid: int) -> bytes:
        asked.append(pid)
        return b"SECRET=1"

    passed, _ = check_environ_isolation(
        make_plan(host_pids=(4321,)), environment_names=lambda: {"PATH"}, read_environ=readable
    )

    assert passed is False
    assert {1, 4321} <= set(asked)


def test_an_unexpected_error_reading_an_environ_is_not_isolation() -> None:
    def broken(_pid: int) -> bytes:
        raise OSError(errno.EIO, "I/O error")

    passed, _ = check_environ_isolation(
        make_plan(), environment_names=lambda: {"PATH"}, read_environ=broken
    )

    assert passed is False


# ---- pid_isolation -----------------------------------------------------------------------------


def test_a_proc_that_lists_no_other_pid_passes() -> None:
    passed, _ = check_pid_isolation(
        make_plan(), list_proc=lambda: ["self", "cpuinfo", "meminfo"], own_pid=1
    )

    assert passed is True


def test_another_pid_in_proc_fails_and_is_named() -> None:
    passed, detail = check_pid_isolation(
        make_plan(), list_proc=lambda: ["self", "1", "27"], own_pid=1
    )

    assert passed is False
    assert "27" in detail


# ---- loopback_and_container_ip, metadata_address -----------------------------------------------


def test_the_loopback_decoy_the_gateway_and_every_listening_port_of_the_container_are_tried() -> (
    None
):
    tried: list[tuple[str, int]] = []

    def nothing_reachable(
        targets: list[tuple[str, int]], timeout_s: float
    ) -> list[tuple[str, int]]:
        tried.extend(targets)
        assert timeout_s == 0.5
        return []

    passed, _ = check_loopback_and_container_ip(make_plan(), reachable=nothing_reachable)

    assert passed is True
    assert set(tried) == {
        ("127.0.0.1", 5555),
        ("10.0.2.2", 5555),
        (CONTAINER_IP, 8000),
        (CONTAINER_IP, 9000),
    }


def test_a_connection_that_succeeds_fails_the_check_and_names_the_target() -> None:
    passed, detail = check_loopback_and_container_ip(
        make_plan(), reachable=lambda _targets, _timeout: [(CONTAINER_IP, 8000)]
    )

    assert passed is False
    assert f"{CONTAINER_IP}:8000" in detail


def test_a_plan_without_listening_ports_fails_because_nothing_was_tried() -> None:
    called = False

    def reachable(_targets: list[tuple[str, int]], _timeout: float) -> list[tuple[str, int]]:
        nonlocal called
        called = True
        return []

    passed, _ = check_loopback_and_container_ip(make_plan(container_ports=()), reachable=reachable)

    # appmux itself listens on the container address, so an empty list means the host side
    # could not read it, and a pass would prove nothing.
    assert passed is False
    assert called is False


def test_the_metadata_address_is_tried_on_port_80() -> None:
    tried: list[tuple[str, int]] = []

    def reachable(targets: list[tuple[str, int]], _timeout: float) -> list[tuple[str, int]]:
        tried.extend(targets)
        return []

    passed, _ = check_metadata_address(make_plan(), reachable=reachable)

    assert passed is True
    assert tried == [("169.254.169.254", 80)]


def test_a_reachable_metadata_address_fails() -> None:
    passed, _ = check_metadata_address(
        make_plan(), reachable=lambda targets, _timeout: list(targets)
    )

    assert passed is False


def test_the_real_probe_reports_a_listening_port_and_not_a_closed_one() -> None:
    with socket.socket() as listener, socket.socket() as closed:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        closed.bind(("127.0.0.1", 0))  # bound, never listening: connect is refused
        open_port = listener.getsockname()[1]
        closed_port = closed.getsockname()[1]

        reached = reachable_targets(
            [("127.0.0.1", open_port), ("127.0.0.1", closed_port)], timeout_s=1.0
        )

    assert reached == [("127.0.0.1", open_port)]


# ---- platform_files ----------------------------------------------------------------------------


def test_files_that_are_absent_or_empty_pass() -> None:
    contents = {"/proc/self/cgroup": b""}

    passed, _ = check_platform_files(make_plan(), content_of=lambda path: contents.get(path))

    assert passed is True


def test_a_platform_file_with_content_fails_and_is_named() -> None:
    passed, detail = check_platform_files(
        make_plan(),
        content_of=lambda path: b"x" if path == "/databricks/runtime.cfg" else None,
    )

    assert passed is False
    assert "/databricks/runtime.cfg" in detail


def test_a_platform_directory_that_exists_fails_even_though_it_has_no_content() -> None:
    passed, detail = check_platform_files(
        make_plan(), content_of=lambda path: b"<directory>" if path == "/databricks" else None
    )

    assert passed is False
    assert "/databricks" in detail


def test_the_real_probe_tells_absent_empty_full_and_directory_apart(tmp_path: Path) -> None:
    (tmp_path / "empty").write_bytes(b"")
    (tmp_path / "full").write_bytes(b"data")
    (tmp_path / "dir").mkdir()

    assert visible_content(str(tmp_path / "missing")) is None
    assert visible_content(str(tmp_path / "missing" / "deeper")) is None
    assert visible_content(str(tmp_path / "empty")) == b""
    assert visible_content(str(tmp_path / "full"))
    assert visible_content(str(tmp_path / "dir"))


# ---- capabilities ------------------------------------------------------------------------------


def run_capabilities(
    *,
    mount: int = errno.EPERM,
    packet: int = errno.EPERM,
    link: int = errno.EPERM,
    sets: tuple[int, int] = (0, 0),
) -> tuple[bool, str]:
    return check_capabilities(
        make_plan(),
        try_mount=lambda: mount,
        try_packet_socket=lambda: packet,
        try_create_link=lambda: link,
        capability_sets=lambda: sets,
    )


def test_when_mount_a_raw_socket_and_a_new_link_are_all_refused_and_no_capability_is_held() -> None:
    passed, _ = run_capabilities()

    assert passed is True


@pytest.mark.parametrize("attempt", ["mount", "packet", "link"])
def test_an_attempt_that_succeeds_fails_the_check_and_is_named(attempt: str) -> None:
    passed, detail = run_capabilities(**{attempt: 0})

    assert passed is False
    assert {"mount": "mount", "packet": "AF_PACKET", "link": "link"}[attempt] in detail


@pytest.mark.parametrize("attempt", ["mount", "packet", "link"])
def test_a_refusal_for_another_reason_than_permission_does_not_count_as_proof(
    attempt: str,
) -> None:
    # EINVAL could come from a malformed request: the canary would then pass without having
    # been refused for lacking a capability.
    passed, _ = run_capabilities(**{attempt: errno.EINVAL})

    assert passed is False


def test_a_capability_still_held_fails_even_if_every_attempt_was_refused() -> None:
    passed, detail = run_capabilities(sets=(1 << 21, 0))

    assert passed is False
    assert "held" in detail


def test_the_capability_sets_are_read_from_the_status_file() -> None:
    status = "Name:\tpython\nCapInh:\t0000000000000000\nCapPrm:\t0000000000000000\n"
    status += "CapEff:\t0000000000000400\nCapBnd:\t000001ffffffffff\n"

    assert parse_capability_sets(status) == (0x400, 0x1FFFFFFFFFF)


def test_a_status_without_capability_lines_cannot_be_read_as_zero() -> None:
    with pytest.raises(ValueError, match="CapEff"):
        parse_capability_sets("Name:\tpython\n")


# ---- read_only_environment ---------------------------------------------------------------------


def test_a_read_only_filesystem_error_on_every_directory_passes() -> None:
    passed, _ = check_read_only_environment(
        make_plan(write_probe_dirs=("/env", "/srv/overlay/0")),
        create_probe_file=lambda _directory: errno.EROFS,
    )

    assert passed is True


def test_a_directory_where_the_file_could_be_created_fails_and_is_named() -> None:
    passed, detail = check_read_only_environment(
        make_plan(write_probe_dirs=("/env", "/srv/overlay/0")),
        create_probe_file=lambda directory: 0 if directory == "/srv/overlay/0" else errno.EROFS,
    )

    assert passed is False
    assert "/srv/overlay/0" in detail


@pytest.mark.parametrize("code", [errno.EACCES, errno.ENOENT])
def test_only_a_read_only_mount_counts_as_protection(code: int) -> None:
    # Permissions alone are not the layer the design relies on (the child is root in its own
    # user namespace), and a missing directory means the probe tested nothing.
    passed, _ = check_read_only_environment(make_plan(), create_probe_file=lambda _d: code)

    assert passed is False


def test_a_plan_without_a_directory_to_probe_fails() -> None:
    passed, _ = check_read_only_environment(
        make_plan(write_probe_dirs=()), create_probe_file=lambda _d: errno.EROFS
    )

    assert passed is False


def test_the_real_probe_creates_and_removes_its_file_where_writing_is_allowed(
    tmp_path: Path,
) -> None:
    assert create_probe_file(str(tmp_path)) == 0
    assert list(tmp_path.iterdir()) == []
    assert create_probe_file(str(tmp_path / "missing")) == errno.ENOENT


# ---- sibling_sockets, appmux_directories -------------------------------------------------------

PathCheck = Callable[..., tuple[bool, str]]


@pytest.mark.parametrize(
    ("check", "field"),
    [(check_sibling_sockets, "sibling_paths"), (check_appmux_directories, "appmux_paths")],
)
class TestPathsThatMustNotExist:
    def test_paths_that_do_not_exist_pass(
        self, check: PathCheck, field: str, tmp_path: Path
    ) -> None:
        passed, _ = check(make_plan(**{field: (str(tmp_path / "gone"), str(tmp_path / "x" / "y"))}))

        assert passed is True

    def test_a_path_that_exists_fails_and_is_named(
        self, check: PathCheck, field: str, tmp_path: Path
    ) -> None:
        visible = tmp_path / "marker"
        visible.write_text("planted by appmux")

        passed, detail = check(make_plan(**{field: (str(tmp_path / "gone"), str(visible))}))

        assert passed is False
        assert str(visible) in detail

    def test_a_path_that_cannot_be_examined_is_not_absent(
        self, check: PathCheck, field: str, tmp_path: Path
    ) -> None:
        # A file is not a directory: ENOTDIR says the path does not exist, but a loop or a
        # permission error says nothing, and only a clear "not found" may pass.
        loop = tmp_path / "loop"
        loop.symlink_to(loop)

        passed, _ = check(make_plan(**{field: (str(loop),)}))

        assert passed is False

    def test_an_empty_list_fails_because_nothing_was_tried(
        self, check: PathCheck, field: str
    ) -> None:
        passed, _ = check(make_plan(**{field: ()}))

        assert passed is False


# ---- sysv_ipc ----------------------------------------------------------------------------------


def test_a_planted_segment_and_queue_that_are_not_found_pass() -> None:
    asked: list[int] = []

    def not_found(key: int) -> tuple[int, int]:
        asked.append(key)
        return errno.ENOENT, errno.ENOENT

    passed, _ = check_sysv_ipc(make_plan(ipc_key=777), ipc_errnos=not_found)

    assert passed is True
    assert asked == [777]


@pytest.mark.parametrize(
    ("found", "named"),
    [((0, errno.ENOENT), "shared memory"), ((errno.ENOENT, 0), "message queue")],
)
def test_a_planted_object_that_is_found_fails_and_is_named(
    found: tuple[int, int], named: str
) -> None:
    passed, detail = check_sysv_ipc(make_plan(), ipc_errnos=lambda _key: found)

    assert passed is False
    assert named in detail


def test_a_permission_error_still_shows_the_object_exists() -> None:
    passed, _ = check_sysv_ipc(make_plan(), ipc_errnos=lambda _key: (errno.EACCES, errno.ENOENT))

    assert passed is False


# ---- nested_user_namespace ---------------------------------------------------------------------


def test_a_refused_nested_user_namespace_passes() -> None:
    passed, _ = check_nested_user_namespace(make_plan(), unshare_user=lambda: errno.EPERM)

    assert passed is True


@pytest.mark.parametrize("code", [0, errno.EINVAL, errno.ENOSPC])
def test_a_nested_user_namespace_that_succeeds_or_fails_for_another_reason_does_not_pass(
    code: int,
) -> None:
    passed, _ = check_nested_user_namespace(make_plan(), unshare_user=lambda: code)

    assert passed is False


# ---- cpu_pin -----------------------------------------------------------------------------------


def run_cpu_pin(
    *,
    pinned: bool = True,
    affinity: set[int] | None = None,
    nice: int = 19,
    widen: int = errno.EPERM,
) -> tuple[bool, str]:
    return check_cpu_pin(
        make_plan(pinned=pinned),
        get_affinity=lambda: {3} if affinity is None else affinity,
        get_priority=lambda: nice,
        widen_affinity=lambda: widen,
    )


def test_one_cpu_at_nice_19_and_a_refused_widening_pass() -> None:
    passed, _ = run_cpu_pin()

    assert passed is True


def test_more_than_one_cpu_fails() -> None:
    passed, _ = run_cpu_pin(affinity={2, 3})

    assert passed is False


def test_a_normal_priority_fails() -> None:
    passed, _ = run_cpu_pin(nice=0)

    assert passed is False


@pytest.mark.parametrize("code", [0, errno.EINVAL])
def test_a_widening_that_works_or_fails_for_another_reason_than_the_filter_fails(code: int) -> None:
    passed, _ = run_cpu_pin(widen=code)

    assert passed is False


def test_with_the_pin_switched_off_there_is_nothing_to_prove_and_the_detail_says_so() -> None:
    # The platform setting child_cpu_pin can be off; the check then has no pin to look for and
    # must not call the platform broken. The detail keeps the page honest about why.
    passed, detail = run_cpu_pin(pinned=False, affinity={0, 1, 2, 3}, nice=0)

    assert passed is True
    assert "child_cpu_pin" in detail


# ---- the host side -----------------------------------------------------------------------------

TCP_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout "
    "inode\n"
)


def tcp_line(index: int, local: str, state: str) -> str:
    return (
        f"   {index}: {local} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000 0 0 1\n"
    )


def little_endian(ip: str) -> str:
    return socket.inet_aton(ip)[::-1].hex().upper()


def test_listening_ports_of_the_container_address_and_of_every_address_are_collected() -> None:
    table = TCP_HEADER
    table += tcp_line(0, f"{little_endian(CONTAINER_IP)}:1F40", "0A")  # container ip, 8000
    table += tcp_line(1, "00000000:2328", "0A")  # every address, 9000
    table += tcp_line(2, "0100007F:15B3", "0A")  # loopback only, 5555: not the container address
    table += tcp_line(3, f"{little_endian(CONTAINER_IP)}:0050", "01")  # established, not listening
    table += tcp_line(4, "00000000:2328", "0A")  # duplicate

    assert container_listening_ports([table], IPv4Address(CONTAINER_IP)) == (8000, 9000)


def test_ipv6_listeners_on_every_address_are_collected_because_they_accept_ipv4_too() -> None:
    table = TCP_HEADER
    table += tcp_line(0, "00000000000000000000000000000000:1F90", "0A")  # [::]:8080
    table += tcp_line(1, "00000000000000000000000001000000:1F91", "0A")  # [::1]:8081

    assert container_listening_ports([table], IPv4Address(CONTAINER_IP)) == (8080,)


class FakeHandle:
    """The part of ``SandboxHandle`` that ``run_canary`` uses."""

    def __init__(
        self, *, never_finishes: bool = False, started_error: SandboxStartError | None = None
    ) -> None:
        self.never_finishes = never_finishes
        self.started_error = started_error
        self.stopped = False

    async def wait_started(self, *, timeout_s: float) -> None:
        if self.started_error is not None:
            raise self.started_error

    async def wait(self) -> SandboxExit:
        if self.never_finishes:
            await asyncio.sleep(60)
        return SandboxExit(status=0, signal=None)

    async def stop(self, *, grace_s: float) -> SandboxExit:
        self.stopped = True
        return SandboxExit(status=0, signal=None)


class FakeSandbox:
    """Stands in for ``start_sandbox``: records the spec, and while the canary 'runs' lets a
    test look at what the host prepared. ``log_lines`` is what the canary printed."""

    def __init__(self, log_lines: list[str], handle: FakeHandle | None = None) -> None:
        self.log_lines = log_lines
        self.handle = handle if handle is not None else FakeHandle()
        self.spec: SandboxSpec | None = None
        self.seen: dict[str, Any] = {}

    async def __call__(self, spec: SandboxSpec) -> FakeHandle:
        self._observe(spec)
        return self.handle

    def _observe(self, spec: SandboxSpec) -> None:
        # Synchronous on purpose: what the host prepared is looked at in one blocking step.
        self.spec = spec
        plan = Plan.from_json(spec.command[2])
        self.seen["plan"] = plan
        self.seen["release_files"] = sorted(path.name for path in spec.release_dir.iterdir())
        self.seen["markers"] = [Path(p) for p in plan.appmux_paths if Path(p).exists()]
        self.seen["overlay_exists"] = all(overlay.is_dir() for overlay in spec.overlays)
        with socket.socket() as probe:
            self.seen["decoy_accepts"] = (
                probe.connect_ex(("127.0.0.1", plan.loopback_decoy_port)) == 0
            )
        spec.log_path.write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")


GOOD_RESULT = json.dumps(
    {"checks": [{"id": name, "passed": True, "detail": ""} for name in EXPECTED_CHECK_IDS]}
)


def make_config(tmp_path: Path, **overrides: Any) -> CanaryConfig:
    environment = tmp_path / "env"
    (environment / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
    tcp = tmp_path / "tcp"
    tcp.write_text(TCP_HEADER + tcp_line(0, f"{little_endian(CONTAINER_IP)}:1F40", "0A"))
    fields: dict[str, Any] = {
        "work_dir": tmp_path / "work",
        "app_environment": environment,
        "container_ip": IPv4Address(CONTAINER_IP),
        "appmux_port": 8000,
        "marker_directories": (tmp_path / "markers",),
        "proc_net_tcp_paths": (tcp,),
        "pin_cpu": False,
        "timeout_s": 5.0,
    }
    fields.update(overrides)
    (tmp_path / "markers").mkdir(exist_ok=True)
    return CanaryConfig(**fields)


async def test_the_canary_runs_through_the_launcher_and_its_one_json_line_is_returned(
    tmp_path: Path,
) -> None:
    start = FakeSandbox(["noise before", GOOD_RESULT, "noise after"])

    text = await run_canary(make_config(tmp_path), start=start)

    assert text == GOOD_RESULT
    assert start.handle.stopped is True


async def test_the_sandbox_gets_the_canary_a_clean_environment_and_the_decoys(
    tmp_path: Path,
) -> None:
    start = FakeSandbox([GOOD_RESULT])
    config = make_config(tmp_path)

    await run_canary(config, start=start)

    spec = start.spec
    assert spec is not None
    assert spec.command[:2] == ("python", "/srv/app/canary.py")
    assert start.seen["release_files"] == ["canary.py"]
    assert start.seen["overlay_exists"] is True
    assert start.seen["decoy_accepts"] is True
    plan: Plan = start.seen["plan"]
    assert plan.container_ports == (8000,)
    assert plan.pinned is False
    assert set(spec.environment) <= plan.allowed_environment_names
    assert spec.omit_layers == frozenset()
    assert 1 in plan.host_pids and os.getpid() in plan.host_pids
    assert spec.app_environment == config.app_environment
    assert Path(plan.write_probe_dirs[0]).name == "site-packages"
    assert plan.write_probe_dirs[1:] == ("/srv/overlay/0",)


def assert_nothing_left(plan: Plan, config: CanaryConfig, tmp_path: Path) -> None:
    assert not any(Path(path).exists() for path in plan.appmux_paths if "markers" in path)
    assert list((tmp_path / "markers").iterdir()) == []
    assert not config.work_dir.exists()
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", plan.loopback_decoy_port)) != 0
    assert canary.sysv_ipc_errnos(plan.ipc_key) == (errno.ENOENT, errno.ENOENT)


async def test_what_the_host_planted_is_gone_when_the_canary_is_done(tmp_path: Path) -> None:
    start = FakeSandbox([GOOD_RESULT])
    config = make_config(tmp_path)

    await run_canary(config, start=start)

    plan: Plan = start.seen["plan"]
    assert start.seen["markers"], "the markers existed while the canary ran"
    assert_nothing_left(plan, config, tmp_path)


async def test_a_sandbox_that_does_not_start_is_unavailable_with_the_launchers_reason(
    tmp_path: Path,
) -> None:
    handle = FakeHandle(started_error=SandboxStartError("stage 1 step 'build_root' failed"))
    start = FakeSandbox([], handle)
    config = make_config(tmp_path)

    with pytest.raises(CanaryUnavailable, match="build_root"):
        await run_canary(config, start=start)

    assert handle.stopped is True
    assert not config.work_dir.exists()


async def test_a_canary_that_printed_no_result_is_unavailable_and_none_of_its_text_is_copied(
    tmp_path: Path,
) -> None:
    start = FakeSandbox(["Traceback: leaked-secret-value"])

    with pytest.raises(CanaryUnavailable) as raised:
        await run_canary(make_config(tmp_path), start=start)

    assert "leaked-secret-value" not in str(raised.value)


async def test_a_canary_that_never_finishes_is_stopped_and_reported(tmp_path: Path) -> None:
    handle = FakeHandle(never_finishes=True)
    start = FakeSandbox([GOOD_RESULT], handle)
    config = make_config(tmp_path, timeout_s=0.2)

    with pytest.raises(CanaryUnavailable, match="did not finish"):
        await run_canary(config, start=start)

    assert handle.stopped is True
    assert not config.work_dir.exists()


async def test_the_canary_is_pinned_to_one_cpu_when_the_platform_setting_is_on(
    tmp_path: Path,
) -> None:
    start = FakeSandbox([GOOD_RESULT])

    await run_canary(make_config(tmp_path, pin_cpu=True), start=start)

    spec = start.spec
    assert spec is not None
    assert spec.pinned_cpu in os.sched_getaffinity(0)
    assert start.seen["plan"].pinned is True
