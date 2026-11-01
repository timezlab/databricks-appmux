"""Environment settings and the ``platform_settings`` schema (data model 4.1, rules 10 and 14).

Every value here is a placeholder: no real host, identifier or credential appears in this file.
"""

from __future__ import annotations

import re
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from appmux.sandbox.rules import EgressRule
from appmux.settings import (
    EnvironmentSettings,
    LevelOfEffect,
    PlatformSettings,
    SettingsError,
    level_of_effect,
)

# A stand-in that is obviously not a credential; tests look for it in messages and reprs.
CLIENT_VALUE_MARKER = "placeholder-client-value-0001"

VALID_ENVIRONMENT: Mapping[str, str] = {
    "DATABRICKS_HOST": "https://workspace.example.invalid",
    "DATABRICKS_CLIENT_ID": "client-id-placeholder",
    "DATABRICKS_CLIENT_SECRET": CLIENT_VALUE_MARKER,
    "DATABRICKS_APP_PORT": "8000",
    "DATABRICKS_WORKSPACE_ID": "1234567890",
    "DATABRICKS_APP_URL": "https://appmux.example.invalid",
    "PGHOST": "lakebase.example.invalid",
    "APPMUX_LAKEBASE_DATABASE": "appmux",
}

REQUIRED_VARIABLES = tuple(VALID_ENVIRONMENT)

OPTIONAL_VARIABLES = ("APPMUX_BOOTSTRAP_ADMINS", "APPMUX_UI_DIR")

# The keys of data model 4.1, in the order of that table.
PLATFORM_SETTING_KEYS = (
    "platform_admins",
    "memory_reserve_mb",
    "package_mode",
    "default_wheel_sources",
    "shared_sp_delegation_allowed",
    "max_releases_per_app",
    "max_locked_releases",
    "egress_global",
    "request_timeout_s",
    "max_request_body_mb",
    "max_concurrent_requests",
    "build_timeout_s",
    "max_processes_per_child",
    "child_cpu_pin",
    "attach_check_warehouse_id",
)


def environment_without(name: str) -> dict[str, str]:
    return {key: value for key, value in VALID_ENVIRONMENT.items() if key != name}


def load(overrides: Mapping[str, str] | None = None) -> EnvironmentSettings:
    return EnvironmentSettings.from_environment({**VALID_ENVIRONMENT, **(overrides or {})})


def error_locations(error: ValidationError) -> list[tuple[Any, ...]]:
    return [item["loc"] for item in error.errors()]


# --- EnvironmentSettings -------------------------------------------------------------------


def test_valid_environment_is_parsed_into_typed_fields() -> None:
    settings = load()

    assert settings.databricks_host == "https://workspace.example.invalid"
    assert settings.databricks_client_id == "client-id-placeholder"
    assert settings.databricks_client_secret.get_secret_value() == CLIENT_VALUE_MARKER
    assert settings.databricks_app_port == 8000
    assert settings.databricks_workspace_id == "1234567890"
    assert settings.databricks_app_url == "https://appmux.example.invalid"
    assert settings.pghost == "lakebase.example.invalid"
    assert settings.appmux_lakebase_database == "appmux"


@pytest.mark.parametrize("variable", REQUIRED_VARIABLES)
def test_missing_required_variable_stops_the_start_and_names_it(variable: str) -> None:
    with pytest.raises(SettingsError) as raised:
        EnvironmentSettings.from_environment(environment_without(variable))

    assert variable in str(raised.value)
    assert raised.value.variables == (variable,)


def test_every_missing_variable_is_named_in_one_message() -> None:
    with pytest.raises(SettingsError) as raised:
        EnvironmentSettings.from_environment({})

    assert set(raised.value.variables) == set(REQUIRED_VARIABLES)
    for variable in REQUIRED_VARIABLES:
        assert variable in str(raised.value)


@pytest.mark.parametrize("variable", REQUIRED_VARIABLES)
def test_a_required_variable_set_to_nothing_counts_as_unusable_and_is_named(
    variable: str,
) -> None:
    with pytest.raises(SettingsError) as raised:
        load({variable: ""})

    assert raised.value.variables == (variable,)


