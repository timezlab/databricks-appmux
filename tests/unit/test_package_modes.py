"""The package modes and the egress list a build gets in each (design 5.3, FR-031, FR-032).

Which mode is the default is the platform setting's business (``PlatformSettings.package_mode``);
this module only says what each mode lets a build do.
"""

from __future__ import annotations

import struct
from ipaddress import IPv4Address

import pytest

from appmux.packages.modes import DENY_ALL_EGRESS, PackageMode, build_egress
from appmux.sandbox.rules import EgressRule, Reason, Ruleset
from appmux.settings import PlatformSettings

APP_RULES = (
    EgressRule(action="allow", cidr="203.0.113.0/24", port="443", comment="the company index"),
)
CONTAINER_IP = IPv4Address("10.4.1.7")


def ipv4_frame(destination: str, port: int = 443) -> bytes:
    """A TCP SYN-shaped Ethernet frame with just enough header for the rule engine."""
    ethernet = b"\x02\x00\x00\x00\x00\x01" + b"\x02\x00\x00\x00\x00\x02" + b"\x08\x00"
    total_bytes = 20 + 20
    ip_header = (
        struct.pack("!BBHHHBBH", 0x45, 0, total_bytes, 0, 0x4000, 64, 6, 0)
        + IPv4Address("10.0.2.100").packed
        + IPv4Address(destination).packed
    )
    tcp_header = struct.pack("!HH", 40000, port) + b"\x00" * 16
    return ethernet + ip_header + tcp_header


def test_the_three_modes_are_the_values_the_platform_setting_accepts() -> None:
    assert {mode.value for mode in PackageMode} == {"index", "wheelhouse", "wheelhouse+index"}
    # the setting is a Literal of the same three words, so a stored value always parses
    default = PlatformSettings().package_mode
    assert PackageMode(default) in set(PackageMode)


@pytest.mark.parametrize(
    ("mode", "uses_wheels", "uses_index", "builds_source_distributions"),
    [
        (PackageMode.INDEX, False, True, True),
        (PackageMode.WHEELHOUSE, True, False, False),
        (PackageMode.WHEELHOUSE_INDEX, True, True, False),
    ],
)
def test_each_mode_says_where_packages_may_come_from(
    mode: PackageMode, uses_wheels: bool, uses_index: bool, builds_source_distributions: bool
) -> None:
    assert mode.uses_wheels is uses_wheels
    assert mode.uses_index is uses_index
    # design 5.3: a source distribution is built only in `index` mode
    assert mode.builds_source_distributions is builds_source_distributions


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="not a valid PackageMode"):
        PackageMode("wheelhouse-plus")


# --- the build's egress list --------------------------------------------------------------------


def test_wheelhouse_gives_the_build_an_all_deny_list_whatever_the_app_allows() -> None:
    assert build_egress(PackageMode.WHEELHOUSE, APP_RULES, needs_index=False) == DENY_ALL_EGRESS
    # even if a caller claims an index is needed: a wheelhouse build has none to reach
    assert build_egress(PackageMode.WHEELHOUSE, APP_RULES, needs_index=True) == DENY_ALL_EGRESS


def test_index_modes_keep_the_apps_rules_only_for_the_step_that_needs_the_index() -> None:
    assert build_egress(PackageMode.INDEX, APP_RULES, needs_index=True) == APP_RULES
    assert build_egress(PackageMode.WHEELHOUSE_INDEX, APP_RULES, needs_index=True) == APP_RULES
    # the wheel pass of `wheelhouse+index` reads local files only, so it reaches nothing
    assert (
        build_egress(PackageMode.WHEELHOUSE_INDEX, APP_RULES, needs_index=False) == DENY_ALL_EGRESS
    )
    assert build_egress(PackageMode.INDEX, APP_RULES, needs_index=False) == DENY_ALL_EGRESS


def test_the_all_deny_list_drops_every_address_except_the_virtual_subnet() -> None:
    ruleset = Ruleset(DENY_ALL_EGRESS, APP_RULES, container_ip=CONTAINER_IP, appmux_port=8000)
    for destination in ("203.0.113.9", "8.8.8.8", "10.4.1.7", "169.254.169.254"):
        decision = ruleset.decide(ipv4_frame(destination))
        assert not decision.forward, destination
        assert decision.reason is Reason.RULE_DENY
    # slirp's gateway and DNS are always reachable (rules.py); the list does not change that
    assert ruleset.decide(ipv4_frame("10.0.2.3", 53)).reason is Reason.VIRTUAL_SUBNET


def test_the_all_deny_list_is_a_valid_stored_rule_shape() -> None:
    assert [rule.model_dump() for rule in DENY_ALL_EGRESS] == [
        {
            "action": "deny",
            "cidr": "0.0.0.0/0",
            "port": "any",
            "comment": DENY_ALL_EGRESS[0].comment,
        }
    ]
    assert DENY_ALL_EGRESS[0].comment
