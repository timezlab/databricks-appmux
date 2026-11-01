"""The network path of design 6.2, as far as it can run without a container.

The pieces that need the kernel (creating the veth pair, opening ``AF_PACKET``, running
``slirp4netns``) are checked by what they would send: the rtnetlink messages as bytes, the
``slirp4netns`` command line, the descriptor handoff over a real unix socket. The frame pump's
decisions run through ``Ruleset.decide`` and ``DropCounters`` of ``rules.py`` on real Ethernet
frames, with a stand-in for the ``AF_PACKET`` socket (which needs ``CAP_NET_RAW``) and a real
``SOCK_SEQPACKET`` pair for the slirp side.
"""

from __future__ import annotations

import errno
import json
import os
import queue
import socket
import stat
import struct
import sys
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from appmux.sandbox.pump import (
    ATTACHED_SIGNAL,
    AfPacketPort,
    FramePump,
    HandoffError,
    NetworkAttachError,
    RulesFileError,
    SlirpBinaryMissing,
    address_message,
    check_acknowledgement,
    configure_child_network,
    default_route_message,
    link_up_message,
    load_ruleset,
    netlink_attribute,
    netlink_message,
    receive_packet_socket,
    require_slirp_binary,
    send_packet_socket,
    signal_attached,
    slirp_binary_path,
    slirp_command,
    start_slirp,
    veth_create_message,
    wait_until_attached,
    write_rules_file,
    write_stats_file,
)
from appmux.sandbox.rules import EgressRule, Reason, Ruleset

RTM_NEWLINK, RTM_NEWADDR, RTM_NEWROUTE = 16, 20, 24
NLM_F_REQUEST, NLM_F_ACK, NLM_F_EXCL, NLM_F_CREATE = 1, 4, 0x200, 0x400
NLMSG_ERROR = 2
IFLA_IFNAME, IFLA_MTU, IFLA_LINKINFO = 3, 4, 18
IFLA_INFO_KIND, IFLA_INFO_DATA, VETH_INFO_PEER = 1, 2, 1
IFA_ADDRESS, IFA_LOCAL = 1, 2
RTA_OIF, RTA_GATEWAY = 4, 5
IFINFOMSG_BYTES = 16
IFADDRMSG_BYTES = 8
RTMSG_BYTES = 12
NLMSG_HEADER_BYTES = 16

CONTAINER_IP = IPv4Address("10.4.1.7")
APPMUX_PORT = 8000
PACKET_HOST, PACKET_OUTGOING = 0, 4


# ---- rtnetlink messages ------------------------------------------------------------------------


def parse_attributes(data: bytes) -> dict[int, bytes]:
    attributes: dict[int, bytes] = {}
    offset = 0
    while offset < len(data):
        length, kind = struct.unpack_from("HH", data, offset)
        attributes[kind] = data[offset + 4 : offset + length]
        offset += (length + 3) & ~3
    return attributes


def header_of(message: bytes) -> tuple[int, int, int]:
    length, message_type, flags, _sequence, _pid = struct.unpack_from("IHHII", message)
    return length, message_type, flags


def test_an_attribute_is_padded_to_four_bytes_but_its_length_is_not() -> None:
    attribute = netlink_attribute(IFLA_IFNAME, b"eth0\0")

    assert len(attribute) == 12
    assert struct.unpack_from("HH", attribute) == (9, IFLA_IFNAME)
    assert attribute[4:9] == b"eth0\0"


def test_a_message_header_counts_itself_and_always_asks_for_an_acknowledgement() -> None:
    message = netlink_message(RTM_NEWLINK, NLM_F_CREATE, b"\x00" * 16)

    length, message_type, flags = header_of(message)
    assert length == len(message) == NLMSG_HEADER_BYTES + 16
    assert message_type == RTM_NEWLINK
    assert flags == NLM_F_CREATE | NLM_F_REQUEST | NLM_F_ACK