@pytest.mark.parametrize(
    "port",
    [
        "not-a-number",
        "0",
        "65536",
        "-1",
        "80.5",
        "8_000",
        "+80",
        "8000.0",
        " 80",
        "80 ",
        "\u0668\u0660",
    ],
)
def test_app_port_must_be_a_tcp_port(port: str) -> None:
    with pytest.raises(SettingsError) as raised:
        load({"DATABRICKS_APP_PORT": port})

    assert raised.value.variables == ("DATABRICKS_APP_PORT",)


@pytest.mark.parametrize(
    "variable", [name for name in REQUIRED_VARIABLES if name != "DATABRICKS_APP_PORT"]
)
@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n"])
def test_a_required_text_variable_of_white_space_is_unusable_and_is_named(
    variable: str, blank: str
) -> None:
    with pytest.raises(SettingsError) as raised:
        load({variable: blank})

    assert raised.value.variables == (variable,)


def test_a_secret_with_inner_or_outer_white_space_is_kept_as_given() -> None:
    padded = f" {CLIENT_VALUE_MARKER} "

    assert (
        load({"DATABRICKS_CLIENT_SECRET": padded}).databricks_client_secret.get_secret_value()
        == padded
    )


@pytest.mark.parametrize("port", ["1", "8000", "65535", "00080"])
def test_app_port_accepts_plain_decimal_digits(port: str) -> None:
    assert load({"DATABRICKS_APP_PORT": port}).databricks_app_port == int(port)


def test_client_secret_has_no_default() -> None:
    field = EnvironmentSettings.model_fields["databricks_client_secret"]

    assert field.is_required()
    assert field.alias == "DATABRICKS_CLIENT_SECRET"


def test_no_default_looks_like_a_secret() -> None:
    secret_words = re.compile(r"secret|token|password|passwd|credential|key", re.IGNORECASE)
    # A default that resembles an issued credential: a vendor prefix, a JWT, or a long opaque run.
    credential_shape = re.compile(r"^(sk-|dapi|ghp_|eyJ)|^[A-Za-z0-9+/_=-]{24,}$")

    for name, field in EnvironmentSettings.model_fields.items():
        if field.is_required():
            continue
        assert not secret_words.search(name), f"{name} holds a secret but has a default"
        default = field.get_default(call_default_factory=True)
        if isinstance(default, (str, Path)):
            assert not credential_shape.search(str(default)), f"default of {name} looks like a key"


def test_secret_value_stays_out_of_repr_and_error_messages() -> None:
    settings = load()
    assert CLIENT_VALUE_MARKER not in repr(settings)
    assert CLIENT_VALUE_MARKER not in str(settings)

    with pytest.raises(SettingsError) as raised:
        load({"DATABRICKS_APP_PORT": "not-a-number"})
    assert CLIENT_VALUE_MARKER not in str(raised.value)
    assert CLIENT_VALUE_MARKER not in repr(raised.value)


def test_the_chained_validation_error_does_not_carry_the_refused_value() -> None:
    # SettingsError is raised "from" the ValidationError, and a traceback prints the whole chain.
    refused = "zzz-not-an-email-marker"

    with pytest.raises(SettingsError) as raised:
        load({"APPMUX_BOOTSTRAP_ADMINS": refused, "DATABRICKS_APP_PORT": "port-marker"})

    cause = raised.value.__cause__
    assert isinstance(cause, ValidationError)
    assert refused not in str(cause)
    assert "port-marker" not in str(cause)
    assert refused not in "".join(traceback.format_exception(raised.value))


def test_bootstrap_admins_become_a_list_of_lower_case_emails() -> None:
    settings = load(
        {"APPMUX_BOOTSTRAP_ADMINS": " Dana@Example.com, eli@example.com ,\nfay@example.com"}
    )

    assert settings.appmux_bootstrap_admins == [
        "dana@example.com",
        "eli@example.com",
        "fay@example.com",
    ]


def test_one_bootstrap_admin_is_a_list_of_one() -> None:
    assert load({"APPMUX_BOOTSTRAP_ADMINS": "dana@example.com"}).appmux_bootstrap_admins == [
        "dana@example.com"
    ]


