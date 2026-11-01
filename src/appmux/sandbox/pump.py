"""The child's network path of design 6.2: a veth pair, a frame pump and ``slirp4netns``.

The container has no TUN device and no ``CAP_NET_ADMIN``, so the cable is built from what an
unprivileged user namespace allows:

1. Stage 1 of the launcher (inside the child's network namespace) creates a veth pair over
   rtnetlink. ``eth0`` (10.0.2.100/24, default route via 10.0.2.2) stays with the child;
   ``vpump0`` is the other end.
2. Stage 1 opens an ``AF_PACKET`` socket on ``vpump0`` and passes the descriptor to the launcher's
   supervisor over a unix socket (``SCM_RIGHTS``). The sandbox keeps no copy.
3. The supervisor (outside the sandbox) runs ``slirp4netns --netns-type=tapfd`` on one end of a
   ``SOCK_SEQPACKET`` socketpair, and :class:`FramePump` copies Ethernet frames between the
   ``AF_PACKET`` socket and the other end. Frames the child sends go through
   ``Ruleset.decide`` of ``rules.py``; drops are counted in ``DropCounters``. Nothing here
   re-implements a rule.
4. The supervisor signals that the NAT is attached; stage 1 waits for that before it builds the
   root and starts the child, so the child never runs with a half-built network.

Rules reload on ``SIGHUP`` from a file (:func:`write_rules_file`, :func:`load_ruleset`) with no
polling. A new ruleset replaces the old one as a whole, so a frame never meets half an update, and
a file that does not parse leaves the old rules in force. :class:`PumpReloader` is the other end
of that signal: appmux's side, which rewrites the files of the running children and sends it.

Ported from ``specs/001-appmux-v1/spikes/probe-archive/appmux-probe2/sandbox_child.py`` (the
rtnetlink calls) and ``specs/001-appmux-v1/spikes/scripts/spike-06/egress_stack.py`` and
``egress_pump.py`` (the slirp command and the pump). Two differences from the prototype: the pump
runs as two threads of the supervisor instead of a third process, and rules reload on a signal
instead of a poll.

Only the standard library and ``rules.py`` (hence Pydantic) are imported, and the rtnetlink half
runs in stage 1 before the root is replaced, so the whole package is still on disk then.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import errno
import fcntl
import json
import logging
import os
import select
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Final, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from appmux.sandbox.rules import DropCounters, EgressRule, Ruleset

logger = logging.getLogger(__name__)

# ---- the addresses of the child's world (design 6.2) -------------------------------------------

CHILD_INTERFACE: Final = "eth0"
PUMP_INTERFACE: Final = "vpump0"
CHILD_ADDRESS: Final = "10.0.2.100"
GATEWAY_ADDRESS: Final = "10.0.2.2"
SUBNET_PREFIX: Final = 24
VETH_MTU: Final = 1500
LOOPBACK_INTERFACE: Final = "lo"

# ---- rtnetlink (linux/rtnetlink.h, linux/if_link.h, linux/if_addr.h) ---------------------------

NLM_F_REQUEST: Final = 0x1
NLM_F_ACK: Final = 0x4
NLM_F_EXCL: Final = 0x200
NLM_F_CREATE: Final = 0x400
NLMSG_ERROR: Final = 2
RTM_NEWLINK: Final = 16
RTM_NEWADDR: Final = 20
RTM_NEWROUTE: Final = 24
IFLA_IFNAME: Final = 3
IFLA_MTU: Final = 4
IFLA_LINKINFO: Final = 18
IFLA_INFO_KIND: Final = 1
IFLA_INFO_DATA: Final = 2
VETH_INFO_PEER: Final = 1
IFA_ADDRESS: Final = 1
IFA_LOCAL: Final = 2
RTA_OIF: Final = 4
RTA_GATEWAY: Final = 5
RT_TABLE_MAIN: Final = 254
RTPROT_BOOT: Final = 3
RTN_UNICAST: Final = 1
IFF_UP: Final = 0x1

NLMSG_HEADER_BYTES: Final = 16
ATTRIBUTE_HEADER_BYTES: Final = 4
ERROR_REPLY_BYTES: Final = NLMSG_HEADER_BYTES + 4  # the header plus the signed error code
NETLINK_REPLY_BUFFER_BYTES: Final = 8192
REQUEST_SEQUENCE: Final = 1  # one request is in flight at a time, so the number never matters

# ---- ethtool: veth hands CHECKSUM_PARTIAL frames to AF_PACKET readers ---------------------------

SIOCETHTOOL: Final = 0x8946
IFREQ_BYTES: Final = 40
ETHTOOL_OFFLOADS_OFF: Final = (
    (0x17, "tx checksum"),
    (0x19, "scatter-gather"),
    (0x1F, "tso"),
    (0x24, "gso"),
    (0x2C, "gro"),
)  # ETHTOOL_S{TXCSUM,SG,TSO,GSO,GRO}, set to 0

# ---- AF_PACKET and the pump ---------------------------------------------------------------------

ETH_P_ALL: Final = 3
PACKET_OUTGOING: Final = 4  # frames this host sent itself; re-injecting them would loop
FRAME_BUFFER_BYTES: Final = 70000
PORT_IDLE_TIMEOUT_S: Final = 0.5  # how long a read waits before the pump checks whether to stop

# ---- the handoff and the attach signal ----------------------------------------------------------

HANDOFF_MESSAGE: Final = b"packet-socket"
HANDOFF_BUFFER_BYTES: Final = 4096  # room for a failure reason as well as the marker
ATTACHED_SIGNAL: Final = b"A"
SLIRP_READY_TIMEOUT_S: Final = 10.0
SLIRP_EXIT_WAIT_S: Final = 5.0
LOG_TAIL_BYTES: Final = 400
SLIRP_BINARY_NAME: Final = "slirp4netns"


class NetworkAttachError(RuntimeError):
    """The child's network did not come up; the child must not start."""


