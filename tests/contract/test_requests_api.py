"""The request inbox and the delegation routes (T141; US8; contracts/admin-api.md).

Seven routes behind the real guard and the real authorizer, on the in-memory registry:

* ``GET /requests``: a visitor sees their own, ``CAN_MANAGE`` on an app sees its app's, a platform
  admin sees all, oldest first;
* ``POST /requests`` (``request.create``) and ``POST /requests/{id}/withdraw``: the appmux service
  principal writes them, the asker alone withdraws;
* ``POST /requests/{id}/approve`` and ``/deny`` (``request.decide``): a platform admin only, in one
  transaction under that admin's own token;
* ``PUT`` and ``DELETE /apps/{app}/delegation/{kind}``: a platform admin's direct grant and revoke.

The rename cascade of ``reconciler/rename.py`` (lane U) is replaced by ``FakeRename``, so what is
pinned here is the route's side of the ``RenameCascade`` seam, not a restart. Starting data: see
``edge_harness`` (``sales`` owned by alice, bob ``CAN_USE``, carol ``CAN_MANAGE``; ``docs`` owned by
frank; dana is the platform admin). Grace has no app; erin is a stranger to all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import httpx
from edge_harness import (
    ALICE,
    BOB,
    CAROL,
    DANA,
    NOW,
    VISITOR_TOKEN,
    AdminApi,
    seeded_registry,
)
from stub_registry import StubRegistry

from appmux.admin.delegation import DelegationRoutes
from appmux.admin.requests import RenameCascadeFailed, RequestRoutes
from appmux.admin.schemas import AppDetail, RequestList, RequestRow
from appmux.registry.models import App
from appmux.registry.writers import Identity, WriteKind

GRACE: Final = "grace@example.com"
ERIN: Final = "erin@example.com"
MARKER: Final = "Marker Value From The Client"
LEAK_MARKERS: Final = (VISITOR_TOKEN, "Traceback", "SELECT")
TAKEN_NOTE: Final = "slug 'reports' is already taken (automatic)"
REGISTER_PAYLOAD: Final[Mapping[str, object]] = {
    "slug": "reports",
    "display_name": "Weekly reports",
    "source_path": "/Workspace/Shared/apps/reports",
    "memory_budget_mb": 256,
}


class FakeRename:
    """The rename cascade: records the calls, fails when told to."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failure: RenameCascadeFailed | None = None

    async def renamed(self, old_slug: str, new_slug: str) -> None:
        self.calls.append((old_slug, new_slug))
        if self.failure is not None:
            raise self.failure


class Inbox:
    """The admin API with the request and delegation routes mounted."""

    def __init__(
        self,
        *,
        shared_sp_allowed: bool = True,
        platform_writer_tokens: set[str] | None = None,
        wire_rename: bool = True,
    ) -> None:
        self.registry: StubRegistry = seeded_registry(platform_writer_tokens=platform_writer_tokens)
        self.registry.seed(
            "platform_settings",
            {
                "key": "shared_sp_delegation_allowed",
                "value": shared_sp_allowed,
                "updated_by": DANA,
                "updated_at": NOW,
            },
        )
        self.rename = FakeRename()
        self.api = AdminApi(
            self.registry,
            lambda authorizer: [
                RequestRoutes(
                    authorizer, rename=self.rename if wire_rename else None, clock=lambda: NOW
                ),
                DelegationRoutes(),
            ],
        )

    def call(
        self, method: str, path: str, email: str | None, body: object = None, **kwargs: bool
    ) -> httpx.Response:
        return self.api.call(method, path, email, json=body, **kwargs)

    def ask(
        self, email: str, kind: str, app: str, payload: Mapping[str, object], reason: str = "why"
    ) -> httpx.Response:
        body = {"kind": kind, "app": app, "payload": dict(payload), "reason": reason}
        return self.call("POST", "/requests", email, body)

    def seed(
        self,
        request_id: int,
        *,
        kind: str,
        payload: Mapping[str, object],
        app_slug: str | None = "sales",
        requested_by: str = CAROL,
        status: str = "pending",
    ) -> None:
        closed = {"closed_by": DANA, "closed_at": NOW} if status != "pending" else {}
        self.registry.seed(
            "permission_requests",
            {
                "request_id": request_id,
                "app_slug": app_slug,
                "kind": kind,
                "payload": dict(payload),
                "reason": "seeded",
                "requested_by": requested_by,
                "requested_at": NOW,
                "status": status,
                **closed,
            },
        )

    def app(self, slug: str) -> App:
        row = self.registry.snapshot.app(slug)
        assert row is not None
        return row

    def actions(self) -> list[str]:
        return [row.action for row in self.registry.audit_rows]