def test_the_veth_pair_is_one_request_that_names_both_ends() -> None:
    message = veth_create_message("eth0", "vpump0")

    _, message_type, flags = header_of(message)
    assert message_type == RTM_NEWLINK
    assert flags & NLM_F_CREATE and flags & NLM_F_EXCL
    attributes = parse_attributes(message[NLMSG_HEADER_BYTES + IFINFOMSG_BYTES :])
    assert attributes[IFLA_IFNAME] == b"eth0\0"
    linkinfo = parse_attributes(attributes[IFLA_LINKINFO])
    assert linkinfo[IFLA_INFO_KIND] == b"veth\0"
    peer = parse_attributes(linkinfo[IFLA_INFO_DATA])[VETH_INFO_PEER]
    assert parse_attributes(peer[IFINFOMSG_BYTES:])[IFLA_IFNAME] == b"vpump0\0"


def test_the_address_message_gives_the_child_its_address_and_prefix() -> None:
    message = address_message(7, "10.0.2.100", 24)

    _, message_type, _ = header_of(message)
    assert message_type == RTM_NEWADDR
    family, prefix, _flags, _scope, index = struct.unpack_from("BBBBI", message, NLMSG_HEADER_BYTES)
    assert (family, prefix, index) == (socket.AF_INET, 24, 7)
    attributes = parse_attributes(message[NLMSG_HEADER_BYTES + IFADDRMSG_BYTES :])
    assert attributes[IFA_LOCAL] == attributes[IFA_ADDRESS] == socket.inet_aton("10.0.2.100")


def test_the_link_message_brings_an_interface_up_with_the_mtu() -> None:
    message = link_up_message(3, mtu=1400)

    _, message_type, _ = header_of(message)
    assert message_type == RTM_NEWLINK
    _family, _type, index, flags, change = struct.unpack_from("BxHiII", message, NLMSG_HEADER_BYTES)
    assert index == 3
    assert flags & 1 and change & 1  # IFF_UP set, and named as the flag to change
    attributes = parse_attributes(message[NLMSG_HEADER_BYTES + IFINFOMSG_BYTES :])
    assert struct.unpack("I", attributes[IFLA_MTU]) == (1400,)


def test_a_link_without_an_mtu_leaves_the_mtu_alone() -> None:
    message = link_up_message(1)

    attributes = parse_attributes(message[NLMSG_HEADER_BYTES + IFINFOMSG_BYTES :])
    assert IFLA_MTU not in attributes


def test_the_default_route_goes_through_the_gateway_on_the_childs_interface() -> None:
    message = default_route_message(7, "10.0.2.2")

    _, message_type, _ = header_of(message)
    assert message_type == RTM_NEWROUTE
    family, destination_prefix = struct.unpack_from("BB", message, NLMSG_HEADER_BYTES)
    assert (family, destination_prefix) == (socket.AF_INET, 0)  # 0.0.0.0/0
    attributes = parse_attributes(message[NLMSG_HEADER_BYTES + RTMSG_BYTES :])
    assert attributes[RTA_GATEWAY] == socket.inet_aton("10.0.2.2")
    assert struct.unpack("I", attributes[RTA_OIF]) == (7,)


def test_an_error_reply_becomes_an_oserror_with_the_kernels_errno() -> None:
    reply = (
        struct.pack("IHHII", 36, NLMSG_ERROR, 0, 1, 0) + struct.pack("i", -errno.EPERM) + b"\0" * 16
    )

    with pytest.raises(OSError, match="Operation not permitted") as raised:
        check_acknowledgement(reply, what="create the veth pair")

    assert raised.value.errno == errno.EPERM
    assert "create the veth pair" in str(raised.value)


def test_a_zero_error_is_the_acknowledgement_and_anything_that_is_not_a_reply_is_refused() -> None:
    ok = struct.pack("IHHII", 36, NLMSG_ERROR, 0, 1, 0) + struct.pack("i", 0) + b"\0" * 16
    check_acknowledgement(ok, what="x")

    with pytest.raises(OSError, match="reply"):
        check_acknowledgement(b"", what="x")
    with pytest.raises(OSError, match="reply"):
        check_acknowledgement(struct.pack("IHHII", 16, 3, 0, 1, 0), what="x")


