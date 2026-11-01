"""``PUT /platform/egress`` and ``PUT /apps/{app}/egress`` (T049, T066; FR-014; design 6.3).

The routes run behind the real guard and authorizer on the in-memory registry, with a fake group
lookup and an injected reloader (the call that tells the pumps of running children to read the new
list; the real signal, ``SIGHUP``, is a container-only action).

What is pinned:

* only a platform admin edits egress: an app's owner or ``CAN_MANAGE`` holder gets ``403``, a
  stranger to the app ``404``;
* an item that is not an ``EgressRule`` is a ``400`` naming its field and never its value; a list
  with an ``allow`` that overlaps ``@container_ip``, ``@appmux_port`` or ``169.254.0.0/16`` is a
  ``400`` naming the rule and the placeholder, and never the container's address;
* an accepted list is stored whole (``platform_settings.egress_global`` or ``apps.egress``) with
  one ``egress.edit`` audit row, and only then are the pumps told;
* a stale ``version`` is a ``409`` and changes nothing; a reload that fails answers ``503`` with
  the rules saved, and saving again retries it.

Starting data: see ``edge_harness`` (``sales`` owned by alice, bob ``CAN_USE``, carol
``CAN_MANAGE``; ``docs`` owned by frank; dana is the platform admin).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from ipaddress import IPv4Address
from typing import Any, Final

import pytest
from edge_harness import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    FRANK,
    MOUNT,
    NOW,
    App,
    FakeGroups,
    call,
    seeded_registry,
    settings_for_wiring,
)
from stub_registry import StubRegistry

from appmux import __main__ as entry
from appmux.admin.platform import ClosedTargets
from appmux.admin.schemas import AppDetail, EgressRules
from appmux.edge.app import create_edge_app
from appmux.edge.gate import Gate
from appmux.sandbox.rules import EgressRule

pytestmark = pytest.mark.anyio

GLOBAL_PATH: Final = f"{MOUNT}/platform/egress"
SALES_PATH: Final = f"{MOUNT}/apps/sales/egress"
CONTAINER_IP: Final = "10.9.8.7"
APPMUX_PORT: Final = 8123
TARGETS: Final = ClosedTargets(container_ip=IPv4Address(CONTAINER_IP), appmux_port=APPMUX_PORT)
# Text that must never come back to the client from a refused body.
SUBMITTED_MARKER: Final = "marker-value-from-the-client"

Rule = dict[str, str]


def rule(
    action: str = "deny", cidr: str = "198.51.100.0/24", port: str = "any", comment: str = ""
) -> Rule:
    return {"action": action, "cidr": cidr, "port": port, "comment": comment}


ALLOW_LIST: Final = [
    rule("allow", "198.51.100.0/24", "443", "the partner API"),
    rule("deny", "0.0.0.0/0", "any", "everything else"),
]


class Reloads:
    """The injected reloader: records what it was asked to reload and can be made to fail."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []
        self.error: Exception | None = None

    async def __call__(self, app_slug: str | None) -> None:
        self.calls.append(app_slug)
        if self.error is not None:
            raise self.error


def edge(
    registry: StubRegistry,
    reloads: Reloads,
    targets: ClosedTargets | None = TARGETS,
) -> App:
    return create_edge_app(
        registry=registry,
        group_source=FakeGroups(),
        closed_targets=lambda: targets,
        egress_reloader=reloads,
    )


def global_rules(registry: StubRegistry) -> list[dict[str, Any]] | None:
    row = registry.snapshot.setting("egress_global")
    if row is None:
        return None
    assert isinstance(row.value, list)
    return [item.model_dump() for item in row.value if isinstance(item, EgressRule)]


def app_rules(registry: StubRegistry, slug: str = "sales") -> list[dict[str, Any]]:
    app = registry.snapshot.app(slug)
    assert app is not None
    return [item.model_dump() for item in app.egress]


