"""Approving, denying and withdrawing a request: the plan and the writes (T140; FR-042 to 045).

``plan_approval`` re-checks a request as it stands now and says what an approval does;
``registry/requests.py`` turns the plan into the ``Change`` values of one transaction and the audit
rows that go with it. Both are pure, so a "transaction" here is the list of changes the registry
would write together, and ``resolve_identity`` is what holds them to one database role.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
from pydantic import SecretStr
from request_fakes import ALICE, CAROL, DANA, GRACE, NOW, make_app, make_request, make_snapshot

from appmux.admin.errors import BadRequest, Conflict
from appmux.admin.request_catalogue import plan_approval
from appmux.registry.audit import AuditEntry
from appmux.registry.grants import build_authz_data
from appmux.registry.models import App, PermissionRequest
from appmux.registry.protocol import Change, resolve_identity
from appmux.registry.requests import (
    ApprovalPlan,
    Outcome,
    answer_audit,
    approval_changes,
    create_request_change,
    deny_audit,
    deny_changes,
    withdraw_change,
)
from appmux.registry.writers import Identity, WriteKind

REGISTER_PAYLOAD: Final[Mapping[str, object]] = {
    "slug": "reports",
    "display_name": "Weekly reports",
    "source_path": "/Workspace/Shared/apps/reports",
    "memory_budget_mb": 256,
}
ADMIN_TOKEN: Final = SecretStr("tok-test-admin")  # a fake value for a test
SRC: Final = Path(__file__).resolve().parents[2] / "src" / "appmux"
TAKEN_NOTE: Final = "slug 'reports' is already taken (automatic)"
FRANK: Final = "frank@example.com"


def plan_for(
    request: PermissionRequest,
    *,
    apps: Sequence[App] | None = None,
    reason: str | None = "the team runs this",
    requester_can_manage: bool = True,
    shared_sp_allowed: bool = True,
) -> ApprovalPlan:
    snapshot = make_snapshot(
        apps=apps if apps is not None else [make_app()],
        requests=[request],
        shared_sp_allowed=shared_sp_allowed,
    )
    return plan_approval(
        snapshot, request, reason=reason, requester_can_manage=requester_can_manage
    )


def settings_request(**payload: object) -> PermissionRequest:
    return make_request(1, kind="settings", payload=payload)


def register_request(**overrides: object) -> PermissionRequest:
    return make_request(
        1, kind="register", payload=REGISTER_PAYLOAD, app_slug=None, requested_by=GRACE
    ).model_copy(update=overrides)


def kinds(changes: Sequence[Change]) -> list[WriteKind]:
    return [change.kind for change in changes]


# --- the plan ---------------------------------------------------------------------------------


def test_a_settings_approval_applies_every_field_it_asked_for() -> None:
    plan = plan_for(
        settings_request(slug="sales-v2", display_name="Sales EU", memory_budget_mb=512)
    )
    assert plan.outcome is Outcome.APPLY
    assert plan.app_values == {
        "slug": "sales-v2",
        "display_name": "Sales EU",
        "memory_budget_mb": 512,
    }
    assert plan.rename == ("sales", "sales-v2")
    assert plan.before == {"slug": "sales", "display_name": "Sales", "memory_budget_mb": 256}
    assert plan.after == {"slug": "sales-v2", "display_name": "Sales EU", "memory_budget_mb": 512}
    assert plan.audit_slug == "sales-v2"


def test_a_delegation_approval_sets_the_flags_that_were_asked() -> None:
    plan = plan_for(make_request(1, kind="delegation", payload={"obo": True, "shared_sp": True}))
    assert plan.app_values == {"obo_delegated": True, "shared_sp_delegated": True}
    assert plan.before == {"obo": False, "shared_sp": False}
    assert plan.after == {"obo": True, "shared_sp": True}
    assert plan.rename is None


def test_a_register_approval_creates_the_app_for_the_asker() -> None:
    plan = plan_for(register_request(), apps=[])
    assert plan.outcome is Outcome.APPLY
    assert plan.new_app == REGISTER_PAYLOAD
    assert plan.audit_slug == "reports"
    assert plan.app_values == {}


def test_a_rename_applies_every_field_or_none_when_the_slug_turns_out_taken() -> None:
    taken = [make_app("sales"), make_app("sales-v2", owner=FRANK)]
    plan = plan_for(settings_request(slug="sales-v2", memory_budget_mb=512), apps=taken)
    assert plan.outcome is Outcome.SLUG_TAKEN
    assert plan.taken_slug == "sales-v2"
    assert plan.app_values == {}  # the budget is not applied alone
    assert plan.rename is None


def test_a_register_for_a_slug_taken_since_the_request_was_made_is_an_automatic_denial() -> None:
    plan = plan_for(register_request(), apps=[make_app("reports", owner=FRANK)], reason=None)
    assert plan.outcome is Outcome.SLUG_TAKEN  # no reason is needed to deny
    assert plan.taken_slug == "reports"


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_a_request_with_a_gate_field_needs_a_reason_to_be_approved(reason: str | None) -> None:
    request = make_request(1, kind="delegation", payload={"obo": True})
    with pytest.raises(BadRequest) as info:
        plan_for(request, reason=reason)
    assert info.value.error_code == "reason_required"


def test_a_request_with_no_gate_field_is_approved_without_a_reason() -> None:
    assert plan_for(settings_request(display_name="Sales EU"), reason=None).outcome is Outcome.APPLY


def test_a_source_path_among_other_fields_makes_the_request_a_gate() -> None:
    request = settings_request(display_name="Sales EU", source_path="/Workspace/Shared/apps/v2")
    with pytest.raises(BadRequest) as info:
        plan_for(request, reason=None)
    assert info.value.error_code == "reason_required"


def test_when_every_value_already_holds_approving_only_closes_the_request() -> None:
    changed_directly = [make_app(display_name="Sales EU", memory_budget_mb=512)]
    plan = plan_for(
        settings_request(display_name="Sales EU", memory_budget_mb=512), apps=changed_directly
    )
    assert plan.outcome is Outcome.CLOSE_ONLY
    assert plan.app_values == {}


@pytest.mark.parametrize("status", ["approved", "denied", "withdrawn"])
def test_a_request_that_is_no_longer_pending_cannot_be_approved(status: str) -> None:
    request = make_request(1, kind="settings", payload={"display_name": "X"}, status=status)
    with pytest.raises(Conflict) as info:
        plan_for(request)
    assert info.value.error_code == "not_pending"


def test_a_row_of_a_kind_the_code_does_not_know_cannot_be_approved() -> None:
    with pytest.raises(BadRequest) as info:
        plan_for(make_request(1, kind="delete", payload={"slug": "sales"}))
    assert info.value.error_code == "unknown_kind"


def test_a_payload_that_no_longer_validates_cannot_be_approved() -> None:
    with pytest.raises(BadRequest) as info:
        plan_for(settings_request(memory_budget_mb=99999))
    assert info.value.error_code == "request_invalid"


def test_a_request_of_an_asker_who_no_longer_manages_the_app_cannot_be_approved() -> None:
    with pytest.raises(Conflict) as info:
        plan_for(settings_request(display_name="X"), requester_can_manage=False)
    assert info.value.error_code == "requester_not_admin"


def test_a_register_request_does_not_need_the_asker_to_manage_anything() -> None:
    plan = plan_for(register_request(), apps=[], requester_can_manage=False)
    assert plan.outcome is Outcome.APPLY


def test_shared_sp_is_not_approved_while_the_master_switch_is_off() -> None:
    request = make_request(1, kind="delegation", payload={"shared_sp": True})
    with pytest.raises(Conflict) as info:
        plan_for(request, shared_sp_allowed=False)
    assert info.value.error_code == "shared_sp_not_allowed"


def test_a_request_for_an_app_that_is_gone_cannot_be_approved() -> None:
    with pytest.raises(Conflict) as info:
        plan_for(settings_request(display_name="X"), apps=[])
    assert info.value.error_code == "app_missing"


# --- the writes of one transaction ------------------------------------------------------------


def test_a_settings_approval_is_one_update_and_one_answer_under_the_admins_role() -> None:
    plan = plan_for(settings_request(slug="sales-v2", display_name="Sales EU"))
    app = make_app()
    changes = approval_changes(plan, app=app, approver=DANA, closed_at=NOW)
    assert kinds(changes) == [WriteKind.APP_UPDATE, WriteKind.REQUEST_ANSWER]
    update, answer = changes
    assert update.key == {"slug": "sales"}
    assert update.expected_version == app.version
    assert update.values == {"slug": "sales-v2", "display_name": "Sales EU", "updated_by": DANA}
    assert answer.key == {"request_id": 1}
    assert answer.expected_status == "pending"
    assert answer.values == {"status": "approved", "closed_by": DANA, "closed_at": NOW}
    assert resolve_identity(changes, ADMIN_TOKEN) is Identity.PLATFORM_ADMIN


def test_a_delegation_approval_writes_the_flags_under_the_app_column_names() -> None:
    plan = plan_for(make_request(1, kind="delegation", payload={"obo": True}))
    changes = approval_changes(plan, app=make_app(), approver=DANA, closed_at=NOW)
    assert changes[0].values == {"obo_delegated": True, "updated_by": DANA}


def test_a_register_approval_inserts_the_app_its_stopped_config_and_links_the_request() -> None:
    plan = plan_for(register_request(), apps=[])
    changes = approval_changes(plan, app=None, approver=DANA, closed_at=NOW)
    assert kinds(changes) == [
        WriteKind.APP_INSERT,
        WriteKind.APP_CONFIG_INSERT,
        WriteKind.REQUEST_ANSWER,
    ]
    app_insert, config_insert, answer = changes
    assert app_insert.values["owner"] == GRACE
    assert app_insert.values["slug"] == "reports"
    assert app_insert.values["created_by"] == DANA
    assert config_insert.values["desired_state"] == "stopped"
    assert answer.values["app_slug"] == "reports"
    assert answer.values["status"] == "approved"
    assert resolve_identity(changes, ADMIN_TOKEN) is Identity.PLATFORM_ADMIN


def test_approving_only_closes_when_nothing_is_left_to_apply() -> None:
    plan = plan_for(
        settings_request(display_name="Sales EU"), apps=[make_app(display_name="Sales EU")]
    )
    changes = approval_changes(plan, app=make_app(), approver=DANA, closed_at=NOW)
    assert kinds(changes) == [WriteKind.REQUEST_ANSWER]


def test_the_automatic_denial_closes_the_request_as_denied_under_the_approving_admin() -> None:
    request = register_request()
    (answer,) = deny_changes(request, closed_by=DANA, closed_at=NOW, note=TAKEN_NOTE)
    assert answer.kind is WriteKind.REQUEST_ANSWER
    assert answer.expected_status == "pending"
    assert answer.values == {
        "status": "denied",
        "closed_by": DANA,
        "closed_at": NOW,
        "closed_note": TAKEN_NOTE,
    }


def test_a_creation_is_a_service_principal_insert_with_no_app_for_a_register() -> None:
    change = create_request_change(
        kind="register",
        app_slug=None,
        payload=REGISTER_PAYLOAD,
        reason="please",
        requested_by=GRACE,
    )
    assert change.kind is WriteKind.REQUEST_CREATE
    assert change.values["app_slug"] is None
    assert change.values["requested_by"] == GRACE
    assert resolve_identity([change], None) is Identity.APPMUX_SP


def test_a_withdrawal_is_a_service_principal_update_that_expects_a_pending_request() -> None:
    row = make_request(4, kind="settings", payload={"display_name": "X"})
    change = withdraw_change(row, closed_by=CAROL, closed_at=NOW)
    assert change.kind is WriteKind.REQUEST_WITHDRAW
    assert change.key == {"request_id": 4}
    assert change.expected_status == "pending"
    assert change.values == {"status": "withdrawn", "closed_by": CAROL, "closed_at": NOW}
    assert resolve_identity([change], None) is Identity.APPMUX_SP


# --- the audit rows ---------------------------------------------------------------------------


def approval_audit(
    plan: ApprovalPlan, request: PermissionRequest, reason: str | None
) -> list[AuditEntry]:
    return answer_audit(plan, request, approver=DANA, reason=reason)


def test_an_approval_records_the_values_before_and_after_and_the_reason() -> None:
    request = settings_request(display_name="Sales EU", source_path="/Workspace/Shared/apps/v2")
    (entry,) = approval_audit(plan_for(request), request, "folder moved")
    assert entry.action == "request.approve"
    assert entry.actor == DANA
    assert entry.reason == "folder moved"
    assert entry.app_slug == "sales"
    assert entry.detail.request == 1
    assert entry.detail.kind == "settings"
    assert entry.detail.before is not None
    assert entry.detail.after is not None
    assert entry.detail.before.display_name == "Sales"
    assert entry.detail.after.display_name == "Sales EU"


def test_a_rename_also_writes_app_rename_under_the_new_slug() -> None:
    request = settings_request(slug="sales-v2")
    approve, rename = approval_audit(plan_for(request, reason=None), request, None)
    assert approve.app_slug == "sales-v2"
    assert (rename.action, rename.app_slug) == ("app.rename", "sales-v2")
    assert (rename.detail.from_, rename.detail.to) == ("sales", "sales-v2")


def test_the_audit_row_of_an_approval_names_the_asker_next_to_the_approver() -> None:
    request = make_request(1, kind="delegation", payload={"obo": True}, requested_by=DANA)
    (entry,) = approval_audit(plan_for(request), request, "I need it")
    assert entry.actor == DANA  # the approver
    assert entry.detail.principal == f"user:{DANA}"  # the asker: the same person here


def test_the_automatic_denial_is_audited_as_request_deny_marked_automatic() -> None:
    request = register_request()
    plan = plan_for(request, apps=[make_app("reports", owner=FRANK)], reason=None)
    (entry,) = approval_audit(plan, request, None)
    assert (entry.action, entry.actor, entry.automatic) == ("request.deny", DANA, True)
    assert entry.detail.request == 1


def test_a_deny_is_audited_with_the_reason_and_not_marked_automatic() -> None:
    request = settings_request(display_name="Sales EU")
    (entry,) = deny_audit(request, denier=DANA, reason="use the old name")
    assert (entry.action, entry.automatic, entry.reason) == (
        "request.deny",
        False,
        "use the old name",
    )
    assert entry.app_slug == "sales"


# --- a request grants nothing (FR-045) --------------------------------------------------------


def test_forged_approved_rows_change_nothing_that_authorization_reads() -> None:
    forged = [
        make_request(1, kind="delegation", payload={"obo": True}, status="approved"),
        make_request(2, kind="settings", payload={"slug": "sales-v2"}, status="approved"),
        make_request(3, kind="delegation", payload={"shared_sp": True}, status="approved"),
    ]
    app = make_app(owner=ALICE)
    with_forged = make_snapshot(apps=[app], requests=forged)
    without = make_snapshot(apps=[app])
    assert build_authz_data(with_forged) == build_authz_data(without)
    stored = with_forged.app("sales")
    assert stored is not None
    assert (stored.obo_delegated, stored.shared_sp_delegated) == (False, False)
    assert with_forged.app("sales-v2") is None


@pytest.mark.parametrize("package", ["authz", "reconciler"])
def test_authorization_and_the_reconciler_never_read_the_requests_table(package: str) -> None:
    offenders = [
        path.name
        for path in sorted((SRC / package).glob("*.py"))
        if any(
            name in path.read_text(encoding="utf-8")
            for name in ("permission_requests", "PermissionRequest")
        )
    ]
    assert offenders == []