class HandoffError(NetworkAttachError):
    """The packet-socket descriptor did not arrive as agreed."""


class SlirpBinaryMissing(NetworkAttachError):
    """``bin/slirp4netns`` is not in the deployment folder or cannot be executed."""


class RulesFileError(ValueError):
    """The egress rules file cannot be read or does not hold a valid ruleset."""


# ---- building rtnetlink messages ---------------------------------------------------------------


def netlink_attribute(kind: int, payload: bytes) -> bytes:
    """One attribute: length (without padding), kind, payload, padded to four bytes."""
    length = ATTRIBUTE_HEADER_BYTES + len(payload)
    return struct.pack("HH", length, kind) + payload + b"\0" * (-length % 4)


def netlink_message(message_type: int, flags: int, body: bytes) -> bytes:
    """A request that asks for an acknowledgement, so every failure comes back as an errno."""
    header = struct.pack(
        "IHHII",
        NLMSG_HEADER_BYTES + len(body),
        message_type,
        flags | NLM_F_REQUEST | NLM_F_ACK,
        REQUEST_SEQUENCE,
        0,
    )
    return header + body


def _interface_info(index: int = 0, flags: int = 0, change: int = 0) -> bytes:
    """``struct ifinfomsg``: family, padding, type, index, flags, change."""
    return struct.pack("BxHiII", 0, 0, index, flags, change)


def _name_attribute(name: str) -> bytes:
    return netlink_attribute(IFLA_IFNAME, name.encode("ascii") + b"\0")


def veth_create_message(child_name: str, pump_name: str) -> bytes:
    """Create a veth pair: ``child_name`` and its peer ``pump_name`` in one request."""
    peer = _interface_info() + _name_attribute(pump_name)
    link_info = netlink_attribute(IFLA_INFO_KIND, b"veth\0") + netlink_attribute(
        IFLA_INFO_DATA, netlink_attribute(VETH_INFO_PEER, peer)
    )
    body = _interface_info() + _name_attribute(child_name)
    body += netlink_attribute(IFLA_LINKINFO, link_info)
    return netlink_message(RTM_NEWLINK, NLM_F_CREATE | NLM_F_EXCL, body)