# ---- the descriptor handoff --------------------------------------------------------------------


def test_the_packet_socket_travels_to_the_other_side_as_a_working_descriptor() -> None:
    child_end, appmux_end = socket.socketpair()
    carried, far_end = socket.socketpair()  # stands in for the AF_PACKET socket
    try:
        send_packet_socket(child_end, carried)
        received = receive_packet_socket(appmux_end)
        try:
            far_end.sendall(b"frame")
            assert received.recv(16) == b"frame"
        finally:
            received.close()
    finally:
        for sock in (child_end, appmux_end, carried, far_end):
            sock.close()


def test_a_handoff_without_a_descriptor_is_refused() -> None:
    child_end, appmux_end = socket.socketpair()
    try:
        child_end.sendall(b"packet-socket")  # the bytes, but nothing attached
        with pytest.raises(HandoffError, match="descriptor"):
            receive_packet_socket(appmux_end)
    finally:
        child_end.close()
        appmux_end.close()


def test_the_reason_stage_one_sent_instead_of_the_descriptor_is_in_the_error() -> None:
    # Stage 1 reports a failure on the same channel; the supervisor must be able to quote it.
    reason = "stage 1 step 'configure_network' failed: Operation not permitted " + "x" * 300
    child_end, appmux_end = socket.socketpair()
    try:
        child_end.sendall(reason.encode())
        with pytest.raises(HandoffError, match="configure_network") as raised:
            receive_packet_socket(appmux_end)
        assert reason in str(raised.value)  # whole, not cut at a small buffer
    finally:
        child_end.close()
        appmux_end.close()


def test_the_child_waits_for_the_attach_signal_and_proceeds_when_it_comes() -> None:
    child_end, appmux_end = socket.socketpair()
    try:
        signal_attached(appmux_end)

        wait_until_attached(child_end, timeout_s=5)
    finally:
        child_end.close()
        appmux_end.close()


def test_the_child_gives_up_when_the_nat_is_never_attached() -> None:
    child_end, appmux_end = socket.socketpair()
    try:
        with pytest.raises(NetworkAttachError, match="not attached"):
            wait_until_attached(child_end, timeout_s=0.05)
    finally:
        child_end.close()
        appmux_end.close()


def test_the_child_gives_up_when_appmux_hangs_up_instead_of_signalling() -> None:
    child_end, appmux_end = socket.socketpair()
    appmux_end.close()
    try:
        with pytest.raises(NetworkAttachError, match="closed"):
            wait_until_attached(child_end, timeout_s=5)
    finally:
        child_end.close()


def test_only_the_attach_signal_itself_counts() -> None:
    child_end, appmux_end = socket.socketpair()
    try:
        appmux_end.sendall(b"x")
        assert ATTACHED_SIGNAL != b"x"

        with pytest.raises(NetworkAttachError, match="unexpected"):
            wait_until_attached(child_end, timeout_s=5)
    finally:
        child_end.close()
        appmux_end.close()


# ---- slirp4netns -------------------------------------------------------------------------------


def test_slirp_runs_on_the_tap_descriptor_with_host_loopback_disabled_and_no_ipv6() -> None:
    command = slirp_command(Path("/deploy/bin/slirp4netns"), slirp_fd=9, ready_fd=8, mtu=1500)

    assert command == [
        "/deploy/bin/slirp4netns",
        "--netns-type=tapfd",
        "--ready-fd=8",
        "--mtu=1500",
        "--disable-host-loopback",
        "9",
    ]
    assert "--enable-ipv6" not in command


