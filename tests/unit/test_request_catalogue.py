"""The request catalogue: three kinds, their payloads, gates and rules (T139; FR-040, FR-041).

Pure functions over a ``RegistrySnapshot``: what a request may ask for, what makes a new request
refusable, who may withdraw. Approval has its own file (``test_request_approval.py``). The status
and ``code`` of each refusal is what ``contracts/admin-api.md`` promises a client.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Final

import pytest
from pydantic import ValidationError
from request_fakes import ALICE, CAROL, GRACE, make_app, make_request, make_snapshot

from appmux.admin.errors import AdminError, BadRequest, Conflict, Forbidden, NotFound
from appmux.admin.request_catalogue import (
    GATE_FIELDS,
    KINDS,
    MAX_PENDING_REGISTER_PER_PERSON,
    asks_without_app,
    check_can_withdraw,
    check_new_request,
    current_values,
    requires_reason,
)
from appmux.admin.schemas import CreatePermissionRequest
from appmux.registry.models import App, PermissionRequest

SALES_V2: Final = "/Workspace/Shared/apps/sales-v2"
REGISTER_PAYLOAD: Final[Mapping[str, object]] = {
    "slug": "reports",
    "display_name": "Weekly reports",
    "source_path": "/Workspace/Shared/apps/reports",
}
MARKER: Final = "Marker Value From The Client"  # not a valid slug, so the payload is refused
FRANK: Final = "frank@example.com"
ERIN: Final = "erin@example.com"


def check(
    kind: str,
    app: str,
    payload: Mapping[str, object],
    *,
    asker: str = CAROL,
    apps: Iterable[App] | None = None,
    requests: Iterable[PermissionRequest] = (),
    shared_sp_allowed: bool = True,
) -> None:
    """``check_new_request`` over a snapshot that holds ``sales`` unless ``apps`` says otherwise."""
    snapshot = make_snapshot(
        apps=apps if apps is not None else [make_app("sales", owner=ALICE)],
        requests=requests,
        shared_sp_allowed=shared_sp_allowed,
    )
    check_new_request(snapshot, kind=kind, app=app, payload=payload, asker=asker)


def refusal_of(
    error: type[AdminError],
    code: str,
    kind: str,
    app: str,
    payload: Mapping[str, object],
    *,
    asker: str = CAROL,
    apps: Iterable[App] | None = None,
    requests: Iterable[PermissionRequest] = (),
    shared_sp_allowed: bool = True,
) -> AdminError:
    with pytest.raises(error) as info:
        check(
            kind,
            app,
            payload,
            asker=asker,
            apps=apps,
            requests=requests,
            shared_sp_allowed=shared_sp_allowed,
        )
    assert info.value.error_code == code
    return info.value


def test_there_are_exactly_three_kinds() -> None:
    assert KINDS == ("register", "delegation", "settings")


@pytest.mark.parametrize(
    ("kind", "app", "payload"),
    [
        pytest.param("register", "reports", REGISTER_PAYLOAD, id="register"),
        pytest.param("delegation", "sales", {"obo": True}, id="delegation-obo"),
        pytest.param("delegation", "sales", {"shared_sp": True}, id="delegation-shared-sp"),
        pytest.param("delegation", "sales", {"obo": True, "shared_sp": True}, id="delegation-both"),
        pytest.param("settings", "sales", {"slug": "sales-v2"}, id="settings-slug"),
        pytest.param("settings", "sales", {"display_name": "Sales EU"}, id="settings-name"),
        pytest.param("settings", "sales", {"source_path": SALES_V2}, id="settings-source"),
        pytest.param("settings", "sales", {"memory_budget_mb": 512}, id="settings-budget"),
        pytest.param(
            "settings",
            "sales",
            {"slug": "sales-v2", "display_name": "Sales EU", "memory_budget_mb": 512},
            id="settings-several-fields",
        ),
    ],
)
def test_a_valid_payload_of_each_catalogue_entry_is_accepted(
    kind: str, app: str, payload: Mapping[str, object]
) -> None:
    check(kind, app, payload)


@pytest.mark.parametrize(
    ("kind", "app", "payload"),
    [
        pytest.param("register", "reports", {"slug": "reports"}, id="register-lacks-fields"),
        pytest.param(
            "register", "other", REGISTER_PAYLOAD, id="register-app-is-not-the-payload-slug"
        ),
        pytest.param(
            "register", "reports", {**REGISTER_PAYLOAD, "slug": "Bad Slug"}, id="register-bad-slug"
        ),
        pytest.param(
            "register",
            "reports",
            {**REGISTER_PAYLOAD, "source_path": "/etc/passwd"},
            id="register-source-outside-the-workspace",
        ),
        pytest.param(
            "register",
            "reports",
            {**REGISTER_PAYLOAD, "memory_budget_mb": 256},
            id="register-does-not-ask-for-a-budget",
        ),
        pytest.param("delegation", "sales", {}, id="delegation-asks-for-nothing"),
        pytest.param("delegation", "sales", {"obo": False}, id="delegation-false-is-not-a-request"),
        pytest.param("delegation", "sales", {"pat": True}, id="delegation-unknown-key"),
        pytest.param("settings", "sales", {}, id="settings-without-a-field"),
        pytest.param("settings", "sales", {"owner": ALICE}, id="settings-owner-is-not-requestable"),
        pytest.param("settings", "sales", {"egress": []}, id="settings-egress-is-not-requestable"),
        pytest.param("settings", "sales", {"slug": "Bad Slug"}, id="settings-bad-slug"),
        pytest.param("settings", "sales", {"memory_budget_mb": 99999}, id="settings-budget-high"),
        pytest.param("delete", "sales", {}, id="deleting-an-app-is-not-a-request"),
    ],
)
def test_an_invalid_payload_is_refused_as_a_bad_request(
    kind: str, app: str, payload: Mapping[str, object]
) -> None:
    with pytest.raises(BadRequest):
        check(kind, app, payload)


def test_the_refusal_of_a_payload_never_repeats_what_was_sent() -> None:
    error = refusal_of(BadRequest, "bad_request", "settings", "sales", {"slug": MARKER})
    assert MARKER not in error.message


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"display_name": "Sales"}, id="same-name"),
        pytest.param({"memory_budget_mb": 256}, id="same-budget"),
        pytest.param({"slug": "sales"}, id="same-slug"),
        pytest.param({"source_path": "/Workspace/Shared/apps/sales"}, id="same-source"),
        pytest.param({"display_name": "Sales", "memory_budget_mb": 512}, id="one-of-two-is-equal"),
    ],
)
def test_a_settings_field_equal_to_the_current_value_is_refused(
    payload: Mapping[str, object],
) -> None:
    refusal_of(BadRequest, "no_change", "settings", "sales", payload, apps=[make_app()])


def test_a_delegation_already_held_is_refused() -> None:
    held = [make_app(obo=True)]
    refusal_of(BadRequest, "no_change", "delegation", "sales", {"obo": True}, apps=held)
    # The other flag is still asked for by a request that names only that one.
    check("delegation", "sales", {"shared_sp": True}, apps=held)


def test_a_shared_sp_request_is_refused_while_the_master_switch_is_off() -> None:
    for payload in ({"shared_sp": True}, {"obo": True, "shared_sp": True}):
        refusal_of(
            Conflict,
            "shared_sp_not_allowed",
            "delegation",
            "sales",
            payload,
            shared_sp_allowed=False,
        )
    # OBO is not behind the switch.
    check("delegation", "sales", {"obo": True}, shared_sp_allowed=False)


def test_a_register_for_a_slug_an_app_has_is_refused() -> None:
    taken = {**REGISTER_PAYLOAD, "slug": "sales"}
    refusal_of(Conflict, "slug_taken", "register", "sales", taken)


def test_a_rename_to_a_slug_another_app_has_is_refused() -> None:
    apps = [make_app("sales"), make_app("docs", owner=FRANK)]
    refusal_of(Conflict, "slug_taken", "settings", "sales", {"slug": "docs"}, apps=apps)


def test_a_request_for_an_app_that_is_not_there_is_refused() -> None:
    refusal_of(NotFound, "not_found", "settings", "gone", {"display_name": "Gone"})


def test_one_pending_request_per_app_and_kind() -> None:
    pending = make_request(1, kind="settings", payload={"display_name": "Sales EU"})
    refusal_of(
        Conflict,
        "request_pending",
        "settings",
        "sales",
        {"memory_budget_mb": 512},
        requests=[pending],
    )


@pytest.mark.parametrize(
    "other",
    [
        pytest.param(
            make_request(1, kind="settings", payload={"display_name": "Sales EU"}, status="denied"),
            id="a-closed-request",
        ),
        pytest.param(make_request(1, kind="delegation", payload={"obo": True}), id="another-kind"),
        pytest.param(
            make_request(1, kind="settings", payload={"display_name": "Docs EU"}, app_slug="docs"),
            id="another-app",
        ),
    ],
)
def test_only_a_pending_request_of_the_same_app_and_kind_blocks_a_new_one(
    other: PermissionRequest,
) -> None:
    apps = [make_app("sales"), make_app("docs", owner=FRANK)]
    check("settings", "sales", {"memory_budget_mb": 512}, apps=apps, requests=[other])


def test_one_pending_register_request_per_proposed_slug() -> None:
    pending = make_request(
        1, kind="register", payload=REGISTER_PAYLOAD, app_slug=None, requested_by=ERIN
    )
    refusal_of(
        Conflict, "request_pending", "register", "reports", REGISTER_PAYLOAD, requests=[pending]
    )
    other = {**REGISTER_PAYLOAD, "slug": "reports-eu"}
    check("register", "reports-eu", other, requests=[pending])


def register_by(person: str, request_id: int, *, status: str = "pending") -> PermissionRequest:
    return make_request(
        request_id,
        kind="register",
        payload={**REGISTER_PAYLOAD, "slug": f"app-{request_id}"},
        app_slug=None,
        requested_by=person,
        status=status,
    )


def test_a_person_has_at_most_three_pending_register_requests() -> None:
    assert MAX_PENDING_REGISTER_PER_PERSON == 3
    three = [register_by(GRACE, number) for number in (1, 2, 3)]
    refusal_of(
        Conflict,
        "too_many_pending",
        "register",
        "reports",
        REGISTER_PAYLOAD,
        asker=GRACE,
        requests=three,
    )


def test_only_pending_requests_of_that_person_count_toward_the_cap() -> None:
    mixed = [
        register_by(GRACE, 1),
        register_by(GRACE, 2, status="withdrawn"),
        register_by(GRACE, 3, status="denied"),
        register_by(ERIN, 4),
        register_by(ERIN, 5),
    ]
    check("register", "reports", REGISTER_PAYLOAD, asker=GRACE, requests=mixed)


@pytest.mark.parametrize(("length", "accepted"), [(1, True), (500, True), (501, False)])
def test_a_reason_is_up_to_500_characters(length: int, accepted: bool) -> None:
    body = {
        "kind": "settings",
        "app": "sales",
        "payload": {"display_name": "X"},
        "reason": "r" * length,
    }
    if accepted:
        CreatePermissionRequest.model_validate(body)
    else:
        with pytest.raises(ValidationError):
            CreatePermissionRequest.model_validate(body)


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_a_blank_reason_is_refused(reason: str) -> None:
    body = {"kind": "settings", "app": "sales", "payload": {"display_name": "X"}, "reason": reason}
    with pytest.raises(ValidationError):
        CreatePermissionRequest.model_validate(body)


def test_only_the_asker_withdraws() -> None:
    row = make_request(1, kind="settings", payload={"display_name": "X"}, requested_by=CAROL)
    check_can_withdraw(row, CAROL)
    check_can_withdraw(row, CAROL.upper())  # an e-mail is compared as the registry stores it
    with pytest.raises(Forbidden) as info:
        check_can_withdraw(row, ALICE)
    assert info.value.error_code == "not_the_asker"


@pytest.mark.parametrize("status", ["approved", "denied", "withdrawn"])
def test_a_request_is_withdrawn_only_while_it_is_pending(status: str) -> None:
    row = make_request(1, kind="settings", payload={"display_name": "X"}, status=status)
    with pytest.raises(Conflict) as info:
        check_can_withdraw(row, CAROL)
    assert info.value.error_code == "not_pending"


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [
        pytest.param("register", REGISTER_PAYLOAD, True, id="register-is-a-gate"),
        pytest.param("delegation", {"obo": True}, True, id="obo-is-a-gate"),
        pytest.param("delegation", {"shared_sp": True}, True, id="shared-sp-is-a-gate"),
        pytest.param("settings", {"source_path": SALES_V2}, True, id="source-path-is-a-gate"),
        pytest.param("settings", {"display_name": "X"}, False, id="name-is-not"),
        pytest.param(
            "settings", {"slug": "x", "memory_budget_mb": 99}, False, id="slug-budget-not"
        ),
        pytest.param(
            "settings", {"display_name": "X", "source_path": SALES_V2}, True, id="one-gate-of-two"
        ),
        pytest.param("delete", {}, False, id="unknown-kind"),
    ],
)
def test_a_reason_is_required_to_approve_exactly_when_a_gate_field_is_present(
    kind: str, payload: Mapping[str, object], expected: bool
) -> None:
    assert requires_reason(kind, payload) is expected


def test_the_gate_fields_are_the_catalogues_and_nothing_else() -> None:
    expected = {
        "register": frozenset({"slug", "display_name", "source_path"}),
        "delegation": frozenset({"obo", "shared_sp"}),
        "settings": frozenset({"source_path"}),
    }
    assert dict(GATE_FIELDS) == expected


def test_only_register_is_asked_without_an_app() -> None:
    assert [asks_without_app(kind) for kind in KINDS] == [True, False, False]


def test_the_current_value_is_shown_next_to_each_requested_key() -> None:
    snapshot = make_snapshot(apps=[make_app(obo=True)])
    settings = make_request(
        1, kind="settings", payload={"display_name": "Sales EU", "memory_budget_mb": 512}
    )
    delegation = make_request(2, kind="delegation", payload={"obo": True, "shared_sp": True})
    register = make_request(3, kind="register", payload=REGISTER_PAYLOAD, app_slug=None)
    assert current_values(snapshot, settings) == {"display_name": "Sales", "memory_budget_mb": 256}
    assert current_values(snapshot, delegation) == {"obo": True, "shared_sp": False}
    assert current_values(snapshot, register) is None


def test_the_current_value_of_a_request_for_a_vanished_app_is_null() -> None:
    row = make_request(1, kind="settings", payload={"display_name": "X"}, app_slug="gone")
    assert current_values(make_snapshot(apps=[make_app()]), row) is None