@pytest.mark.parametrize("value", [None, "", "  ,  "])
def test_bootstrap_admins_are_optional_and_default_to_none_listed(value: str | None) -> None:
    overrides = {} if value is None else {"APPMUX_BOOTSTRAP_ADMINS": value}

    assert load(overrides).appmux_bootstrap_admins == []


@pytest.mark.parametrize(
    "entry",
    [
        "a@b;c",
        "a;b@example.com",
        "a@exa;mple.com",
        "x@y",
        "a@b.c;d.e",
        "dana@",
        "@example.com",
        "a@@example.com",
        "a@example..com",
        "a@.com",
        "a@example.",
    ],
)
def test_a_bootstrap_admin_must_look_like_name_at_domain_with_a_dot(entry: str) -> None:
    with pytest.raises(SettingsError) as raised:
        load({"APPMUX_BOOTSTRAP_ADMINS": f"dana@example.com {entry}"})

    assert raised.value.variables == ("APPMUX_BOOTSTRAP_ADMINS",)
    assert entry not in str(raised.value)


def test_a_bootstrap_admin_is_an_email_with_a_dotted_domain() -> None:
    admins = load({"APPMUX_BOOTSTRAP_ADMINS": "dana.lee+ops@mail.example.com"})

    assert admins.appmux_bootstrap_admins == ["dana.lee+ops@mail.example.com"]


def test_a_bootstrap_admin_that_is_not_an_email_is_refused_by_name() -> None:
    with pytest.raises(SettingsError) as raised:
        load({"APPMUX_BOOTSTRAP_ADMINS": "dana@example.com, not-an-email"})

    assert raised.value.variables == ("APPMUX_BOOTSTRAP_ADMINS",)
    assert "not-an-email" not in str(raised.value)


def test_ui_dir_defaults_to_a_relative_path_and_can_be_set() -> None:
    assert load().ui_dir == Path("web/out")
    assert not load().ui_dir.is_absolute()
    assert load({"APPMUX_UI_DIR": "elsewhere/export"}).ui_dir == Path("elsewhere/export")


@pytest.mark.parametrize("blank", ["", " ", "\t\n"])
def test_a_blank_ui_dir_means_the_default_not_the_working_directory(blank: str) -> None:
    assert load({"APPMUX_UI_DIR": blank}).ui_dir == Path("web/out")


def test_unrelated_environment_variables_are_ignored() -> None:
    settings = load({"PATH": "/usr/bin", "HOME": "/home/app", "SOMETHING_ELSE": "x"})

    assert settings.databricks_app_port == 8000


def test_settings_are_immutable_once_loaded() -> None:
    settings = load()

    with pytest.raises(ValidationError):
        setattr(settings, "databricks_app_port", 9000)  # noqa: B010 - the name is the point


def test_from_environment_reads_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*REQUIRED_VARIABLES, *OPTIONAL_VARIABLES):
        monkeypatch.delenv(name, raising=False)
    for name, value in VALID_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("APPMUX_BOOTSTRAP_ADMINS", "dana@example.com")

    settings = EnvironmentSettings.from_environment()

    assert settings.databricks_app_port == 8000
    assert settings.appmux_bootstrap_admins == ["dana@example.com"]

    monkeypatch.delenv("PGHOST")
    with pytest.raises(SettingsError, match="PGHOST"):
        EnvironmentSettings.from_environment()


# --- PlatformSettings ----------------------------------------------------------------------