def test_the_binary_is_found_beside_the_package_not_in_the_working_directory() -> None:
    module_file = Path("/deploy/appmux/sandbox/pump.py")

    assert slirp_binary_path(module_file) == Path("/deploy/bin/slirp4netns")


def test_a_missing_binary_is_named_with_what_to_run(tmp_path: Path) -> None:
    missing = tmp_path / "bin" / "slirp4netns"

    with pytest.raises(SlirpBinaryMissing, match="slirp4netns") as raised:
        require_slirp_binary(missing)

    assert str(missing) in str(raised.value)
    assert "assemble-deploy" in str(raised.value)


def test_a_binary_that_lost_its_executable_bit_in_the_deploy_gets_it_back(tmp_path: Path) -> None:
    # The first container start found bin/slirp4netns present but not executable: a workspace
    # folder does not keep the mode bits of the files uploaded to it.
    binary = tmp_path / "slirp4netns"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o644)

    assert require_slirp_binary(binary) == binary

    assert os.access(binary, os.X_OK)
    assert binary.stat().st_mode & 0o777 == 0o744  # the owner only: nothing else gains the bit


def test_a_binary_whose_mode_cannot_be_changed_is_as_good_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "slirp4netns"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o644)

    def refuse(self: Path, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(Path, "chmod", refuse)

    with pytest.raises(SlirpBinaryMissing, match="not executable") as raised:
        require_slirp_binary(binary)

    assert "Operation not permitted" in str(raised.value)
    assert isinstance(raised.value.__cause__, PermissionError)


def test_a_binary_on_a_mount_that_forbids_execution_is_as_good_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "slirp4netns"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o755)
    monkeypatch.setattr(os, "access", lambda path, mode: False)  # what a noexec mount answers

    with pytest.raises(SlirpBinaryMissing, match="mounted"):
        require_slirp_binary(binary)


# ---- the rules file ----------------------------------------------------------------------------


def deny(cidr: str, port: str = "any") -> EgressRule:
    return EgressRule(action="deny", cidr=cidr, port=port, comment="")


def allow(cidr: str, port: str = "any") -> EgressRule:
    return EgressRule(action="allow", cidr=cidr, port=port, comment="")


def test_the_rules_file_round_trips_into_the_same_decisions(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    write_rules_file(
        path,
        app_rules=[allow("93.184.216.0/24", "443")],
        global_rules=[deny("169.254.0.0/16"), deny("@container_ip/32", "@appmux_port")],
        container_ip=CONTAINER_IP,
        appmux_port=APPMUX_PORT,
    )

    rules = load_ruleset(path)

    assert rules.decide(ipv4_frame("169.254.169.254", 80)).rule_id == "global:1"
    assert rules.decide(ipv4_frame(str(CONTAINER_IP), APPMUX_PORT)).rule_id == "global:2"
    assert rules.decide(ipv4_frame("93.184.216.34", 443)).rule_id == "app:1"
    assert rules.decide(ipv4_frame("8.8.8.8", 443)).reason == Reason.DEFAULT_ALLOW
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_rules_file_is_replaced_whole_so_a_reader_never_sees_half_of_it(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    for number in range(3):
        write_rules_file(
            path,
            app_rules=[deny(f"10.9.{number}.0/24")],
            global_rules=[],
            container_ip=CONTAINER_IP,
            appmux_port=APPMUX_PORT,
        )
        assert load_ruleset(path).rule_ids == ("app:1",)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["rules.json"]


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "{}",
        '{"app_rules": [], "global_rules": [], "container_ip": "10.4.1.7"}',
        '{"app_rules": [{"action": "deny", "cidr": "300.0.0.0/8", "port": "any", "comment": ""}],'
        ' "global_rules": [], "container_ip": "10.4.1.7", "appmux_port": 8000}',
    ],
)
def test_an_unusable_rules_file_is_refused_with_its_path(tmp_path: Path, content: str) -> None:
    path = tmp_path / "rules.json"
    path.write_text(content)

    with pytest.raises(RulesFileError, match=r"rules\.json"):
        load_ruleset(path)


