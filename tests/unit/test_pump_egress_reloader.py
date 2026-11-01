"""The egress reloader: rewrite a child's rules file, then signal its pump (T066, FR-018).

``PumpReloader`` is the real implementation of the ``EgressReloader`` seam of the egress routes
(``admin/platform.py``). It runs in appmux, not in the sandbox: it rewrites each running child's
rules file with ``write_rules_file`` and then asks the child's supervisor to read it
(``SandboxHandle.reload_rules``, a ``SIGHUP``). What the pump does with the signal is tested in
``test_sandbox_pump.py``; delivery of the signal to a real supervisor needs the container.
"""

from __future__ import annotations

import socket
import stat
import struct
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path

import pytest

from appmux.sandbox.pump import (
    PumpReloader,
    PumpReloadError,
    PumpTarget,
    load_ruleset,
)
from appmux.sandbox.rules import EgressRule, Reason, Ruleset

pytestmark = pytest.mark.anyio

CONTAINER_IP = IPv4Address("10.4.1.7")
APPMUX_PORT = 8000


@dataclass(frozen=True, slots=True)
class Closed:
    container_ip: IPv4Address = CONTAINER_IP
    appmux_port: int = APPMUX_PORT


def deny(cidr: str, port: str = "any") -> EgressRule:
    return EgressRule(action="deny", cidr=cidr, port=port, comment="")


def allow(cidr: str, port: str = "any") -> EgressRule:
    return EgressRule(action="allow", cidr=cidr, port=port, comment="")


def ipv4_frame(destination: str, port: int) -> bytes:
    transport = struct.pack("!HHLLBBHHH", 40000, port, 0, 0, 5 << 4, 0, 0, 0, 0)
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
        socket.inet_aton(destination),
    )
    return b"\x02" * 6 + b"\x04" * 6 + b"\x08\x00" + ip_header + transport


class FakeSupervisor:
    """Stands in for ``SandboxHandle``: reads the rules file at the moment it is signalled."""

    def __init__(self, rules_path: Path, *, is_running: bool = True) -> None:
        self._rules_path = rules_path
        self.is_running = is_running
        self.rulesets_seen: list[Ruleset] = []

    def reload_rules(self) -> bool:
        if not self.is_running:
            return False
        self.rulesets_seen.append(load_ruleset(self._rules_path))
        return True