def seed_release(registry: StubRegistry, *, runtime: str = "python") -> None:
    registry.seed(
        "releases",
        {
            "app_slug": "sales",
            "release_no": 1,
            "snapshot_digest": "sha256:" + "a" * 64,
            "detected": {"runtime": runtime},
            "status": "active",
            "status_at": NOW,
            "label": "first",
            "tags": ["stable"],
            "created_by": ALICE,
            "created_at": NOW,
        },
    )
    registry.seed(
        "app_config",
        {
            "app_slug": "sales",
            "desired_state": "running",
            "target_release_no": 1,
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )


def unchanged(registry: StubRegistry, reloads: Reloads) -> bool:
    return registry.audit_rows == () and reloads.calls == []


# --- the global list ----------------------------------------------------------------------------


async def test_a_platform_admin_replaces_the_global_list() -> None:
    registry, reloads = seeded_registry(), Reloads()

    reply = await call(
        edge(registry, reloads), "PUT", GLOBAL_PATH, email=DANA, json_body={"rules": ALLOW_LIST}
    )

    assert reply.status == 200
    assert EgressRules.model_validate_json(reply.body).model_dump() == {"items": ALLOW_LIST}
    assert global_rules(registry) == ALLOW_LIST


async def test_the_second_save_replaces_the_whole_list_and_the_empty_list_is_allowed() -> None:
    registry, reloads = seeded_registry(), Reloads()
    app = edge(registry, reloads)
    await call(app, "PUT", GLOBAL_PATH, email=DANA, json_body={"rules": ALLOW_LIST})

    shorter = [ALLOW_LIST[1]]
    await call(app, "PUT", GLOBAL_PATH, email=DANA, json_body={"rules": shorter})
    assert global_rules(registry) == shorter

    emptied = await call(app, "PUT", GLOBAL_PATH, email=DANA, json_body={"rules": []})
    assert emptied.status == 200
    assert emptied.json() == {"items": []}
    assert global_rules(registry) == []


async def test_a_save_records_one_egress_edit_audit_row_then_tells_the_pumps() -> None:
    registry, reloads = seeded_registry(), Reloads()

    await call(
        edge(registry, reloads), "PUT", GLOBAL_PATH, email=DANA, json_body={"rules": ALLOW_LIST}
    )

    (row,) = registry.audit_rows
    assert (row.action, row.actor, row.app_slug) == ("egress.edit", DANA, None)
    assert row.detail == {"setting": "egress_global"}
    assert reloads.calls == [None]  # None: every running child


async def test_the_same_list_saved_again_is_written_audited_and_signalled_again() -> None:
    # A save is also how an admin retries a reload that failed; it is never skipped as a no-op.
    registry, reloads = seeded_registry(), Reloads()
    app = edge(registry, reloads)

    for _ in range(2):
        await call(app, "PUT", GLOBAL_PATH, email=DANA, json_body={"rules": ALLOW_LIST})

    assert len(registry.audit_rows) == 2
    assert reloads.calls == [None, None]


@pytest.mark.parametrize("person", [ALICE, BOB, CAROL, FRANK])
async def test_a_person_who_is_not_a_platform_admin_gets_403_on_the_global_list(
    person: str,
) -> None:
    registry, reloads = seeded_registry(), Reloads()

    reply = await call(
        edge(registry, reloads), "PUT", GLOBAL_PATH, email=person, json_body={"rules": []}
    )

    assert reply.status == 403
    assert reply.json()["error"]["code"] == "forbidden"
    assert unchanged(registry, reloads)
    assert global_rules(registry) is None


# --- one app's list -----------------------------------------------------------------------------


async def test_a_platform_admin_replaces_an_apps_list_and_gets_the_app_page_back() -> None:
    registry, reloads = seeded_registry(), Reloads()

    reply = await call(
        edge(registry, reloads),
        "PUT",
        SALES_PATH,
        email=DANA,
        json_body={"version": 1, "rules": ALLOW_LIST},
    )

    assert reply.status == 200
    detail = AppDetail.model_validate_json(reply.body)
    assert detail.slug == "sales"
    assert detail.version == 2  # the version after the write
    assert [rule_.model_dump() for rule_ in detail.egress] == ALLOW_LIST
    assert app_rules(registry) == ALLOW_LIST
    assert app_rules(registry, "docs") == []  # another app is untouched
    assert global_rules(registry) is None  # and so is the global list


async def test_an_app_save_records_one_audit_row_for_the_app_then_tells_its_pump() -> None:
    registry, reloads = seeded_registry(), Reloads()

    await call(
        edge(registry, reloads),
        "PUT",
        SALES_PATH,
        email=DANA,
        json_body={"version": 1, "rules": ALLOW_LIST},
    )

    (row,) = registry.audit_rows
    assert (row.action, row.actor, row.app_slug) == ("egress.edit", DANA, "sales")
    assert reloads.calls == ["sales"]


async def test_the_page_returned_by_an_app_save_carries_the_rest_of_the_app() -> None:
    registry, reloads = seeded_registry(), Reloads()
    seed_release(registry)

    reply = await call(
        edge(registry, reloads),
        "PUT",
        SALES_PATH,
        email=DANA,
        json_body={"version": 1, "rules": ALLOW_LIST},
    )

    detail = AppDetail.model_validate_json(reply.body)
    assert (detail.owner, detail.display_name, detail.memory_budget_mb) == (ALICE, "Sales", 256)
    assert detail.runtime == "python"
    assert [(r.release_no, r.status, r.name, r.tags, r.is_target) for r in detail.releases] == [
        (1, "active", "first", ["stable"], True)
    ]
    assert (detail.config.desired_state, detail.config.target_release_no) == ("running", 1)
    assert [(g.type, g.name, g.permission) for g in detail.grants] == [
        ("user", BOB, "CAN_USE"),
        ("user", CAROL, "CAN_MANAGE"),
    ]
    assert detail.delegation.shared_sp_delegation_allowed is False


@pytest.mark.parametrize("person", [ALICE, CAROL])
async def test_an_app_admin_gets_403_the_list_is_read_only_for_them(person: str) -> None:
    registry, reloads = seeded_registry(), Reloads()

    reply = await call(
        edge(registry, reloads),
        "PUT",
        SALES_PATH,
        email=person,
        json_body={"version": 1, "rules": []},
    )

    assert reply.status == 403
    assert unchanged(registry, reloads)
    assert app_rules(registry) == []


async def test_a_person_who_can_only_use_the_app_gets_403_and_a_stranger_gets_404() -> None:
    registry, reloads = seeded_registry(), Reloads()
    app = edge(registry, reloads)
    body = {"version": 1, "rules": []}

    user = await call(app, "PUT", SALES_PATH, email=BOB, json_body=body)
    stranger = await call(app, "PUT", SALES_PATH, email=FRANK, json_body=body)

    assert user.status == 403
    assert stranger.status == 404  # the same answer as for an app that does not exist
    assert unchanged(registry, reloads)


async def test_an_unknown_app_is_404_for_a_platform_admin() -> None:
    registry, reloads = seeded_registry(), Reloads()

    reply = await call(
        edge(registry, reloads),
        "PUT",
        f"{MOUNT}/apps/nothing/egress",
        email=DANA,
        json_body={"version": 1, "rules": []},
    )

    assert reply.status == 404
    assert unchanged(registry, reloads)


async def test_a_stale_version_is_409_and_changes_nothing() -> None:
    registry, reloads = seeded_registry(), Reloads()
    app = edge(registry, reloads)
    await call(app, "PUT", SALES_PATH, email=DANA, json_body={"version": 1, "rules": ALLOW_LIST})
    audited, signalled = len(registry.audit_rows), len(reloads.calls)

    reply = await call(app, "PUT", SALES_PATH, email=DANA, json_body={"version": 1, "rules": []})

    assert reply.status == 409
    assert app_rules(registry) == ALLOW_LIST
    assert (len(registry.audit_rows), len(reloads.calls)) == (audited, signalled)


async def test_the_version_is_required_for_an_app_and_unknown_for_the_global_list() -> None:
    registry, reloads = seeded_registry(), Reloads()
    app = edge(registry, reloads)

    missing = await call(app, "PUT", SALES_PATH, email=DANA, json_body={"rules": []})
    extra = await call(app, "PUT", GLOBAL_PATH, email=DANA, json_body={"version": 1, "rules": []})

    assert missing.status == 400 and "version" in missing.json()["error"]["message"]
    assert extra.status == 400
    assert unchanged(registry, reloads)


# --- what the editor refuses --------------------------------------------------------------------


@pytest.fixture(params=["global", "app"])
def target(request: pytest.FixtureRequest) -> tuple[str, Mapping[str, object]]:
    """The two routes, as (path, extra body fields)."""
    if request.param == "global":
        return GLOBAL_PATH, {}
    return SALES_PATH, {"version": 1}


def nothing_stored(registry: StubRegistry) -> bool:
    return global_rules(registry) is None and app_rules(registry) == []


@pytest.mark.parametrize(
    ("bad_rule", "field"),
    [
        ({**rule(), "action": "permit"}, "rules.0.action"),
        ({**rule(), "cidr": "10.0.0.1/8"}, "rules.0.cidr"),  # host bits set
        ({**rule(), "cidr": "not-an-address"}, "rules.0.cidr"),
        ({**rule(), "cidr": "2001:db8::/32"}, "rules.0.cidr"),  # IPv4 only
        ({**rule(), "port": "70000"}, "rules.0.port"),
        ({**rule(), "port": "0443"}, "rules.0.port"),
        ({**rule(), "extra": "field"}, "rules.0.extra"),
        ({k: v for k, v in rule().items() if k != "comment"}, "rules.0.comment"),
    ],
)
async def test_an_item_that_is_not_an_egress_rule_is_400_naming_its_field(
    target: tuple[str, Mapping[str, object]], bad_rule: Rule, field: str
) -> None:
    path, extra = target
    registry, reloads = seeded_registry(), Reloads()

    reply = await call(
        edge(registry, reloads), "PUT", path, email=DANA, json_body={**extra, "rules": [bad_rule]}
    )

    assert reply.status == 400
    assert field in reply.json()["error"]["message"]
    assert unchanged(registry, reloads) and nothing_stored(registry)


async def test_the_second_item_is_named_by_its_position_and_a_value_is_never_repeated(
    target: tuple[str, Mapping[str, object]],
) -> None:
    path, extra = target
    registry, reloads = seeded_registry(), Reloads()
    bad = rule(cidr=SUBMITTED_MARKER)

    reply = await call(
        edge(registry, reloads),
        "PUT",
        path,
        email=DANA,
        json_body={**extra, "rules": [rule(), bad]},
    )

    assert reply.status == 400
    assert "rules.1.cidr" in reply.json()["error"]["message"]
    assert SUBMITTED_MARKER not in reply.text


@pytest.mark.parametrize(
    "body", [[], "rules", {"rules": "not a list"}, {"rules": [1]}, {"rules": None}, {}]
)
async def test_a_body_that_is_not_a_rule_list_is_400(
    target: tuple[str, Mapping[str, object]], body: object
) -> None:
    path, extra = target
    registry, reloads = seeded_registry(), Reloads()
    sent = {**extra, **body} if isinstance(body, dict) else body

    reply = await call(edge(registry, reloads), "PUT", path, email=DANA, json_body=sent)

    assert reply.status == 400
    assert unchanged(registry, reloads)


async def test_a_body_that_is_not_json_is_400(target: tuple[str, Mapping[str, object]]) -> None:
    path, _ = target
    registry, reloads = seeded_registry(), Reloads()

    reply = await call(
        edge(registry, reloads), "PUT", path, email=DANA, headers=(("Content-Type", "text/plain"),)
    )

    assert reply.status == 400
    assert unchanged(registry, reloads)


@pytest.mark.parametrize(
    ("overlapping", "named"),
    [
        (rule("allow", "0.0.0.0/0"), ["rule 1", "@container_ip", "169.254.0.0/16"]),
        (rule("allow", "10.9.0.0/16", "443"), ["rule 1", "@container_ip"]),  # holds the address
        (rule("allow", "@container_ip/32"), ["rule 1", "@container_ip", "@appmux_port"]),
        (rule("allow", "169.254.169.254/32"), ["rule 1", "169.254.0.0/16"]),
        (rule("allow", "169.254.0.0/16", "80"), ["rule 1", "169.254.0.0/16"]),
        (rule("allow", "198.51.100.0/24", "@appmux_port"), ["rule 1", "@appmux_port"]),
    ],
)
async def test_an_allow_that_overlaps_a_closed_target_is_400_naming_the_rule_and_the_target(
    target: tuple[str, Mapping[str, object]], overlapping: Rule, named: Sequence[str]
) -> None:
    path, extra = target
    registry, reloads = seeded_registry(), Reloads()

    reply = await call(
        edge(registry, reloads),
        "PUT",
        path,
        email=DANA,
        json_body={**extra, "rules": [overlapping]},
    )

    assert reply.status == 400
    message = reply.json()["error"]["message"]
    for name in named:
        assert name in message
    assert CONTAINER_IP not in reply.text  # the address stays unnamed
    assert unchanged(registry, reloads) and nothing_stored(registry)


async def test_every_offending_rule_is_named_not_only_the_first(
    target: tuple[str, Mapping[str, object]],
) -> None:
    path, extra = target
    registry, reloads = seeded_registry(), Reloads()
    rules = [
        rule("allow", "0.0.0.0/0"),
        rule("deny", "203.0.113.0/24"),
        rule("allow", "169.254.1.1/32"),
    ]

    reply = await call(
        edge(registry, reloads), "PUT", path, email=DANA, json_body={**extra, "rules": rules}
    )

    message = reply.json()["error"]["message"]
    assert "rule 1" in message and "rule 3" in message and "rule 2" not in message


async def test_a_deny_may_cover_the_closed_targets(
    target: tuple[str, Mapping[str, object]],
) -> None:
    path, extra = target
    registry, reloads = seeded_registry(), Reloads()
    denies = [
        rule("deny", "@container_ip/32", "any", "the container's own address"),
        rule("deny", "169.254.0.0/16", "any", "metadata and link-local"),
        rule("deny", "0.0.0.0/0", "@appmux_port", "appmux's port"),
    ]

    reply = await call(
        edge(registry, reloads), "PUT", path, email=DANA, json_body={**extra, "rules": denies}
    )

    assert reply.status == 200


async def test_the_check_uses_the_containers_real_address_not_a_probe(
    target: tuple[str, Mapping[str, object]],
) -> None:
    # 10.9.0.0/16 holds the container's address here, and does not hold another one.
    path, extra = target
    body = {**extra, "rules": [rule("allow", "10.9.0.0/16", "443")]}
    elsewhere = ClosedTargets(container_ip=IPv4Address("192.0.2.77"), appmux_port=APPMUX_PORT)

    refused = await call(
        edge(seeded_registry(), Reloads()), "PUT", path, email=DANA, json_body=body
    )
    accepted = await call(
        edge(seeded_registry(), Reloads(), elsewhere), "PUT", path, email=DANA, json_body=body
    )

    assert (refused.status, accepted.status) == (400, 200)


@pytest.mark.parametrize("configured", ["unknown", "absent"])
async def test_without_the_containers_address_the_check_cannot_run_and_nothing_is_saved(
    target: tuple[str, Mapping[str, object]], configured: str
) -> None:
    path, extra = target
    registry, reloads = seeded_registry(), Reloads()
    app = (
        edge(registry, reloads, None)
        if configured == "unknown"
        else create_edge_app(registry=registry, group_source=FakeGroups(), egress_reloader=reloads)
    )

    reply = await call(app, "PUT", path, email=DANA, json_body={**extra, "rules": ALLOW_LIST})

    assert reply.status == 503
    assert reply.json()["error"]["code"] == "egress_targets_unknown"
    assert unchanged(registry, reloads) and nothing_stored(registry)


# --- applying the list --------------------------------------------------------------------------


async def test_a_reload_that_fails_is_503_with_the_rules_saved_and_a_second_save_retries_it(
    target: tuple[str, Mapping[str, object]],
) -> None:
    path, extra = target
    registry, reloads = seeded_registry(), Reloads()
    reloads.error = OSError(f"signal failed {SUBMITTED_MARKER}")
    app = edge(registry, reloads)

    failed = await call(app, "PUT", path, email=DANA, json_body={**extra, "rules": ALLOW_LIST})

    assert failed.status == 503
    assert failed.json()["error"]["code"] == "egress_not_applied"
    assert SUBMITTED_MARKER not in failed.text  # the error's text stays in the log
    assert len(registry.audit_rows) == 1  # saved and recorded
    stored = global_rules(registry) if path == GLOBAL_PATH else app_rules(registry)
    assert stored == ALLOW_LIST

    reloads.error = None
    version = {"version": 2} if extra else {}
    retried = await call(app, "PUT", path, email=DANA, json_body={**version, "rules": ALLOW_LIST})
    assert retried.status == 200
    assert len(reloads.calls) == 2


async def test_a_registry_that_is_down_is_503_and_nobody_is_told(
    target: tuple[str, Mapping[str, object]],
) -> None:
    path, extra = target
    registry, reloads = seeded_registry(), Reloads()
    registry.go_down()

    reply = await call(
        edge(registry, reloads), "PUT", path, email=DANA, json_body={**extra, "rules": ALLOW_LIST}
    )

    assert reply.status == 503
    assert reloads.calls == []


@pytest.mark.parametrize("path", [GLOBAL_PATH, SALES_PATH])
async def test_identity_and_same_origin_are_checked_before_anything(path: str) -> None:
    registry, reloads = seeded_registry(), Reloads()
    app = edge(registry, reloads)
    body = {"version": 1, "rules": []} if path == SALES_PATH else {"rules": []}

    anonymous = await call(app, "PUT", path, json_body=body)
    cross_origin = await call(app, "PUT", path, email=DANA, json_body=body, same_origin=False)

    assert (anonymous.status, cross_origin.status) == (401, 403)
    assert unchanged(registry, reloads)


# --- where the closed targets come from ---------------------------------------------------------

ROUTE_TABLE: Final = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t0102000A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t0002000A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n"
)