def address_message(index: int, address: str, prefix_length: int) -> bytes:
    """Give interface ``index`` an IPv4 address (``struct ifaddrmsg`` then the address twice)."""
    packed = socket.inet_aton(address)
    body = struct.pack("BBBBI", socket.AF_INET, prefix_length, 0, 0, index)
    body += netlink_attribute(IFA_LOCAL, packed) + netlink_attribute(IFA_ADDRESS, packed)
    return netlink_message(RTM_NEWADDR, NLM_F_CREATE | NLM_F_EXCL, body)


def link_up_message(index: int, *, mtu: int | None = None) -> bytes:
    """Bring interface ``index`` up, and set its MTU when one is given."""
    body = _interface_info(index, IFF_UP, IFF_UP)
    if mtu is not None:
        body += netlink_attribute(IFLA_MTU, struct.pack("I", mtu))
    return netlink_message(RTM_NEWLINK, 0, body)


def default_route_message(index: int, gateway: str) -> bytes:
    """Route 0.0.0.0/0 through ``gateway`` on interface ``index`` (``struct rtmsg``)."""
    body = struct.pack(
        "BBBBBBBBI", socket.AF_INET, 0, 0, 0, RT_TABLE_MAIN, RTPROT_BOOT, 0, RTN_UNICAST, 0
    )
    body += netlink_attribute(RTA_GATEWAY, socket.inet_aton(gateway))
    body += netlink_attribute(RTA_OIF, struct.pack("I", index))
    return netlink_message(RTM_NEWROUTE, NLM_F_CREATE | NLM_F_EXCL, body)


def check_acknowledgement(reply: bytes, *, what: str) -> None:
    """Raise ``OSError`` with the kernel's errno unless ``reply`` acknowledges the request."""
    if len(reply) < ERROR_REPLY_BYTES:
        raise OSError(errno.EPROTO, f"rtnetlink gave no usable reply when asked to {what}")
    _length, reply_type, _flags, _sequence, _pid = struct.unpack_from("IHHII", reply)
    if reply_type != NLMSG_ERROR:
        raise OSError(errno.EPROTO, f"rtnetlink sent a reply of type {reply_type} to {what}")
    (code,) = struct.unpack_from("i", reply, NLMSG_HEADER_BYTES)
    if code != 0:
        raise OSError(-code, f"cannot {what}: {os.strerror(-code)}")


class NetlinkRequester(Protocol):
    """Sends one rtnetlink request and returns when the kernel has acknowledged it."""

    def request(self, message: bytes, *, what: str) -> None: ...


class RtnetlinkSocket:
    """A ``NETLINK_ROUTE`` socket in the network namespace of the process that opens it."""

    def __init__(self) -> None:
        self._socket = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
        try:
            self._socket.bind((0, 0))
        except OSError:
            self._socket.close()
            raise

    def request(self, message: bytes, *, what: str) -> None:
        self._socket.send(message)
        check_acknowledgement(self._socket.recv(NETLINK_REPLY_BUFFER_BYTES), what=what)

    def close(self) -> None:
        self._socket.close()


def disable_offloads(interface: str) -> None:
    """Turn the checksum and segmentation offloads of ``interface`` off.

    Without this, veth passes frames with a partial checksum to the ``AF_PACKET`` reader and they
    leave slirp4netns unusable. A kernel that refuses one of them is logged and the rest are still
    tried: a frame that fails its checksum is dropped by the peer, which is a broken connection
    and not an escape.
    """
    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for command, label in ETHTOOL_OFFLOADS_OFF:
            # ``struct ethtool_value { __u32 cmd; __u32 data; }`` with data 0 = off; the ifreq
            # carries a pointer to it.
            value = ctypes.create_string_buffer(struct.pack("II", command, 0))
            request = struct.pack("16sP", interface.encode("ascii"), ctypes.addressof(value))
            request = request.ljust(IFREQ_BYTES, b"\0")
            try:
                fcntl.ioctl(control.fileno(), SIOCETHTOOL, request)
            except OSError as exc:
                logger.warning("cannot turn the %s offload off on %s: %s", label, interface, exc)
    finally:
        control.close()


