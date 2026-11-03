"""The audit entry: what it can hold, what it refuses, and the INSERT it produces.

Data model rule 15: a secret value never appears in ``audit_log.detail``. The detail model has no
field that can carry one, and every text field refuses a string shaped like a credential. The
field-by-field test derives the list of fields from the models, so a field added later cannot be
left out of it.
"""

from __future__ import annotations

import json
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from appmux.registry import audit
from appmux.registry.audit import AuditDetail, AuditEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_MODEL_PATH = REPO_ROOT / "docs" / "product" / "v1-data-model.md"
BASELINE_PATH = REPO_ROOT / "migrations" / "0001_baseline.sql"

# Fake values built from fragments so no scanner mistakes this file for a leak.
# None of them is a real credential.
FAKE_PASSWORD = "hunter2" + "hunter2"
FAKE_DATABRICKS_TOKEN = "dapi" + "0123456789abcdef" * 2
FAKE_OAUTH_SECRET = "dose" + "fedcba9876543210" * 2
FAKE_JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "c2lnbmF0dXJl"
FAKE_VENDOR_KEY = "sk" + "-proj-" + "A1b2C3d4E5f6G7h8"

# Every spelling a value takes when someone pastes it into a name, a path or a reason.
SECRET_SHAPES: dict[str, str] = {
    "databricks token": FAKE_DATABRICKS_TOKEN,
    # The first 20 characters of a token still fit a field as short as an app slug (31).
    "start of a databricks token": FAKE_DATABRICKS_TOKEN[:20],
    "start of an oauth client secret": FAKE_OAUTH_SECRET[:20],
    "start of a vendor key": "sk-proj-a1b2c3d4e5f6",
    "databricks token after an underscore": "env_" + FAKE_DATABRICKS_TOKEN,
    "oauth client secret": FAKE_OAUTH_SECRET,
    "oauth client secret after an underscore": "env_" + FAKE_OAUTH_SECRET,
    "bearer value": "Bearer " + "abcdefgh12345678",
    "bearer value after an underscore": "header_bearer " + "abcdefgh12345678",
    "authorization header": "Authorization: Bearer " + "abcdefgh12345678",
    "basic authorization header": "Authorization: Basic " + "dXNlcjpwYXNzd29yZA==",
    "json web token": FAKE_JWT,
    "json web token after an underscore": "jwt_" + FAKE_JWT,
    "vendor key": FAKE_VENDOR_KEY,
    "vendor key after an underscore": "key_" + FAKE_VENDOR_KEY,
    "aws access key id": "AKIA" + "IOSFODNN7EXAMPLE",
    "private key header": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "url with a password": "https://user:" + FAKE_PASSWORD + "@workspace.example.invalid/x",
    "api_key assignment": "api_key=" + FAKE_PASSWORD,
    "password assignment with spaces": "password = " + FAKE_PASSWORD,
    "client_secret assignment": "client_secret=" + FAKE_PASSWORD,
    "access_token assignment": "access_token=" + FAKE_PASSWORD,
    "environment variable assignment": "DATABRICKS_CLIENT_SECRET=" + FAKE_PASSWORD,
    "environment variable of the token": "DATABRICKS_TOKEN=" + FAKE_DATABRICKS_TOKEN,
    "colon form": "client_secret: " + FAKE_PASSWORD,
    "colon form without a space": "access_token:" + FAKE_PASSWORD,
    "json form": '{"client_secret": "' + FAKE_PASSWORD + '"}',
    "quoted assignment": "client_secret='" + FAKE_PASSWORD + "'",
}

# Plain words about secrets, with no value after them, are what an audit reason is made of.
ALLOWED_REASONS = [
    "dashboard queries the SQL warehouse as the viewer",
    "the app reads per-viewer data only",
    "slug 'reports' is already taken (automatic)",
    "finance owns the folder; 256 MB fits",
    "joined the platform team",
    "the secret was rotated",
    "token revoked after the leak",
    "needs the shared token to call the API",
    "api key rotation",
    "attached the secret from scope sales-secrets",
    "password reset requested by the owner",
    "authorization granted by the platform team",
]