# ---- the frame pump ----------------------------------------------------------------------------


def ipv4_frame(destination: str, port: int, *, protocol: int = 6) -> bytes:
    transport = struct.pack("!HHLLBBHHH", 40000, port, 0, 0, 5 << 4, 0, 0, 0, 0)  # 20 bytes
    total_bytes = 20 + len(transport)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_bytes,
        0,
        0,
        64,
        protocol,
        0,
        socket.inet_aton("10.0.2.100"),
        socket.inet_aton(destination),
    )
    return b"\x02" * 6 + b"\x04" * 6 + b"\x08\x00" + ip_header + transport


IPV6_FRAME = b"\x02" * 6 + b"\x04" * 6 + b"\x86\xdd" + b"\x00" * 46
ARP_FRAME = b"\x02" * 6 + b"\x04" * 6 + b"\x08\x06" + b"\x00" * 28


class FakePort:
    """Stands in for the AF_PACKET socket: frames in, frames out, a stop sentinel."""

    def __init__(self) -> None:
        self.incoming: queue.Queue[tuple[bytes, int] | None] = queue.Queue()
        self.sent: queue.Queue[bytes] = queue.Queue()

    def receive(self) -> tuple[bytes, int] | None:
        return self.incoming.get(timeout=5)

    def send(self, frame: bytes) -> None:
        self.sent.put(frame)

    def close(self) -> None:
        self.incoming.put(None)


def make_pump(
    rules: Ruleset, *, filter_frames: bool = True
) -> tuple[FramePump, FakePort, socket.socket]:
    port = FakePort()
    slirp_end, pump_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    pump = FramePump(port, pump_end, rules, filter_frames=filter_frames)
    return pump, port, slirp_end


def ruleset_of(*rules: EgressRule) -> Ruleset:
    return Ruleset([], list(rules), container_ip=CONTAINER_IP, appmux_port=APPMUX_PORT)


def received_by_slirp(slirp_end: socket.socket) -> list[bytes]:
    slirp_end.setblocking(False)
    frames = []
    while True:
        try:
            frames.append(slirp_end.recv(70000))
        except BlockingIOError:
            return frames


def test_an_allowed_frame_reaches_slirp_and_a_denied_one_does_not() -> None:
    pump, _, slirp_end = make_pump(ruleset_of(deny("169.254.0.0/16")))
    allowed = ipv4_frame("93.184.216.34", 443)
    denied = ipv4_frame("169.254.169.254", 80)

    assert pump.handle_child_frame(allowed, PACKET_HOST) is True
    assert pump.handle_child_frame(denied, PACKET_HOST) is False

    assert received_by_slirp(slirp_end) == [allowed]


def test_arp_passes_and_ipv6_and_garbage_are_dropped() -> None:
    pump, _, slirp_end = make_pump(ruleset_of())

    assert pump.handle_child_frame(ARP_FRAME, PACKET_HOST) is True
    assert pump.handle_child_frame(IPV6_FRAME, PACKET_HOST) is False
    assert pump.handle_child_frame(b"\x00" * 5, PACKET_HOST) is False

    assert received_by_slirp(slirp_end) == [ARP_FRAME]


def test_a_frame_the_host_sent_itself_is_ignored_and_not_counted() -> None:
    pump, _, slirp_end = make_pump(ruleset_of(deny("169.254.0.0/16")))

    assert pump.handle_child_frame(ipv4_frame("169.254.169.254", 80), PACKET_OUTGOING) is False

    assert received_by_slirp(slirp_end) == []
    assert pump.snapshot()["dropped_total"] == 0  # re-injecting it would loop; it is not a drop


