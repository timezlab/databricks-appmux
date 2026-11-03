"""The negative rules of the admin API schemas, one test per rule (T186).

Each refusal below fails if the validator that makes it is dropped. The messages are those the UI
shows (``web/src/lib/api/schemas/``), so a person reads the same words from either side. A rule
that needs state (the app's owner) is tested through the helper the route calls.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from appmux.admin import schemas
from appmux.registry import models as registry_models

OWNER = "alice@example.com"
EXAMPLES_DIR = (
    Path(__file__).resolve().parents[2] / "specs" / "001-appmux-v1" / "contracts" / "examples"
)


def refused(model: type[BaseModel], body: dict[str, Any], message: str) -> None:
    """The body is refused, and the refusal says ``message``."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(body)
    assert message in str(caught.value)


def accepted(model: type[BaseModel], body: dict[str, Any]) -> None:
    model.model_validate(body)


def rule(**overrides: str) -> dict[str, str]:
    return {
        "action": "deny",
        "cidr": "10.20.0.0/16",
        "port": "443",
        "comment": "",
    } | overrides


def grant(**overrides: str) -> dict[str, str]:
    return {"type": "group", "name": "sales-team", "permission": "CAN_USE"} | overrides


# --- grants ---------------------------------------------------------------------------------


def test_everyone_is_never_can_manage() -> None:
    refused(
        schemas.GrantRequest,
        grant(type="everyone", name="*", permission="CAN_MANAGE"),
        "everyone is CAN_USE only",
    )
    accepted(schemas.GrantRequest, grant(type="everyone", name="*", permission="CAN_USE"))


def test_everyone_is_named_star() -> None:
    refused(
        schemas.GrantRequest,
        grant(type="everyone", name="sales-team"),
        "everyone is named '*'",
    )


def test_a_group_is_not_named_star_or_blank() -> None:
    refused(schemas.GrantRequest, grant(name="*"), "names everyone, not a group")
    refused(schemas.GrantRequest, grant(name="  "), "must not be blank")


@pytest.mark.parametrize("name", ["Carol", "Alice@example.com", " alice@example.com", "a@b"])
def test_a_user_is_a_lower_case_email(name: str) -> None:
    refused(schemas.GrantRequest, grant(type="user", name=name), "lower case")


def test_a_grant_type_is_one_of_four() -> None:
    with pytest.raises(ValidationError):
        schemas.GrantRequest.model_validate(grant(type="team", name="x"))


SALES_BOT = "4f3c1b2a-9d8e-4f7a-b6c5-0123456789ab"


def test_a_service_principal_is_named_by_its_application_id_and_is_can_use() -> None:
    accepted(schemas.GrantRequest, grant(type="service_principal", name=SALES_BOT))


def test_a_service_principal_is_never_can_manage() -> None:
    refused(
        schemas.GrantRequest,
        grant(type="service_principal", name=SALES_BOT, permission="CAN_MANAGE"),
        "a service principal is CAN_USE only",
    )


@pytest.mark.parametrize(
    "name",
    [
        "sales-bot",
        SALES_BOT.upper(),
        SALES_BOT.replace("-", ""),
        "{" + SALES_BOT + "}",
        SALES_BOT + "\n",
        " " + SALES_BOT,
        SALES_BOT[:-1] + "g",
        "*",
        "",
    ],
    ids=[
        "a-name",
        "upper-case",
        "no-dashes",
        "braces",
        "newline",
        "space",
        "not-hex",
        "star",
        "empty",
    ],
)
def test_a_service_principal_name_that_is_not_a_lower_case_uuid_is_refused(name: str) -> None:
    refused(
        schemas.GrantRequest,
        grant(type="service_principal", name=name),
        "application id",
    )


@pytest.mark.parametrize(
    ("principal_type", "name"), [("user", "carol@example.com"), ("group", "sales-team")]
)
@pytest.mark.parametrize("permission", ["ADMIN", "CAN_EDIT", "can_use", "CAN_USE ", "READ", ""])
def test_a_grant_permission_is_one_of_two_levels(
    principal_type: str, name: str, permission: str
) -> None:
    # The grant string reaches app_grants, so a third level is refused at the boundary. (An
    # everyone grant would be refused by its own CAN_USE rule whatever the closed list says.)
    with pytest.raises(ValidationError):
        schemas.GrantRequest.model_validate(
            grant(type=principal_type, name=name, permission=permission)
        )
    for level in ("CAN_USE", "CAN_MANAGE"):
        accepted(schemas.GrantRequest, grant(type=principal_type, name=name, permission=level))