def configure_child_network(
    netlink: NetlinkRequester,
    *,
    index_of: Callable[[str], int],
    disable_offloads: Callable[[str], None],
    mtu: int = VETH_MTU,
) -> None:
    """Bring up ``lo``, create the veth pair, address and route ``eth0``.

    The order is the kernel's: the pair exists before it is addressed; the offloads are off
    before either end is up; the route needs its interface up. Any failure raises and nothing
    later is attempted, so the child never starts with a half-built network.
    """
    netlink.request(
        link_up_message(index_of(LOOPBACK_INTERFACE)), what="bring the loopback interface up"
    )
    netlink.request(
        veth_create_message(CHILD_INTERFACE, PUMP_INTERFACE), what="create the veth pair"
    )
    child_index = index_of(CHILD_INTERFACE)
    pump_index = index_of(PUMP_INTERFACE)
    netlink.request(
        address_message(child_index, CHILD_ADDRESS, SUBNET_PREFIX),
        what=f"give {CHILD_INTERFACE} its address",
    )
    for name in (CHILD_INTERFACE, PUMP_INTERFACE):
        disable_offloads(name)
    for name, index in ((CHILD_INTERFACE, child_index), (PUMP_INTERFACE, pump_index)):
        netlink.request(link_up_message(index, mtu=mtu), what=f"bring {name} up")
    netlink.request(
        default_route_message(child_index, GATEWAY_ADDRESS), what="add the default route"
    )


def open_packet_socket(interface: str) -> socket.socket:
    """An ``AF_PACKET`` socket that sees every frame on ``interface`` (needs ``CAP_NET_RAW``)."""
    packet_socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    try:
        packet_socket.bind((interface, 0))
    except OSError:
        packet_socket.close()
        raise
    return packet_socket


# ---- handing the packet socket to the supervisor -----------------------------------------------


def send_packet_socket(channel: socket.socket, packet_socket: socket.socket) -> None:
    """Pass the descriptor over ``channel`` (``SCM_RIGHTS``); the caller closes its own copy."""
    socket.send_fds(channel, [HANDOFF_MESSAGE], [packet_socket.fileno()])


def receive_packet_socket(channel: socket.socket) -> socket.socket:
    """Receive exactly one descriptor from ``channel`` and wrap it as a socket."""
    message, descriptors, _flags, _address = socket.recv_fds(channel, HANDOFF_BUFFER_BYTES, 2)
    if len(descriptors) != 1:
        for descriptor in descriptors:
            os.close(descriptor)
        # A stage 1 that failed before the handoff sends its reason in place of the descriptor.
        reason = message.decode("utf-8", errors="replace").strip()
        raise HandoffError(
            f"the sandbox's handoff carried {len(descriptors)} descriptors instead of the one "
            "packet-socket descriptor; the child is not started" + (f": {reason}" if reason else "")
        )
    return socket.socket(fileno=descriptors[0])


def signal_attached(channel: socket.socket) -> None:
    """Tell stage 1 that slirp4netns is attached and the pump is running."""
    channel.sendall(ATTACHED_SIGNAL)


def wait_until_attached(channel: socket.socket, *, timeout_s: float) -> None:
    """Block until the supervisor signals; fail closed on a timeout, a hang-up or noise."""
    channel.settimeout(timeout_s)
    try:
        received = channel.recv(len(ATTACHED_SIGNAL))
    except TimeoutError as exc:
        raise NetworkAttachError(
            f"the network was not attached within {timeout_s:g} s; the child is not started"
        ) from exc
    except ConnectionError as exc:
        raise NetworkAttachError("the supervisor closed the handoff before attaching") from exc
    if not received:
        raise NetworkAttachError("the supervisor closed the handoff before it attached the network")
    if received != ATTACHED_SIGNAL:
        raise NetworkAttachError(f"unexpected bytes {received!r} on the handoff")


