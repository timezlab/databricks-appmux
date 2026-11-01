"""The egress rule engine of design 6.3: which frames a child may send out.

Pure: no I/O, no clock, no environment. The pump hands every Ethernet frame the child sends to
``Ruleset.decide`` and forwards or drops it by the answer; ``DropCounters`` keeps the per-rule
counts the dashboard shows. A ``Ruleset`` never changes after it is built: a rule edit builds a
new one, so a frame never meets half of an update.

Evaluation order (design 6.3): ``10.0.2.0/24`` (the cable's far end, where slirp's gateway and
DNS live) is always allowed; then the app's own rules, then the global rules, in list order and
first match wins; a frame that matches nothing is allowed. A frame that cannot be parsed, any
IPv6 frame and any other non-IP frame (ARP excepted) is dropped, so a parse failure never opens
a path. This is a port of ``Ruleset`` and ``decide`` from the spike 6 prototype.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv4Network
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, field_validator

CONTAINER_IP_PLACEHOLDER = "@container_ip"
APPMUX_PORT_PLACEHOLDER = "@appmux_port"
LINK_LOCAL_NETWORK = IPv4Network("169.254.0.0/16")
# Slirp's own gateway and DNS: always reachable, or a name lookup fails under an allow-list.
VIRTUAL_SUBNET = IPv4Network("10.0.2.0/24")

TCP, UDP = 6, 17
PORT_PROTOCOLS = frozenset({TCP, UDP})
MIN_PORT, MAX_PORT = 1, 65535
ANY_PORT = "any"

ETHERTYPE_ARP, ETHERTYPE_IPV4, ETHERTYPE_IPV6 = b"\x08\x06", b"\x08\x00", b"\x86\xdd"
ETHERNET_HEADER_BYTES = 14
IPV4_MIN_HEADER_BYTES = 20
PORTS_BYTES = 4  # source and destination port, the first four bytes of a TCP or UDP header
FRAGMENT_BITS = 0x3FFF  # the more-fragments flag plus the 13-bit offset
RECENT_DROPS_KEPT = 60

_CONTAINER_IP_CIDR = re.compile(rf"{CONTAINER_IP_PLACEHOLDER}(?:/([0-9]{{1,2}}))?", re.ASCII)
# ASCII digits only, no leading zero: the stored string is the canonical spelling of the port.
_PORT_NUMBER = re.compile(r"[1-9][0-9]{0,4}", re.ASCII)


class Reason(StrEnum):
    """Why a frame was forwarded or dropped; the value is what metrics and logs show."""

    RUNT = "runt"
    ARP = "arp"
    IPV6 = "ipv6"
    NON_IP = "non-ip"
    MALFORMED = "malformed"
    VIRTUAL_SUBNET = "virtual-subnet"
    FRAGMENT = "fragment"
    RULE_ALLOW = "rule-allow"
    RULE_DENY = "rule-deny"
    DEFAULT_ALLOW = "default-allow"


@dataclass(frozen=True, slots=True)
class Decision:
    """The verdict on one frame.

    ``rule_index`` counts the app's list and then the global list; ``rule_id`` is the label the
    metrics use (``app:2`` is the second rule of the app's list, ``global:1`` the first global
    rule). Both are ``None`` when no rule made the decision.
    """

    forward: bool
    reason: Reason
    rule_index: int | None = None
    rule_id: str | None = None


_RUNT = Decision(forward=False, reason=Reason.RUNT)
_ARP = Decision(forward=True, reason=Reason.ARP)
_IPV6 = Decision(forward=False, reason=Reason.IPV6)
_NON_IP = Decision(forward=False, reason=Reason.NON_IP)
_MALFORMED = Decision(forward=False, reason=Reason.MALFORMED)
_VIRTUAL_SUBNET = Decision(forward=True, reason=Reason.VIRTUAL_SUBNET)
_FRAGMENT = Decision(forward=False, reason=Reason.FRAGMENT)
_DEFAULT_ALLOW = Decision(forward=True, reason=Reason.DEFAULT_ALLOW)


class _ContainerNetwork(NamedTuple):
    """The ``@container_ip`` placeholder with its prefix length, before the address is known."""

    prefix: int


def _parse_destination(text: str) -> IPv4Network | _ContainerNetwork:
    if text.startswith("@"):
        match = _CONTAINER_IP_CIDR.fullmatch(text)
        if match is None:
            raise ValueError(
                f"{text!r} is not a destination: the only placeholder allowed in a cidr is "
                f"{CONTAINER_IP_PLACEHOLDER} (put {APPMUX_PORT_PLACEHOLDER} in the port field)"
            )
        prefix = 32 if match[1] is None else int(match[1])
        if prefix > 32:
            raise ValueError(f"{text!r}: an IPv4 prefix length is at most 32")
        return _ContainerNetwork(prefix)
    try:
        network = IPv4Network(text, strict=True)
    except ValueError as exc:
        raise ValueError(
            f"{text!r} is not an IPv4 CIDR with no host bits set "
            f"(IPv6 is not supported in v1): {exc}"
        ) from exc
    return network


def _parse_port(text: str) -> int | None:
    """``None`` for any port, else the port number; ``@appmux_port`` is not here."""
    if text == ANY_PORT:
        return None
    if _PORT_NUMBER.fullmatch(text) is None or int(text) > MAX_PORT:
        raise ValueError(
            f"{text!r} is not a port: write {ANY_PORT}, a number from {MIN_PORT} to {MAX_PORT} "
            f"or {APPMUX_PORT_PLACEHOLDER}"
        )
    return int(text)


@dataclass(frozen=True, slots=True)
class ResolvedRule:
    """A rule with its placeholders replaced; ``port`` is ``None`` for any port."""

    allow: bool
    network: IPv4Network
    port: int | None


class EgressRule(BaseModel):
    """One stored rule: the ``EgressRule`` of the admin API contract (data model 4.1 and 4.2).

    Every field is required, as in the contract; a key the contract does not list is refused.
    This is the one definition of the shape: the settings and the registry import it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    action: Literal["allow", "deny"]
    cidr: str  # an IPv4 CIDR with no host bits set, or @container_ip[/n]
    port: str  # any, a single port such as 443, or @appmux_port
    comment: str  # free text for the next reader; may be empty

    @field_validator("cidr")
    @classmethod
    def _cidr_is_a_destination(cls, value: str) -> str:
        _parse_destination(value)
        return value

    @field_validator("port")
    @classmethod
    def _port_is_a_port_spec(cls, value: str) -> str:
        if value != APPMUX_PORT_PLACEHOLDER:
            _parse_port(value)
        return value

    def resolve(self, *, container_ip: IPv4Address, appmux_port: int) -> ResolvedRule:
        """Replace ``@container_ip`` and ``@appmux_port`` with the values known at start."""
        destination = _parse_destination(self.cidr)
        if isinstance(destination, _ContainerNetwork):
            destination = IPv4Network(f"{container_ip}/{destination.prefix}", strict=False)
        port = appmux_port if self.port == APPMUX_PORT_PLACEHOLDER else _parse_port(self.port)
        return ResolvedRule(allow=self.action == "allow", network=destination, port=port)