def test_the_owner_is_not_named_with_a_lower_level() -> None:
    lowered = schemas.GrantRequest.model_validate(
        grant(type="user", name=OWNER, permission="CAN_USE")
    )
    with pytest.raises(schemas.OwnerGrantRefused, match="cannot be named with a lower level"):
        schemas.refuse_lowering_owner(lowered, owner=OWNER)
    # Other people, groups and the owner at CAN_MANAGE are not refused.
    other = schemas.GrantRequest.model_validate(
        grant(type="user", name="carol@example.com", permission="CAN_USE")
    )
    schemas.refuse_lowering_owner(other, owner=OWNER)
    same_name_group = schemas.GrantRequest.model_validate(grant(name=OWNER))
    schemas.refuse_lowering_owner(same_name_group, owner=OWNER)
    at_manage = schemas.GrantRequest.model_validate(
        grant(type="user", name=OWNER, permission="CAN_MANAGE")
    )
    schemas.refuse_lowering_owner(at_manage, owner=OWNER)


def test_the_owner_is_matched_whatever_the_case_of_the_owner_argument() -> None:
    # A grant name is a lower-case email already; the owner a caller passes may not be, and
    # comparing the two exactly would let the owner be lowered (it fails open on a safety path).
    lowered = schemas.GrantRequest.model_validate(
        grant(type="user", name=OWNER, permission="CAN_USE")
    )
    with pytest.raises(schemas.OwnerGrantRefused, match="cannot be named with a lower level"):
        schemas.refuse_lowering_owner(lowered, owner="Alice@Example.com")
    with pytest.raises(schemas.OwnerGrantRefused, match="cannot be removed"):
        schemas.refuse_removing_owner("user", OWNER, owner="Alice@Example.com")


def test_the_owner_cannot_be_removed_from_the_grants() -> None:
    with pytest.raises(schemas.OwnerGrantRefused, match="cannot be removed"):
        schemas.refuse_removing_owner("user", OWNER, owner=OWNER)
    with pytest.raises(schemas.OwnerGrantRefused):
        schemas.refuse_removing_owner("user", OWNER.upper(), owner=OWNER)
    schemas.refuse_removing_owner("user", "carol@example.com", owner=OWNER)
    schemas.refuse_removing_owner("group", OWNER, owner=OWNER)


# --- egress ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "target"),
    [
        ({"cidr": "@container_ip/32", "port": "any"}, "@container_ip"),
        ({"cidr": "@container_ip", "port": "any"}, "@container_ip"),
        ({"cidr": "169.254.0.0/16", "port": "any"}, "169.254.0.0/16"),
        ({"cidr": "169.254.169.254/32", "port": "80"}, "169.254.0.0/16"),
        ({"cidr": "169.0.0.0/8", "port": "any"}, "169.254.0.0/16"),
        ({"cidr": "0.0.0.0/0", "port": "any"}, "169.254.0.0/16"),
        ({"cidr": "10.20.0.0/16", "port": "@appmux_port"}, "@appmux_port"),
    ],
)
@pytest.mark.parametrize(
    "model_and_extra",
    [(schemas.PutEgressRequest, {}), (schemas.PutAppEgressRequest, {"version": 3})],
    ids=["global", "app"],
)
def test_an_allow_that_overlaps_a_closed_target_is_refused(
    model_and_extra: tuple[type[BaseModel], dict[str, int]],
    overrides: dict[str, str],
    target: str,
) -> None:
    model, extra = model_and_extra
    refused(
        model,
        extra | {"rules": [rule(), rule(action="allow", **overrides)]},
        "rule 2 is an allow",
    )
    refused(model, extra | {"rules": [rule(action="allow", **overrides)]}, target)


def test_a_deny_may_name_the_closed_targets() -> None:
    accepted(
        schemas.PutEgressRequest,
        {
            "rules": [
                rule(cidr="@container_ip/32", port="any"),
                rule(cidr="169.254.0.0/16", port="any"),
                rule(cidr="0.0.0.0/0", port="@appmux_port"),
            ]
        },
    )


def test_an_allow_clear_of_the_closed_targets_is_accepted() -> None:
    for cidr in ["10.20.0.0/16", "169.253.255.255/32", "169.255.0.0/16"]:
        accepted(schemas.PutEgressRequest, {"rules": [rule(action="allow", cidr=cidr)]})
    accepted(schemas.PutEgressRequest, {"rules": []})


def test_every_overlapping_allow_of_the_list_is_named() -> None:
    with pytest.raises(ValidationError) as caught:
        schemas.PutEgressRequest.model_validate(
            {
                "rules": [
                    rule(action="allow", cidr="@container_ip/32"),
                    rule(),
                    rule(action="allow", cidr="169.254.0.0/16"),
                ]
            }
        )
    text = str(caught.value)
    assert "rule 1" in text
    assert "rule 3" in text
    assert "rule 2" not in text