def test_drops_are_counted_per_rule_with_the_destinations_that_were_refused() -> None:
    pump, _, _ = make_pump(
        ruleset_of(deny("169.254.0.0/16"), deny("@container_ip/32", "@appmux_port"))
    )

    for _ in range(3):
        pump.handle_child_frame(ipv4_frame("169.254.169.254", 80), PACKET_HOST)
    pump.handle_child_frame(ipv4_frame(str(CONTAINER_IP), APPMUX_PORT), PACKET_HOST)
    pump.handle_child_frame(IPV6_FRAME, PACKET_HOST)
    snapshot = pump.snapshot()

    assert snapshot["dropped_total"] == 5
    assert snapshot["by_rule"] == {"global:1": 3, "global:2": 1}
    assert snapshot["by_reason"] == {"ipv6": 1}
    assert {
        "rule_id": "global:1",
        "reason": "rule-deny",
        "destination": "169.254.169.254",
    } in snapshot["recent"]
    assert {"rule_id": None, "reason": "ipv6", "destination": None} in snapshot["recent"]


def test_forwarded_frames_and_bytes_are_counted_in_both_directions() -> None:
    pump, port, slirp_end = make_pump(ruleset_of())
    frame = ipv4_frame("93.184.216.34", 443)

    pump.handle_child_frame(frame, PACKET_HOST)
    pump.handle_slirp_frame(b"reply-frame")
    snapshot = pump.snapshot()

    assert port.sent.get_nowait() == b"reply-frame"
    assert (snapshot["frames_to_slirp"], snapshot["bytes_to_slirp"]) == (1, len(frame))
    assert (snapshot["frames_to_child"], snapshot["bytes_to_child"]) == (1, len(b"reply-frame"))
    slirp_end.close()


def test_new_rules_apply_to_the_next_frame_of_a_connection_that_is_already_open() -> None:
    pump, _, slirp_end = make_pump(ruleset_of())
    frame = ipv4_frame("93.184.216.34", 443)
    assert pump.handle_child_frame(frame, PACKET_HOST) is True

    pump.replace_rules(ruleset_of(deny("93.184.216.0/24")))

    assert pump.handle_child_frame(frame, PACKET_HOST) is False
    assert pump.snapshot()["rules_generation"] == 1
    assert received_by_slirp(slirp_end) == [frame]


