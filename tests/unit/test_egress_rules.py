"""Egress rules of design 6.3 and data model rule 18, as a table over raw Ethernet frames.

The pump hands every frame the child sends to ``Ruleset.decide``; these tests build such
frames and check the decision and the rule that made it, so the pump can count drops per rule.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from ipaddress import IPv4Address

import pytest
from pydantic import ValidationError

from appmux.sandbox.rules import (
    DropCounters,
    EgressRule,
    EgressRulesRefused,
    Reason,
    Ruleset,
    destination_of,
    refuse_overlapping_allows,
)

CONTAINER_IP = IPv4Address("10.4.1.7")
APPMUX_PORT = 8000

ICMP, TCP, UDP = 1, 6, 17
ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_ARP = 0x0806
ETHERTYPE_IPV6 = 0x86DD
ETHERTYPE_VLAN = 0x8100
MORE_FRAGMENTS_FLAG = 0x2000
DONT_FRAGMENT_FLAG = 0x4000

CHILD_ADDRESS = IPv4Address("10.0.2.100")
OTHER_MAC = b"\x02\x00\x00\x00\x00\x01"
CHILD_MAC = b"\x02\x00\x00\x00\x00\x02"
TCP_HEADER_BYTES = 20
UDP_HEADER_BYTES = 8
ICMP_HEADER_BYTES = 8


def ethernet(ethertype: int, payload: bytes = b"\x00" * 46) -> bytes:
    return OTHER_MAC + CHILD_MAC + struct.pack("!H", ethertype) + payload


def ipv4_frame(
    destination: str,
    *,
    protocol: int = TCP,
    destination_port: int = 443,
    flags_and_offset: int = DONT_FRAGMENT_FLAG,
    transport: bytes | None = None,
) -> bytes:
    """An Ethernet frame holding an IPv4 packet whose transport header starts with the ports."""
    if transport is None:
        header_bytes = {TCP: TCP_HEADER_BYTES, UDP: UDP_HEADER_BYTES}.get(
            protocol, ICMP_HEADER_BYTES
        )
        transport = struct.pack("!HH", 40000, destination_port).ljust(header_bytes, b"\x00")
    total_bytes = 20 + len(transport)
    header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_bytes,
        0,
        flags_and_offset,
        64,
        protocol,
        0,
        CHILD_ADDRESS.packed,
        IPv4Address(destination).packed,
    )
    return ethernet(ETHERTYPE_IPV4, header + transport)


def rule(action: str, cidr: str = "0.0.0.0/0", *, port: str = "any") -> EgressRule:
    return EgressRule.model_validate({"action": action, "cidr": cidr, "port": port, "comment": ""})


def ruleset(
    app: Sequence[EgressRule] = (),
    global_rules: Sequence[EgressRule] = (),
    *,
    container_ip: IPv4Address = CONTAINER_IP,
    appmux_port: int = APPMUX_PORT,
) -> Ruleset:
    return Ruleset(app, global_rules, container_ip=container_ip, appmux_port=appmux_port)


# --- Ordering ---------------------------------------------------------------------------------


def test_no_match_allows() -> None:
    decision = ruleset([rule("deny", "198.51.100.0/24")]).decide(ipv4_frame("203.0.113.9"))

    assert decision.forward
    assert decision.reason is Reason.DEFAULT_ALLOW
    assert decision.rule_index is None
    assert decision.rule_id is None


def test_an_empty_ruleset_allows_everything() -> None:
    assert ruleset().decide(ipv4_frame("203.0.113.9")).forward


def test_first_match_wins_deny_before_allow() -> None:
    rules = [rule("deny", "203.0.113.0/24"), rule("allow", "203.0.113.5/32")]

    decision = ruleset(rules).decide(ipv4_frame("203.0.113.5"))

    assert not decision.forward
    assert decision.reason is Reason.RULE_DENY
    assert decision.rule_index == 0


def test_first_match_wins_allow_before_deny() -> None:
    rules = [rule("allow", "203.0.113.5/32"), rule("deny", "203.0.113.0/24")]
    rules_in_force = ruleset(rules)

    allowed = rules_in_force.decide(ipv4_frame("203.0.113.5"))
    denied = rules_in_force.decide(ipv4_frame("203.0.113.6"))

    assert allowed.forward
    assert allowed.reason is Reason.RULE_ALLOW
    assert allowed.rule_index == 0
    assert not denied.forward
    assert denied.rule_index == 1


def test_an_allow_list_is_allow_rules_then_a_trailing_deny_of_everything() -> None:
    rules_in_force = ruleset([rule("allow", "203.0.113.0/24"), rule("deny", "0.0.0.0/0")])

    assert rules_in_force.decide(ipv4_frame("203.0.113.40")).forward
    assert not rules_in_force.decide(ipv4_frame("198.51.100.40")).forward


def test_the_apps_own_rules_come_before_the_global_rules() -> None:
    app = [rule("allow", "203.0.113.5/32")]
    global_rules = [rule("deny", "203.0.113.0/24")]
    rules_in_force = ruleset(app, global_rules)

    exempted = rules_in_force.decide(ipv4_frame("203.0.113.5"))
    blocked = rules_in_force.decide(ipv4_frame("203.0.113.6"))

    assert exempted.forward
    assert exempted.rule_id == "app:1"
    assert not blocked.forward
    assert blocked.rule_id == "global:1"


def test_an_app_deny_beats_a_global_allow() -> None:
    rules_in_force = ruleset([rule("deny", "203.0.113.5/32")], [rule("allow", "203.0.113.0/24")])

    decision = rules_in_force.decide(ipv4_frame("203.0.113.5"))

    assert not decision.forward
    assert decision.rule_id == "app:1"


def test_rule_index_counts_the_apps_list_then_the_global_list() -> None:
    app = [rule("deny", "192.0.2.1/32"), rule("deny", "192.0.2.2/32")]
    global_rules = [rule("deny", "192.0.2.3/32"), rule("deny", "203.0.113.0/24")]
    rules_in_force = ruleset(app, global_rules)

    decision = rules_in_force.decide(ipv4_frame("203.0.113.8"))

    assert decision.rule_index == 3
    assert decision.rule_id == "global:2"
    assert rules_in_force.rule_ids == ("app:1", "app:2", "global:1", "global:2")


def test_the_global_list_applies_when_the_app_has_no_rules() -> None:
    decision = ruleset((), [rule("deny", "169.254.0.0/16")]).decide(ipv4_frame("169.254.169.254"))

    assert not decision.forward
    assert decision.rule_id == "global:1"


# --- The virtual subnet -----------------------------------------------------------------------


@pytest.mark.parametrize("address", ["10.0.2.0", "10.0.2.2", "10.0.2.3", "10.0.2.255"])
def test_the_virtual_subnet_is_always_allowed(address: str) -> None:
    rules_in_force = ruleset([rule("deny", "0.0.0.0/0")], [rule("deny", "10.0.0.0/8")])

    decision = rules_in_force.decide(ipv4_frame(address, protocol=UDP, destination_port=53))

    assert decision.forward
    assert decision.reason is Reason.VIRTUAL_SUBNET
    assert decision.rule_index is None


@pytest.mark.parametrize("flags_and_offset", [MORE_FRAGMENTS_FLAG, 185])
def test_the_virtual_subnet_stays_allowed_with_port_rules_before_the_fragment_drop(
    flags_and_offset: int,
) -> None:
    # A name lookup is a UDP datagram to 10.0.2.3; a port rule must not stop it, even when the
    # datagram is a fragment (the subnet check comes before the fragment drop).
    rules_in_force = ruleset([rule("deny", port="53"), rule("deny")])

    decision = rules_in_force.decide(
        ipv4_frame("10.0.2.3", protocol=UDP, destination_port=53, flags_and_offset=flags_and_offset)
    )

    assert decision.forward
    assert decision.reason is Reason.VIRTUAL_SUBNET


def test_the_virtual_subnet_stays_allowed_with_port_rules_before_the_short_transport_drop() -> None:
    rules_in_force = ruleset([rule("deny", port="53"), rule("deny")])

    decision = rules_in_force.decide(ipv4_frame("10.0.2.3", transport=b"\x9c\x40"))

    assert decision.forward
    assert decision.reason is Reason.VIRTUAL_SUBNET


def test_the_virtual_subnet_is_allowed_by_port_rules_that_name_its_port() -> None:
    rules_in_force = ruleset([rule("deny", "10.0.2.0/24", port="53")])

    decision = rules_in_force.decide(ipv4_frame("10.0.2.3", protocol=UDP, destination_port=53))

    assert decision.reason is Reason.VIRTUAL_SUBNET


@pytest.mark.parametrize("address", ["10.0.1.255", "10.0.3.0"])
def test_the_neighbours_of_the_virtual_subnet_follow_the_rules(address: str) -> None:
    decision = ruleset([rule("deny", "0.0.0.0/0")]).decide(ipv4_frame(address))

    assert not decision.forward
    assert decision.reason is Reason.RULE_DENY


# --- CIDR and port ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cidr", "address", "matches"),
    [
        ("203.0.113.0/24", "203.0.113.0", True),
        ("203.0.113.0/24", "203.0.113.255", True),
        ("203.0.113.0/24", "203.0.112.255", False),
        ("203.0.113.0/24", "203.0.114.0", False),
        ("203.0.113.5/32", "203.0.113.5", True),
        ("203.0.113.5/32", "203.0.113.6", False),
        ("198.51.100.0/23", "198.51.101.9", True),
        ("0.0.0.0/0", "203.0.113.5", True),
    ],
)
def test_cidr_matching(cidr: str, address: str, matches: bool) -> None:
    decision = ruleset([rule("deny", cidr)]).decide(ipv4_frame(address))

    assert decision.forward is not matches


@pytest.mark.parametrize(
    ("destination_port", "protocol", "dropped"),
    [
        (443, TCP, True),
        (443, UDP, True),
        (80, TCP, False),
        (80, UDP, False),
    ],
)
def test_a_port_rule_covers_tcp_and_udp(
    destination_port: int, protocol: int, dropped: bool
) -> None:
    rules_in_force = ruleset([rule("deny", "203.0.113.0/24", port="443")])

    decision = rules_in_force.decide(
        ipv4_frame("203.0.113.5", protocol=protocol, destination_port=destination_port)
    )

    assert decision.forward is not dropped


def test_a_packet_without_ports_never_matches_a_port_rule() -> None:
    rules_in_force = ruleset([rule("deny", "203.0.113.0/24", port="53")])

    assert rules_in_force.decide(ipv4_frame("203.0.113.5", protocol=ICMP)).forward


def test_a_rule_without_a_port_covers_packets_without_ports() -> None:
    assert (
        not ruleset([rule("deny", "203.0.113.0/24")])
        .decide(ipv4_frame("203.0.113.5", protocol=ICMP))
        .forward
    )


def test_a_destination_option_header_is_skipped_to_read_the_port() -> None:
    options = b"\x01\x01\x01\x00"
    header = struct.pack(
        "!BBHHHBBH4s4s",
        0x46,
        0,
        20 + len(options) + TCP_HEADER_BYTES,
        0,
        DONT_FRAGMENT_FLAG,
        64,
        TCP,
        0,
        CHILD_ADDRESS.packed,
        IPv4Address("203.0.113.5").packed,
    )
    transport = struct.pack("!HH", 40000, 443).ljust(TCP_HEADER_BYTES, b"\x00")
    frame = ethernet(ETHERTYPE_IPV4, header + options + transport)

    assert not ruleset([rule("deny", port="443")]).decide(frame).forward


# --- Frames the pump must not pass ------------------------------------------------------------


def test_ipv6_frames_are_dropped_with_or_without_rules() -> None:
    frame = ethernet(ETHERTYPE_IPV6)

    for rules_in_force in (ruleset(), ruleset([rule("allow", "0.0.0.0/0")])):
        decision = rules_in_force.decide(frame)
        assert not decision.forward
        assert decision.reason is Reason.IPV6


def test_arp_passes_so_the_child_can_find_the_gateway() -> None:
    decision = ruleset([rule("deny", "0.0.0.0/0")]).decide(ethernet(ETHERTYPE_ARP))

    assert decision.forward
    assert decision.reason is Reason.ARP


def test_other_ethertypes_are_dropped() -> None:
    decision = ruleset().decide(ethernet(ETHERTYPE_VLAN))

    assert not decision.forward
    assert decision.reason is Reason.NON_IP


def test_a_frame_shorter_than_an_ethernet_header_is_dropped() -> None:
    decision = ruleset().decide(b"\x00" * 13)

    assert not decision.forward
    assert decision.reason is Reason.RUNT


def _ipv4_with(header: bytes, payload: bytes = b"") -> bytes:
    return ethernet(ETHERTYPE_IPV4, header + payload)


def _header(*, version_and_length: int = 0x45, total_bytes: int = 40, protocol: int = TCP) -> bytes:
    return struct.pack(
        "!BBHHHBBH4s4s",
        version_and_length,
        0,
        total_bytes,
        0,
        DONT_FRAGMENT_FLAG,
        64,
        protocol,
        0,
        CHILD_ADDRESS.packed,
        IPv4Address("203.0.113.5").packed,
    )


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(_ipv4_with(b"\x45" + b"\x00" * 10), id="header cut short"),
        pytest.param(_ipv4_with(_header(version_and_length=0x65), b"\x00" * 20), id="version 6"),
        pytest.param(_ipv4_with(_header(version_and_length=0x44), b"\x00" * 20), id="header < 20"),
        pytest.param(_ipv4_with(_header(total_bytes=200), b"\x00" * 20), id="total beyond frame"),
        pytest.param(_ipv4_with(_header(total_bytes=10), b"\x00" * 20), id="total < header"),
    ],
)
def test_a_frame_that_does_not_parse_is_dropped_even_with_no_rules(frame: bytes) -> None:
    decision = ruleset().decide(frame)

    assert not decision.forward
    assert decision.reason is Reason.MALFORMED


# --- Fragments --------------------------------------------------------------------------------

FIRST_FRAGMENT = MORE_FRAGMENTS_FLAG
LATER_FRAGMENT = 185  # an offset in 8-byte units, so the ports are not in this packet


@pytest.mark.parametrize("flags_and_offset", [FIRST_FRAGMENT, LATER_FRAGMENT])
def test_fragments_are_dropped_when_port_rules_exist(flags_and_offset: int) -> None:
    rules_in_force = ruleset([rule("allow", "203.0.113.0/24", port="443"), rule("deny")])

    decision = rules_in_force.decide(ipv4_frame("203.0.113.5", flags_and_offset=flags_and_offset))

    assert not decision.forward
    assert decision.reason is Reason.FRAGMENT


def test_port_rules_in_the_global_list_also_drop_fragments() -> None:
    rules_in_force = ruleset((), [rule("deny", "203.0.113.0/24", port="25")])

    decision = rules_in_force.decide(ipv4_frame("198.51.100.5", flags_and_offset=FIRST_FRAGMENT))

    assert decision.reason is Reason.FRAGMENT


@pytest.mark.parametrize("flags_and_offset", [FIRST_FRAGMENT, LATER_FRAGMENT])
def test_fragments_follow_the_cidr_rules_when_no_rule_has_a_port(flags_and_offset: int) -> None:
    rules_in_force = ruleset([rule("deny", "203.0.113.0/24")])

    assert not rules_in_force.decide(
        ipv4_frame("203.0.113.5", flags_and_offset=flags_and_offset)
    ).forward
    assert rules_in_force.decide(
        ipv4_frame("198.51.100.5", flags_and_offset=flags_and_offset)
    ).forward


def test_a_transport_header_too_short_for_ports_is_dropped_when_port_rules_exist() -> None:
    frame = ipv4_frame("203.0.113.5", transport=b"\x9c\x40")

    assert ruleset([rule("deny", port="443")]).decide(frame).reason is Reason.MALFORMED
    assert ruleset([rule("deny", "198.51.100.0/24")]).decide(frame).forward


# --- Placeholders resolve at start ------------------------------------------------------------


def test_container_ip_resolves_to_the_address_given_at_start() -> None:
    rules = [rule("deny", "@container_ip/32")]
    here = ruleset((), rules, container_ip=IPv4Address("10.4.1.7"))
    elsewhere = ruleset((), rules, container_ip=IPv4Address("10.9.9.9"))

    assert not here.decide(ipv4_frame("10.4.1.7")).forward
    assert here.decide(ipv4_frame("10.4.1.8")).forward
    assert elsewhere.decide(ipv4_frame("10.4.1.7")).forward
    assert not elsewhere.decide(ipv4_frame("10.9.9.9")).forward


def test_container_ip_without_a_prefix_means_one_address() -> None:
    rules_in_force = ruleset((), [rule("deny", "@container_ip")])

    assert not rules_in_force.decide(ipv4_frame("10.4.1.7")).forward
    assert rules_in_force.decide(ipv4_frame("10.4.1.8")).forward


def test_container_ip_with_a_shorter_prefix_means_its_network() -> None:
    rules_in_force = ruleset((), [rule("deny", "@container_ip/24")])

    assert not rules_in_force.decide(ipv4_frame("10.4.1.200")).forward
    assert rules_in_force.decide(ipv4_frame("10.4.2.7")).forward


def test_appmux_port_resolves_to_the_port_given_at_start() -> None:
    rules = [rule("deny", "@container_ip/32", port="@appmux_port")]
    on_8000 = ruleset((), rules, appmux_port=8000)
    on_9000 = ruleset((), rules, appmux_port=9000)

    assert not on_8000.decide(ipv4_frame("10.4.1.7", destination_port=8000)).forward
    assert on_8000.decide(ipv4_frame("10.4.1.7", destination_port=9000)).forward
    assert on_9000.decide(ipv4_frame("10.4.1.7", destination_port=8000)).forward
    assert not on_9000.decide(ipv4_frame("10.4.1.7", destination_port=9000)).forward


def test_the_default_global_list_closes_the_container_and_the_metadata_address() -> None:
    default_global_list = [
        EgressRule.model_validate(
            {
                "action": "deny",
                "cidr": "@container_ip/32",
                "port": "any",
                "comment": "the container's own address",
            }
        ),
        EgressRule.model_validate(
            {
                "action": "deny",
                "cidr": "169.254.0.0/16",
                "port": "any",
                "comment": "metadata and link-local",
            }
        ),
    ]
    rules_in_force = ruleset((), default_global_list)

    assert not rules_in_force.decide(ipv4_frame("10.4.1.7", destination_port=7270)).forward
    assert not rules_in_force.decide(ipv4_frame("169.254.169.254", destination_port=80)).forward
    assert rules_in_force.decide(ipv4_frame("203.0.113.5")).forward


# --- Drop counters ----------------------------------------------------------------------------


def test_drops_are_counted_per_rule_and_allowed_frames_are_not() -> None:
    app = [rule("deny", "203.0.113.0/24")]
    global_rules = [rule("deny", "169.254.0.0/16")]
    rules_in_force = ruleset(app, global_rules)
    counters = DropCounters()

    sent = ["203.0.113.5", "203.0.113.6", "169.254.169.254", "198.51.100.1"]
    for address in sent:
        frame = ipv4_frame(address)
        counters.record(rules_in_force.decide(frame), frame)

    assert counters.dropped_total == 3
    assert dict(counters.by_rule) == {"app:1": 2, "global:1": 1}
    assert dict(counters.by_reason) == {}


def test_drops_that_no_rule_made_are_counted_by_reason() -> None:
    rules_in_force = ruleset()
    counters = DropCounters()

    ipv6 = ethernet(ETHERTYPE_IPV6)
    counters.record(rules_in_force.decide(ipv6), ipv6)
    counters.record(rules_in_force.decide(ipv6), ipv6)
    runt = b"\x00" * 5
    counters.record(rules_in_force.decide(runt), runt)

    assert counters.dropped_total == 3
    assert dict(counters.by_rule) == {}
    assert dict(counters.by_reason) == {Reason.IPV6: 2, Reason.RUNT: 1}


def test_recent_drops_keep_the_destination_and_are_bounded() -> None:
    rules_in_force = ruleset([rule("deny", "203.0.113.0/24")])
    counters = DropCounters(samples_kept=2)

    for last_octet in (1, 2, 3):
        frame = ipv4_frame(f"203.0.113.{last_octet}")
        counters.record(rules_in_force.decide(frame), frame)

    assert [sample.destination for sample in counters.recent] == [
        IPv4Address("203.0.113.2"),
        IPv4Address("203.0.113.3"),
    ]
    assert all(sample.rule_id == "app:1" for sample in counters.recent)


def test_destination_of_reads_only_ipv4_frames() -> None:
    assert destination_of(ipv4_frame("203.0.113.5")) == IPv4Address("203.0.113.5")
    assert destination_of(ethernet(ETHERTYPE_IPV6)) is None
    assert destination_of(b"\x00" * 20) is None


# --- The stored shape -------------------------------------------------------------------------

STORED_RULE = {
    "action": "allow",
    "cidr": "10.20.0.0/16",
    "port": "443",
    "comment": "warehouse subnet",
}


def test_the_rows_of_the_data_model_parse() -> None:
    parsed = EgressRule.model_validate(STORED_RULE)

    assert parsed.action == "allow"
    assert parsed.cidr == "10.20.0.0/16"
    assert parsed.port == "443"
    assert parsed.comment == "warehouse subnet"
    assert parsed.model_dump() == STORED_RULE


def test_the_fields_of_a_rule_are_exactly_the_contracts() -> None:
    # specs/001-appmux-v1/contracts/admin-api.md, EgressRule: action, cidr, port, comment.
    assert list(EgressRule.model_fields) == ["action", "cidr", "port", "comment"]
    assert all(field.is_required() for field in EgressRule.model_fields.values())


def test_a_rule_may_have_an_empty_comment_and_the_placeholders() -> None:
    parsed = EgressRule.model_validate(
        {"action": "deny", "cidr": "@container_ip/32", "port": "@appmux_port", "comment": ""}
    )

    assert parsed.comment == ""


def test_a_rule_is_immutable() -> None:
    parsed = rule("deny", "203.0.113.0/24")
    field = "action"

    with pytest.raises(ValidationError):
        setattr(parsed, field, "allow")


def _stored_without(field: str) -> dict[str, object]:
    return {name: value for name, value in STORED_RULE.items() if name != field}


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({**STORED_RULE, "action": "permit"}, id="unknown action"),
        pytest.param({**STORED_RULE, "cidr": "not-a-cidr"}, id="not a cidr"),
        pytest.param({**STORED_RULE, "cidr": "203.0.113.5/24"}, id="host bits set"),
        pytest.param({**STORED_RULE, "cidr": "2001:db8::/32"}, id="ipv6"),
        pytest.param({**STORED_RULE, "cidr": "@gateway"}, id="unknown placeholder"),
        pytest.param({**STORED_RULE, "cidr": "@container_ip/33"}, id="prefix too long"),
        pytest.param({**STORED_RULE, "cidr": "@container_ip/\u0663\u0662"}, id="non-ascii prefix"),
        pytest.param({**STORED_RULE, "cidr": "@appmux_port"}, id="port placeholder as cidr"),
        pytest.param({**STORED_RULE, "cidr": None}, id="no destination"),
        pytest.param({**STORED_RULE, "cidr": ""}, id="empty destination"),
        pytest.param({**STORED_RULE, "port": "0"}, id="port zero"),
        pytest.param({**STORED_RULE, "port": "65536"}, id="port too high"),
        pytest.param({**STORED_RULE, "port": "0443"}, id="leading zero"),
        pytest.param({**STORED_RULE, "port": "8000-8100"}, id="port range"),
        pytest.param({**STORED_RULE, "port": "http"}, id="port name"),
        pytest.param({**STORED_RULE, "port": "\u0664\u0664\u0663"}, id="non-ascii digits"),
        pytest.param({**STORED_RULE, "port": "4\u0664\u0663"}, id="mixed digits"),
        pytest.param({**STORED_RULE, "port": " 443"}, id="padded port"),
        pytest.param({**STORED_RULE, "port": "@container_ip"}, id="cidr placeholder as port"),
        pytest.param({**STORED_RULE, "port": 443}, id="port as a number"),
        pytest.param({**STORED_RULE, "protocol": "tcp"}, id="protocol is not in the contract"),
        pytest.param({**STORED_RULE, "ports": "443"}, id="misspelt key"),
        pytest.param(_stored_without("action"), id="no action"),
        pytest.param(_stored_without("cidr"), id="no cidr"),
        pytest.param(_stored_without("port"), id="no port"),
        pytest.param(_stored_without("comment"), id="no comment"),
        pytest.param({"action": "deny"}, id="action only"),
    ],
)
def test_a_rule_body_the_contract_calls_invalid_is_refused(document: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EgressRule.model_validate(document)


# --- The editor refusals (data model rule 18) -------------------------------------------------


def refusal(rules: Sequence[EgressRule]) -> EgressRulesRefused:
    with pytest.raises(EgressRulesRefused) as raised:
        refuse_overlapping_allows(rules, container_ip=CONTAINER_IP, appmux_port=APPMUX_PORT)
    return raised.value


def test_an_allow_list_with_a_trailing_deny_is_accepted() -> None:
    rules = [
        rule("allow", "10.20.0.0/16", port="443"),
        rule("allow", "203.0.113.0/24"),
        rule("deny", "0.0.0.0/0"),
    ]

    refuse_overlapping_allows(rules, container_ip=CONTAINER_IP, appmux_port=APPMUX_PORT)


def test_denies_may_cover_the_closed_addresses() -> None:
    rules = [
        rule("deny", "@container_ip/32"),
        rule("deny", "169.254.0.0/16"),
        rule("deny", port="@appmux_port"),
        rule("deny", "0.0.0.0/0"),
    ]

    refuse_overlapping_allows(rules, container_ip=CONTAINER_IP, appmux_port=APPMUX_PORT)


def test_an_empty_list_is_accepted() -> None:
    refuse_overlapping_allows([], container_ip=CONTAINER_IP, appmux_port=APPMUX_PORT)


@pytest.mark.parametrize(
    ("allow", "named", "not_named"),
    [
        pytest.param(
            rule("allow", "@container_ip/32"),
            ["@container_ip", "@appmux_port"],
            ["169.254.0.0/16"],
            id="the placeholder itself",
        ),
        pytest.param(
            rule("allow", "10.4.1.7/32", port="22"),
            ["@container_ip"],
            ["@appmux_port", "169.254.0.0/16"],
            id="the address on another port",
        ),
        pytest.param(
            rule("allow", "10.0.0.0/8", port="443"),
            ["@container_ip"],
            ["@appmux_port", "169.254.0.0/16"],
            id="a network that holds the container",
        ),
        pytest.param(
            rule("allow", "10.4.0.0/16", port="8000"),
            ["@container_ip", "@appmux_port"],
            ["169.254.0.0/16"],
            id="appmux's port on a network that holds the container",
        ),
        pytest.param(
            rule("allow", "10.4.1.7/32", port="@appmux_port"),
            ["@container_ip", "@appmux_port"],
            ["169.254.0.0/16"],
            id="the port placeholder",
        ),
        pytest.param(
            rule("allow", "169.254.169.254/32", port="80"),
            ["169.254.0.0/16"],
            ["@container_ip", "@appmux_port"],
            id="the metadata address",
        ),
        pytest.param(
            rule("allow", "169.254.0.0/15"),
            ["169.254.0.0/16"],
            ["@container_ip", "@appmux_port"],
            id="a network that holds link-local",
        ),
        pytest.param(
            rule("allow", "0.0.0.0/0"),
            ["@container_ip", "@appmux_port", "169.254.0.0/16"],
            [],
            id="everything",
        ),
    ],
)
def test_an_allow_that_overlaps_a_closed_address_is_refused_by_name(
    allow: EgressRule, named: list[str], not_named: list[str]
) -> None:
    message = str(refusal([allow]))

    assert "rule 1" in message
    for name in named:
        assert name in message
    for name in not_named:
        assert name not in message


def test_every_offending_rule_is_reported_with_its_number() -> None:
    rules = [
        rule("allow", "203.0.113.0/24"),
        rule("allow", "169.254.169.254/32"),
        rule("deny", "10.0.0.0/8"),
        rule("allow", "@container_ip/32"),
    ]

    refused = refusal(rules)

    assert len(refused.problems) == 2
    assert "rule 2" in refused.problems[0]
    assert "169.254.0.0/16" in refused.problems[0]
    assert "rule 4" in refused.problems[1]
    assert "@container_ip" in refused.problems[1]


def test_the_refusal_does_not_print_the_container_address() -> None:
    message = str(refusal([rule("allow", "10.0.0.0/8")]))

    assert str(CONTAINER_IP) not in message