def test_the_schema_has_exactly_the_keys_of_the_data_model() -> None:
    assert tuple(PlatformSettings.model_fields) == PLATFORM_SETTING_KEYS


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # Defaults fixed by the plan.
        ("request_timeout_s", 120),
        ("max_request_body_mb", 100),
        ("max_concurrent_requests", 64),
        ("build_timeout_s", 600),
        ("max_processes_per_child", 256),
        ("child_cpu_pin", True),
        ("attach_check_warehouse_id", None),
        # Defaults stated in data model 4.1.
        ("shared_sp_delegation_allowed", False),
        ("max_releases_per_app", 10),
        ("max_locked_releases", 3),
        # No admin until the bootstrap admin sets them.
        ("platform_admins", None),
        # Chosen by the builder: the documents give a sample value or nothing. A change of one is
        # a decision of the owner, so a test has to be edited with it.
        ("memory_reserve_mb", 1024),
        ("package_mode", "wheelhouse"),
        ("default_wheel_sources", []),
    ],
)
def test_defaults(key: str, expected: object) -> None:
    assert getattr(PlatformSettings(), key) == expected


def test_default_egress_blocks_the_container_address_and_link_local() -> None:
    rules = PlatformSettings().egress_global

    assert [(rule.action, rule.cidr) for rule in rules] == [
        ("deny", "@container_ip/32"),
        ("deny", "169.254.0.0/16"),
    ]


def test_an_unknown_key_is_rejected() -> None:
    with pytest.raises(ValidationError) as raised:
        PlatformSettings.model_validate({"max_releases_per_app": 10, "not_a_setting": 1})

    assert error_locations(raised.value) == [("not_a_setting",)]


def test_a_stored_row_set_is_validated_as_one_mapping() -> None:
    settings = PlatformSettings.model_validate(
        {
            "package_mode": "wheelhouse+index",
            "default_wheel_sources": ["/Workspace/Shared/example/wheelhouse"],
            "request_timeout_s": 290,
        }
    )

    assert settings.package_mode == "wheelhouse+index"
    assert settings.default_wheel_sources == ["/Workspace/Shared/example/wheelhouse"]
    assert settings.request_timeout_s == 290


@pytest.mark.parametrize(
    ("locked", "releases", "accepted"),
    [
        (6, 10, True),
        (7, 10, False),
        (0, 4, True),
        (1, 4, False),
        (3, 7, True),
        (4, 7, False),
    ],
)
def test_locked_releases_stay_four_below_the_release_cap(
    locked: int, releases: int, accepted: bool
) -> None:
    values = {"max_locked_releases": locked, "max_releases_per_app": releases}

    if accepted:
        assert PlatformSettings.model_validate(values).max_locked_releases == locked
        return
    with pytest.raises(ValidationError, match="max_locked_releases"):
        PlatformSettings.model_validate(values)


def test_lowering_the_release_cap_alone_can_break_the_rule_with_the_default_lock_cap() -> None:
    with pytest.raises(ValidationError, match="max_releases_per_app"):
        PlatformSettings.model_validate({"max_releases_per_app": 6})


def test_the_release_cap_cannot_be_below_the_four_it_must_keep_free() -> None:
    with pytest.raises(ValidationError):
        PlatformSettings.model_validate({"max_releases_per_app": 3, "max_locked_releases": 0})


@pytest.mark.parametrize(
    ("key", "accepted", "rejected"),
    [
        ("request_timeout_s", 290, 291),
        ("request_timeout_s", 1, 0),
        ("max_request_body_mb", 1, 0),
        ("max_concurrent_requests", 1, 0),
        ("build_timeout_s", 1, 0),
        ("max_processes_per_child", 1, 0),
        ("memory_reserve_mb", 1, 0),
    ],
)
def test_numeric_bounds(key: str, accepted: int, rejected: int) -> None:
    assert getattr(PlatformSettings.model_validate({key: accepted}), key) == accepted
    with pytest.raises(ValidationError):
        PlatformSettings.model_validate({key: rejected})


@pytest.mark.parametrize("mode", ["index", "wheelhouse", "wheelhouse+index"])
def test_package_mode_accepts_the_three_modes(mode: str) -> None:
    assert PlatformSettings.model_validate({"package_mode": mode}).package_mode == mode


def test_package_mode_rejects_anything_else() -> None:
    with pytest.raises(ValidationError):
        PlatformSettings.model_validate({"package_mode": "pip"})


def test_values_are_not_coerced_from_look_alike_types() -> None:
    for values in (
        {"child_cpu_pin": "false"},
        {"request_timeout_s": "120"},
        {"shared_sp_delegation_allowed": 0},
    ):
        with pytest.raises(ValidationError):
            PlatformSettings.model_validate(values)