def row_of(response: httpx.Response) -> RequestRow:
    assert response.status_code == 200, response.text
    return RequestRow.model_validate_json(response.content)


def error_code(response: httpx.Response, status: int) -> str:
    assert response.status_code == status, response.text
    body = response.json()["error"]
    assert all(marker not in response.text for marker in LEAK_MARKERS)
    return str(body["code"])


# --- POST /requests ---------------------------------------------------------------------------


def test_an_app_admin_asks_for_a_setting_and_the_service_principal_writes_it() -> None:
    inbox = Inbox()
    row = row_of(inbox.ask(CAROL, "settings", "sales", {"display_name": "Sales EU"}, "new name"))
    assert (row.status, row.requested_by, row.app_slug) == ("pending", CAROL, "sales")
    assert row.current == {"display_name": "Sales"}
    assert (row.requires_reason, row.app_version, row.closed_at) == (False, None, None)
    (write,) = inbox.registry.writes
    assert write.kinds == (WriteKind.REQUEST_CREATE,)
    assert write.identity is Identity.APPMUX_SP
    (audit,) = inbox.registry.audit_rows
    assert (audit.actor, audit.action, audit.app_slug) == (CAROL, "request.create", "sales")
    assert inbox.app("sales").display_name == "Sales"  # a request grants nothing


def test_any_visitor_asks_for_a_new_app_and_the_request_has_no_app_yet() -> None:
    inbox = Inbox()
    row = row_of(inbox.ask(GRACE, "register", "reports", REGISTER_PAYLOAD))
    assert (row.app_slug, row.kind, row.requires_reason, row.current) == (
        None,
        "register",
        True,
        None,
    )
    assert inbox.registry.snapshot.app("reports") is None
    (audit,) = inbox.registry.audit_rows
    assert (audit.actor, audit.action) == (GRACE, "request.create")


def test_a_person_who_cannot_manage_the_app_cannot_ask_for_a_change() -> None:
    inbox = Inbox()
    payload = {"display_name": "Sales EU"}
    assert error_code(inbox.ask(BOB, "settings", "sales", payload), 403) == "forbidden"
    # A stranger cannot even open the app, so it looks like one that does not exist.
    assert error_code(inbox.ask(ERIN, "settings", "sales", payload), 404) == "not_found"
    assert error_code(inbox.ask(CAROL, "settings", "docs", payload), 404) == "not_found"
    assert inbox.registry.writes == ()


def test_an_invalid_request_is_refused_and_nothing_sent_comes_back() -> None:
    inbox = Inbox()
    for body in (
        {"kind": "settings", "app": "sales", "payload": {"slug": MARKER}, "reason": "r"},
        {"kind": "settings", "app": "sales", "payload": {"display_name": "X"}, "reason": " "},
        {"kind": "settings", "app": "sales", "payload": {"display_name": "X"}, "reason": "r" * 501},
        {"kind": "delete", "app": "sales", "payload": {}, "reason": "r"},
        {"kind": "settings", "app": "sales", "payload": {"owner": ALICE}, "reason": "r"},
        {"kind": "register", "app": "other", "payload": REGISTER_PAYLOAD, "reason": "r"},
    ):
        response = inbox.call("POST", "/requests", CAROL, body)
        assert error_code(response, 400) == "bad_request"
        assert MARKER not in response.text
    assert inbox.registry.writes == ()