@pytest.mark.parametrize(
    "overrides",
    [
        {"cidr": "10.20.0.1/16"},  # host bits set
        {"cidr": "10.20.0.0"},  # no prefix length
        {"cidr": "2001:db8::/32"},  # no IPv6 in v1
        {"cidr": "example.com"},
        {"cidr": "@container_ip/33"},
        {"cidr": "@appmux_port"},
        {"port": "0"},
        {"port": "65536"},
        {"port": "80-90"},
        {"port": "ANY"},
        {"action": "drop"},
    ],
)
def test_an_egress_rule_is_a_valid_cidr_port_and_action(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        schemas.PutEgressRequest.model_validate({"rules": [rule(**overrides)]})


def test_an_egress_rule_needs_all_four_fields() -> None:
    incomplete = rule()
    del incomplete["comment"]
    with pytest.raises(ValidationError):
        schemas.PutEgressRequest.model_validate({"rules": [incomplete]})


# --- attaching a resource -------------------------------------------------------------------

WAREHOUSE = {"type": "sql_warehouse", "target": "a1b2", "permission": "CAN_USE", "access_as": "obo"}
SECRET = {"type": "secret", "target": "sales-secrets/openai-api-key", "permission": "READ"}


def test_attach_needs_access_as_for_every_type_but_secret() -> None:
    accepted(schemas.AttachResourceRequest, WAREHOUSE)
    without = {k: v for k, v in WAREHOUSE.items() if k != "access_as"}
    refused(
        schemas.AttachResourceRequest,
        without,
        "access_as is required for every type but secret",
    )
    refused(
        schemas.AttachResourceRequest,
        without | {"access_as": None},
        "access_as is required for every type but secret",
    )


def test_attach_access_as_is_one_of_three() -> None:
    with pytest.raises(ValidationError):
        schemas.AttachResourceRequest.model_validate(WAREHOUSE | {"access_as": "me"})


def test_a_secret_has_no_access_as() -> None:
    accepted(schemas.AttachResourceRequest, SECRET)
    accepted(schemas.AttachResourceRequest, SECRET | {"access_as": None})
    refused(
        schemas.AttachResourceRequest, SECRET | {"access_as": "obo"}, "a secret has no access_as"
    )


def test_a_secret_takes_the_level_read() -> None:
    refused(
        schemas.AttachResourceRequest,
        SECRET | {"permission": "WRITE"},
        "a secret takes the level READ",
    )


@pytest.mark.parametrize("target", ["no-slash", "/key", "scope/", "a/b/c"])
def test_a_secret_target_is_a_scope_key_reference(target: str) -> None:
    refused(
        schemas.AttachResourceRequest,
        SECRET | {"target": target},
        "a secret target is a scope/key reference",
    )


def test_from_bundle_defaults_to_false_and_null_is_false() -> None:
    assert schemas.AttachResourceRequest.model_validate(WAREHOUSE).from_bundle is False
    assert (
        schemas.AttachResourceRequest.model_validate(WAREHOUSE | {"from_bundle": None}).from_bundle
        is False
    )
    assert (
        schemas.AttachResourceRequest.model_validate(WAREHOUSE | {"from_bundle": True}).from_bundle
        is True
    )


@pytest.mark.parametrize("field", ["type", "target", "permission"])
def test_a_blank_type_target_or_permission_is_refused(field: str) -> None:
    refused(schemas.AttachResourceRequest, WAREHOUSE | {field: " "}, "must not be blank")


# --- the config: the own service principal's key pair ----------------------------------------


def config(**overrides: Any) -> dict[str, Any]:
    return {
        "version": 12,
        "env": [{"name": "LOG_LEVEL", "value": "info", "value_from": None}],
        "wheel_sources": [],
        "health_path": "/",
        "health_deadline_s": 60,
        "own_sp_client_id_key": None,
        "own_sp_client_secret_key": None,
    } | overrides


def own_sp(id_key: str | None, second_key: str | None) -> dict[str, str | None]:
    return {"own_sp_client_id_key": id_key, "own_sp_client_secret_key": second_key}


def test_the_own_sp_keys_come_as_a_pair() -> None:
    accepted(schemas.UpdateConfigRequest, config())
    accepted(
        schemas.UpdateConfigRequest,
        config(**own_sp("sp-id", "sp-key")),
    )
    pair_message = "set both own_sp_*_key fields, or neither"
    refused(schemas.UpdateConfigRequest, config(**own_sp("sp-id", None)), pair_message)
    refused(schemas.UpdateConfigRequest, config(**own_sp(None, "sp-key")), pair_message)


@pytest.mark.parametrize("key", ["SP ID", "a/b", "a" * 64, ""])
def test_an_own_sp_key_is_a_resource_key(key: str) -> None:
    with pytest.raises(ValidationError):
        schemas.UpdateConfigRequest.model_validate(config(**own_sp(key, "sp-key")))


@pytest.mark.parametrize("deadline", [4, 601])
def test_the_health_deadline_is_five_to_six_hundred(deadline: int) -> None:
    with pytest.raises(ValidationError):
        schemas.UpdateConfigRequest.model_validate(config(health_deadline_s=deadline))
    accepted(schemas.UpdateConfigRequest, config(health_deadline_s=5))
    accepted(schemas.UpdateConfigRequest, config(health_deadline_s=600))


@pytest.mark.parametrize("path", ["healthz", "http://x/", " /x", ""])
def test_the_health_path_starts_with_a_slash(path: str) -> None:
    refused(schemas.UpdateConfigRequest, config(health_path=path), "must start with /")


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "A"},
        {"name": "A", "value": "1", "value_from": "key"},
        {"name": "A", "value": None, "value_from": None},
    ],
    ids=["neither", "both", "both null"],
)
def test_an_env_entry_has_exactly_one_of_value_and_value_from(entry: dict[str, Any]) -> None:
    refused(
        schemas.UpdateConfigRequest,
        config(env=[entry]),
        "exactly one of value and value_from must be set",
    )