def test_the_default_gateway_is_read_from_the_route_table() -> None:
    assert entry._default_gateway(ROUTE_TABLE) == IPv4Address("10.0.2.1")


@pytest.mark.parametrize(
    "table",
    [
        "",
        "Iface\tDestination\tGateway\tFlags\n",
        "eth0\t0002000A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n",  # no default route
        "eth0\t00000000\t0102000A\t0002\t0\t0\t0\t00000000\t0\t0\t0\n",  # the route is not up
        "eth0\t00000000\tnot-hex\t0003\t0\t0\t0\t00000000\t0\t0\t0\n",
        "eth0\t00000000\n",
    ],
)
def test_a_route_table_without_a_usable_default_route_gives_no_gateway(table: str) -> None:
    assert entry._default_gateway(table) is None


def test_a_default_route_with_no_gateway_address_is_not_used() -> None:
    table = "eth0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"

    assert entry._default_gateway(table) is None


# --- the running service's wiring ---------------------------------------------------------------


def test_the_closed_targets_are_the_containers_address_and_the_apps_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(entry, "_container_ip", lambda: IPv4Address(CONTAINER_IP))

    assert entry._closed_targets(settings_for_wiring())() == TARGETS


def test_the_container_address_is_looked_up_until_it_is_found_and_then_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[IPv4Address | None] = [None, IPv4Address(CONTAINER_IP)]
    lookups: list[None] = []

    def container_ip() -> IPv4Address | None:
        lookups.append(None)
        return answers.pop(0) if answers else None

    monkeypatch.setattr(entry, "_container_ip", container_ip)
    source = entry._closed_targets(settings_for_wiring())

    assert [source(), source(), source()] == [None, TARGETS, TARGETS]
    assert len(lookups) == 2  # none after it was found


@pytest.mark.parametrize(("address", "status"), [(CONTAINER_IP, 200), (None, 503)])
async def test_the_real_wiring_gives_the_egress_routes_the_containers_address(
    monkeypatch: pytest.MonkeyPatch, address: str | None, status: int
) -> None:
    monkeypatch.setattr(
        entry, "_container_ip", lambda: None if address is None else IPv4Address(address)
    )
    registry = seeded_registry()
    _, build = entry._load_wiring(settings_for_wiring(), Gate())  # opens no connection

    reply = await call(
        build(registry), "PUT", GLOBAL_PATH, email=DANA, json_body={"rules": ALLOW_LIST}
    )

    assert reply.status == status