def test_the_rules_of_a_new_request_answer_with_their_codes() -> None:
    inbox = Inbox(shared_sp_allowed=False)
    assert error_code(inbox.ask(CAROL, "settings", "sales", {"display_name": "Sales"}), 400) == (
        "no_change"
    )
    assert error_code(inbox.ask(CAROL, "delegation", "sales", {"shared_sp": True}), 409) == (
        "shared_sp_not_allowed"
    )
    taken = {**REGISTER_PAYLOAD, "slug": "docs"}
    assert error_code(inbox.ask(GRACE, "register", "docs", taken), 409) == "slug_taken"
    assert inbox.registry.writes == ()


def test_a_second_pending_request_of_the_same_app_and_kind_is_refused() -> None:
    inbox = Inbox()
    row_of(inbox.ask(CAROL, "settings", "sales", {"display_name": "Sales EU"}))
    response = inbox.ask(ALICE, "settings", "sales", {"memory_budget_mb": 512})
    assert error_code(response, 409) == "request_pending"
    assert len(inbox.registry.writes) == 1


def test_a_person_has_at_most_three_pending_requests_for_a_new_app() -> None:
    inbox = Inbox()
    for number in (1, 2, 3):
        slug = f"app-{number}"
        row_of(inbox.ask(GRACE, "register", slug, {**REGISTER_PAYLOAD, "slug": slug}))
    fourth = inbox.ask(GRACE, "register", "app-4", {**REGISTER_PAYLOAD, "slug": "app-4"})
    assert error_code(fourth, 409) == "too_many_pending"
    elsewhere = inbox.ask(ERIN, "register", "app-4", {**REGISTER_PAYLOAD, "slug": "app-4"})
    assert row_of(elsewhere).requested_by == ERIN


def test_a_post_needs_an_identity_the_same_origin_and_a_registry() -> None:
    inbox = Inbox()
    body = {"kind": "settings", "app": "sales", "payload": {"display_name": "X"}, "reason": "r"}
    assert inbox.call("POST", "/requests", None, body).status_code == 401
    assert inbox.call("POST", "/requests", CAROL, body, same_origin=False).status_code == 403
    inbox.registry.go_down()
    assert inbox.call("POST", "/requests", CAROL, body).status_code == 503
    assert inbox.registry.audit_rows == ()


# --- GET /requests ----------------------------------------------------------------------------


def seeded_inbox() -> Inbox:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"display_name": "Sales EU"}, requested_by=CAROL)
    inbox.seed(
        2,
        kind="settings",
        payload={"display_name": "Docs EU"},
        app_slug="docs",
        requested_by="frank@example.com",
    )
    inbox.seed(3, kind="register", payload=REGISTER_PAYLOAD, app_slug=None, requested_by=GRACE)
    inbox.seed(4, kind="delegation", payload={"obo": True}, requested_by=ALICE, status="denied")
    inbox.seed(5, kind="delete", payload={"slug": "sales"}, requested_by=GRACE)  # a forged kind
    return inbox


def listed(response: httpx.Response) -> list[int]:
    assert response.status_code == 200, response.text
    return [item.request_id for item in RequestList.model_validate_json(response.content).items]


def test_a_visitor_sees_their_own_requests_only() -> None:
    inbox = seeded_inbox()
    assert listed(inbox.call("GET", "/requests", GRACE)) == [3]
    assert listed(inbox.call("GET", "/requests", ERIN)) == []
    assert listed(inbox.call("GET", "/requests", BOB)) == []


def test_an_app_admin_sees_the_requests_of_their_app_and_their_own() -> None:
    inbox = seeded_inbox()
    assert listed(inbox.call("GET", "/requests", CAROL)) == [1, 4]
    assert listed(inbox.call("GET", "/requests", ALICE)) == [1, 4]
    assert listed(inbox.call("GET", "/requests", "frank@example.com")) == [2]