# ---- slirp4netns --------------------------------------------------------------------------------


def slirp_binary_path(module_file: Path) -> Path:
    """``bin/slirp4netns`` in the deployment folder, which holds ``appmux/`` and ``bin/``."""
    return module_file.parent.parent.parent / "bin" / SLIRP_BINARY_NAME


def require_slirp_binary(binary: Path) -> Path:
    """Return ``binary`` once it can be executed; otherwise say what to run.

    The first container start found the file present but not executable. The likely cause (not
    read from the file's mode there) is that a workspace folder does not keep the mode bits of
    what is uploaded to it, so the owner's bit is restored here, and nothing else gains one.
    """
    if not binary.is_file():
        raise SlirpBinaryMissing(
            f"{binary} is missing: run scripts/fetch-slirp4netns.sh and then "
            "scripts/assemble-deploy.sh, and deploy again"
        )
    if os.access(binary, os.X_OK):
        return binary
    try:
        binary.chmod(stat.S_IMODE(binary.stat().st_mode) | stat.S_IXUSR)
    except OSError as exc:
        raise SlirpBinaryMissing(
            f"{binary} is not executable and its mode cannot be changed ({exc.strerror}): "
            "deploy the folder again as the user that runs appmux"
        ) from exc
    if not os.access(binary, os.X_OK):
        raise SlirpBinaryMissing(
            f"{binary} is not executable even with its mode set: the folder may be mounted "
            "so that nothing in it can run"
        )
    return binary


def slirp_command(binary: Path, *, slirp_fd: int, ready_fd: int, mtu: int) -> list[str]:
    """slirp4netns on the tap descriptor, with the host's loopback out of reach and no IPv6."""
    return [
        str(binary),
        "--netns-type=tapfd",
        f"--ready-fd={ready_fd}",
        f"--mtu={mtu}",
        "--disable-host-loopback",
        str(slirp_fd),
    ]


def log_tail(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
            return handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def start_slirp(
    binary: Path,
    slirp_socket: socket.socket,
    *,
    mtu: int,
    log_path: Path,
    ready_timeout_s: float = SLIRP_READY_TIMEOUT_S,
) -> subprocess.Popen[bytes]:
    """Start slirp4netns on ``slirp_socket`` and return once it reports ready.

    The process gets an empty environment, so none of the platform's credentials reach it. A
    process that exits, or that is not ready in time, is killed and reaped, and the error quotes
    the end of its log.
    """
    require_slirp_binary(binary)
    ready_read, ready_write = os.pipe()
    try:
        command = slirp_command(
            binary, slirp_fd=slirp_socket.fileno(), ready_fd=ready_write, mtu=mtu
        )
        try:
            with log_path.open("ab") as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env={},
                    pass_fds=(slirp_socket.fileno(), ready_write),
                    start_new_session=True,
                )
        finally:
            os.close(ready_write)
        readable, _, _ = select.select([ready_read], [], [], ready_timeout_s)
        signalled = os.read(ready_read, 8) if readable else None
    finally:
        os.close(ready_read)
    if signalled:
        return process
    if signalled is None:
        process.kill()
        problem = f"{SLIRP_BINARY_NAME} did not report ready within {ready_timeout_s:g} s"
    else:
        try:
            status = process.wait(timeout=SLIRP_EXIT_WAIT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            status = process.wait()
        problem = f"{SLIRP_BINARY_NAME} exited with status {status} before it was ready"
    process.wait()
    tail = log_tail(log_path)
    raise NetworkAttachError(f"{problem}" + (f": {tail}" if tail else ""))


# ---- the rules file ----------------------------------------------------------------------------


class _RulesDocument(BaseModel):
    """What the supervisor reads on ``SIGHUP``: both lists and the values they resolve against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    app_rules: list[EgressRule]
    global_rules: list[EgressRule]
    container_ip: IPv4Address
    appmux_port: int = Field(ge=1, le=65535)


def _write_json_atomically(path: Path, document: object) -> None:
    """Replace ``path`` whole, so a reader never sees half a file; the file is owner-only."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.replace(temporary, path)
    except BaseException:
        # Whatever went wrong, do not leave a half-written temporary file next to the real one.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def write_rules_file(
    path: Path,
    *,
    app_rules: Sequence[EgressRule],
    global_rules: Sequence[EgressRule],
    container_ip: IPv4Address,
    appmux_port: int,
) -> None:
    """Write the rules the pump should enforce; the lists are in the order they are matched."""
    document = _RulesDocument(
        app_rules=list(app_rules),
        global_rules=list(global_rules),
        container_ip=container_ip,
        appmux_port=appmux_port,
    )
    _write_json_atomically(path, document.model_dump(mode="json"))