class _CompiledRule(NamedTuple):
    network: int
    mask: int
    port: int | None


class Ruleset:
    """The rules in force for one child: its own list, then the global list, then allow."""

    def __init__(
        self,
        app_rules: Sequence[EgressRule],
        global_rules: Sequence[EgressRule],
        *,
        container_ip: IPv4Address,
        appmux_port: int,
    ) -> None:
        listed = [(f"app:{number}", rule) for number, rule in enumerate(app_rules, start=1)]
        listed += [(f"global:{number}", rule) for number, rule in enumerate(global_rules, start=1)]
        resolved = [
            rule.resolve(container_ip=container_ip, appmux_port=appmux_port) for _, rule in listed
        ]
        self.rule_ids: tuple[str, ...] = tuple(rule_id for rule_id, _ in listed)
        self._compiled = tuple(
            _CompiledRule(int(rule.network.network_address), int(rule.network.netmask), rule.port)
            for rule in resolved
        )
        self._decisions = tuple(
            Decision(
                forward=rule.allow,
                reason=Reason.RULE_ALLOW if rule.allow else Reason.RULE_DENY,
                rule_index=index,
                rule_id=rule_id,
            )
            for index, (rule_id, rule) in enumerate(zip(self.rule_ids, resolved, strict=True))
        )
        self._has_port_rules = any(rule.port is not None for rule in resolved)
        self._virtual_network = int(VIRTUAL_SUBNET.network_address)
        self._virtual_mask = int(VIRTUAL_SUBNET.netmask)

    def decide(self, frame: bytes) -> Decision:
        """Forward or drop one frame the child sent; a frame that does not parse is dropped."""
        if len(frame) < ETHERNET_HEADER_BYTES:
            return _RUNT
        ethertype = frame[12:14]
        if ethertype == ETHERTYPE_ARP:
            return _ARP
        if ethertype == ETHERTYPE_IPV6:
            return _IPV6
        if ethertype != ETHERTYPE_IPV4:
            return _NON_IP
        if len(frame) < ETHERNET_HEADER_BYTES + IPV4_MIN_HEADER_BYTES or frame[14] >> 4 != 4:
            return _MALFORMED
        header_bytes = (frame[14] & 0x0F) * 4
        total_bytes = int.from_bytes(frame[16:18], "big")
        if (
            header_bytes < IPV4_MIN_HEADER_BYTES
            or total_bytes < header_bytes
            or ETHERNET_HEADER_BYTES + total_bytes > len(frame)
        ):
            return _MALFORMED
        destination = int.from_bytes(frame[30:34], "big")
        if destination & self._virtual_mask == self._virtual_network:
            return _VIRTUAL_SUBNET
        protocol = frame[23]
        destination_port: int | None = None
        if self._has_port_rules:
            # Ports are in the first fragment only, so with port rules every fragment is dropped
            # (conservative: with fragments passed, slirp would reassemble them).
            if int.from_bytes(frame[20:22], "big") & FRAGMENT_BITS:
                return _FRAGMENT
            if protocol in PORT_PROTOCOLS:
                if total_bytes < header_bytes + PORTS_BYTES:
                    return _MALFORMED
                port_at = ETHERNET_HEADER_BYTES + header_bytes + 2
                destination_port = int.from_bytes(frame[port_at : port_at + 2], "big")
        for index, (network, mask, port) in enumerate(self._compiled):
            if destination & mask != network:
                continue
            if port is not None and port != destination_port:
                continue
            return self._decisions[index]
        return _DEFAULT_ALLOW