def test_a_platform_admin_sees_every_request_oldest_first_and_the_forged_kind_is_left_out() -> None:
    inbox = seeded_inbox()
    assert listed(inbox.call("GET", "/requests", DANA)) == [1, 2, 3, 4]


def test_the_list_shows_the_current_value_next_to_the_requested_one() -> None:
    inbox = seeded_inbox()
    items = RequestList.model_validate_json(inbox.call("GET", "/requests", DANA).content).items
    by_id = {item.request_id: item for item in items}
    assert by_id[1].current == {"display_name": "Sales"}
    assert by_id[4].current == {"obo": False}
    assert by_id[3].current is None
    assert by_id[1].app_version is None


# --- withdraw ---------------------------------------------------------------------------------


def test_the_asker_withdraws_a_pending_request() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"display_name": "Sales EU"})
    row = row_of(inbox.call("POST", "/requests/1/withdraw", CAROL))
    assert (row.status, row.closed_by) == ("withdrawn", CAROL)
    assert row.closed_at is not None
    (write,) = inbox.registry.writes
    assert write.kinds == (WriteKind.REQUEST_WITHDRAW,)
    assert write.identity is Identity.APPMUX_SP
    assert inbox.actions() == ["request.withdraw"]


def test_nobody_but_the_asker_withdraws() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"display_name": "Sales EU"})
    assert error_code(inbox.call("POST", "/requests/1/withdraw", ALICE), 403) == "not_the_asker"
    assert error_code(inbox.call("POST", "/requests/1/withdraw", DANA), 403) == "not_the_asker"
    assert error_code(inbox.call("POST", "/requests/1/withdraw", ERIN), 404) == "not_found"
    assert inbox.registry.writes == ()


def test_a_request_is_withdrawn_only_while_pending_and_a_missing_one_is_404() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"display_name": "X"}, status="approved")
    assert error_code(inbox.call("POST", "/requests/1/withdraw", CAROL), 409) == "not_pending"
    assert error_code(inbox.call("POST", "/requests/99/withdraw", CAROL), 404) == "not_found"


# --- approve and deny -------------------------------------------------------------------------


def test_approving_a_setting_changes_the_app_in_one_transaction_under_the_admins_token() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"display_name": "Sales EU", "memory_budget_mb": 512})
    row = row_of(inbox.call("POST", "/requests/1/approve", DANA, {"note": "ok"}))
    assert (row.status, row.closed_by, row.closed_note) == ("approved", DANA, "ok")
    assert row.app_version == 2
    app = inbox.app("sales")
    assert (app.display_name, app.memory_budget_mb, app.version, app.updated_by) == (
        "Sales EU",
        512,
        2,
        DANA,
    )
    (write,) = inbox.registry.writes
    assert write.kinds == (WriteKind.APP_UPDATE, WriteKind.REQUEST_ANSWER)
    assert write.identity is Identity.PLATFORM_ADMIN
    assert write.admin_token is not None
    assert inbox.actions() == ["request.approve"]


def test_only_a_platform_admin_decides() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"display_name": "Sales EU"})
    for email in (CAROL, ALICE, GRACE):
        assert error_code(inbox.call("POST", "/requests/1/approve", email, {}), 403) == "forbidden"
        assert error_code(inbox.call("POST", "/requests/1/deny", email, {}), 403) == "forbidden"
    assert inbox.registry.writes == ()


def test_a_request_with_a_gate_field_is_not_approved_without_a_reason() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="delegation", payload={"obo": True})
    assert error_code(inbox.call("POST", "/requests/1/approve", DANA, {}), 400) == (
        "reason_required"
    )
    assert inbox.registry.writes == ()
    row = row_of(inbox.call("POST", "/requests/1/approve", DANA, {"reason": "reviewed the app"}))
    assert row.status == "approved"
    assert inbox.app("sales").obo_delegated is True
    (audit,) = inbox.registry.audit_rows
    assert (audit.action, audit.reason) == ("request.approve", "reviewed the app")