def load_ruleset(path: Path) -> Ruleset:
    """Read the rules file into a :class:`Ruleset`, or raise :class:`RulesFileError`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesFileError(f"cannot read the egress rules file {path}: {exc}") from exc
    try:
        document = _RulesDocument.model_validate_json(text)
    except ValidationError as exc:
        raise RulesFileError(f"the egress rules file {path} is not valid: {exc}") from exc
    return Ruleset(
        document.app_rules,
        document.global_rules,
        container_ip=document.container_ip,
        appmux_port=document.appmux_port,
    )


# ---- reloading the rules of the running children ------------------------------------------------


class PumpReloadError(RuntimeError):
    """The rules of some running children could not be rewritten.

    ``slugs`` names those children (empty when nothing could be tried). The message names slugs
    and never a path, because it may be shown to a client; the causes are chained for the log.
    """

    def __init__(self, message: str, *, slugs: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.slugs = tuple(slugs)


class RulesSignal(Protocol):
    """What a child's supervisor handle offers: ``SandboxHandle.reload_rules`` of the launcher."""

    def reload_rules(self) -> bool:
        """Ask the supervisor to read its rules file; ``False`` if it is no longer running."""
        ...


class ClosedTargetValues(Protocol):
    """What ``@container_ip`` and ``@appmux_port`` stand for in this running App."""

    @property
    def container_ip(self) -> IPv4Address: ...

    @property
    def appmux_port(self) -> int: ...


@dataclass(frozen=True, slots=True)
class PumpTarget:
    """One running child whose pump can be told to read its rules again."""

    slug: str
    rules_path: Path
    handle: RulesSignal


class PumpReloader:
    """The ``EgressReloader`` of the egress routes: rewrite the rules files, then signal the pumps.

    ``await reloader(slug)`` serves one child, ``await reloader(None)`` every running child (the
    global list changed). For each child it writes the file with that child's own list and the
    global list, as they are when the call is made, and only then signals the supervisor, so the
    pump never reads a file older than the call. A child that is not running is not an error: it
    reads its lists from the registry when it starts. A child whose file cannot be written is
    skipped and named in the error that ends the call; the others are still served.

    A signal is not an acknowledgement: this does not wait for the pump to report the new rules
    (``rules_generation`` in its stats), and a pump that rejects the file keeps the old rules and
    logs it in the supervisor's log. Only a container can show either happen.

    The four sources are injected so that this module imports nothing of the reconciler or the
    registry (module boundaries): ``running`` the live children, ``app_rules`` a child's own list,
    ``global_rules`` the global list, ``closed_targets`` the values the ``@`` names resolve to
    (``None`` while the container's address is not known).
    """

    def __init__(
        self,
        *,
        running: Callable[[], Sequence[PumpTarget]],
        app_rules: Callable[[str], Sequence[EgressRule]],
        global_rules: Callable[[], Sequence[EgressRule]],
        closed_targets: Callable[[], ClosedTargetValues | None],
    ) -> None:
        self._running = running
        self._app_rules = app_rules
        self._global_rules = global_rules
        self._closed_targets = closed_targets

    async def __call__(self, app_slug: str | None) -> None:
        targets = [t for t in self._running() if app_slug is None or t.slug == app_slug]
        if not targets:
            return
        closed = self._closed_targets()
        if closed is None:
            raise PumpReloadError(
                "the container's address is not known yet, so no rules file can be written; "
                "try again in a moment"
            )
        global_rules = list(self._global_rules())
        failures: dict[str, Exception] = {}
        for target in targets:
            try:
                await asyncio.to_thread(
                    write_rules_file,
                    target.rules_path,
                    app_rules=list(self._app_rules(target.slug)),
                    global_rules=global_rules,
                    container_ip=closed.container_ip,
                    appmux_port=closed.appmux_port,
                )
            except (OSError, ValueError) as exc:
                failures[target.slug] = exc
                continue  # not signalled: the pump must not be told to read a file that is stale
            target.handle.reload_rules()  # False: it has ended, and reads its rules at start
        if failures:
            raise PumpReloadError(
                f"the egress rules could not be written for: {', '.join(failures)}; "
                "save the list again to retry",
                slugs=list(failures),
            ) from ExceptionGroup("rules files not written", list(failures.values()))