def test_reloading_from_a_file_swaps_the_rules_and_a_bad_file_keeps_the_old_ones(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rules.json"
    pump, _, _ = make_pump(ruleset_of())
    frame = ipv4_frame("93.184.216.34", 443)
    write_rules_file(
        path,
        app_rules=[deny("93.184.216.0/24")],
        global_rules=[],
        container_ip=CONTAINER_IP,
        appmux_port=APPMUX_PORT,
    )

    assert pump.reload_rules(path) is True
    assert pump.handle_child_frame(frame, PACKET_HOST) is False

    path.write_text("{ half a file")
    assert pump.reload_rules(path) is False
    assert pump.handle_child_frame(frame, PACKET_HOST) is False  # the last good rules stay in force
    assert pump.snapshot()["rules_generation"] == 1


def test_with_the_filter_layer_omitted_every_frame_is_forwarded_and_nothing_is_counted() -> None:
    pump, _, slirp_end = make_pump(ruleset_of(deny("0.0.0.0/0")), filter_frames=False)
    frames = [ipv4_frame("169.254.169.254", 80), IPV6_FRAME]

    assert [pump.handle_child_frame(frame, PACKET_HOST) for frame in frames] == [True, True]

    assert received_by_slirp(slirp_end) == frames
    assert pump.snapshot()["dropped_total"] == 0


def test_the_pump_threads_carry_frames_both_ways_and_end_when_the_cable_is_cut() -> None:
    pump, port, slirp_end = make_pump(ruleset_of(deny("169.254.0.0/16")))
    pump.start()
    try:
        allowed = ipv4_frame("93.184.216.34", 443)
        port.incoming.put((ipv4_frame("169.254.169.254", 80), PACKET_HOST))
        port.incoming.put((allowed, PACKET_HOST))
        slirp_end.settimeout(5)
        assert slirp_end.recv(70000) == allowed  # the denied frame never arrives

        slirp_end.send(b"from-the-internet")
        assert port.sent.get(timeout=5) == b"from-the-internet"
    finally:
        slirp_end.close()  # slirp died: the pump's world-to-child thread sees end of stream
        port.close()
        assert pump.wait(timeout_s=5) is True


def test_an_af_packet_port_reports_the_packet_type_of_each_frame() -> None:
    class StubSocket:
        def __init__(self) -> None:
            self.sent: list[bytes] = []

        def recvfrom(self, size: int) -> tuple[bytes, tuple[object, ...]]:
            return b"frame", ("vpump0", 3, PACKET_OUTGOING, 1, b"\x00" * 6)

        def send(self, data: bytes) -> int:
            self.sent.append(data)
            return len(data)

        def close(self) -> None: ...

    stub = StubSocket()
    port = AfPacketPort(stub)

    assert port.receive() == (b"frame", PACKET_OUTGOING)
    port.send(b"out")
    assert stub.sent == [b"out"]


def test_an_idle_packet_socket_is_a_timeout_and_not_a_closed_port() -> None:
    class IdleSocket:
        def recvfrom(self, size: int) -> tuple[bytes, tuple[object, ...]]:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

        def send(self, data: bytes) -> int:
            return len(data)

        def close(self) -> None: ...

    with pytest.raises(TimeoutError):
        AfPacketPort(IdleSocket()).receive()


def test_the_stats_file_is_replaced_atomically_and_readable_as_json(tmp_path: Path) -> None:
    pump, _, _ = make_pump(ruleset_of(deny("169.254.0.0/16")))
    pump.handle_child_frame(ipv4_frame("169.254.169.254", 80), PACKET_HOST)
    path = tmp_path / "pump-stats.json"

    write_stats_file(path, pump.snapshot())
    write_stats_file(path, pump.snapshot())

    assert json.loads(path.read_text())["by_rule"] == {"global:1": 1}
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["pump-stats.json"]
    assert os.access(path, os.R_OK)


# ---- building the child's network --------------------------------------------------------------


def test_the_child_network_is_built_in_the_order_the_kernel_needs() -> None:
    events: list[str] = []
    messages: list[bytes] = []
    names = {RTM_NEWLINK: "link", RTM_NEWADDR: "address", RTM_NEWROUTE: "route"}

    class RecordingNetlink:
        def request(self, message: bytes, *, what: str) -> None:
            events.append(names[header_of(message)[1]])
            messages.append(message)

    indexes = {"lo": 1, "eth0": 7, "vpump0": 8}
    configure_child_network(
        RecordingNetlink(),
        index_of=indexes.__getitem__,
        disable_offloads=lambda name: events.append(f"offloads {name}"),
        mtu=1400,
    )

    assert events == [
        "link",  # loopback up
        "link",  # the veth pair
        "address",
        "offloads eth0",  # before either end is up, or frames leave with partial checksums
        "offloads vpump0",
        "link",  # eth0 up
        "link",  # vpump0 up
        "route",
    ]
    loopback_up, _veth, address, child_up, pump_up, route = messages
    assert struct.unpack_from("BxHiII", loopback_up, NLMSG_HEADER_BYTES)[2] == 1
    assert struct.unpack_from("BBBBI", address, NLMSG_HEADER_BYTES)[4] == 7
    for message, index in ((child_up, 7), (pump_up, 8)):
        assert struct.unpack_from("BxHiII", message, NLMSG_HEADER_BYTES)[2] == index
        attributes = parse_attributes(message[NLMSG_HEADER_BYTES + IFINFOMSG_BYTES :])
        assert struct.unpack("I", attributes[IFLA_MTU]) == (1400,)
    route_attributes = parse_attributes(route[NLMSG_HEADER_BYTES + RTMSG_BYTES :])
    assert struct.unpack("I", route_attributes[RTA_OIF]) == (7,)


def test_a_netlink_failure_stops_the_setup_before_anything_is_left_half_done() -> None:
    requests: list[str] = []

    class RefusingNetlink:
        def request(self, message: bytes, *, what: str) -> None:
            requests.append(what)
            if len(requests) == 2:
                raise OSError(errno.EPERM, f"cannot {what}")

    with pytest.raises(OSError, match="cannot"):
        configure_child_network(
            RefusingNetlink(),
            index_of={"lo": 1, "eth0": 7, "vpump0": 8}.__getitem__,
            disable_offloads=lambda name: None,
        )

    assert len(requests) == 2  # nothing after the failed veth creation was attempted


# ---- starting slirp4netns ----------------------------------------------------------------------


def write_fake_slirp(directory: Path, body: str) -> Path:
    """An executable that stands in for slirp4netns: it records how it was started."""
    script = directory / "slirp4netns"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, stat, sys, time\n"
        "arguments = sys.argv[1:]\n"
        "ready_fd = int(next(a for a in arguments if a.startswith('--ready-fd=')).split('=')[1])\n"
        "tap_fd = int(arguments[-1])\n"
        f"report = {str(directory / 'report.json')!r}\n"
        "json.dump({'argv': arguments, 'pid': os.getpid(),\n"
        "           'tap_is_socket': stat.S_ISSOCK(os.fstat(tap_fd).st_mode),\n"
        "           'environment': sorted(os.environ)}, open(report, 'w'))\n" + body
    )
    script.chmod(0o755)
    return script