def _entry(**changes: Any) -> AuditEntry:
    fields: dict[str, Any] = {
        "actor": "alice@example.com",
        "action": "release.activate",
        "app_slug": "sales",
        "detail": {"release": 7},
    }
    return AuditEntry.model_validate({**fields, **changes})


def test_an_entry_of_the_data_model_examples_is_accepted() -> None:
    attach = _entry(
        action="resource.attach",
        detail={
            "key": "openai-api-key",
            "type": "secret",
            "target": "sales-secrets/openai-api-key",
            "check": "ok",
        },
    )
    approve = _entry(
        actor="dana@example.com",
        action="request.approve",
        detail={
            "request": 2,
            "kind": "settings",
            "before": {"display_name": "Docs", "memory_budget_mb": 128},
            "after": {"display_name": "Team docs", "memory_budget_mb": 256},
        },
    )
    rename = _entry(action="app.rename", detail={"from": "sales", "to": "revenue"})
    cleanup = _entry(
        actor="appmux", action="release.delete", detail={"release": 1, "by": "retention", "cap": 10}
    )
    assert attach.detail.target == "sales-secrets/openai-api-key"
    assert approve.detail.after is not None
    assert approve.detail.after.memory_budget_mb == 256
    assert rename.detail.from_ == "sales"
    assert cleanup.actor == "appmux"


@pytest.mark.parametrize(
    "secret_field", ["value", "secret", "token", "password", "new_value", "env"]
)
def test_the_detail_model_has_no_field_that_can_hold_a_value(secret_field: str) -> None:
    assert secret_field not in AuditDetail.model_fields
    with pytest.raises(ValidationError):
        AuditDetail.model_validate({secret_field: "not-a-real-secret"})


def test_a_changed_field_set_cannot_carry_an_unlisted_key() -> None:
    with pytest.raises(ValidationError):
        AuditDetail.model_validate({"after": {"client_secret": "x"}})


@dataclass(frozen=True)
class TextFieldCase:
    """How the field-by-field test treats one text field of the audit models."""

    # A value the field accepts: proves a refusal below is about the secret, not about the setup.
    good: Any
    # Wraps a secret into a value of the field's own form, so the secret is refused by the
    # credential check and not merely by the field's pattern.
    carrier: str = "{}"
    # Secret shapes this field accepts on purpose, with the reason in the row below. The test
    # requires the field to accept exactly these, so an exemption cannot outlive its reason.
    legal_shapes: frozenset[str] = frozenset()


# One row per text field, named "Model.field". The test below fails when the models have a text
# field that is not listed here, and when a listed one no longer exists.
TEXT_FIELD_CASES: dict[str, TextFieldCase] = {
    "AuditEntry.actor": TextFieldCase("alice@example.com", "{}@example.com"),
    "AuditEntry.action": TextFieldCase("app.start"),
    # A slug is a name picked by a platform admin: "sk-learn-demo" is a legal one, and only the
    # Databricks token shapes are refused there.
    "AuditEntry.app_slug": TextFieldCase(
        "sales", legal_shapes=frozenset({"start of a vendor key"})
    ),
    "AuditEntry.reason": TextFieldCase("the app reads per-viewer data only"),
    "AuditDetail.kind": TextFieldCase("delegation"),
    "AuditDetail.locks": TextFieldCase("1 of 3"),
    "AuditDetail.owner": TextFieldCase("alice@example.com", "{}@example.com"),
    "AuditDetail.principal": TextFieldCase("group:sales-team", "user:{}"),
    "AuditDetail.permission": TextFieldCase("CAN_USE"),
    "AuditDetail.key": TextFieldCase("openai-api-key"),
    "AuditDetail.type": TextFieldCase("sql_warehouse"),
    "AuditDetail.target": TextFieldCase("sales-secrets/openai-api-key"),
    "AuditDetail.access_as": TextFieldCase("obo"),
    "AuditDetail.check": TextFieldCase("ok"),
    "AuditDetail.from_": TextFieldCase("sales"),
    "AuditDetail.to": TextFieldCase("revenue"),
    "AuditDetail.by": TextFieldCase("retention"),
    "AuditDetail.path": TextFieldCase("app.py"),
    "AuditDetail.digest": TextFieldCase("sha256:9f2c1234"),
    "AuditDetail.setting": TextFieldCase("shared_sp_delegation_allowed"),
    "ChangedFields.slug": TextFieldCase("reports"),
    "ChangedFields.display_name": TextFieldCase("Team docs"),
    "ChangedFields.source_path": TextFieldCase("/Workspace/Shared/apps/sales"),
    "ChangedFields.health_path": TextFieldCase("/healthz"),
}