def destination_of(frame: bytes) -> IPv4Address | None:
    """The destination address of an IPv4 frame, for the sample destinations of a drop."""
    if len(frame) < ETHERNET_HEADER_BYTES + IPV4_MIN_HEADER_BYTES:
        return None
    if frame[12:14] != ETHERTYPE_IPV4:
        return None
    return IPv4Address(frame[30:34])


@dataclass(frozen=True, slots=True)
class DropSample:
    """One recent drop, for the dashboard."""

    rule_id: str | None
    reason: Reason
    destination: IPv4Address | None


class DropCounters:
    """Counts dropped frames per rule, or per reason when no rule made the drop."""

    def __init__(self, samples_kept: int = RECENT_DROPS_KEPT) -> None:
        self.dropped_total = 0
        self.by_rule: Counter[str] = Counter()
        self.by_reason: Counter[Reason] = Counter()
        self.recent: deque[DropSample] = deque(maxlen=samples_kept)

    def record(self, decision: Decision, frame: bytes) -> None:
        """Count one decision; a forwarded frame is not counted."""
        if decision.forward:
            return
        self.dropped_total += 1
        if decision.rule_id is None:
            self.by_reason[decision.reason] += 1
        else:
            self.by_rule[decision.rule_id] += 1
        self.recent.append(DropSample(decision.rule_id, decision.reason, destination_of(frame)))


class EgressRulesRefused(ValueError):
    """The editor refuses a rule list; ``problems`` holds one message per offending rule."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


def _closed_targets_reached(
    allow: ResolvedRule, *, container_ip: IPv4Address, appmux_port: int
) -> list[str]:
    reached: list[str] = []
    holds_container = container_ip in allow.network
    if holds_container:
        reached.append(CONTAINER_IP_PLACEHOLDER)
    if holds_container and allow.port in (None, appmux_port):
        reached.append(APPMUX_PORT_PLACEHOLDER)
    if allow.network.overlaps(LINK_LOCAL_NETWORK):
        reached.append(str(LINK_LOCAL_NETWORK))
    return reached


def refuse_overlapping_allows(
    rules: Sequence[EgressRule], *, container_ip: IPv4Address, appmux_port: int
) -> None:
    """Refuse a list holding an ``allow`` that overlaps a target ``self_check()`` keeps closed.

    The closed targets are ``@container_ip`` (the container's own address, on any port),
    ``@appmux_port`` (appmux's port on that address) and ``169.254.0.0/16``. A ``deny`` may
    cover them; an ``allow`` may not, wherever it sits in the list, because the app's rules are
    evaluated before the global ones and would reopen what the global list closes. The message
    names the placeholder and never prints the container's address.
    """
    problems: list[str] = []
    for number, rule in enumerate(rules, start=1):
        if rule.action != "allow":
            continue
        resolved = rule.resolve(container_ip=container_ip, appmux_port=appmux_port)
        reached = _closed_targets_reached(
            resolved, container_ip=container_ip, appmux_port=appmux_port
        )
        if reached:
            problems.append(
                f"rule {number} is an allow that overlaps {', '.join(reached)}, which stays "
                "closed because self_check() assumes it; narrow its destination or make it a deny"
            )
    if problems:
        raise EgressRulesRefused(problems)