@pytest.mark.parametrize(
    "key",
    ["No Key", "OPENAI_API_KEY", "_hidden", ".dot", pytest.param("a" * 64, id="64 characters"), ""],
)
def test_an_env_value_from_a_client_sends_is_a_resource_key(key: str) -> None:
    with pytest.raises(ValidationError):
        schemas.UpdateConfigRequest.model_validate(config(env=[{"name": "A", "value_from": key}]))
    accepted(
        schemas.UpdateConfigRequest,
        config(env=[{"name": "A", "value_from": "openai-api-key"}]),
    )


@pytest.mark.parametrize(
    "key",
    [
        "OPENAI_API_KEY",
        "_hidden",
        "my key",
        "sql_warehouse",
        pytest.param("a" * 255, id="255 characters"),
    ],
)
def test_an_env_value_from_a_response_returns_is_any_key_the_apps_manifest_names(
    key: str,
) -> None:
    # `detected.env` is read from the child's app.yaml, which appmux must not change (constitution
    # I), so a response takes whatever key it names; only a request is held to the resource form.
    entry = schemas.EnvEntry.model_validate({"name": "A", "value_from": key})
    assert entry.value_from == key


@pytest.mark.parametrize("key", ["", pytest.param("a" * 256, id="256 characters")])
def test_a_response_env_value_from_is_not_empty_and_has_a_cap(key: str) -> None:
    with pytest.raises(ValidationError):
        schemas.EnvEntry.model_validate({"name": "A", "value_from": key})


def test_a_release_whose_app_yaml_names_a_free_form_key_can_be_described() -> None:
    # The page that explains a failed release must be buildable for any app.yaml.
    detail = example("ReleaseDetail")
    detail["detected"]["env"][1]["value_from"] = "OPENAI_API_KEY"
    detail["config"]["env"] = [{"name": "TOKEN", "value": None, "value_from": "_Hidden Key"}]
    parsed = schemas.ReleaseDetail.model_validate_json(json.dumps(detail))
    assert parsed.detected.env[1].value_from == "OPENAI_API_KEY"
    assert parsed.config.env[0].value_from == "_Hidden Key"


@pytest.mark.parametrize("model_name", ["EnvEntry", "EnvEntryRequest"])
def test_an_env_entry_names_its_variable_and_has_exactly_one_source(model_name: str) -> None:
    model = getattr(schemas, model_name)
    accepted(model, {"name": "A", "value": "1"})
    accepted(model, {"name": "A", "value_from": "key"})
    for broken in (
        {"name": "", "value": "1"},
        {"name": "A"},
        {"name": "A", "value": "1", "value_from": "key"},
        {"name": "A", "value": None, "value_from": None},
    ):
        with pytest.raises(ValidationError):
            model.model_validate(broken)


# --- platform admins ------------------------------------------------------------------------

DANA = {"type": "user", "name": "dana@example.com"}
GROUP = {"type": "group", "name": "data-platform"}


def admins(*entries: dict[str, str], reason: str = "why") -> dict[str, Any]:
    return {"admins": list(entries), "reason": reason}


def test_the_admins_list_keeps_at_least_one_user() -> None:
    accepted(schemas.PutAdminsRequest, admins(DANA))
    accepted(schemas.PutAdminsRequest, admins(GROUP, DANA))
    refused(schemas.PutAdminsRequest, admins(), "at least one user must remain")
    refused(schemas.PutAdminsRequest, admins(GROUP), "at least one user must remain")


def test_the_admins_list_names_users_and_groups_only() -> None:
    with pytest.raises(ValidationError):
        schemas.PutAdminsRequest.model_validate(admins(DANA, {"type": "everyone", "name": "*"}))


@pytest.mark.parametrize("name", ["Dana@example.com", "dana", "dana@example.com;eve@example.com"])
def test_an_admin_user_is_a_lower_case_email(name: str) -> None:
    refused(
        schemas.PutAdminsRequest,
        admins({"type": "user", "name": name}),
        "must be an email address in lower case",
    )


@pytest.mark.parametrize("name", ["", "   "])
def test_an_admin_entry_is_not_empty(name: str) -> None:
    with pytest.raises(ValidationError):
        schemas.PutAdminsRequest.model_validate(admins(DANA, {"type": "group", "name": name}))


def test_the_admins_list_has_no_duplicate_after_lower_casing() -> None:
    refused(schemas.PutAdminsRequest, admins(DANA, DANA), "is listed twice")
    refused(
        schemas.PutAdminsRequest,
        admins(DANA, GROUP, {"type": "group", "name": "Data-Platform"}),
        "is listed twice",
    )
    # The same name as a user and as a group are two different principals.
    accepted(schemas.PutAdminsRequest, admins(DANA, {"type": "group", "name": "dana@example.com"}))


