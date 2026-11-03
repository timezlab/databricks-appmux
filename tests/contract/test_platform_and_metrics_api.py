"""``GET /platform``, ``PUT /platform/settings/{key}``, ``GET /audit`` and ``GET /metrics`` (T166).

The routes run behind the real guard and authorizer on the in-memory registry, with a fake group
lookup, a scripted canary behind the shared ``SelfCheckState`` and a fixed memory ledger (the
reconciler's books are a seam: ``PlatformRoutes(memory=...)``). What is pinned:

* ``GET /platform`` (``platform.view``, ``CAN_MANAGE`` on the platform) answers the contract's
  ``PlatformView`` from the latest snapshot alone: the effective value of every settable key (the
  default for a key never set), the admins in force, the global egress, the latest self-check, the
  memory against the slot limit and every app ordered by display name then slug. It never answers
  with an invented self-check or memory: while either is unknown it is ``503``;
* ``PUT /platform/settings/{key}`` (``settings.edit``) stores a value of the key's own type in one
  transaction with one ``settings.edit`` audit row; an unknown key, a key with its own route
  (``platform_admins``, ``egress_global``), a value of the wrong type or range, ``null`` and a
  value that breaks the rule between ``max_locked_releases`` and ``max_releases_per_app`` are
  ``400`` and write nothing; the later write wins (no ``version``, TD-013);
* ``GET /audit`` (``platform.view``) pages the audit rows newest first inside a half-open window,
  filtered by app, with a limit of 1 to 1000; a row that holds something shaped like a credential
  (a manual row, say) is withheld, never repeated;
* ``GET /metrics`` is open to ``CAN_MANAGE`` on the app asked for, or to a platform admin for any
  app, for ``_platform`` and for all of them; a window over 1440 minutes is ``400``; more rows than
  the cap drop the oldest whole minutes, move ``from`` to the oldest minute kept and say so;
* ``GET /metrics?step=`` (T193) sums the minutes of each step by the rule of each column (counts
  and histograms added, peaks maximised, gauges the last minute's value), starts every step on its
  UTC boundary, leaves empty steps out, and takes a window of up to 7 days if it holds at most 1440
  steps (else ``400 window_too_long`` naming the step that fits); ``GET /metrics/breakdown``
  (``platform.view``) ranks the apps by requests, returns the top ``limit`` as series and sums the
  rest as ``other``.

A reader of a response never sees the visitor's access token, a stack trace or a submitted value.
Starting data: see ``edge_harness`` (``sales`` owned by alice, bob ``CAN_USE``, carol
``CAN_MANAGE``; ``docs`` owned by frank; dana is the platform admin). All emails are placeholders.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final, get_args

import httpx
import pytest
from edge_harness import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    FRANK,
    NOW,
    VISITOR_TOKEN,
    AdminApi,
    FakeGroups,
    ScriptedCanary,
    app_row,
    canary_text,
    seed_standard_rows,
    seeded_registry,
)
from pydantic import AwareDatetime
from stub_registry import StubRegistry

from appmux.admin.api import RouteProvider
from appmux.admin.authorizer import RegistryAuthorizer
from appmux.admin.metrics import MAX_METRIC_ROWS, METRIC_STEPS, MetricsRoutes
from appmux.admin.platform import SETTABLE_KEYS, PlatformRoutes
from appmux.admin.schemas import (
    AuditList,
    MetricsBreakdown,
    MetricsPoint,
    MetricsResponse,
    MetricsRow,
    MetricStep,
    PlatformSetting,
    PlatformView,
)
from appmux.reconciler.admission import MemoryLedger
from appmux.registry.metric_steps import BREAKDOWN_COLUMNS
from appmux.registry.models import AuditLogRow, DroppedByRule, MetricRow
from appmux.sandbox.self_check import CANARY_CHECK_NAME, CHECK_IDS, SelfCheckState
from appmux.settings import PlatformSettings

# What the admin API answers at the moment of the call: three hours after the registry's clock,
# which stamps every row it writes with ``NOW``.
CLOCK_NOW: Final = NOW + timedelta(hours=3)
LEDGER: Final = MemoryLedger(limit_mb=8192, reserve_mb=1024, committed_mb=5775)
# Text that must never come back to the client from a refused body.
SUBMITTED_MARKER: Final = "marker-value-from-the-client"
LEAKS: Final = (VISITOR_TOKEN, "Traceback", "SELECT ", SUBMITTED_MARKER)
NON_ADMINS: Final = (ALICE, BOB, CAROL, FRANK)
ERIN: Final = "erin@example.com"  # in the bootstrap list only

# A valid value of each settable key, different from its default.
VALID_VALUES: Final[dict[str, object]] = {
    "memory_reserve_mb": 2048,
    "package_mode": "index",
    "default_wheel_sources": ["/Workspace/Shared/wheels"],
    "shared_sp_delegation_allowed": True,
    "max_releases_per_app": 12,
    "max_locked_releases": 2,
    "request_timeout_s": 200,
    "max_request_body_mb": 50,
    "max_concurrent_requests": 32,
    "build_timeout_s": 900,
    "max_processes_per_child": 128,
    "child_cpu_pin": False,
    "attach_check_warehouse_id": "a1b2c3d4e5f67890",
}


def passing_state() -> SelfCheckState:
    """A self-check state whose canary has already run once and passed."""
    state = SelfCheckState(ScriptedCanary(), clock=lambda: NOW)
    asyncio.run(state.run())
    return state


class AuditRegistry(StubRegistry):
    """A lane-local fake: ``StubRegistry`` plus audit rows a test places, with their own stamps.

    The stub stamps every audit row it writes with its clock, and no code path writes a manual row
    (``db:<role>`` with free text in ``detail``). The rows placed here are read back next to the
    ones the routes write.
    """

    def __init__(self) -> None:
        super().__init__(clock=lambda: NOW)
        self.placed: list[AuditLogRow] = []

    async def read_audit(
        self, *, app_slug: str | None, start: AwareDatetime, end: AwareDatetime, limit: int
    ) -> tuple[AuditLogRow, ...]:
        written = await super().read_audit(
            app_slug=app_slug, start=start, end=end, limit=limit + len(self.placed)
        )
        both = [
            *written,
            *(
                row
                for row in self.placed
                if start <= row.ts < end and (app_slug is None or row.app_slug == app_slug)
            ),
        ]
        newest_first = sorted(both, key=lambda row: (row.ts, row.id), reverse=True)
        return tuple(newest_first[:limit]) if limit > 0 else ()


class Platform:
    """The admin API with the platform and metrics providers, over one registry."""

    def __init__(
        self,
        registry: StubRegistry | None = None,
        *,
        state: SelfCheckState | None = None,
        ledger: MemoryLedger | None = LEDGER,
        bootstrap_admins: Sequence[str] = (),
        max_rows: int = MAX_METRIC_ROWS,
    ) -> None:
        self.state = state if state is not None else passing_state()

        def providers(_authorizer: RegistryAuthorizer) -> list[RouteProvider]:
            return [
                PlatformRoutes(
                    self_check=self.state,
                    memory=None if ledger is None else (lambda: ledger),
                    bootstrap_admins=bootstrap_admins,
                ),
                MetricsRoutes(clock=lambda: CLOCK_NOW, max_rows=max_rows),
            ]

        self.api = AdminApi(
            registry if registry is not None else seeded_registry(),
            providers,
            groups=FakeGroups(),
            bootstrap_admins=bootstrap_admins,
        )
        self.registry = self.api.registry
        self.call = self.api.call

    def get_platform(self, email: str | None = DANA) -> httpx.Response:
        return self.call("GET", "/platform", email)

    def put_setting(
        self, key: str, value: object, email: str | None = DANA, **options: Any
    ) -> httpx.Response:
        return self.call(
            "PUT", f"/platform/settings/{key}", email, json={"value": value}, **options
        )

    def stored(self, key: str) -> object:
        row = self.registry.snapshot.setting(key)
        return None if row is None else row.value

    def settings_audit(self) -> list[tuple[str, str, str | None, dict[str, object]]]:
        return [
            (row.actor, row.action, row.app_slug, row.detail) for row in self.registry.audit_rows
        ]


def assert_no_leak(response: httpx.Response) -> None:
    for text in LEAKS:
        assert text not in response.text


def error_of(response: httpx.Response) -> tuple[str, str]:
    body = response.json()["error"]
    return body["code"], body["message"]


# --- GET /platform ------------------------------------------------------------------------------


def test_a_platform_admin_reads_the_platform_page_with_the_defaults_for_unset_keys() -> None:
    platform = Platform()

    response = platform.get_platform()

    assert response.status_code == 200
    view = PlatformView.model_validate_json(response.content)
    defaults = PlatformSettings()
    assert view.settings.model_dump() == {
        name: getattr(defaults, name) for name in view.settings.model_dump()
    }
    assert set(view.settings.model_dump()) == set(SETTABLE_KEYS)
    assert [(admin.type, admin.name) for admin in view.admins] == [("user", DANA)]
    assert view.egress_global == defaults.egress_global  # the two deny rules of the design
    assert_no_leak(response)


def test_a_stored_value_replaces_the_default_and_the_other_keys_keep_theirs() -> None:
    registry = seeded_registry()
    registry.seed(
        "platform_settings",
        {"key": "max_releases_per_app", "value": 15, "updated_by": DANA, "updated_at": NOW},
    )
    registry.seed(
        "platform_settings",
        {"key": "package_mode", "value": "index", "updated_by": DANA, "updated_at": NOW},
    )

    view = PlatformView.model_validate_json(Platform(registry).get_platform().content)

    assert (view.settings.max_releases_per_app, view.settings.package_mode) == (15, "index")
    assert view.settings.max_locked_releases == 3  # never set: the default


def test_the_global_egress_is_the_stored_list() -> None:
    registry = seeded_registry()
    rules = [{"action": "deny", "cidr": "198.51.100.0/24", "port": "any", "comment": "partner"}]
    registry.seed(
        "platform_settings",
        {"key": "egress_global", "value": rules, "updated_by": DANA, "updated_at": NOW},
    )

    view = PlatformView.model_validate_json(Platform(registry).get_platform().content)

    assert [rule.model_dump() for rule in view.egress_global] == rules


def test_the_admins_are_the_setting_or_the_bootstrap_list_while_the_setting_is_unset() -> None:
    stored = seeded_registry()
    stored.seed(
        "platform_settings",
        {
            "key": "platform_admins",
            "value": [{"type": "user", "name": DANA}, {"type": "group", "name": "data-platform"}],
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )
    unset = StubRegistry(clock=lambda: NOW)

    from_setting = PlatformView.model_validate_json(Platform(stored).get_platform().content)
    from_bootstrap = PlatformView.model_validate_json(
        Platform(unset, bootstrap_admins=[ERIN]).get_platform(ERIN).content
    )

    assert [(a.type, a.name) for a in from_setting.admins] == [
        ("user", DANA),
        ("group", "data-platform"),
    ]
    assert [(a.type, a.name) for a in from_bootstrap.admins] == [("user", ERIN)]


def test_the_self_check_is_the_latest_result_with_each_check() -> None:
    failing = SelfCheckState(
        ScriptedCanary(canary_text(failing={"capabilities": "a capability is still held"})),
        clock=lambda: NOW,
    )
    asyncio.run(failing.run())

    view = PlatformView.model_validate_json(Platform(state=failing).get_platform().content)

    assert view.self_check.passed is False
    assert view.self_check.ran_at == NOW
    assert [check.name for check in view.self_check.checks] == list(CHECK_IDS)
    broken = [check for check in view.self_check.checks if not check.passed]
    assert [(check.name, check.detail) for check in broken] == [
        ("capabilities", "a capability is still held")
    ]


def test_a_passing_self_check_is_shown_as_passed() -> None:
    view = PlatformView.model_validate_json(Platform().get_platform().content)

    assert view.self_check.passed is True
    assert all(check.passed for check in view.self_check.checks)


def test_the_page_does_not_run_the_canary() -> None:
    canary = ScriptedCanary()
    state = SelfCheckState(canary, clock=lambda: NOW)
    asyncio.run(state.run())
    platform = Platform(state=state)

    platform.get_platform()
    platform.get_platform()

    assert canary.calls == 1  # reading the page is not running a self-check


def test_before_the_first_self_check_finished_the_page_is_503_not_an_invented_result() -> None:
    never_run = SelfCheckState(ScriptedCanary(), clock=lambda: NOW)

    response = Platform(state=never_run).get_platform()

    assert response.status_code == 503
    assert error_of(response)[0] == "self_check_pending"
    assert CANARY_CHECK_NAME not in response.text  # no made-up check row


def test_the_memory_is_the_ledger_of_the_slot() -> None:
    view = PlatformView.model_validate_json(Platform().get_platform().content)

    assert view.memory.model_dump() == {
        "limit_mb": 8192,
        "reserve_mb": 1024,
        "committed_mb": 5775,
        "free_mb": 1393,
    }


def test_an_over_committed_slot_shows_a_negative_free_figure() -> None:
    ledger = MemoryLedger(limit_mb=4096, reserve_mb=2048, committed_mb=2500)

    view = PlatformView.model_validate_json(Platform(ledger=ledger).get_platform().content)

    assert view.memory.free_mb == -452  # the debt is shown, not hidden


def test_without_a_memory_source_the_page_is_503_not_a_made_up_figure() -> None:
    response = Platform(ledger=None).get_platform()

    assert response.status_code == 503
    assert error_of(response)[0] == "memory_unknown"


def test_every_app_is_listed_by_display_name_then_slug_with_its_platform_fields() -> None:
    registry = seeded_registry()
    rules = [{"action": "deny", "cidr": "198.51.100.0/24", "port": "any", "comment": ""}]
    registry.seed("apps", {**app_row("sales", owner=ALICE), "obo_delegated": True, "egress": rules})
    registry.seed("apps", {**app_row("alpha", owner=FRANK), "display_name": "Sales"})
    registry.seed("apps", {**app_row("aardvark", owner=FRANK), "display_name": "Zebra"})

    view = PlatformView.model_validate_json(Platform(registry).get_platform().content)

    # "Sales" twice (alpha, sales), "Docs", "Zebra": the display name decides, then the slug.
    assert [app.slug for app in view.apps] == ["docs", "alpha", "sales", "aardvark"]
    sales = next(app for app in view.apps if app.slug == "sales")
    assert (sales.display_name, sales.owner, sales.memory_budget_mb) == ("Sales", ALICE, 256)
    assert (sales.obo_delegated, sales.shared_sp_delegated, sales.version) == (True, False, 1)
    assert [rule.model_dump() for rule in sales.egress] == rules
    assert sales.source_path == f"/Workspace/Users/{ALICE}/sales"


@pytest.mark.parametrize("person", NON_ADMINS)
def test_a_person_who_is_not_a_platform_admin_gets_403_on_the_page(person: str) -> None:
    response = Platform().get_platform(person)

    assert response.status_code == 403
    assert error_of(response)[0] == "forbidden"
    assert_no_leak(response)


def test_the_page_needs_an_identity() -> None:
    assert Platform().get_platform(None).status_code == 401


# --- PUT /platform/settings/{key} ---------------------------------------------------------------


def test_a_platform_admin_sets_a_key_and_gets_the_stored_setting_back() -> None:
    platform = Platform()

    response = platform.put_setting("max_releases_per_app", 12)

    assert response.status_code == 200
    setting = PlatformSetting.model_validate_json(response.content)
    assert (setting.key, setting.value, setting.updated_by) == ("max_releases_per_app", 12, DANA)
    assert setting.updated_at == NOW  # the registry's stamp
    assert platform.stored("max_releases_per_app") == 12


def test_the_write_records_one_settings_edit_audit_row_without_the_value() -> None:
    platform = Platform()

    platform.put_setting("package_mode", "index")

    assert platform.settings_audit() == [
        (DANA, "settings.edit", None, {"setting": "package_mode"}),
    ]


@pytest.mark.parametrize("key", SETTABLE_KEYS)
def test_every_settable_key_takes_a_value_of_its_own_type_and_the_page_shows_it(key: str) -> None:
    platform = Platform()
    value = VALID_VALUES[key]

    response = platform.put_setting(key, value)

    assert response.status_code == 200, response.text
    assert response.json()["value"] == value
    view = PlatformView.model_validate_json(platform.get_platform().content)
    assert getattr(view.settings, key) == value


def test_every_settable_key_has_a_test_value() -> None:
    assert set(VALID_VALUES) == set(SETTABLE_KEYS)


def test_the_later_write_wins_with_no_version_to_send() -> None:
    # TD-013: two admins saving at once end as the later one; the second write is an update.
    platform = Platform()

    platform.put_setting("request_timeout_s", 100)
    second = platform.put_setting("request_timeout_s", 250, email=DANA)

    assert second.status_code == 200
    assert platform.stored("request_timeout_s") == 250
    assert len(platform.registry.audit_rows) == 2


def test_the_same_value_saved_again_is_written_and_audited_again() -> None:
    platform = Platform()

    platform.put_setting("child_cpu_pin", False)
    platform.put_setting("child_cpu_pin", False)

    assert len(platform.registry.audit_rows) == 2


@pytest.mark.parametrize("person", NON_ADMINS)
def test_a_person_who_is_not_a_platform_admin_gets_403_and_nothing_is_written(
    person: str,
) -> None:
    platform = Platform()

    response = platform.put_setting("max_releases_per_app", 12, email=person)

    assert response.status_code == 403
    assert platform.stored("max_releases_per_app") is None
    assert platform.registry.audit_rows == ()


def test_a_change_from_another_origin_is_refused_before_anything_is_read() -> None:
    platform = Platform()

    response = platform.put_setting("max_releases_per_app", 12, same_origin=False)

    assert response.status_code == 403
    assert platform.registry.audit_rows == ()


def test_an_unknown_key_is_400_and_the_answer_lists_the_keys_that_exist() -> None:
    platform = Platform()

    response = platform.put_setting("no_such_key", 1)

    assert response.status_code == 400
    code, message = error_of(response)
    assert code == "unknown_setting"
    assert "no_such_key" not in message  # a name from the client is not repeated
    assert all(key in message for key in SETTABLE_KEYS)
    assert platform.registry.audit_rows == ()


@pytest.mark.parametrize(
    ("key", "route"),
    [("platform_admins", "/platform/admins"), ("egress_global", "/platform/egress")],
)
def test_the_two_keys_with_their_own_route_are_400_and_name_that_route(
    key: str, route: str
) -> None:
    platform = Platform()
    before = platform.stored(key)

    response = platform.put_setting(key, [])

    assert response.status_code == 400
    assert error_of(response)[0] == "setting_has_own_route"
    assert route in error_of(response)[1]
    assert platform.registry.audit_rows == ()
    assert platform.stored(key) == before


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_releases_per_app", SUBMITTED_MARKER),  # a string for an integer
        ("max_releases_per_app", 10.5),
        ("max_releases_per_app", True),  # a bool is not an integer here
        ("max_releases_per_app", 3),  # below the 4 releases that must stay free
        ("max_releases_per_app", None),
        ("request_timeout_s", 291),  # the platform proxy ends a request at 300 s
        ("request_timeout_s", 0),
        ("memory_reserve_mb", 0),
        ("max_concurrent_requests", -1),
        ("package_mode", SUBMITTED_MARKER),
        ("package_mode", 1),
        ("shared_sp_delegation_allowed", "true"),
        ("shared_sp_delegation_allowed", 1),
        ("child_cpu_pin", SUBMITTED_MARKER),
        ("default_wheel_sources", SUBMITTED_MARKER),  # a string for a list
        ("default_wheel_sources", [SUBMITTED_MARKER, 5]),
        ("default_wheel_sources", [""]),
        ("attach_check_warehouse_id", ""),
        ("attach_check_warehouse_id", 7),
        ("attach_check_warehouse_id", None),
        ("build_timeout_s", {"seconds": 5}),
    ],
)
def test_a_value_of_the_wrong_type_or_range_is_400_names_the_key_and_writes_nothing(
    key: str, value: object
) -> None:
    platform = Platform()

    response = platform.put_setting(key, value)

    assert response.status_code == 400, response.text
    code, message = error_of(response)
    assert code == "bad_request"
    assert key in message
    assert_no_leak(response)
    assert platform.stored(key) is None
    assert platform.registry.audit_rows == ()


def test_a_body_without_a_value_or_with_another_field_is_400() -> None:
    platform = Platform()
    path = "/platform/settings/max_releases_per_app"

    missing = platform.call("PUT", path, DANA, json={})
    extra = platform.call("PUT", path, DANA, json={"value": 12, "version": 1})
    not_json = platform.call("PUT", path, DANA, headers={"Content-Type": "text/plain"})

    assert (missing.status_code, extra.status_code, not_json.status_code) == (400, 400, 400)
    assert platform.registry.audit_rows == ()


def test_lowering_the_release_limit_below_the_locks_that_must_fit_is_400() -> None:
    # Locks 3 (the default) need at most 7 releases + 4 free: a limit of 6 would leave room for 2.
    platform = Platform()

    response = platform.put_setting("max_releases_per_app", 6)

    assert response.status_code == 400
    code, message = error_of(response)
    assert code == "bad_request"
    assert "max_locked_releases" in message and "max_releases_per_app" in message
    assert platform.stored("max_releases_per_app") is None
    assert platform.registry.audit_rows == ()


def test_raising_the_locks_above_what_the_release_limit_allows_is_400() -> None:
    platform = Platform()  # the default limit is 10, so at most 6 locks

    refused = platform.put_setting("max_locked_releases", 7)
    allowed = platform.put_setting("max_locked_releases", 6)

    assert refused.status_code == 400
    assert allowed.status_code == 200
    assert platform.stored("max_locked_releases") == 6


def test_the_rule_is_checked_against_the_stored_value_of_the_other_key() -> None:
    platform = Platform()

    # The limit goes up first, so the locks may follow; the other order would be refused.
    assert platform.put_setting("max_releases_per_app", 20).status_code == 200
    assert platform.put_setting("max_locked_releases", 16).status_code == 200
    assert platform.put_setting("max_releases_per_app", 19).status_code == 400
    assert platform.stored("max_releases_per_app") == 20


def test_a_stored_key_that_is_not_valid_does_not_hide_which_key_is_at_fault() -> None:
    registry = seeded_registry()
    # Written by hand, outside the route: a string where an integer belongs.
    registry.seed(
        "platform_settings",
        {"key": "request_timeout_s", "value": "soon", "updated_by": DANA, "updated_at": NOW},
    )
    platform = Platform(registry)

    page = platform.get_platform()
    write = platform.put_setting("max_releases_per_app", 12)

    assert page.status_code == 503
    assert error_of(page)[0] == "settings_invalid"
    assert "request_timeout_s" in error_of(page)[1]
    assert write.status_code == 400
    assert "request_timeout_s" in error_of(write)[1]
    assert platform.registry.audit_rows == ()
    assert platform.put_setting("request_timeout_s", 120).status_code == 200  # the way out
    assert platform.get_platform().status_code == 200


def test_a_registry_that_is_down_answers_503_and_writes_nothing() -> None:
    platform = Platform()
    platform.registry.go_down()

    response = platform.put_setting("max_releases_per_app", 12)

    assert response.status_code == 503
    assert_no_leak(response)


def test_a_missing_access_token_is_refused_by_the_registry_and_writes_nothing() -> None:
    platform = Platform()

    response = platform.call(
        "PUT",
        "/platform/settings/max_releases_per_app",
        DANA,
        json={"value": 12},
        headers={"X-Forwarded-Access-Token": ""},
    )

    assert response.status_code in (401, 403)
    assert platform.stored("max_releases_per_app") is None
    assert platform.registry.audit_rows == ()


# --- GET /audit ---------------------------------------------------------------------------------


def audit_row(
    row_id: int,
    ts: datetime,
    *,
    action: str = "grant.app",
    app_slug: str | None = "sales",
    actor: str = ALICE,
    detail: dict[str, Any] | None = None,
    reason: str | None = None,
) -> AuditLogRow:
    return AuditLogRow(
        id=row_id,
        ts=ts,
        actor=actor,
        action=action,
        app_slug=app_slug,
        detail={} if detail is None else detail,
        reason=reason,
    )


def platform_with_audit(*rows: AuditLogRow) -> Platform:
    registry = seed_standard_rows(AuditRegistry())
    registry.placed.extend(rows)
    return Platform(registry)


def audit_ids(response: httpx.Response) -> list[int]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def test_audit_rows_come_newest_first_in_the_contract_shape() -> None:
    hour = timedelta(hours=1)
    platform = platform_with_audit(
        audit_row(410, CLOCK_NOW - 3 * hour, action="request.approve", reason="per-viewer data"),
        audit_row(411, CLOCK_NOW - 2 * hour, action="release.activate", detail={"release": 7}),
        audit_row(412, CLOCK_NOW - hour, action="release.view_source", actor=DANA),
    )

    response = platform.call("GET", "/audit", DANA)

    assert response.status_code == 200
    listing = AuditList.model_validate_json(response.content)
    assert [item.id for item in listing.items] == [412, 411, 410]
    assert listing.to == CLOCK_NOW
    assert listing.from_ == CLOCK_NOW - timedelta(hours=24)
    assert response.json()["from"] == "2026-10-29T12:00:00Z"  # the wire name is "from"
    middle = listing.items[1]
    assert (middle.actor, middle.action, middle.app_slug) == (ALICE, "release.activate", "sales")
    assert middle.detail == {"release": 7} and middle.reason is None
    assert listing.items[0].reason is None
    assert listing.items[2].reason == "per-viewer data"  # a reason is shown as it was stored


def test_the_default_window_is_the_day_before_now_and_the_end_is_not_included() -> None:
    day = timedelta(hours=24)
    platform = platform_with_audit(
        audit_row(1, CLOCK_NOW - day - timedelta(seconds=1)),  # just before the window
        audit_row(2, CLOCK_NOW - day),  # the first instant of the window
        audit_row(3, CLOCK_NOW - timedelta(seconds=1)),
        audit_row(4, CLOCK_NOW),  # the end instant is outside the half-open window
    )

    assert audit_ids(platform.call("GET", "/audit", DANA)) == [3, 2]


def test_from_and_to_choose_the_window() -> None:
    platform = platform_with_audit(
        audit_row(1, datetime.fromisoformat("2026-10-30T08:00:00+00:00")),
        audit_row(2, datetime.fromisoformat("2026-10-30T09:30:00+00:00")),
        audit_row(3, datetime.fromisoformat("2026-10-30T10:00:00+00:00")),
    )

    response = platform.call(
        "GET", "/audit?from=2026-10-30T09:00:00Z&to=2026-10-30T10:00:00Z", DANA
    )

    assert audit_ids(response) == [2]
    assert response.json()["from"] == "2026-10-30T09:00:00Z"
    assert response.json()["to"] == "2026-10-30T10:00:00Z"


def test_a_time_with_an_offset_is_read_as_that_instant() -> None:
    platform = platform_with_audit(
        audit_row(1, datetime.fromisoformat("2026-10-30T08:30:00+00:00"))
    )

    response = platform.call(
        "GET", "/audit?from=2026-10-30T10:00:00%2B02:00&to=2026-10-30T09:00:00Z", DANA
    )

    assert audit_ids(response) == [1]
    assert response.json()["from"] == "2026-10-30T08:00:00Z"


def test_the_app_filter_keeps_that_apps_rows_and_includes_an_app_that_no_longer_exists() -> None:
    platform = platform_with_audit(
        audit_row(1, CLOCK_NOW - timedelta(hours=1), app_slug="sales"),
        audit_row(2, CLOCK_NOW - timedelta(hours=1), app_slug="docs"),
        audit_row(3, CLOCK_NOW - timedelta(hours=1), app_slug="deleted-app"),
        audit_row(4, CLOCK_NOW - timedelta(hours=1), app_slug=None, action="settings.edit"),
    )

    assert audit_ids(platform.call("GET", "/audit?app=sales", DANA)) == [1]
    assert audit_ids(platform.call("GET", "/audit?app=deleted-app", DANA)) == [3]
    assert sorted(audit_ids(platform.call("GET", "/audit", DANA))) == [1, 2, 3, 4]


def test_limit_caps_the_rows_and_defaults_to_100() -> None:
    rows = [audit_row(n, CLOCK_NOW - timedelta(minutes=n)) for n in range(1, 151)]
    platform = platform_with_audit(*rows)

    assert len(audit_ids(platform.call("GET", "/audit", DANA))) == 100
    assert audit_ids(platform.call("GET", "/audit?limit=2", DANA)) == [1, 2]  # newest first
    assert len(audit_ids(platform.call("GET", "/audit?limit=1000", DANA))) == 150


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=1001",
        "limit=-5",
        "limit=ten",
        "limit=",
        "limit=1.5",
        "from=yesterday",
        "to=2026-10-30",
        "from=2026-10-30T09:00:00",  # no zone: the instant would be a guess
        "from=2026-10-30T10:00:00Z&to=2026-10-30T09:00:00Z",
        "from=2026-10-30T10:00:00Z&to=2026-10-30T10:00:00Z",
        "app=Not%20A%20Slug",
    ],
)
def test_a_query_that_is_not_valid_is_400_and_repeats_nothing(query: str) -> None:
    platform = platform_with_audit()

    response = platform.call("GET", f"/audit?{query}", DANA)

    assert response.status_code == 400, response.text
    assert error_of(response)[0] == "bad_request"
    for part in query.split("&"):
        value = part.partition("=")[2]
        # A short value ("0") may appear in the fixed text by chance; a longer one is a copy.
        assert len(value) < 5 or value not in response.text


@pytest.mark.parametrize("person", NON_ADMINS)
def test_the_audit_is_for_platform_admins_only(person: str) -> None:
    platform = platform_with_audit(audit_row(1, CLOCK_NOW - timedelta(hours=1)))

    response = platform.call("GET", "/audit", person)

    assert response.status_code == 403


def test_a_row_the_routes_wrote_is_read_back_and_reading_writes_no_row() -> None:
    platform = platform_with_audit()
    platform.put_setting("package_mode", "index")
    written = len(platform.registry.audit_rows)

    # The stub stamps a row with ``NOW``, inside the default window of the routes' clock.
    listing = platform.call("GET", "/audit", DANA)

    items = AuditList.model_validate_json(listing.content).items
    assert [(item.actor, item.action, item.app_slug, item.detail) for item in items] == [
        (DANA, "settings.edit", None, {"setting": "package_mode"})
    ]
    assert len(platform.registry.audit_rows) == written  # a read leaves no audit row


@pytest.mark.parametrize(
    ("detail", "reason"),
    [
        ({"note": "password=hunter2hunter2"}, None),  # a key the writer never produces
        ({"release": 7}, "token=abc123def456ghi789"),
        ({"path": "dapi0123456789abcdef0123"}, None),
        ({"path": "ok.py", "setting": "Bearer abcdefghijklmnop"}, None),
    ],
)
def test_a_row_with_something_shaped_like_a_credential_is_withheld_not_repeated(
    detail: dict[str, Any], reason: str | None
) -> None:
    platform = platform_with_audit(
        audit_row(
            9,
            CLOCK_NOW - timedelta(hours=1),
            actor="db:operator",
            action="release.view_source",
            detail=detail,
            reason=reason,
        ),
        audit_row(8, CLOCK_NOW - timedelta(hours=2), detail={"release": 3}),
    )

    response = platform.call("GET", "/audit", DANA)

    assert response.status_code == 200
    for fragment in ("hunter2", "abc123def456", "dapi0123456789", "abcdefghijklmnop"):
        assert fragment not in response.text
    withheld, intact = response.json()["items"]
    assert (withheld["id"], withheld["action"], withheld["actor"]) == (
        9,
        "release.view_source",
        "db:operator",
    )  # the fact that it happened stays
    assert withheld["detail"] == {} and withheld["reason"] is not None
    assert intact["detail"] == {"release": 3}  # an ordinary row is shown whole


def test_the_audit_needs_the_registry() -> None:
    platform = platform_with_audit()
    platform.registry.go_down()

    response = platform.call("GET", "/audit", DANA)

    assert response.status_code == 503
    assert_no_leak(response)


# --- GET /metrics -------------------------------------------------------------------------------

HOUR_START: Final = CLOCK_NOW - timedelta(hours=1)  # 11:00


def metric(app: str, minute: datetime, **values: Any) -> MetricRow:
    platform_columns: dict[str, int] = (
        {
            "mem_used_mb": 1500,
            "mem_limit_mb": 6144,
            "children_running": 1,
            "children_registered": 2,
            "admission_rejections": 0,
            "registry_errors": 0,
            "registry_ms_max": 14,
            "edge_rss_mb": 160,
        }
        if app == "_platform"
        else {}
    )
    return MetricRow(app_slug=app, minute=minute, lat_hist=[0] * 11, **platform_columns, **values)


def platform_with_metrics(rows: Sequence[MetricRow], **options: Any) -> Platform:
    registry = seeded_registry()
    asyncio.run(registry.append_metrics(rows))
    return Platform(registry, **options)


def at(minute: int, *, hour: int = 11) -> datetime:
    return datetime.fromisoformat(f"2026-10-30T{hour:02d}:{minute:02d}:00+00:00")


def minutes_of(response: httpx.Response) -> list[tuple[str, str]]:
    assert response.status_code == 200, response.text
    return [(item["minute"][11:16], item["app_slug"]) for item in response.json()["items"]]


def test_a_platform_admin_reads_every_app_and_the_platform_row_oldest_first() -> None:
    platform = platform_with_metrics(
        [
            metric("sales", at(5), requests=412, r2xx=389, r3xx=11, r4xx=9, r5xx=3),
            metric("_platform", at(5), requests=412),
            metric("docs", at(5), requests=5),
            metric("sales", at(6), requests=1),
            metric("_platform", at(6), requests=1),
        ]
    )

    response = platform.call("GET", "/metrics", DANA)

    assert minutes_of(response) == [
        ("11:05", "_platform"),
        ("11:05", "docs"),
        ("11:05", "sales"),
        ("11:06", "_platform"),
        ("11:06", "sales"),
    ]
    body = MetricsResponse.model_validate_json(response.content)
    assert body.truncated is False
    assert (body.from_, body.to) == (HOUR_START, CLOCK_NOW)  # the default window: the last hour
    sales = next(row for row in body.items if row.app_slug == "sales" and row.minute == at(5))
    assert (sales.requests, sales.r2xx, sales.r3xx, sales.r4xx, sales.r5xx) == (412, 389, 11, 9, 3)
    assert len(sales.lat_hist) == 11
    assert sales.mem_used_mb is None and sales.edge_rss_mb is None  # the platform columns
    platform_row = next(row for row in body.items if row.app_slug == "_platform")
    assert (platform_row.mem_used_mb, platform_row.mem_limit_mb) == (1500, 6144)
    assert platform_row.registry_ms_max == 14
    assert_no_leak(response)


def test_the_dropped_packets_by_rule_come_back_as_integers() -> None:
    platform = platform_with_metrics(
        [
            metric(
                "sales",
                at(5),
                egress_dropped=4,
                egress_dropped_by_rule=DroppedByRule({"app:2": 3, "global:1": 1}),
            )
        ]
    )

    [row] = platform.call("GET", "/metrics?app=sales", DANA).json()["items"]

    assert row["egress_dropped"] == 4
    assert row["egress_dropped_by_rule"] == {"app:2": 3, "global:1": 1}


def test_app_selects_one_series_and_platform_selects_the_platform_row() -> None:
    platform = platform_with_metrics(
        [metric("sales", at(5)), metric("docs", at(5)), metric("_platform", at(5))]
    )

    assert minutes_of(platform.call("GET", "/metrics?app=sales", DANA)) == [("11:05", "sales")]
    assert minutes_of(platform.call("GET", "/metrics?app=_platform", DANA)) == [
        ("11:05", "_platform")
    ]


def test_the_window_is_half_open_and_from_and_to_choose_it() -> None:
    platform = platform_with_metrics([metric("sales", at(m)) for m in (14, 15, 16, 17)])

    response = platform.call(
        "GET", "/metrics?app=sales&from=2026-10-30T11:15:00Z&to=2026-10-30T11:17:00Z", DANA
    )

    assert minutes_of(response) == [("11:15", "sales"), ("11:16", "sales")]
    assert response.json()["from"] == "2026-10-30T11:15:00Z"
    assert response.json()["to"] == "2026-10-30T11:17:00Z"
    assert response.json()["truncated"] is False


def test_a_window_of_1440_minutes_is_allowed_and_one_minute_more_is_400() -> None:
    platform = platform_with_metrics([])

    allowed = platform.call(
        "GET", "/metrics?from=2026-10-29T12:00:00Z&to=2026-10-30T12:00:00Z", DANA
    )
    refused = platform.call(
        "GET", "/metrics?from=2026-10-29T11:59:00Z&to=2026-10-30T12:00:00Z", DANA
    )

    assert allowed.status_code == 200
    assert refused.status_code == 400
    assert "1440" in error_of(refused)[1]


@pytest.mark.parametrize(
    "query",
    [
        "from=soon",
        "to=2026-10-30",
        "from=2026-10-30T09:00:00",
        "from=2026-10-30T11:00:00Z&to=2026-10-30T10:00:00Z",
        "app=Not%20A%20Slug",
    ],
)
def test_a_metrics_query_that_is_not_valid_is_400(query: str) -> None:
    response = platform_with_metrics([]).call("GET", f"/metrics?{query}", DANA)

    assert response.status_code == 400, response.text
    assert error_of(response)[0] == "bad_request"


def test_an_app_admin_reads_the_series_of_the_app_they_manage() -> None:
    platform = platform_with_metrics(
        [metric("sales", at(5)), metric("docs", at(5)), metric("_platform", at(5))]
    )

    for person in (ALICE, CAROL):  # the owner and a CAN_MANAGE holder
        assert minutes_of(platform.call("GET", "/metrics?app=sales", person)) == [
            ("11:05", "sales")
        ]


def test_an_app_admin_cannot_read_another_apps_series_the_platform_row_or_all_of_them() -> None:
    platform = platform_with_metrics(
        [metric("sales", at(5)), metric("docs", at(5)), metric("_platform", at(5))]
    )

    other_app = platform.call("GET", "/metrics?app=docs", CAROL)
    platform_row = platform.call("GET", "/metrics?app=_platform", CAROL)
    everything = platform.call("GET", "/metrics", CAROL)

    assert other_app.status_code == 404  # an app they cannot open looks like a missing one
    assert platform_row.status_code == 403
    assert everything.status_code == 403
    for response in (other_app, platform_row, everything):
        assert "requests" not in response.text


def test_a_person_who_can_only_use_the_app_or_is_a_stranger_gets_no_series() -> None:
    platform = platform_with_metrics([metric("sales", at(5))])

    can_use = platform.call("GET", "/metrics?app=sales", BOB)
    stranger = platform.call("GET", "/metrics?app=sales", FRANK)
    missing = platform.call("GET", "/metrics?app=nothing", FRANK)

    assert can_use.status_code == 403  # they can open the app but not manage it
    assert stranger.status_code == 404
    assert stranger.json() == missing.json()  # the same body as for an app that does not exist


def test_an_unknown_app_is_404_even_for_a_platform_admin() -> None:
    platform = platform_with_metrics([metric("sales", at(5))])

    assert platform.call("GET", "/metrics?app=nothing", DANA).status_code == 404


def test_metrics_need_an_identity_and_the_registry() -> None:
    platform = platform_with_metrics([metric("sales", at(5))])

    assert platform.call("GET", "/metrics", None).status_code == 401
    platform.registry.go_down()
    down = platform.call("GET", "/metrics?app=sales", DANA)
    assert down.status_code == 503
    assert_no_leak(down)


def small_cap_rows() -> list[MetricRow]:
    """Three apps over three minutes: nine rows."""
    return [
        metric(app, at(minute)) for minute in (5, 6, 7) for app in ("sales", "docs", "_platform")
    ]


def test_rows_over_the_cap_drop_the_oldest_whole_minutes_and_say_so() -> None:
    platform = platform_with_metrics(small_cap_rows(), max_rows=7)

    response = platform.call("GET", "/metrics", DANA)

    # Nine rows, room for seven: the oldest minute goes whole, six rows stay.
    assert minutes_of(response) == [
        ("11:06", "_platform"),
        ("11:06", "docs"),
        ("11:06", "sales"),
        ("11:07", "_platform"),
        ("11:07", "docs"),
        ("11:07", "sales"),
    ]
    body = response.json()
    assert body["truncated"] is True
    assert body["from"] == "2026-10-30T11:06:00Z"  # the oldest minute kept
    assert body["to"] == "2026-10-30T12:00:00Z"


def test_a_minute_the_database_returned_only_part_of_is_dropped_whole() -> None:
    # Ten rows, room for seven. The registry is asked for eight and returns the newest eight, so
    # its oldest minute (11:05) holds only one of its three series. A client must not see a
    # partial minute: 11:06 to 11:08 are whole and fit exactly (3 + 3 + 1 rows).
    rows = [
        metric(app, at(minute)) for minute in (5, 6, 7) for app in ("sales", "docs", "_platform")
    ]
    rows.append(metric("sales", at(8)))
    platform = platform_with_metrics(rows, max_rows=7)

    response = platform.call("GET", "/metrics", DANA)

    kept = minutes_of(response)
    assert {minute for minute, _ in kept} == {"11:06", "11:07", "11:08"}
    assert len(kept) == 7
    assert response.json()["truncated"] is True
    assert response.json()["from"] == "2026-10-30T11:06:00Z"


def test_exactly_the_cap_is_not_truncated() -> None:
    platform = platform_with_metrics(small_cap_rows(), max_rows=9)

    response = platform.call("GET", "/metrics", DANA)

    assert len(minutes_of(response)) == 9
    assert response.json()["truncated"] is False
    assert response.json()["from"] == "2026-10-30T11:00:00Z"


def test_the_real_cap_is_10000_rows_and_101_series_keep_whole_minutes() -> None:
    assert MAX_METRIC_ROWS == 10_000
    slugs = ["_platform"] + [f"app{n:02d}" for n in range(100)]
    base = datetime.fromisoformat("2026-10-29T13:00:00+00:00")
    rows = [metric(slug, base + timedelta(minutes=m)) for m in range(100) for slug in slugs]
    platform = platform_with_metrics(rows)

    response = platform.call(
        "GET", "/metrics?from=2026-10-29T13:00:00Z&to=2026-10-30T12:00:00Z", DANA
    )

    body = response.json()
    assert body["truncated"] is True
    assert len(body["items"]) <= MAX_METRIC_ROWS
    assert len(body["items"]) == 99 * 101  # 99 whole minutes of 101 series
    assert len({item["minute"] for item in body["items"]}) == 99
    assert body["from"] == body["items"][0]["minute"] == "2026-10-29T13:01:00Z"
    assert sum(item["minute"] == body["from"] for item in body["items"]) == 101


def test_the_rows_of_an_app_a_person_may_not_read_are_never_asked_for() -> None:
    # The registry is read for the one slug asked; a CAN_MANAGE holder's request never widens.
    registry = seeded_registry()
    asked: list[str | None] = []
    original = registry.read_metrics

    async def spy(**kwargs: Any) -> Any:
        asked.append(kwargs["app_slug"])
        return await original(**kwargs)

    registry.read_metrics = spy  # type: ignore[method-assign]  # a test double for one call
    platform = Platform(registry)

    platform.call("GET", "/metrics?app=sales", CAROL)
    platform.call("GET", "/metrics", CAROL)

    assert asked == ["sales"]


# --- the models the routes join have the same names -----------------------------------------------


def test_the_response_row_and_the_stored_row_have_the_same_columns() -> None:
    # ``metrics_row`` reads each response field from the stored row by name, so a field the stored
    # row lacks fails at once; this catches the other way round, a new column that the API would
    # silently leave out.
    assert set(MetricsRow.model_fields) == set(MetricRow.model_fields)


def test_every_settable_key_is_a_key_the_settings_model_validates() -> None:
    assert set(SETTABLE_KEYS) <= set(PlatformSettings.model_fields)
    assert set(PlatformSettings.model_fields) - set(SETTABLE_KEYS) == {
        "platform_admins",
        "egress_global",
    }


def test_the_step_names_are_the_schemas_and_a_point_holds_the_breakdown_columns() -> None:
    assert set(METRIC_STEPS) == set(get_args(MetricStep))
    assert set(MetricsPoint.model_fields) == {"minute", *BREAKDOWN_COLUMNS}


# --- GET /metrics?step= (T193) ------------------------------------------------------------------

PLATFORM_FIGURES: Final[dict[str, int]] = {
    "mem_used_mb": 1500,
    "mem_limit_mb": 6144,
    "children_running": 1,
    "children_registered": 2,
    "admission_rejections": 0,
    "registry_errors": 0,
    "registry_ms_max": 14,
    "edge_rss_mb": 160,
}


def minute_row(app: str, minute: datetime, **values: Any) -> MetricRow:
    """A stored minute whose every column may be set; the platform row gets its figures."""
    figures = PLATFORM_FIGURES if app == "_platform" else {}
    return MetricRow.model_validate(
        {"app_slug": app, "minute": minute, "lat_hist": [0] * 11, **figures, **values}
    )


def hist(*counts: int) -> list[int]:
    """A latency histogram whose first buckets are ``counts`` and the rest zero."""
    return [*counts, *([0] * (11 - len(counts)))]


def items_of(response: httpx.Response) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def test_a_15m_step_sums_the_counts_and_histograms_of_its_minutes_from_the_quarter() -> None:
    platform = platform_with_metrics(
        [
            minute_row(
                "sales",
                at(1),
                requests=10,
                r2xx=8,
                r5xx=2,
                bytes_out=100,
                restarts=1,
                lat_hist=hist(1, 1, 1),
                egress_dropped=1,
                egress_dropped_by_rule=DroppedByRule({"app:1": 1}),
            ),
            minute_row(
                "sales",
                at(14),
                requests=5,
                r2xx=5,
                bytes_out=50,
                lat_hist=hist(0, 2),
                egress_dropped=2,
                egress_dropped_by_rule=DroppedByRule({"app:1": 1, "global:1": 1}),
            ),
            minute_row("sales", at(15), requests=7, r2xx=7),
        ]
    )

    first, second = items_of(platform.call("GET", "/metrics?app=sales&step=15m", DANA))

    assert first["minute"] == "2026-10-30T11:00:00Z"
    assert (first["requests"], first["r2xx"], first["r5xx"]) == (15, 13, 2)
    assert (first["bytes_out"], first["restarts"]) == (150, 1)
    assert first["lat_hist"] == hist(1, 3, 1)
    assert first["egress_dropped"] == 3
    assert first["egress_dropped_by_rule"] == {"app:1": 2, "global:1": 1}
    assert (second["minute"], second["requests"]) == ("2026-10-30T11:15:00Z", 7)
    assert second["egress_dropped_by_rule"] == {}


def test_a_step_keeps_the_peaks_and_the_last_value_of_each_gauge() -> None:
    platform = platform_with_metrics(
        [
            minute_row("sales", at(1), in_flight_max=9, rss_mb_max=300, ws_open=4),
            minute_row("sales", at(10), in_flight_max=3, rss_mb_max=340, ws_open=1),
            minute_row(
                "_platform",
                at(1),
                mem_used_mb=2000,
                edge_rss_mb=170,
                mem_limit_mb=8192,
                children_running=2,
                children_registered=3,
                registry_ms_max=40,
                registry_errors=1,
                admission_rejections=1,
            ),
            minute_row(
                "_platform",
                at(10),
                mem_used_mb=1800,
                edge_rss_mb=180,
                mem_limit_mb=6144,
                children_running=1,
                children_registered=4,
                registry_ms_max=12,
                registry_errors=2,
                admission_rejections=0,
            ),
        ]
    )

    platform_row, sales = items_of(platform.call("GET", "/metrics?step=15m", DANA))

    assert platform_row["app_slug"] == "_platform"
    assert (platform_row["mem_used_mb"], platform_row["edge_rss_mb"]) == (2000, 180)  # peaks
    assert platform_row["registry_ms_max"] == 40
    assert platform_row["mem_limit_mb"] == 6144  # the last minute's gauges
    assert (platform_row["children_running"], platform_row["children_registered"]) == (1, 4)
    assert (platform_row["registry_errors"], platform_row["admission_rejections"]) == (3, 1)
    assert (sales["in_flight_max"], sales["rss_mb_max"], sales["ws_open"]) == (9, 340, 1)
    assert sales["mem_used_mb"] is None and sales["children_running"] is None


def test_a_window_that_cuts_a_step_counts_only_its_own_minutes() -> None:
    platform = platform_with_metrics(
        [minute_row("sales", at(minute), requests=1) for minute in (5, 7, 14, 20)]
    )

    response = platform.call(
        "GET",
        "/metrics?app=sales&step=15m&from=2026-10-30T11:07:00Z&to=2026-10-30T11:20:00Z",
        DANA,
    )

    # 11:05 is before the window and 11:20 is its end: the cut quarter holds 11:07 and 11:14.
    [step] = items_of(response)
    assert (step["minute"], step["requests"]) == ("2026-10-30T11:00:00Z", 2)
    assert response.json()["from"] == "2026-10-30T11:07:00Z"


def test_an_hour_step_starts_on_the_hour_and_an_empty_step_is_left_out() -> None:
    platform = platform_with_metrics(
        [
            minute_row("sales", at(59, hour=8), requests=3),
            minute_row("sales", at(0), requests=2),
            minute_row("sales", at(1), requests=4),
        ]
    )

    response = platform.call(
        "GET",
        "/metrics?app=sales&step=1h&from=2026-10-30T08:30:00Z&to=2026-10-30T11:30:00Z",
        DANA,
    )

    # 09:00 to 10:59 hold no minute: two steps, not four.
    assert [(item["minute"], item["requests"]) for item in items_of(response)] == [
        ("2026-10-30T08:00:00Z", 3),
        ("2026-10-30T11:00:00Z", 6),
    ]


def test_a_window_with_no_minutes_has_no_steps() -> None:
    platform = platform_with_metrics([minute_row("sales", at(5))])

    response = platform.call(
        "GET", "/metrics?step=5m&from=2026-10-30T10:00:00Z&to=2026-10-30T11:00:00Z", DANA
    )

    assert items_of(response) == []
    assert response.json()["truncated"] is False


def test_steps_over_the_cap_drop_the_oldest_whole_step_and_say_so() -> None:
    # Three apps over three five-minute steps are nine rows; room for seven keeps two steps.
    rows = [
        minute_row(app, at(minute))
        for minute in (0, 1, 5, 10)
        for app in ("sales", "docs", "_platform")
    ]
    platform = platform_with_metrics(rows, max_rows=7)

    response = platform.call("GET", "/metrics?step=5m", DANA)

    kept = items_of(response)
    assert [(item["minute"][11:16], item["app_slug"]) for item in kept] == [
        ("11:05", "_platform"),
        ("11:05", "docs"),
        ("11:05", "sales"),
        ("11:10", "_platform"),
        ("11:10", "docs"),
        ("11:10", "sales"),
    ]
    assert response.json()["truncated"] is True
    assert response.json()["from"] == "2026-10-30T11:05:00Z"


def test_a_step_of_1m_is_the_default() -> None:
    platform = platform_with_metrics([minute_row("sales", at(m), requests=m) for m in (1, 2)])

    assert (
        platform.call("GET", "/metrics?app=sales&step=1m", DANA).json()
        == platform.call("GET", "/metrics?app=sales", DANA).json()
    )


@pytest.mark.parametrize("query", ["step=2m", "step=15M", "step=", "step=1m&step=5m"])
def test_a_step_outside_the_list_is_400(query: str) -> None:
    response = platform_with_metrics([]).call("GET", f"/metrics?{query}", DANA)

    assert response.status_code == 400
    code, message = error_of(response)
    assert code == "bad_request"
    assert "step" in message


@pytest.mark.parametrize(
    ("start", "step"),
    [
        ("2026-10-29T12:00:00Z", "1m"),  # 24 hours
        ("2026-10-25T12:00:00Z", "5m"),  # 5 days: exactly 1440 steps
        ("2026-10-23T12:00:00Z", "15m"),  # 7 days
        ("2026-10-23T12:00:00Z", "1h"),
    ],
)
def test_a_window_of_up_to_7_days_and_1440_steps_is_allowed(start: str, step: str) -> None:
    platform = platform_with_metrics([])

    response = platform.call(
        "GET", f"/metrics?from={start}&to=2026-10-30T12:00:00Z&step={step}", DANA
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("start", "step", "named"),
    [
        ("2026-10-23T12:00:00Z", "1m", "15m"),  # 7 days at 1m
        ("2026-10-29T11:59:00Z", "1m", "5m"),  # 1441 minutes
        ("2026-10-25T11:59:00Z", "1m", "15m"),  # one minute over 5 days
        ("2026-10-25T11:59:00Z", "5m", "15m"),
    ],
)
def test_a_window_of_more_than_1440_steps_names_the_smallest_step_that_fits(
    start: str, step: str, named: str
) -> None:
    platform = platform_with_metrics([])

    response = platform.call(
        "GET", f"/metrics?from={start}&to=2026-10-30T12:00:00Z&step={step}", DANA
    )

    assert response.status_code == 400
    code, message = error_of(response)
    assert code == "window_too_long"
    assert f"step={named}" in message
    assert "1440" in message


def test_a_window_of_more_than_7_days_is_too_long_at_every_step() -> None:
    platform = platform_with_metrics([])

    response = platform.call(
        "GET", "/metrics?from=2026-10-23T11:59:00Z&to=2026-10-30T12:00:00Z&step=1h", DANA
    )

    assert response.status_code == 400
    code, message = error_of(response)
    assert code == "window_too_long"
    assert "7 days" in message
    assert "step=" not in message  # no step makes it fit


# --- GET /metrics/breakdown (T193) ---------------------------------------------------------------


def breakdown_platform(**options: Any) -> Platform:
    """docs 25 requests, sales 10, two apps no longer registered 3 each, and the platform row."""
    return platform_with_metrics(
        [
            minute_row(
                "docs",
                at(1),
                requests=20,
                r4xx=2,
                bytes_out=200,
                rss_mb_max=100,
                lat_hist=hist(2),
                ws_open=5,
            ),
            minute_row("docs", at(20), requests=5, r5xx=1, rss_mb_max=120),
            minute_row("sales", at(1), requests=10, rss_mb_max=300),
            minute_row("old", at(1), requests=3, rss_mb_max=70, egress_dropped=2, lat_hist=hist(1)),
            minute_row(
                "gone", at(2), requests=3, rss_mb_max=50, egress_dropped=1, lat_hist=hist(0, 1)
            ),
            minute_row("_platform", at(1), requests=41),
        ],
        **options,
    )


def breakdown_of(response: httpx.Response) -> MetricsBreakdown:
    assert response.status_code == 200, response.text
    return MetricsBreakdown.model_validate_json(response.content)


def test_the_breakdown_ranks_apps_by_requests_and_sums_the_rest_as_other() -> None:
    platform = breakdown_platform()

    response = platform.call("GET", "/metrics/breakdown?step=15m&limit=2", DANA)

    body = breakdown_of(response)
    assert body.step == "15m"
    assert (body.from_, body.to) == (HOUR_START, CLOCK_NOW)
    assert [(series.app_slug, series.display_name) for series in body.series] == [
        ("docs", "Docs"),
        ("sales", "Sales"),
    ]
    docs_points = response.json()["series"][0]["points"]
    assert docs_points == [
        {
            "minute": "2026-10-30T11:00:00Z",
            "requests": 20,
            "r4xx": 2,
            "r5xx": 0,
            "rss_mb_max": 100,
            "bytes_out": 200,
            "egress_dropped": 0,
            "lat_hist": hist(2),
        },
        {
            "minute": "2026-10-30T11:15:00Z",
            "requests": 5,
            "r4xx": 0,
            "r5xx": 1,
            "rss_mb_max": 120,
            "bytes_out": 0,
            "egress_dropped": 0,
            "lat_hist": hist(),
        },
    ]
    # old and gone, summed per step: their memory peaks add up, as their requests do.
    assert body.other is not None
    [other] = body.other
    assert (other.requests, other.rss_mb_max, other.egress_dropped) == (6, 120, 3)
    assert other.lat_hist == hist(1, 1)


def test_a_tie_is_ranked_by_slug_and_an_app_no_longer_registered_has_no_display_name() -> None:
    body = breakdown_of(breakdown_platform().call("GET", "/metrics/breakdown?limit=3", DANA))

    assert [(series.app_slug, series.display_name) for series in body.series] == [
        ("docs", "Docs"),
        ("sales", "Sales"),
        ("gone", None),
    ]
    assert body.other is not None
    assert [(point.minute, point.requests) for point in body.other] == [(at(1), 3)]


def test_other_is_null_when_every_app_fits_and_the_platform_row_is_not_an_app() -> None:
    body = breakdown_of(breakdown_platform().call("GET", "/metrics/breakdown", DANA))

    assert [series.app_slug for series in body.series] == ["docs", "sales", "gone", "old"]
    assert body.other is None
    assert body.step == "1m"


def test_a_breakdown_of_a_window_with_no_rows_is_empty() -> None:
    body = breakdown_of(
        breakdown_platform().call(
            "GET", "/metrics/breakdown?from=2026-10-30T10:00:00Z&to=2026-10-30T11:00:00Z", DANA
        )
    )

    assert (body.series, body.other) == ([], None)


@pytest.mark.parametrize("limit", ["1", "20"])
def test_a_limit_from_1_to_20_is_allowed(limit: str) -> None:
    body = breakdown_of(breakdown_platform().call("GET", f"/metrics/breakdown?limit={limit}", DANA))

    assert len(body.series) == min(int(limit), 4)


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=21", "limit=-1", "limit=two", "limit=1.5", "limit=", "limit=2&limit=3"],
)
def test_a_limit_outside_1_to_20_is_400(query: str) -> None:
    response = breakdown_platform().call("GET", f"/metrics/breakdown?{query}", DANA)

    assert response.status_code == 400
    code, message = error_of(response)
    assert code == "bad_request"
    assert "limit" in message


def test_the_breakdown_follows_the_step_and_window_rules_of_get_metrics() -> None:
    platform = breakdown_platform()
    week = "from=2026-10-23T12:00:00Z&to=2026-10-30T12:00:00Z"

    too_fine = platform.call("GET", f"/metrics/breakdown?{week}", DANA)
    allowed = platform.call("GET", f"/metrics/breakdown?{week}&step=15m", DANA)
    unknown = platform.call("GET", "/metrics/breakdown?step=10m", DANA)

    assert too_fine.status_code == 400
    assert error_of(too_fine)[0] == "window_too_long"
    assert "step=15m" in error_of(too_fine)[1]
    assert allowed.status_code == 200
    assert unknown.status_code == 400
    assert error_of(unknown)[0] == "bad_request"


def test_the_breakdown_needs_can_manage_on_the_platform() -> None:
    platform = breakdown_platform()

    for person in NON_ADMINS:  # an owner and a CAN_MANAGE holder of an app included
        response = platform.call("GET", "/metrics/breakdown", person)
        assert response.status_code == 403, person
        assert "requests" not in response.text
    assert platform.call("GET", "/metrics/breakdown", None).status_code == 401
    assert platform.call("GET", "/metrics/breakdown", DANA).status_code == 200


def test_the_breakdown_needs_the_registry() -> None:
    platform = breakdown_platform()
    platform.registry.go_down()

    down = platform.call("GET", "/metrics/breakdown", DANA)

    assert down.status_code == 503
    assert_no_leak(down)