def test_approving_a_new_app_creates_it_for_the_asker_stopped_with_no_release() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="register", payload=REGISTER_PAYLOAD, app_slug=None, requested_by=GRACE)
    row = row_of(inbox.call("POST", "/requests/1/approve", DANA, {"reason": "the team asked"}))
    assert (row.status, row.app_slug, row.app_version) == ("approved", "reports", 1)
    app = inbox.app("reports")
    assert (app.owner, app.created_by) == (GRACE, DANA)
    config = inbox.registry.snapshot.app_config("reports")
    assert config is not None
    assert (config.desired_state, config.target_release_no) == ("stopped", None)
    assert inbox.registry.snapshot.releases_for("reports") == ()
    (write,) = inbox.registry.writes
    assert write.kinds == (
        WriteKind.APP_INSERT,
        WriteKind.APP_CONFIG_INSERT,
        WriteKind.REQUEST_ANSWER,
    )


def test_a_slug_taken_at_approval_closes_the_request_as_denied_under_the_approving_admin() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="register", payload=REGISTER_PAYLOAD, app_slug=None, requested_by=GRACE)
    inbox.registry.seed(
        "apps",
        {
            "slug": "reports",
            "display_name": "Reports",
            "owner": ALICE,
            "source_path": "/Workspace/Shared/apps/other",
            "memory_budget_mb": 256,
            "created_by": DANA,
            "created_at": NOW,
            "updated_by": DANA,
            "updated_at": NOW,
        },
    )
    row = row_of(inbox.call("POST", "/requests/1/approve", DANA, {}))
    assert (row.status, row.closed_by, row.closed_note) == ("denied", DANA, TAKEN_NOTE)
    assert row.app_version is None
    assert inbox.app("reports").owner == ALICE
    (audit,) = inbox.registry.audit_rows
    assert (audit.action, audit.actor, audit.detail["automatic"]) == ("request.deny", DANA, True)


def test_the_asker_may_approve_their_own_request_and_the_audit_row_shows_both_names() -> None:
    inbox = Inbox()
    inbox.seed(
        1,
        kind="register",
        payload={**REGISTER_PAYLOAD, "slug": "ops"},
        app_slug=None,
        requested_by=DANA,
    )
    row = row_of(inbox.call("POST", "/requests/1/approve", DANA, {"reason": "my own app"}))
    assert row.status == "approved"
    (audit,) = inbox.registry.audit_rows
    assert audit.actor == DANA
    assert audit.detail["principal"] == f"user:{DANA}"


def test_a_rename_is_followed_by_the_cascade_with_the_old_and_the_new_slug() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"slug": "sales-v2"})
    row = row_of(inbox.call("POST", "/requests/1/approve", DANA, {}))
    assert (row.status, row.app_slug) == ("approved", "sales-v2")
    assert inbox.registry.snapshot.app("sales") is None
    assert inbox.app("sales-v2").version == 2
    assert inbox.rename.calls == [("sales", "sales-v2")]
    assert inbox.actions() == ["request.approve", "app.rename"]


def test_a_cascade_that_fails_leaves_the_rename_saved_and_says_so() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"slug": "sales-v2"})
    inbox.rename.failure = RenameCascadeFailed("the new copy did not pass its health check")
    response = inbox.call("POST", "/requests/1/approve", DANA, {})
    assert error_code(response, 503) == "rename_not_applied"
    assert inbox.app("sales-v2").slug == "sales-v2"  # the row holds the new slug: Restart retries
    assert "health check" not in response.text


def test_a_setting_without_a_rename_does_not_call_the_cascade() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"display_name": "Sales EU"})
    row_of(inbox.call("POST", "/requests/1/approve", DANA, {}))
    assert inbox.rename.calls == []