def test_slirp_is_started_on_the_tap_socket_without_the_platform_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPMUX_TEST_PLATFORM_SECRET", "must-not-reach-slirp")
    binary = write_fake_slirp(tmp_path, "os.write(ready_fd, b'1')\ntime.sleep(60)\n")
    slirp_end, pump_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    process = None
    try:
        process = start_slirp(binary, slirp_end, mtu=1400, log_path=tmp_path / "slirp.log")

        report = json.loads((tmp_path / "report.json").read_text())
        assert report["argv"][0] == "--netns-type=tapfd"
        assert "--mtu=1400" in report["argv"]
        assert "--disable-host-loopback" in report["argv"]
        assert report["tap_is_socket"] is True
        assert "APPMUX_TEST_PLATFORM_SECRET" not in report["environment"]
        assert process.poll() is None
    finally:
        if process is not None:
            process.kill()
            process.wait()
        slirp_end.close()
        pump_end.close()


def test_slirp_that_exits_before_it_is_ready_is_an_error_that_quotes_its_log(
    tmp_path: Path,
) -> None:
    binary = write_fake_slirp(
        tmp_path, "sys.stderr.write('tap: Operation not permitted\\n')\nsys.exit(3)\n"
    )
    slirp_end, pump_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(NetworkAttachError, match="exited") as raised:
            start_slirp(binary, slirp_end, mtu=1500, log_path=tmp_path / "slirp.log")

        assert "Operation not permitted" in str(raised.value)
    finally:
        slirp_end.close()
        pump_end.close()


def test_slirp_that_never_reports_ready_is_killed_and_reaped(tmp_path: Path) -> None:
    binary = write_fake_slirp(tmp_path, "time.sleep(60)\n")
    slirp_end, pump_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(NetworkAttachError, match="did not report ready"):
            start_slirp(
                binary,
                slirp_end,
                mtu=1500,
                log_path=tmp_path / "slirp.log",
                ready_timeout_s=1.5,
            )

        pid = json.loads((tmp_path / "report.json").read_text())["pid"]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        slirp_end.close()
        pump_end.close()


def test_a_missing_binary_stops_slirp_from_starting(tmp_path: Path) -> None:
    slirp_end, pump_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        with pytest.raises(SlirpBinaryMissing):
            start_slirp(
                tmp_path / "slirp4netns", slirp_end, mtu=1500, log_path=tmp_path / "slirp.log"
            )
    finally:
        slirp_end.close()
        pump_end.close()