def test_the_admins_reason_is_not_blank() -> None:
    refused(schemas.PutAdminsRequest, admins(DANA, reason=" "), "must not be blank")


# --- the rest of the request limits ---------------------------------------------------------

REGISTER = {
    "slug": "sales",
    "display_name": "Sales dashboard",
    "source_path": "/Workspace/Shared/apps/sales",
    "memory_budget_mb": 512,
    "owner": OWNER,
}


@pytest.mark.parametrize("slug", ["a", "0", "a-b", "a" * 31])
def test_a_slug_is_accepted(slug: str) -> None:
    accepted(schemas.RegisterAppRequest, REGISTER | {"slug": slug})


@pytest.mark.parametrize("slug", ["", "-a", "A", "a_b", "a b", "a" * 32, "_platform"])
def test_a_slug_is_refused(slug: str) -> None:
    with pytest.raises(ValidationError):
        schemas.RegisterAppRequest.model_validate(REGISTER | {"slug": slug})


@pytest.mark.parametrize("budget", [63, 16385, 64.5, "512", True])
def test_a_memory_budget_is_an_integer_from_64_to_16384(budget: object) -> None:
    with pytest.raises(ValidationError):
        schemas.RegisterAppRequest.model_validate(REGISTER | {"memory_budget_mb": budget})
    for good in (64, 16384):
        accepted(schemas.RegisterAppRequest, REGISTER | {"memory_budget_mb": good})


@pytest.mark.parametrize("path", ["/home/a", "/workspace/a", "Workspace/a", ""])
def test_a_source_path_starts_with_workspace(path: str) -> None:
    refused(
        schemas.RegisterAppRequest, REGISTER | {"source_path": path}, "must start with /Workspace/"
    )


def test_a_display_name_is_not_blank_and_an_owner_is_a_lower_case_email() -> None:
    for blank in ("", " ", "\t"):
        refused(schemas.RegisterAppRequest, REGISTER | {"display_name": blank}, "must not be blank")
    for owner in ("Alice@example.com", "alice", ""):
        refused(schemas.RegisterAppRequest, REGISTER | {"owner": owner}, "lower case")


def test_an_app_update_needs_a_field_besides_version() -> None:
    message = "at least one field besides version must be present"
    refused(schemas.UpdateAppRequest, {"version": 1}, message)
    refused(schemas.UpdateAppRequest, {"version": 1, "slug": None}, message)
    accepted(schemas.UpdateAppRequest, {"version": 1, "slug": "a" * 31})
    with pytest.raises(ValidationError):
        schemas.UpdateAppRequest.model_validate({"slug": "a"})  # no version
    with pytest.raises(ValidationError):
        schemas.UpdateAppRequest.model_validate({"version": 1, "memory_budget_mb": 63})
    refused(schemas.UpdateAppRequest, {"version": 1, "source_path": "/home"}, "/Workspace/")


def test_a_new_owner_is_a_lower_case_email_sent_with_the_version() -> None:
    accepted(schemas.SetOwnerRequest, {"version": 3, "owner": "carol@example.com"})
    refused(schemas.SetOwnerRequest, {"version": 3, "owner": "Carol@example.com"}, "lower case")
    with pytest.raises(ValidationError):
        schemas.SetOwnerRequest.model_validate({"owner": "carol@example.com"})


def test_a_release_name_is_null_or_one_to_eighty_characters() -> None:
    for good in (None, "a", "a" * 80):
        accepted(schemas.UpdateReleaseRequest, {"name": good, "tags": []})
    for bad in ("", "a" * 81):
        with pytest.raises(ValidationError):
            schemas.UpdateReleaseRequest.model_validate({"name": bad, "tags": []})
    with pytest.raises(ValidationError):
        schemas.UpdateReleaseRequest.model_validate({"tags": []})  # name is required


def test_release_tags_are_at_most_ten_lower_case_and_thirty_two_characters() -> None:
    accepted(schemas.UpdateReleaseRequest, {"name": None, "tags": [f"t{n}" for n in range(10)]})
    accepted(schemas.UpdateReleaseRequest, {"name": None, "tags": ["a" * 32]})
    with pytest.raises(ValidationError):
        schemas.UpdateReleaseRequest.model_validate(
            {"name": None, "tags": [f"t{n}" for n in range(11)]}
        )
    with pytest.raises(ValidationError):
        schemas.UpdateReleaseRequest.model_validate({"name": None, "tags": ["a" * 33]})
    refused(schemas.UpdateReleaseRequest, {"name": None, "tags": ["Prod"]}, "must be lower case")


def test_a_delegation_reason_is_not_blank_at_most_500_characters_and_has_a_version() -> None:
    accepted(schemas.DelegationReasonRequest, {"version": 3, "reason": "r"})
    accepted(schemas.DelegationReasonRequest, {"version": 3, "reason": "r" * 500})
    with pytest.raises(ValidationError):
        schemas.DelegationReasonRequest.model_validate({"version": 3, "reason": "r" * 501})
    refused(schemas.DelegationReasonRequest, {"version": 3, "reason": " "}, "must not be blank")
    with pytest.raises(ValidationError):
        schemas.DelegationReasonRequest.model_validate({"reason": "r"})