def test_a_rename_with_no_cascade_wired_is_refused_before_anything_is_saved() -> None:
    inbox = Inbox(wire_rename=False)
    inbox.seed(1, kind="settings", payload={"slug": "sales-v2"})
    assert error_code(inbox.call("POST", "/requests/1/approve", DANA, {}), 503) == (
        "rename_unavailable"
    )
    assert inbox.registry.writes == ()
    inbox.seed(
        2,
        kind="settings",
        payload={"display_name": "Docs EU"},
        app_slug="docs",
        requested_by="frank@example.com",
    )
    assert row_of(inbox.call("POST", "/requests/2/approve", DANA, {})).status == "approved"


def test_a_request_answered_twice_is_answered_once() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"display_name": "Sales EU"})
    row_of(inbox.call("POST", "/requests/1/approve", DANA, {}))
    assert error_code(inbox.call("POST", "/requests/1/approve", DANA, {}), 409) == "not_pending"
    assert error_code(inbox.call("POST", "/requests/1/deny", DANA, {}), 409) == "not_pending"
    assert inbox.app("sales").version == 2


def test_a_row_of_an_unknown_kind_is_left_as_it_is_by_every_answer() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="delete", payload={"slug": "ghost"}, app_slug=None, requested_by=GRACE)
    for path, email in (
        ("/requests/1/approve", DANA),
        ("/requests/1/deny", DANA),
        ("/requests/1/withdraw", GRACE),
    ):
        body = {} if email == DANA else None
        assert error_code(inbox.call("POST", path, email, body), 400) == "unknown_kind"
    assert inbox.registry.writes == ()
    assert inbox.registry.snapshot.app("sales") is not None


def test_the_shared_sp_is_not_approved_while_the_switch_is_off() -> None:
    inbox = Inbox(shared_sp_allowed=False)
    inbox.seed(1, kind="delegation", payload={"shared_sp": True})
    response = inbox.call("POST", "/requests/1/approve", DANA, {"reason": "reviewed"})
    assert error_code(response, 409) == "shared_sp_not_allowed"
    assert inbox.app("sales").shared_sp_delegated is False


def test_an_approval_without_the_admins_database_role_writes_nothing() -> None:
    inbox = Inbox(platform_writer_tokens={"some-other-token"})
    inbox.seed(1, kind="settings", payload={"display_name": "Sales EU"})
    assert inbox.call("POST", "/requests/1/approve", DANA, {}).status_code == 403
    assert inbox.app("sales").display_name == "Sales"
    assert inbox.registry.audit_rows == ()


def test_denying_closes_the_request_with_the_note_and_changes_nothing() -> None:
    inbox = Inbox()
    inbox.seed(1, kind="settings", payload={"display_name": "Sales EU"})
    row = row_of(inbox.call("POST", "/requests/1/deny", DANA, {"reason": "use the old name"}))
    assert (row.status, row.closed_by, row.closed_note) == ("denied", DANA, "use the old name")
    assert inbox.app("sales").display_name == "Sales"
    (audit,) = inbox.registry.audit_rows
    assert (audit.action, audit.reason) == ("request.deny", "use the old name")
    assert "automatic" not in audit.detail


def test_a_missing_request_is_404_for_a_decision() -> None:
    inbox = Inbox()
    assert error_code(inbox.call("POST", "/requests/99/approve", DANA, {}), 404) == "not_found"
    assert error_code(inbox.call("POST", "/requests/99/deny", DANA, {}), 404) == "not_found"


# --- delegation -------------------------------------------------------------------------------


def detail_of(response: httpx.Response) -> AppDetail:
    assert response.status_code == 200, response.text
    return AppDetail.model_validate_json(response.content)


def test_a_platform_admin_grants_a_delegation_directly_with_a_reason() -> None:
    inbox = Inbox()
    body = {"version": 1, "reason": "the app calls Genie as its users"}
    detail = detail_of(inbox.call("PUT", "/apps/sales/delegation/obo", DANA, body))
    assert detail.delegation.obo_delegated is True
    assert detail.version == 2
    (audit,) = inbox.registry.audit_rows
    assert (audit.actor, audit.action, audit.app_slug) == (DANA, "delegation.grant", "sales")
    assert audit.reason == body["reason"]
    assert audit.detail["kind"] == "obo"
    (write,) = inbox.registry.writes
    assert write.identity is Identity.PLATFORM_ADMIN