class Slot:
    """The running children of a test, their per-app lists and the global list."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.targets: dict[str, tuple[PumpTarget, FakeSupervisor]] = {}
        self.app_lists: dict[str, list[EgressRule]] = {}
        self.global_list: list[EgressRule] = [deny("169.254.0.0/16")]
        self.closed: Closed | None = Closed()
        self.app_list_reads: list[str] = []

    def add(
        self, slug: str, *rules: EgressRule, is_running: bool = True, directory: Path | None = None
    ) -> FakeSupervisor:
        scratch = directory if directory is not None else self.tmp_path / slug
        scratch.mkdir(parents=True, exist_ok=True)
        path = scratch / "rules.json"
        supervisor = FakeSupervisor(path, is_running=is_running)
        self.targets[slug] = (PumpTarget(slug=slug, rules_path=path, handle=supervisor), supervisor)
        self.app_lists[slug] = list(rules)
        return supervisor

    def path_of(self, slug: str) -> Path:
        return self.targets[slug][0].rules_path

    def app_rules(self, slug: str) -> list[EgressRule]:
        self.app_list_reads.append(slug)
        return self.app_lists[slug]

    def reloader(self) -> PumpReloader:
        return PumpReloader(
            running=lambda: [target for target, _ in self.targets.values()],
            app_rules=self.app_rules,
            global_rules=lambda: self.global_list,
            closed_targets=lambda: self.closed,
        )


@pytest.fixture
def slot(tmp_path: Path) -> Slot:
    return Slot(tmp_path)


async def test_one_app_is_rewritten_and_signalled_and_the_others_are_left_alone(
    slot: Slot,
) -> None:
    mine = slot.add("sales", deny("93.184.216.0/24"))
    other = slot.add("hr", deny("10.9.0.0/16"))

    await slot.reloader()("sales")

    (ruleset,) = mine.rulesets_seen
    assert ruleset.decide(ipv4_frame("93.184.216.34", 443)).rule_id == "app:1"
    assert other.rulesets_seen == []
    assert not slot.path_of("hr").exists()  # nothing was written for the app that was not named
    assert slot.app_list_reads == ["sales"]


async def test_the_global_list_rewrites_every_running_app_with_its_own_list(slot: Slot) -> None:
    sales = slot.add("sales", deny("93.184.216.0/24"))
    hr = slot.add("hr", allow("10.9.0.0/16", "443"))

    await slot.reloader()(None)

    (sales_rules,) = sales.rulesets_seen
    (hr_rules,) = hr.rulesets_seen
    assert sales_rules.decide(ipv4_frame("169.254.169.254", 80)).rule_id == "global:1"
    assert sales_rules.decide(ipv4_frame("93.184.216.34", 443)).rule_id == "app:1"
    assert sales_rules.decide(ipv4_frame("10.9.1.1", 443)).reason == Reason.DEFAULT_ALLOW
    assert hr_rules.decide(ipv4_frame("169.254.169.254", 80)).rule_id == "global:1"
    assert hr_rules.decide(ipv4_frame("10.9.1.1", 443)).rule_id == "app:1"
    assert hr_rules.decide(ipv4_frame("93.184.216.34", 443)).reason == Reason.DEFAULT_ALLOW


async def test_the_file_is_complete_before_the_signal_and_only_the_owner_can_read_it(
    slot: Slot,
) -> None:
    supervisor = slot.add("sales", deny("93.184.216.0/24"))

    await slot.reloader()("sales")

    # The fake loads the file when it is signalled; a file written after the signal would not load.
    assert len(supervisor.rulesets_seen) == 1
    assert stat.S_IMODE(slot.path_of("sales").stat().st_mode) == 0o600


async def test_the_container_values_come_from_the_closed_targets_of_the_moment(
    slot: Slot,
) -> None:
    supervisor = slot.add("sales")
    slot.global_list = [deny("@container_ip/32", "@appmux_port")]
    slot.closed = Closed(IPv4Address("10.4.9.9"), 8123)

    await slot.reloader()("sales")

    (ruleset,) = supervisor.rulesets_seen
    assert ruleset.decide(ipv4_frame("10.4.9.9", 8123)).rule_id == "global:1"
    assert ruleset.decide(ipv4_frame(str(CONTAINER_IP), APPMUX_PORT)).reason == Reason.DEFAULT_ALLOW


async def test_each_call_reads_the_lists_again(slot: Slot) -> None:
    supervisor = slot.add("sales", deny("93.184.216.0/24"))
    reloader = slot.reloader()
    await reloader("sales")

    slot.app_lists["sales"] = [allow("93.184.216.0/24", "443")]
    slot.global_list = []
    await reloader("sales")

    first, second = supervisor.rulesets_seen
    assert first.decide(ipv4_frame("93.184.216.34", 443)).forward is False
    assert second.decide(ipv4_frame("93.184.216.34", 443)).forward is True
    assert second.decide(ipv4_frame("169.254.169.254", 80)).reason == Reason.DEFAULT_ALLOW


async def test_an_app_that_is_not_running_needs_nothing(slot: Slot) -> None:
    slot.add("hr")

    await slot.reloader()("sales")  # it reads its list from the registry when it starts

    assert slot.app_list_reads == []


async def test_nothing_running_is_not_an_error_even_when_the_container_address_is_unknown(
    slot: Slot,
) -> None:
    slot.closed = None

    await slot.reloader()(None)


async def test_an_unknown_container_address_stops_the_reload_before_any_file_is_written(
    slot: Slot,
) -> None:
    supervisor = slot.add("sales", deny("93.184.216.0/24"))
    slot.closed = None

    with pytest.raises(PumpReloadError, match="container"):
        await slot.reloader()("sales")

    assert not slot.path_of("sales").exists()
    assert supervisor.rulesets_seen == []


async def test_a_supervisor_that_has_ended_is_skipped_without_an_error(slot: Slot) -> None:
    gone = slot.add("sales", is_running=False)
    alive = slot.add("hr", deny("10.9.0.0/16"))

    await slot.reloader()(None)

    assert gone.rulesets_seen == []
    assert len(alive.rulesets_seen) == 1


async def test_one_app_that_cannot_be_written_does_not_stop_the_others(
    slot: Slot, tmp_path: Path
) -> None:
    slot.add("sales", deny("93.184.216.0/24"), directory=tmp_path / "vanished")
    healthy = slot.add("hr", deny("10.9.0.0/16"))
    (tmp_path / "vanished").rmdir()  # the scratch directory is gone

    with pytest.raises(PumpReloadError) as caught:
        await slot.reloader()(None)

    assert len(healthy.rulesets_seen) == 1
    assert "sales" in str(caught.value)
    assert "hr" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)  # the message names slugs, not paths


async def test_every_app_that_failed_is_named_once(slot: Slot, tmp_path: Path) -> None:
    for slug in ("sales", "hr"):
        slot.add(slug, directory=tmp_path / f"vanished-{slug}")
        (tmp_path / f"vanished-{slug}").rmdir()

    with pytest.raises(PumpReloadError) as caught:
        await slot.reloader()(None)

    message = str(caught.value)
    assert message.count("sales") == 1
    assert message.count("hr") == 1
    assert caught.value.slugs == ("sales", "hr")


async def test_the_unwritable_app_does_not_appear_to_have_been_signalled(
    slot: Slot, tmp_path: Path
) -> None:
    broken = slot.add("sales", directory=tmp_path / "vanished")
    (tmp_path / "vanished").rmdir()

    with pytest.raises(PumpReloadError):
        await slot.reloader()("sales")

    assert broken.rulesets_seen == []  # a stale file must not be announced as new