def test_platform_admins_accept_users_and_groups_and_fold_user_emails() -> None:
    settings = PlatformSettings.model_validate(
        {
            "platform_admins": [
                {"type": "user", "name": "Dana@Example.com"},
                {"type": "group", "name": "data-platform"},
            ]
        }
    )

    assert settings.platform_admins is not None
    assert [(admin.type, admin.name) for admin in settings.platform_admins] == [
        ("user", "dana@example.com"),
        ("group", "data-platform"),
    ]


@pytest.mark.parametrize(
    "admins",
    [
        [],
        [{"type": "group", "name": "data-platform"}],
    ],
)
def test_platform_admins_keep_at_least_one_user(admins: list[dict[str, str]]) -> None:
    with pytest.raises(ValidationError, match="user"):
        PlatformSettings.model_validate({"platform_admins": admins})


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "role", "name": "dana@example.com"},
        {"type": "user", "name": ""},
        {"type": "user", "name": "not-an-email"},
        {"type": "user", "name": "dana@example.com", "extra": "x"},
        {"type": "group"},
    ],
)
def test_a_malformed_admin_entry_is_rejected(entry: dict[str, str]) -> None:
    valid = {"type": "user", "name": "dana@example.com"}

    with pytest.raises(ValidationError):
        PlatformSettings.model_validate({"platform_admins": [valid, entry]})


def test_egress_rules_need_an_allow_or_deny_action_and_no_unknown_fields() -> None:
    good = {"action": "allow", "cidr": "10.20.0.0/16", "port": "443", "comment": "warehouse"}
    assert PlatformSettings.model_validate({"egress_global": [good]}).egress_global[0].port == "443"

    for bad in ({**good, "action": "permit"}, {**good, "proto": "tcp"}):
        with pytest.raises(ValidationError):
            PlatformSettings.model_validate({"egress_global": [bad]})


def test_the_stored_egress_rule_is_the_one_of_the_rule_engine() -> None:
    assert PlatformSettings.model_fields["egress_global"].annotation == list[EgressRule]


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "allow", "cidr": "10.20.0.0/16", "port": "443"},
        {"action": "allow", "port": "443", "comment": ""},
        {"action": "allow", "cidr": "10.20.0.0/16", "port": "8000-8100", "comment": ""},
        {"action": "allow", "cidr": "not-a-cidr", "port": "any", "comment": ""},
        {
            "action": "allow",
            "cidr": "10.20.0.0/16",
            "port": "any",
            "comment": "",
            "protocol": "tcp",
        },
    ],
)
def test_the_global_egress_list_refuses_what_the_contract_calls_invalid(
    bad: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        PlatformSettings.model_validate({"egress_global": [bad]})


def test_warehouse_id_when_given_is_not_empty() -> None:
    assert (
        PlatformSettings.model_validate(
            {"attach_check_warehouse_id": "wh-0001"}
        ).attach_check_warehouse_id
        == "wh-0001"
    )
    with pytest.raises(ValidationError):
        PlatformSettings.model_validate({"attach_check_warehouse_id": ""})


def test_every_key_states_its_level_of_effect() -> None:
    for key in PLATFORM_SETTING_KEYS:
        assert level_of_effect(key) is LevelOfEffect.ALL_CHILDREN


def test_level_of_effect_of_an_unknown_key_is_an_error() -> None:
    with pytest.raises(KeyError, match="not_a_setting"):
        level_of_effect("not_a_setting")


def test_the_level_of_effect_travels_with_the_json_schema() -> None:
    properties = PlatformSettings.model_json_schema()["properties"]

    assert set(properties) == set(PLATFORM_SETTING_KEYS)
    for key in PLATFORM_SETTING_KEYS:
        assert properties[key]["level_of_effect"] == "all children"


def test_platform_settings_are_immutable_once_built() -> None:
    settings = PlatformSettings()

    with pytest.raises(ValidationError):
        setattr(settings, "max_releases_per_app", 20)  # noqa: B010 - the name is the point