def test_a_delegation_is_granted_with_a_current_version_a_reason_and_a_known_kind() -> None:
    inbox = Inbox()
    stale = {"version": 7, "reason": "why"}
    assert error_code(inbox.call("PUT", "/apps/sales/delegation/obo", DANA, stale), 409) == (
        "changed_by_others"
    )
    for body in ({"version": 1, "reason": " "}, {"version": 1}, {"reason": "why"}):
        assert error_code(inbox.call("PUT", "/apps/sales/delegation/obo", DANA, body), 400) == (
            "bad_request"
        )
    ok = {"version": 1, "reason": "why"}
    assert inbox.call("PUT", "/apps/sales/delegation/pat", DANA, ok).status_code == 404
    assert inbox.registry.writes == ()


def test_only_a_platform_admin_grants_or_revokes_a_delegation() -> None:
    inbox = Inbox()
    body = {"version": 1, "reason": "why"}
    for email in (CAROL, ALICE):
        assert inbox.call("PUT", "/apps/sales/delegation/obo", email, body).status_code == 403
        url = "/apps/sales/delegation/obo?reason=why&version=1"
        assert inbox.call("DELETE", url, email).status_code == 403
    assert inbox.call("PUT", "/apps/sales/delegation/obo", ERIN, body).status_code == 404
    assert inbox.call("PUT", "/apps/gone/delegation/obo", DANA, body).status_code == 404
    assert inbox.registry.writes == ()


def test_the_shared_sp_is_granted_only_while_the_master_switch_is_on() -> None:
    off = Inbox(shared_sp_allowed=False)
    body = {"version": 1, "reason": "why"}
    response = off.call("PUT", "/apps/sales/delegation/shared_sp", DANA, body)
    assert error_code(response, 409) == "shared_sp_not_allowed"
    on = Inbox(shared_sp_allowed=True)
    detail = detail_of(on.call("PUT", "/apps/sales/delegation/shared_sp", DANA, body))
    assert detail.delegation.shared_sp_delegated is True


def test_a_delegation_already_held_is_not_granted_again() -> None:
    inbox = Inbox()
    body = {"version": 1, "reason": "why"}
    detail_of(inbox.call("PUT", "/apps/sales/delegation/obo", DANA, body))
    again = {"version": 2, "reason": "why"}
    assert error_code(inbox.call("PUT", "/apps/sales/delegation/obo", DANA, again), 409) == (
        "already_delegated"
    )


def test_a_platform_admin_revokes_a_delegation_with_the_reason_in_the_query() -> None:
    inbox = Inbox()
    detail_of(inbox.call("PUT", "/apps/sales/delegation/obo", DANA, {"version": 1, "reason": "g"}))
    url = "/apps/sales/delegation/obo?reason=the%20app%20is%20retired&version=2"
    detail = detail_of(inbox.call("DELETE", url, DANA))
    assert detail.delegation.obo_delegated is False
    assert detail.version == 3
    revoke = inbox.registry.audit_rows[-1]
    assert (revoke.action, revoke.reason) == ("delegation.revoke", "the app is retired")


def test_a_revoke_needs_its_query_and_a_delegation_to_revoke() -> None:
    inbox = Inbox()
    assert error_code(inbox.call("DELETE", "/apps/sales/delegation/obo", DANA), 400) == (
        "bad_request"
    )
    bad_version = "/apps/sales/delegation/obo?reason=why&version=abc"
    assert error_code(inbox.call("DELETE", bad_version, DANA), 400) == "bad_request"
    not_held = "/apps/sales/delegation/obo?reason=why&version=1"
    assert error_code(inbox.call("DELETE", not_held, DANA), 409) == "not_delegated"
    assert inbox.registry.writes == ()