def test_the_delegation_revoke_reads_its_fields_from_the_query_not_a_body() -> None:
    query = schemas.DelegationReasonRequest.from_query(
        {"version": "3", "reason": "the app no longer needs it"}
    )
    assert query.version == 3
    assert query.reason == "the app no longer needs it"
    for bad in (
        {"reason": "r"},  # version required
        {"version": "3"},  # reason required
        {"version": "3", "reason": " "},
        {"version": "three", "reason": "r"},
        {"version": "3", "reason": "r" * 501},
        {"version": "3", "reason": "r", "is_admin": "1"},
    ):
        with pytest.raises(ValidationError):
            schemas.DelegationReasonRequest.from_query(bad)
    # The body form stays strict: a version in text is not a version.
    with pytest.raises(ValidationError):
        schemas.DelegationReasonRequest.model_validate({"version": "3", "reason": "r"})


def test_a_permission_request_reason_is_not_blank_and_at_most_500_characters() -> None:
    body = {
        "kind": "delegation",
        "app": "sales",
        "payload": {"obo": True},
        "reason": "r" * 500,
    }
    accepted(schemas.CreatePermissionRequest, body)
    for bad in ("", " ", "r" * 501):
        with pytest.raises(ValidationError):
            schemas.CreatePermissionRequest.model_validate(body | {"reason": bad})


REGISTER_PAYLOAD = {
    "slug": "wiki",
    "display_name": "Team wiki",
    "source_path": "/Workspace/Shared/apps/wiki",
    "memory_budget_mb": 128,
}


def permission_request(kind: str, app: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "app": app, "payload": payload, "reason": "because"}


def test_a_register_request_holds_all_four_fields_and_names_its_own_slug() -> None:
    accepted(
        schemas.CreatePermissionRequest, permission_request("register", "wiki", REGISTER_PAYLOAD)
    )
    for key in REGISTER_PAYLOAD:
        partial = {k: v for k, v in REGISTER_PAYLOAD.items() if k != key}
        refused(
            schemas.CreatePermissionRequest,
            permission_request("register", "wiki", partial),
            f"payload.{key}",
        )
    refused(
        schemas.CreatePermissionRequest,
        permission_request("register", "other", REGISTER_PAYLOAD),
        "app must equal payload.slug",
    )
    refused(
        schemas.CreatePermissionRequest,
        permission_request("register", "wiki", REGISTER_PAYLOAD | {"memory_budget_mb": 63}),
        "payload.memory_budget_mb",
    )


def test_a_delegation_request_asks_only_for_true() -> None:
    for good in ({"obo": True}, {"shared_sp": True}, {"obo": True, "shared_sp": True}):
        accepted(schemas.CreatePermissionRequest, permission_request("delegation", "sales", good))
    refused(
        schemas.CreatePermissionRequest,
        permission_request("delegation", "sales", {}),
        "ask for obo, shared_sp or both",
    )
    for bad in ({"obo": False}, {"obo": 1}, {"obo": "true"}, {"obo": True, "other": True}):
        with pytest.raises(ValidationError):
            schemas.CreatePermissionRequest.model_validate(
                permission_request("delegation", "sales", bad)
            )
    refused(
        schemas.CreatePermissionRequest,
        permission_request("delegation", "sales", {"shared_sp": False}),
        "only true can be asked for",
    )


def test_a_settings_request_holds_a_non_empty_subset_of_the_platform_fields() -> None:
    accepted(
        schemas.CreatePermissionRequest,
        permission_request("settings", "sales", {"slug": "revenue"}),
    )
    refused(
        schemas.CreatePermissionRequest,
        permission_request("settings", "sales", {}),
        "ask for at least one field",
    )
    refused(
        schemas.CreatePermissionRequest,
        permission_request("settings", "sales", {"owner": "x@example.com"}),
        "payload.owner",
    )
    refused(
        schemas.CreatePermissionRequest,
        permission_request("settings", "sales", {"source_path": "/home"}),
        "/Workspace/",
    )


def test_a_payload_error_never_repeats_the_value() -> None:
    with pytest.raises(ValidationError) as caught:
        schemas.CreatePermissionRequest.model_validate(
            permission_request("settings", "sales", {"source_path": "/home/very-private-folder"})
        )
    assert "very-private-folder" not in str(caught.value).split("input_value")[0]


def test_a_request_kind_is_one_of_three() -> None:
    with pytest.raises(ValidationError):
        schemas.CreatePermissionRequest.model_validate(
            permission_request("grant", "sales", {"obo": True})
        )


def test_a_setting_request_carries_a_value_and_nothing_else() -> None:
    for value in (10, "wheelhouse", True, None, ["/Workspace/wheels"], {"a": 1}):
        accepted(schemas.PutSettingRequest, {"value": value})
    with pytest.raises(ValidationError):
        schemas.PutSettingRequest.model_validate({})
    with pytest.raises(ValidationError):
        schemas.PutSettingRequest.model_validate({"value": 1, "key": "x"})