# ---- the frame pump ----------------------------------------------------------------------------


class DropSampleStats(TypedDict):
    rule_id: str | None
    reason: str
    destination: str | None


class PumpStats(TypedDict):
    """What the supervisor writes to the stats file; the metrics read it."""

    dropped_total: int
    by_rule: dict[str, int]
    by_reason: dict[str, int]
    recent: list[DropSampleStats]
    frames_to_slirp: int
    bytes_to_slirp: int
    frames_to_child: int
    bytes_to_child: int
    rules_generation: int


def write_stats_file(path: Path, stats: PumpStats) -> None:
    _write_json_atomically(path, stats)


class PacketPort(Protocol):
    """The child's side of the cable: whole Ethernet frames in and out."""

    def receive(self) -> tuple[bytes, int] | None:
        """The next frame and its packet type; ``None`` when the port is closed.

        Raises ``TimeoutError`` after a short idle period so the pump can notice a stop request.
        """
        ...

    def send(self, frame: bytes) -> None: ...


class PacketSocket(Protocol):
    """The part of a socket that :class:`AfPacketPort` uses."""

    def recvfrom(self, bufsize: int, /) -> tuple[bytes, Any]: ...

    def send(self, data: bytes, /) -> int: ...

    def close(self) -> None: ...


class AfPacketPort:
    """A :class:`PacketPort` on an ``AF_PACKET`` socket.

    The socket must have a receive timeout (see :func:`packet_port_for`): closing a descriptor
    does not wake a thread that is blocked reading it.
    """

    def __init__(self, packet_socket: PacketSocket) -> None:
        self._socket = packet_socket

    def receive(self) -> tuple[bytes, int] | None:
        try:
            frame, address = self._socket.recvfrom(FRAME_BUFFER_BYTES)
        except BlockingIOError as exc:  # SO_RCVTIMEO expired
            raise TimeoutError("no frame arrived within the idle period") from exc
        # The address of an AF_PACKET socket is (interface, protocol, packet type, ...).
        return frame, int(address[2])

    def send(self, frame: bytes) -> None:
        self._socket.send(frame)

    def close(self) -> None:
        self._socket.close()


def packet_port_for(packet_socket: socket.socket) -> AfPacketPort:
    """Give the socket a receive timeout (``SO_RCVTIMEO``) and wrap it as a port."""
    seconds = int(PORT_IDLE_TIMEOUT_S)
    microseconds = int((PORT_IDLE_TIMEOUT_S - seconds) * 1_000_000)
    packet_socket.setsockopt(
        socket.SOL_SOCKET, socket.SO_RCVTIMEO, struct.pack("ll", seconds, microseconds)
    )
    return AfPacketPort(packet_socket)