def _leaf_types(annotation: Any) -> list[Any]:
    """The types a field annotation can hold, with ``Annotated`` and unions taken apart."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _leaf_types(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        return [leaf for arg in get_args(annotation) for leaf in _leaf_types(arg)]
    return [annotation]


def _kind_of(leaf: Any) -> str:
    if leaf is str or get_origin(leaf) is Literal:
        return "text"
    if leaf in (int, bool, type(None)):
        return "number"
    if isinstance(leaf, type) and issubclass(leaf, BaseModel):
        return "model"
    return "unknown"


def _text_field_routes(
    model: type[BaseModel], route: tuple[str, ...] = ()
) -> list[tuple[str, tuple[str, ...]]]:
    """Every text field reachable from ``model`` as (``Model.field``, the keys that reach it).

    The keys are the names the data holds (``from``, not ``from_``). A model reached twice, like
    ``ChangedFields`` under ``before`` and ``after``, is listed once for each way in.
    """
    found: list[tuple[str, tuple[str, ...]]] = []
    for name, info in model.model_fields.items():
        key = info.alias or name
        leaves = _leaf_types(info.annotation)
        kinds = {_kind_of(leaf) for leaf in leaves}
        assert "unknown" not in kinds, f"{model.__name__}.{name}: teach this test its type"
        if "text" in kinds:
            found.append((f"{model.__name__}.{name}", (*route, key)))
        for leaf in leaves:
            if _kind_of(leaf) == "model":
                found.extend(_text_field_routes(leaf, (*route, key)))
    return found


TEXT_FIELD_ROUTES = _text_field_routes(AuditEntry)


def _entry_with(route: tuple[str, ...], value: Any) -> AuditEntry:
    changes: Any = value
    for key in reversed(route):
        changes = {key: changes}
    return _entry(**changes)


def test_the_text_field_table_lists_every_text_field_of_the_models_and_no_other() -> None:
    assert {label for label, _ in TEXT_FIELD_ROUTES} == set(TEXT_FIELD_CASES)


def test_the_route_walk_reaches_both_the_before_and_the_after_values() -> None:
    routes = [route for label, route in TEXT_FIELD_ROUTES if label == "ChangedFields.slug"]
    assert sorted(routes) == [("detail", "after", "slug"), ("detail", "before", "slug")]
    assert ("detail", "from") in [route for _, route in TEXT_FIELD_ROUTES]


@pytest.mark.parametrize(("label", "route"), TEXT_FIELD_ROUTES)
def test_every_text_field_accepts_a_value_of_its_own_form(
    label: str, route: tuple[str, ...]
) -> None:
    _entry_with(route, TEXT_FIELD_CASES[label].good)


@pytest.mark.parametrize(("label", "route"), TEXT_FIELD_ROUTES)
def test_no_text_field_stores_a_secret_shaped_value(label: str, route: tuple[str, ...]) -> None:
    case = TEXT_FIELD_CASES[label]
    carrier = case.carrier
    accepted: list[str] = []
    repeated: list[str] = []
    for shape, secret in SECRET_SHAPES.items():
        try:
            _entry_with(route, carrier.format(secret))
        except ValidationError as refusal:
            # A refusal must not repeat what it refused: this text ends up in logs.
            if secret in str(refusal) or secret in repr(refusal.errors(include_input=False)):
                repeated.append(shape)
        else:
            accepted.append(shape)
    assert set(accepted) == case.legal_shapes, f"{label} stored a value shaped like: {accepted}"
    assert repeated == [], f"{label} repeated the refused value of: {repeated}"


@pytest.mark.parametrize(
    "app_slug", ["sk-learn-demo", "pk-explorer-app", "ghp-reports-hub", "pk-dashboards"]
)
def test_a_slug_that_starts_like_a_vendor_key_is_still_a_slug(app_slug: str) -> None:
    assert _entry(app_slug=app_slug).app_slug == app_slug


@pytest.mark.parametrize("secret_shaped", SECRET_SHAPES.values(), ids=list(SECRET_SHAPES))
def test_a_secret_shaped_value_cannot_be_stored_in_a_before_or_after_value(
    secret_shaped: str,
) -> None:
    with pytest.raises(ValidationError):
        AuditDetail.model_validate({"after": {"display_name": secret_shaped}})
    with pytest.raises(ValidationError):
        AuditDetail.model_validate({"before": {"source_path": "/Workspace/" + secret_shaped}})


@pytest.mark.parametrize("secret_shaped", SECRET_SHAPES.values(), ids=list(SECRET_SHAPES))
def test_a_secret_shaped_value_cannot_be_stored_in_the_reason(secret_shaped: str) -> None:
    with pytest.raises(ValidationError):
        _entry(reason=f"rotated, new value is {secret_shaped}")


@pytest.mark.parametrize("reason", ALLOWED_REASONS)
def test_a_reason_that_only_names_a_secret_is_accepted(reason: str) -> None:
    assert _entry(reason=reason).reason == reason


def test_the_credential_check_runs_on_the_database_role_form_of_the_actor() -> None:
    assert _entry(actor="db:appmux_platform_writers").actor == "db:appmux_platform_writers"
    with pytest.raises(ValidationError):
        _entry(actor="db:" + FAKE_DATABRICKS_TOKEN)


def _example_rows_of_the_data_model() -> list[dict[str, Any]]:
    """The audit_log example rows of data model 4.9, as ``AuditEntry`` input."""
    text = DATA_MODEL_PATH.read_text()
    section = text[text.index("### 4.9 `audit_log`") : text.index("## 5. Walkthroughs")]
    rows: list[dict[str, Any]] = []
    for line in section.splitlines():
        if not line.startswith("| 2026-"):
            continue
        _, actor, action, app_slug, detail, reason = (
            cell.strip().strip("`") for cell in line.strip().strip("|").split("|")
        )
        # The document abbreviates a digest with an ellipsis: "sha256:9f2c…".
        rows.append(
            {
                "actor": actor,
                "action": action,
                "app_slug": app_slug or None,
                "detail": json.loads(detail.replace("…", "0000")),
                "reason": reason or None,
            }
        )
    return rows


def test_every_example_row_of_the_data_model_is_accepted() -> None:
    rows = _example_rows_of_the_data_model()
    assert len(rows) >= 14, "the example rows of data model 4.9 were not found"
    for row in rows:
        automatic = row["detail"].get("automatic") is True
        AuditEntry.model_validate({**row, "automatic": automatic})


def test_a_secret_resource_target_must_be_a_scope_and_key_reference() -> None:
    attach: dict[str, Any] = {"key": "openai-api-key", "type": "secret"}
    with pytest.raises(ValidationError):
        AuditDetail.model_validate({**attach, "target": "just-one-word"})
    with pytest.raises(ValidationError):
        AuditDetail.model_validate({**attach, "target": "scope/key/extra"})
    assert AuditDetail.model_validate({**attach, "target": "scope/key"}).target == "scope/key"
    # Any other type names an object, so a warehouse id is fine.
    warehouse = {"key": "sql-warehouse", "type": "sql_warehouse", "target": "a1b2c3d4e5f67890"}
    assert AuditDetail.model_validate(warehouse).target == "a1b2c3d4e5f67890"


def test_the_stored_json_holds_only_the_fields_that_were_set() -> None:
    entry = _entry(
        action="request.approve",
        detail={"request": 1, "kind": "delegation", "after": {"obo": False}},
        automatic=True,
    )
    statement, parameters = audit.insert_statement(entry)
    stored = json.loads(parameters[3])
    assert stored == {
        "request": 1,
        "kind": "delegation",
        "after": {"obo": False},
        "automatic": True,
    }
    assert statement.startswith("INSERT INTO audit_log")


def test_the_stored_json_uses_the_names_of_the_data_model() -> None:
    entry = _entry(action="app.rename", detail={"from": "sales", "to": "revenue"})
    _, parameters = audit.insert_statement(entry)
    assert json.loads(parameters[3]) == {"from": "sales", "to": "revenue"}


def test_the_insert_is_parameterised_and_carries_no_value_in_the_sql_text() -> None:
    entry = _entry(app_slug="sales", reason="because", detail={"release": 7})
    statement, parameters = audit.insert_statement(entry)
    assert statement.count("%s") == len(parameters) == 5
    assert "sales" not in statement
    assert "because" not in statement
    assert parameters == (
        "alice@example.com",
        "release.activate",
        "sales",
        '{"release":7}',
        "because",
    )


def test_the_automatic_flag_lands_in_the_detail_and_a_conflict_is_refused() -> None:
    automatic = _entry(
        actor="dana@example.com",
        action="request.deny",
        detail={"request": 8, "kind": "settings"},
        reason="slug 'reports' is already taken (automatic)",
        automatic=True,
    )
    assert automatic.row_detail().automatic is True
    assert _entry().row_detail().automatic is None
    with pytest.raises(ValidationError):
        _entry(detail={"release": 7, "automatic": True}, automatic=False)


@pytest.mark.parametrize(
    "action",
    ["delegation.grant", "delegation.revoke", "platform_admin.add", "platform_admin.remove"],
)
def test_the_actions_that_pass_a_risk_through_a_gate_require_a_reason(action: str) -> None:
    with pytest.raises(ValidationError):
        _entry(action=action, detail={"principal": "user:erin@example.com"})
    with pytest.raises(ValidationError):
        _entry(action=action, detail={"principal": "user:erin@example.com"}, reason="   ")
    accepted = _entry(
        action=action,
        detail={"principal": "user:erin@example.com"},
        reason="joined the platform team",
    )
    assert accepted.reason == "joined the platform team"


@pytest.mark.parametrize(
    "actor", ["alice@example.com", "db:appmux_platform_writers", "appmux", "dana@example.com"]
)
def test_actor_forms_of_the_data_model_are_accepted(actor: str) -> None:
    assert _entry(actor=actor).actor == actor


@pytest.mark.parametrize(
    "actor",
    [
        "",
        "Alice@example.com",
        "alice",
        "db:",
        "db:Role",
        "root",
        "alice@example.com extra",
        "alice@example.com\n",
        "token@host",
        "alice@example",
        "https://user:" + FAKE_PASSWORD + "@example.com",
        pytest.param("a" * 65 + "@example.com", id="local part too long"),
        pytest.param("alice@" + "a" * 300 + ".example.com", id="longer than an email can be"),
    ],
)
def test_other_actor_forms_are_refused(actor: str) -> None:
    with pytest.raises(ValidationError):
        _entry(actor=actor)


@pytest.mark.parametrize(
    "owner", ["alice@example.com", "first.last+tag@sub.example.com", "o'brien@example.co.uk"]
)
def test_an_owner_is_an_email_in_lower_case(owner: str) -> None:
    assert _entry(detail={"owner": owner}).detail.owner == owner


@pytest.mark.parametrize(
    "owner",
    [
        "",
        "alice",
        "Alice@example.com",
        "alice@example.com extra",
        "token@host",
        "alice@@example.com",
        "https://user:" + FAKE_PASSWORD + "@example.com",
        "password=" + FAKE_PASSWORD + "@example.com",
        pytest.param("a" * 65 + "@example.com", id="local part too long"),
        pytest.param("alice@" + "a" * 300 + ".example.com", id="longer than an email can be"),
    ],
)
def test_other_owner_forms_are_refused(owner: str) -> None:
    with pytest.raises(ValidationError):
        _entry(detail={"owner": owner})


@pytest.mark.parametrize("app_slug", ["sales", "a", "0day", "my-app-2", "a" * 31])
def test_an_app_slug_of_the_data_model_form_is_accepted(app_slug: str) -> None:
    assert _entry(app_slug=app_slug).app_slug == app_slug


@pytest.mark.parametrize(
    "app_slug", ["", "Sales", "sales_2", "_platform", "-sales", "sa les", "sales/x", "a" * 32]
)
def test_an_app_slug_that_is_not_a_slug_is_refused(app_slug: str) -> None:
    with pytest.raises(ValidationError):
        _entry(app_slug=app_slug)


def test_the_app_slug_pattern_is_the_one_the_database_checks() -> None:
    # audit_log has no foreign key to apps, so only this pattern keeps a value out of the column.
    assert f"CHECK (slug ~ '{audit.SLUG_PATTERN}')" in BASELINE_PATH.read_text()


def test_an_unknown_action_is_refused() -> None:
    with pytest.raises(ValidationError):
        _entry(action="release.explode")


def test_every_action_name_of_the_data_model_is_known() -> None:
    expected = {
        "app.register", "app.delete", "app.rename", "app.view_as_user", "app.set_owner",
        "app.edit_platform_fields", "app.start", "app.stop", "app.restart", "request.create",
        "request.withdraw", "request.approve", "request.deny", "delegation.grant",
        "delegation.revoke", "platform_admin.add", "platform_admin.remove", "grant.app",
        "egress.edit", "settings.edit", "resource.attach", "resource.detach", "config.edit",
        "release.create", "release.activate", "release.tag", "release.untag", "release.label",
        "release.lock", "release.unlock", "release.delete", "release.view_source",
        "app.view_logs",
    }  # fmt: skip
    assert set(audit.AUDIT_ACTIONS) == expected


@pytest.mark.parametrize("field", list(AuditEntry.model_fields))
def test_an_entry_is_frozen(field: str) -> None:
    entry = _entry()
    with pytest.raises(ValidationError):
        setattr(entry, field, getattr(entry, field))


# --- the principal of a group (decision D3) -----------------------------------------------------


@pytest.mark.parametrize(
    "group_name",
    ["Data Platform", "sales team (EMEA)", "finance/reporting", "équipe données", "a" * 255],
)
def test_a_group_principal_may_hold_spaces_and_other_printable_characters(
    group_name: str,
) -> None:
    entry = _entry(action="grant.app", detail={"principal": f"group:{group_name}"})

    assert entry.detail.principal == f"group:{group_name}"


@pytest.mark.parametrize(
    "group_name",
    [
        "",
        "a" * 256,
        "line\nbreak",
        "trailing\n",
        "carriage\rreturn",
        "tab\there",
        "nul\x00byte",
        "bell\x07",
        "del\x7f",
        "next\x85line",
        "separator\u2028here",
        "separator\u2029here",
    ],
    ids=lambda name: repr(name)[:30],
)
def test_a_group_principal_refuses_a_control_character_a_newline_or_a_bad_length(
    group_name: str,
) -> None:
    with pytest.raises(ValidationError):
        _entry(action="grant.app", detail={"principal": f"group:{group_name}"})


@pytest.mark.parametrize("secret_shaped", SECRET_SHAPES.values(), ids=list(SECRET_SHAPES))
def test_a_secret_shaped_group_name_is_still_refused(secret_shaped: str) -> None:
    with pytest.raises(ValidationError):
        _entry(action="grant.app", detail={"principal": f"group:{secret_shaped}"})
    with pytest.raises(ValidationError):
        _entry(action="grant.app", detail={"principal": f"group:Data Platform {secret_shaped}"})


@pytest.mark.parametrize("principal", ["user:two words@example.com", "everyone:a b", "everyone:"])
def test_user_and_everyone_principals_keep_their_shapes(principal: str) -> None:
    with pytest.raises(ValidationError):
        _entry(action="grant.app", detail={"principal": principal})
    assert _entry(action="grant.app", detail={"principal": "user:erin@example.com"})
    assert _entry(action="grant.app", detail={"principal": "everyone:*"})


def test_a_service_principal_is_recorded_by_its_application_id() -> None:
    # ADR 0020; the id is made up. The grant schema holds it to a UUID; the audit keeps the shape.
    application_id = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab"

    assert _entry(action="grant.app", detail={"principal": f"service_principal:{application_id}"})
    for principal in ("service_principal:", "service_principal:a b", "robot:" + application_id):
        with pytest.raises(ValidationError):
            _entry(action="grant.app", detail={"principal": principal})


def test_a_refused_group_name_is_not_repeated_in_the_error() -> None:
    with pytest.raises(ValidationError) as refusal:
        _entry(action="grant.app", detail={"principal": "group:" + FAKE_DATABRICKS_TOKEN})

    assert FAKE_DATABRICKS_TOKEN not in str(refusal.value)


# A shape inside a longer word keeps the start of the word: only the value is masked.
MASKED_IN_A_LONGER_WORD = {
    "json web token after a prefix": ("jwt_" + FAKE_JWT, "jwt_***c2lnbmF0dXJl"),
    "json web token after a dash": ("x-" + FAKE_JWT, "x-***c2lnbmF0dXJl"),
    "json web token after another eyJ": ("eyJ" + FAKE_JWT, "***c2lnbmF0dXJl"),
    "name inside a longer name": ("MY_CLIENT_SECRET=" + FAKE_PASSWORD, "MY_CLIENT_SECRET=***"),
    "name after a repeated name": (
        "tokentoken.secret_value: " + FAKE_PASSWORD,
        "tokentoken.secret_value: ***",
    ),
    "two names on one line": (
        "client.token=" + FAKE_PASSWORD + " then password: x",
        "client.token=*** then password: ***",
    ),
}


@pytest.mark.parametrize(
    ("text", "masked"), MASKED_IN_A_LONGER_WORD.values(), ids=list(MASKED_IN_A_LONGER_WORD)
)
def test_masking_keeps_the_start_of_the_word_a_shape_sits_in(text: str, masked: str) -> None:
    assert audit.mask_credentials(text) == masked


# Lines a hostile child can print: a name or "eyJ" repeated with nothing that completes the shape.
# An open quantifier after the name made every start rescan the rest of the line (review T205 R1).
LONG_LINES_WITH_NO_CREDENTIAL = {
    "a credential name repeated, 32 KB": "token" * 6_400,
    "eyJ repeated, 64 KB": "eyJ" * 21_334,
}


@pytest.mark.parametrize(
    "line", LONG_LINES_WITH_NO_CREDENTIAL.values(), ids=list(LONG_LINES_WITH_NO_CREDENTIAL)
)
def test_masking_and_refusing_a_long_line_take_time_in_proportion_to_its_length(line: str) -> None:
    # A child's output is masked in the event loop every app shares. Measured on the machine this
    # was written on, to mask and refuse each line: the quadratic shapes took 10 s (the name) and
    # 2.9 s (eyJ), the linear ones take 6 ms and 21 ms. The bound is over 20 times the linear cost,
    # so a slow runner does not fail it, and a fraction of the quadratic one.
    started = time.perf_counter()
    masked = audit.mask_credentials(line)
    kept = audit._refuse_credential_shape(line)
    took_s = time.perf_counter() - started

    assert (masked, kept) == (line, line)
    assert took_s < 0.5