def test_an_open_mode_is_one_of_two() -> None:
    with pytest.raises(ValidationError):
        schemas.OpenModeRequest.model_validate({"mode": "root"})


# --- the invariants a response states --------------------------------------------------------


def example(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def test_locked_at_is_null_exactly_when_locked_by_is() -> None:
    summary = example("ReleaseSummary")
    schemas.ReleaseSummary.model_validate_json(json.dumps(summary))
    for broken in (summary | {"locked_at": None}, summary | {"locked_by": None}):
        refused_json(schemas.ReleaseSummary, broken, "locked_at is null exactly when locked_by is")


def test_a_request_row_is_closed_exactly_when_it_is_not_pending() -> None:
    row = example("RequestRow")
    assert row["status"] == "pending"
    schemas.RequestRow.model_validate_json(json.dumps(row))
    closed = row | {"status": "approved"}
    refused_json(schemas.RequestRow, closed, "closed_at is null exactly while")
    refused_json(
        schemas.RequestRow,
        row | {"closed_at": "2026-10-30T10:00:00Z"},
        "closed_at is null exactly while",
    )
    refused_json(
        schemas.RequestRow,
        row | {"status": "denied", "closed_at": "2026-10-30T10:00:00Z"},
        "closed_by is null exactly while",
    )


def test_a_resource_has_no_access_as_exactly_when_it_is_a_secret() -> None:
    resource = example("ResourceView")
    schemas.ResourceView.model_validate_json(json.dumps(resource))
    refused_json(
        schemas.ResourceView,
        resource | {"access_as": None},
        "access_as is null exactly for a secret",
    )
    secret = resource | {"type": "secret", "permission": "READ"}
    refused_json(schemas.ResourceView, secret, "access_as is null exactly for a secret")
    schemas.ResourceView.model_validate_json(json.dumps(secret | {"access_as": None}))


def test_one_failed_check_fails_the_self_check() -> None:
    result = example("PlatformView")["self_check"]
    schemas.SelfCheckResult.model_validate_json(json.dumps(result))
    failing = copy.deepcopy(result)
    failing["checks"][3] = failing["checks"][3] | {"passed": False, "detail": "reachable"}
    refused_json(schemas.SelfCheckResult, failing, "passed is true only when every check passed")
    schemas.SelfCheckResult.model_validate_json(json.dumps(failing | {"passed": False}))


def test_the_latency_histogram_has_eleven_counts() -> None:
    row = example("MetricsRow")
    for length in (10, 12):
        refused_json(schemas.MetricsRow, row | {"lat_hist": [0] * length}, "lat_hist")


def test_a_time_leaves_as_utc_with_whole_seconds_and_a_z() -> None:
    result = schemas.ConfigChangeResult(config_version=1, release_no=None)
    assert result.model_dump(mode="json") == {"config_version": 1, "release_no": None}
    grant_row = schemas.AppGrant(
        type="user",
        name="carol@example.com",
        permission="CAN_MANAGE",
        granted_by=OWNER,
        granted_at=datetime(2026, 10, 30, 8, 53, 7, 999999, tzinfo=UTC),
    )
    assert grant_row.model_dump(mode="json")["granted_at"] == "2026-10-30T08:53:07Z"
    with pytest.raises(ValidationError):
        schemas.AppGrant(
            type="user",
            name="carol@example.com",
            permission="CAN_MANAGE",
            granted_by=OWNER,
            granted_at=datetime(2026, 10, 30, 8, 53),  # no time zone
        )


def test_the_audit_window_is_called_from_on_the_wire() -> None:
    window = example("AuditList")
    parsed = schemas.AuditList.model_validate_json(json.dumps(window))
    assert parsed.from_ == datetime(2026, 10, 30, tzinfo=UTC)
    assert parsed.model_dump(mode="json")["from"] == "2026-10-30T00:00:00Z"
    assert "from_" not in parsed.model_dump(mode="json")


# --- a NUL character -------------------------------------------------------------------------

NUL_MESSAGE = "must not contain a NUL character"


def test_a_nul_character_in_free_text_is_refused_where_it_would_fail_at_the_database() -> None:
    # Postgres text and jsonb cannot hold NUL, so the write would fail there; refuse it at the door.
    register = example("RegisterAppRequest")
    for field in ("display_name", "source_path"):
        refused_json(schemas.RegisterAppRequest, register | {field: "a\x00b"}, NUL_MESSAGE)
    refused_json(
        schemas.CreatePermissionRequest,
        example("CreatePermissionRequest") | {"reason": "why\x00"},
        NUL_MESSAGE,
    )
    config_body = example("UpdateConfigRequest")
    config_body["env"][0]["value"] = "x\x00"
    refused_json(schemas.UpdateConfigRequest, config_body, NUL_MESSAGE)
    refused_json(schemas.GrantRequest, grant(name="sales\x00team"), NUL_MESSAGE)
    refused_json(schemas.PutAdminsRequest, admins(DANA, {**GROUP, "name": "g\x00"}), NUL_MESSAGE)


def test_a_nul_character_in_free_form_json_is_refused_in_a_value_and_in_a_key() -> None:
    for value in ("x\x00", ["ok", {"deep": ["x\x00"]}], {"k\x00": 1}, {"outer": {"k\x00": 1}}):
        refused_json(schemas.PutSettingRequest, {"value": value}, NUL_MESSAGE)
    refused_json(
        schemas.CreatePermissionRequest,
        example("CreatePermissionRequest") | {"payload": {"slug\x00": "x"}},
        NUL_MESSAGE,
    )


def test_a_nul_character_in_the_revoke_query_is_refused() -> None:
    # DELETE reads its fields from the percent-decoded query string, where %00 arrives as NUL.
    schemas.DelegationReasonRequest.from_query({"version": "3", "reason": "left the team"})
    with pytest.raises(ValidationError) as caught:
        schemas.DelegationReasonRequest.from_query({"version": "3", "reason": "left\x00"})
    assert NUL_MESSAGE in str(caught.value)


def test_a_nul_refusal_names_the_field_and_never_repeats_the_text() -> None:
    with pytest.raises(ValidationError) as caught:
        schemas.RegisterAppRequest.model_validate(
            example("RegisterAppRequest") | {"display_name": "visible-text\x00"}
        )
    (error,) = caught.value.errors(include_input=False, include_url=False)
    assert error["loc"] == ("display_name",)
    assert "visible-text" not in error["msg"]


# --- limits the registry's row models hold too ------------------------------------------------
# The schema repeats these limits (the registry models keep them inline and export nothing), so
# a test holds the two together: a boundary the request accepts must be one the row takes, and
# the other way round, or a valid request would fail at the registry (or a bad one pass).

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


def registry_app(**overrides: Any) -> registry_models.App:
    return registry_models.App.model_validate(
        {
            "slug": "sales",
            "display_name": "Sales",
            "owner": OWNER,
            "source_path": "/Workspace/Users/alice@example.com/sales",
            "memory_budget_mb": 512,
            "created_by": OWNER,
            "created_at": NOW,
            "updated_by": OWNER,
            "updated_at": NOW,
        }
        | overrides
    )


def registry_config(**overrides: Any) -> registry_models.AppConfig:
    return registry_models.AppConfig.model_validate(
        {"app_slug": "sales", "updated_by": OWNER, "updated_at": NOW} | overrides
    )


def takes(build: Any, *body: Any, **fields: Any) -> bool:
    """Whether ``build`` (a model's ``model_validate`` or a row builder) takes the input."""
    try:
        build(*body, **fields)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("budget", [63, 64, 16384, 16385])
def test_the_memory_budget_limits_are_the_registrys(budget: int) -> None:
    request = example("RegisterAppRequest") | {"memory_budget_mb": budget}
    assert takes(schemas.RegisterAppRequest.model_validate, request) == takes(
        registry_app, memory_budget_mb=budget
    )


@pytest.mark.parametrize(
    "path", ["/Workspace/a", "/Workspace/", "/Workspace", "/workspace/a", "Workspace/a", "/Users/a"]
)
def test_the_source_path_prefix_is_the_registrys(path: str) -> None:
    request = example("RegisterAppRequest") | {"source_path": path}
    assert takes(schemas.RegisterAppRequest.model_validate, request) == takes(
        registry_app, source_path=path
    )


@pytest.mark.parametrize("deadline", [4, 5, 600, 601])
def test_the_health_deadline_limits_are_the_registrys(deadline: int) -> None:
    request = config(health_deadline_s=deadline)
    assert takes(schemas.UpdateConfigRequest.model_validate, request) == takes(
        registry_config, health_deadline_s=deadline
    )


@pytest.mark.parametrize("path", ["/", "/healthz", "healthz", "", "http://x/"])
def test_the_health_path_form_is_the_registrys(path: str) -> None:
    request = config(health_path=path)
    assert takes(schemas.UpdateConfigRequest.model_validate, request) == takes(
        registry_config, health_path=path
    )


@pytest.mark.parametrize(
    "target", ["scope/key", "no-slash", "/key", "scope/", "a/b/c", "a b/c", "a/"]
)
def test_the_secret_reference_form_is_the_registrys(target: str) -> None:
    request = WAREHOUSE | {"type": "secret", "permission": "READ", "access_as": None}
    row = {
        "app_slug": "sales",
        "key": "token",
        "type": "secret",
        "target": target,
        "permission": "READ",
        "access_as": None,
        "check_result": "unverified",
        "attached_by": OWNER,
        "attached_at": NOW,
    }
    schema_takes = takes(schemas.AttachResourceRequest.model_validate, request | {"target": target})
    row_takes = takes(registry_models.AppResource.model_validate, row)
    assert schema_takes == row_takes


def refused_json(model: type[BaseModel], body: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError) as caught:
        model.model_validate_json(json.dumps(body))
    assert message in str(caught.value)