class FramePump:
    """Copies frames between the child's port and slirp4netns, filtering what the child sends.

    With ``filter_frames`` false (the integration test of fail-closed self checks) every frame is
    forwarded and none is counted.
    """

    def __init__(
        self,
        port: PacketPort,
        slirp_socket: socket.socket,
        rules: Ruleset,
        *,
        filter_frames: bool = True,
    ) -> None:
        self._port = port
        self._slirp = slirp_socket
        self._rules = rules
        self._filter_frames = filter_frames
        self._counters = DropCounters()
        self._lock = threading.Lock()  # guards the counters below and the snapshot of them
        self._frames_to_slirp = 0
        self._bytes_to_slirp = 0
        self._frames_to_child = 0
        self._bytes_to_child = 0
        self._rules_generation = 0
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- decisions --

    def handle_child_frame(self, frame: bytes, packet_type: int) -> bool:
        """Decide one frame the child sent; forward it to slirp when it may leave."""
        if packet_type == PACKET_OUTGOING:
            return False  # appmux's own transmission on the veth; re-injecting it would loop
        if self._filter_frames:
            decision = self._rules.decide(frame)
            with self._lock:
                self._counters.record(decision, frame)
            if not decision.forward:
                return False
        self._slirp.send(frame)
        with self._lock:
            self._frames_to_slirp += 1
            self._bytes_to_slirp += len(frame)
        return True

    def handle_slirp_frame(self, frame: bytes) -> None:
        """Pass a frame from the outside world to the child; replies are not filtered."""
        self._port.send(frame)
        with self._lock:
            self._frames_to_child += 1
            self._bytes_to_child += len(frame)

    # -- rules --

    def replace_rules(self, rules: Ruleset) -> None:
        """Apply new rules to the next frame, including frames of connections already open."""
        self._rules = rules
        with self._lock:
            self._rules_generation += 1

    def reload_rules(self, path: Path) -> bool:
        """Load ``path``; a file that does not parse leaves the last good rules in force."""
        try:
            rules = load_ruleset(path)
        except RulesFileError as exc:
            logger.warning("keeping the previous egress rules: %s", exc)
            return False
        self.replace_rules(rules)
        return True

    # -- stats --

    def snapshot(self) -> PumpStats:
        with self._lock:
            counters = self._counters
            return PumpStats(
                dropped_total=counters.dropped_total,
                by_rule=dict(counters.by_rule),
                by_reason={str(reason): count for reason, count in counters.by_reason.items()},
                recent=[
                    DropSampleStats(
                        rule_id=sample.rule_id,
                        reason=str(sample.reason),
                        destination=None if sample.destination is None else str(sample.destination),
                    )
                    for sample in counters.recent
                ],
                frames_to_slirp=self._frames_to_slirp,
                bytes_to_slirp=self._bytes_to_slirp,
                frames_to_child=self._frames_to_child,
                bytes_to_child=self._bytes_to_child,
                rules_generation=self._rules_generation,
            )

    # -- threads --

    def start(self) -> None:
        """Start one thread per direction."""
        self._threads = [
            threading.Thread(target=self._child_to_slirp, name="pump-to-slirp", daemon=True),
            threading.Thread(target=self._slirp_to_child, name="pump-to-child", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Ask both directions to end; a thread notices within the idle period."""
        self._stopping.set()
        with contextlib.suppress(OSError):  # already shut down or closed by slirp's exit
            self._slirp.shutdown(socket.SHUT_RDWR)

    def wait(self, *, timeout_s: float) -> bool:
        """Wait for both threads to end; ``True`` if they did."""
        deadline = time.monotonic() + timeout_s
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def _child_to_slirp(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    received = self._port.receive()
                except TimeoutError:
                    continue
                if received is None:
                    break
                self.handle_child_frame(*received)
        except OSError as exc:
            self._log_end("child to slirp", exc)
        finally:
            self.stop()

    def _slirp_to_child(self) -> None:
        try:
            while not self._stopping.is_set():
                frame = self._slirp.recv(FRAME_BUFFER_BYTES)
                if not frame:
                    break  # slirp4netns went away
                self.handle_slirp_frame(frame)
        except OSError as exc:
            self._log_end("slirp to child", exc)
        finally:
            self.stop()

    def _log_end(self, direction: str, exc: OSError) -> None:
        if not self._stopping.is_set():
            logger.warning("the %s pump ended: %s", direction, exc)
