"""The environment a child receives (design 5.4, research D16).

Later wins: the ``app.yaml`` values, then the release ``env``, then appmux's own variables,
which cannot be overridden. Nothing of appmux's own environment reaches the child, because the
builder is handed every input and never reads ``os.environ``.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping

import pytest

from appmux.sandbox.environment import (
    ChildEnvironmentError,
    ChildIdentity,
    EnvEntry,
    appmux_variables,
    build_child_environment,
)

CHILD_PORT = 41234
SECRET_VALUE = "s3cr3t-value-for-tests"  # noqa: S105 - an obviously fake placeholder
# Obviously fake credential values: tests check that none of them ever shows in a repr or a log.
CREDENTIAL_VALUE = "fake-credential-value-0001"
OTHER_CREDENTIAL_VALUE = "fake-credential-value-0002"

# The child's per-child run directory is /run/appmux inside its root (design 5.4 and 6.2) and its
# HOME is a private directory under it. /home/app is not used: design 6.4 lists it among the
# paths a child must not reach, because it is appmux's own.
CHILD_RUN_DIRECTORY = "/run/appmux"
CHILD_HOME = f"{CHILD_RUN_DIRECTORY}/home"


def identity(
    *,
    parent_url: str = "https://appmux.example.invalid",
    home_directory: str = CHILD_HOME,
    credentials: Mapping[str, str] | None = None,
) -> ChildIdentity:
    return ChildIdentity(
        slug="sales",
        port=CHILD_PORT,
        workspace_host="https://workspace.example.invalid",
        workspace_id="1234567890",
        parent_url=parent_url,
        home_directory=home_directory,
        credentials=credentials or {},
    )


def plain(**values: str) -> list[EnvEntry]:
    return [EnvEntry(name=name, value=value) for name, value in values.items()]


def build(
    app_yaml: list[EnvEntry] | None = None,
    release: list[EnvEntry] | None = None,
    resources: Mapping[str, str] | None = None,
    child: ChildIdentity | None = None,
) -> dict[str, str]:
    return build_child_environment(
        app_yaml_env=app_yaml or [],
        release_env=release or [],
        appmux_variables=appmux_variables(child or identity()),
        resource_values=resources,
    )


# --- Precedence -------------------------------------------------------------------------------


def test_a_child_gets_the_app_yaml_values() -> None:
    environment = build(plain(LOG_LEVEL="info"))

    assert environment["LOG_LEVEL"] == "info"


def test_the_release_env_overrides_app_yaml() -> None:
    environment = build(plain(LOG_LEVEL="info", REGION="eu"), plain(LOG_LEVEL="debug"))

    assert environment["LOG_LEVEL"] == "debug"
    assert environment["REGION"] == "eu"


def test_the_release_env_can_add_a_name_app_yaml_lacks() -> None:
    environment = build(plain(LOG_LEVEL="info"), plain(FEATURE_FLAG="on"))

    assert environment["FEATURE_FLAG"] == "on"


def test_appmux_variables_cannot_be_overridden_by_app_yaml_or_the_release() -> None:
    hostile = plain(
        DATABRICKS_APP_PORT="1",
        PORT="2",
        APPMUX_BASE_PATH="/other",
        HOME="/root",
        LC_CTYPE="POSIX",
        DATABRICKS_APP_URL="https://evil.example.invalid",
        UVICORN_HOST="127.0.0.1",
    )

    environment = build(hostile, hostile)

    assert environment["DATABRICKS_APP_PORT"] == str(CHILD_PORT)
    assert environment["PORT"] == str(CHILD_PORT)
    assert environment["APPMUX_BASE_PATH"] == "/sales"
    assert environment["HOME"] == CHILD_HOME
    assert environment["LC_CTYPE"] == "C.UTF-8"
    assert environment["DATABRICKS_APP_URL"] == "https://appmux.example.invalid/sales"
    assert environment["UVICORN_HOST"] == "0.0.0.0"  # noqa: S104 - the value the platform sets


def test_a_later_entry_of_the_same_layer_wins() -> None:
    environment = build(plain(MODE="first") + plain(MODE="second"))

    assert environment["MODE"] == "second"


def test_the_inputs_are_not_changed() -> None:
    app_yaml = plain(A="1")
    variables = appmux_variables(identity())
    before = dict(variables)

    build_child_environment(
        app_yaml_env=app_yaml, release_env=plain(A="2"), appmux_variables=variables
    )

    assert variables == before
    assert app_yaml == plain(A="1")


# --- Nothing of appmux's own environment ------------------------------------------------------


def test_no_variable_of_appmuxs_own_environment_reaches_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appmux_environment = {
        "DATABRICKS_CLIENT_ID": "client-id-placeholder",
        "DATABRICKS_CLIENT_SECRET": "client-secret-placeholder",
        "PGHOST": "database.example.invalid",
        "APPMUX_LAKEBASE_DATABASE": "appmux",
        "APPMUX_BOOTSTRAP_ADMINS": "admin@example.com",
        "PATH": "/usr/bin",
        "PYTHONPATH": "/app/appmux",
        "UV_PYTHON_INSTALL_DIR": "/app/.uv-python",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example.invalid",
    }
    for name, value in appmux_environment.items():
        monkeypatch.setenv(name, value)

    environment = build(plain(LOG_LEVEL="info"), plain(REGION="eu"))

    assert set(appmux_environment).isdisjoint(environment)
    assert set(appmux_environment.values()).isdisjoint(environment.values())


def test_the_child_gets_exactly_the_three_layers() -> None:
    environment = build(plain(FROM_APP_YAML="1"), plain(FROM_RELEASE="2"))

    expected = {"FROM_APP_YAML": "1", "FROM_RELEASE": "2", **appmux_variables(identity())}
    assert environment == expected


def test_the_variables_the_platform_withholds_from_a_child_are_not_set() -> None:
    environment = build()

    for name in (
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "UV_PYTHON_INSTALL_DIR",
        "STREAMLIT_SERVER_BASE_URL_PATH",
        "PATH",
    ):
        assert name not in environment
    assert not [name for name in environment if name.startswith("OTEL_")]


# --- appmux's variables -----------------------------------------------------------------------


def test_the_platform_values_that_appmux_sets_by_hand() -> None:
    environment = build()

    assert environment["LC_CTYPE"] == "C.UTF-8"
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert environment["HOME"] == CHILD_HOME


def test_home_is_a_directory_under_the_childs_run_directory_as_the_caller_gives_it() -> None:
    other_home = f"{CHILD_RUN_DIRECTORY}/home-of-another-layout"

    environment = build(child=identity(home_directory=other_home))

    assert environment["HOME"] == other_home
    assert environment["HOME"].startswith(f"{CHILD_RUN_DIRECTORY}/")


@pytest.mark.parametrize(
    "parent_url", ["https://appmux.example.invalid", "https://appmux.example.invalid/"]
)
def test_the_app_url_is_the_parent_url_plus_the_slug(parent_url: str) -> None:
    environment = build(child=identity(parent_url=parent_url))

    assert environment["DATABRICKS_APP_URL"] == "https://appmux.example.invalid/sales"


def test_the_app_identity_variables() -> None:
    environment = build()

    assert environment["DATABRICKS_APP_NAME"] == "sales"
    assert environment["DATABRICKS_HOST"] == "https://workspace.example.invalid"
    assert environment["DATABRICKS_WORKSPACE_ID"] == "1234567890"
    assert environment["APPMUX_BASE_PATH"] == "/sales"


def test_every_framework_port_variable_is_the_childs_port() -> None:
    environment = build()

    for name in (
        "DATABRICKS_APP_PORT",
        "GRADIO_SERVER_PORT",
        "STREAMLIT_SERVER_PORT",
        "UVICORN_PORT",
        "FLASK_RUN_PORT",
        "PORT",
    ):
        assert environment[name] == str(CHILD_PORT)


def test_the_framework_settings_follow_the_platform() -> None:
    environment = build()

    assert environment["GRADIO_SERVER_NAME"] == "0.0.0.0"  # noqa: S104 - the value the platform sets
    assert environment["STREAMLIT_SERVER_ADDRESS"] == "0.0.0.0"  # noqa: S104 - same
    assert environment["UVICORN_HOST"] == "0.0.0.0"  # noqa: S104 - same
    assert environment["FLASK_RUN_HOST"] == "0.0.0.0"  # noqa: S104 - same
    assert environment["STREAMLIT_SERVER_HEADLESS"] == "true"
    assert environment["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] == "false"
    assert environment["GRADIO_ANALYTICS_ENABLED"] == "false"
    assert environment["STREAMLIT_SERVER_ENABLE_CORS"] == "false"
    assert environment["STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION"] == "false"


def test_the_base_path_variables_make_unmodified_code_generate_prefixed_links() -> None:
    environment = build()

    assert environment["UVICORN_ROOT_PATH"] == "/sales"
    assert environment["GRADIO_ROOT_PATH"] == "/sales"
    assert environment["DASH_REQUESTS_PATHNAME_PREFIX"] == "/sales/"
    assert environment["DASH_ROUTES_PATHNAME_PREFIX"] == "/"


def test_credentials_of_the_apps_mode_are_added() -> None:
    credentials = {
        "DATABRICKS_AUTH_TYPE": "databricks-cli",
        "DATABRICKS_CLI_PATH": "/run/appmux/token",
    }

    environment = build(child=identity(credentials=credentials))

    assert environment["DATABRICKS_AUTH_TYPE"] == "databricks-cli"
    assert environment["DATABRICKS_CLI_PATH"] == "/run/appmux/token"


def test_credentials_cannot_override_the_variables_appmux_owns() -> None:
    environment = build(child=identity(credentials={"DATABRICKS_APP_PORT": "1", "HOME": "/root"}))

    assert environment["DATABRICKS_APP_PORT"] == str(CHILD_PORT)
    assert environment["HOME"] == CHILD_HOME


def test_credentials_override_app_yaml_and_release_values() -> None:
    environment = build(
        plain(DATABRICKS_AUTH_TYPE="pat"),
        plain(DATABRICKS_AUTH_TYPE="basic"),
        child=identity(credentials={"DATABRICKS_AUTH_TYPE": "databricks-cli"}),
    )

    assert environment["DATABRICKS_AUTH_TYPE"] == "databricks-cli"


# --- Values from resources --------------------------------------------------------------------


def test_a_value_from_entry_takes_the_resolved_value_of_its_resource() -> None:
    app_yaml = [
        EnvEntry(name="OPENAI_API_KEY", value_from="openai-api-key"),
        EnvEntry(name="WAREHOUSE_ID", value_from="sql-warehouse"),
    ]

    environment = build(
        app_yaml, resources={"openai-api-key": SECRET_VALUE, "sql-warehouse": "a1b2"}
    )

    assert environment["OPENAI_API_KEY"] == SECRET_VALUE
    assert environment["WAREHOUSE_ID"] == "a1b2"


def test_a_release_value_from_overrides_an_app_yaml_value() -> None:
    environment = build(
        plain(API_KEY="placeholder"),
        [EnvEntry(name="API_KEY", value_from="api-key")],
        resources={"api-key": SECRET_VALUE},
    )

    assert environment["API_KEY"] == SECRET_VALUE


def test_a_release_value_overrides_an_app_yaml_value_from() -> None:
    environment = build(
        [EnvEntry(name="API_KEY", value_from="api-key")],
        plain(API_KEY="from-the-release"),
        resources={"api-key": SECRET_VALUE},
    )

    assert environment["API_KEY"] == "from-the-release"
    assert SECRET_VALUE not in environment.values()


def test_a_resource_nobody_asked_for_is_not_placed_in_the_environment() -> None:
    environment = build(plain(LOG_LEVEL="info"), resources={"unused": SECRET_VALUE})

    assert SECRET_VALUE not in environment.values()


def test_a_value_from_without_a_resolved_value_is_refused_naming_the_key() -> None:
    with pytest.raises(ChildEnvironmentError, match="openai-api-key"):
        build([EnvEntry(name="OPENAI_API_KEY", value_from="openai-api-key")], resources={})


def test_a_missing_value_is_refused_even_when_a_later_layer_overrides_the_name() -> None:
    with pytest.raises(ChildEnvironmentError, match="openai-api-key"):
        build(
            [EnvEntry(name="OPENAI_API_KEY", value_from="openai-api-key")],
            plain(OPENAI_API_KEY="override"),
        )


def test_the_dollar_form_is_left_as_written_in_a_value() -> None:
    environment = build(plain(PORT_COPY="$DATABRICKS_APP_PORT", BRACED="${DATABRICKS_APP_PORT}"))

    assert environment["PORT_COPY"] == "$DATABRICKS_APP_PORT"
    assert environment["BRACED"] == "${DATABRICKS_APP_PORT}"


# --- Entries and names that cannot be exported ------------------------------------------------


def test_an_entry_needs_exactly_one_of_value_and_value_from() -> None:
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        EnvEntry(name="LOG_LEVEL")
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        EnvEntry(name="LOG_LEVEL", value="info", value_from="key")


@pytest.mark.parametrize("name", ["", "A=B", "A\x00B"])
def test_a_name_that_cannot_be_exported_is_refused(name: str) -> None:
    with pytest.raises(ChildEnvironmentError):
        build([EnvEntry(name=name, value="x")])


def test_a_value_with_a_nul_byte_is_refused_naming_the_variable() -> None:
    with pytest.raises(ChildEnvironmentError, match="BROKEN"):
        build(plain(BROKEN="a\x00b"))


def test_a_refusal_never_contains_a_secret_value_and_nothing_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    entries = [
        EnvEntry(name="OPENAI_API_KEY", value_from="openai-api-key"),
        EnvEntry(name="BAD=NAME", value="x"),
    ]

    with pytest.raises(ChildEnvironmentError) as raised:
        build(entries, resources={"openai-api-key": SECRET_VALUE})

    assert SECRET_VALUE not in str(raised.value)
    assert SECRET_VALUE not in caplog.text
    assert caplog.records == []


def test_a_nul_in_a_secret_is_refused_without_printing_it() -> None:
    secret_with_nul = f"{SECRET_VALUE}\x00tail"

    with pytest.raises(ChildEnvironmentError) as raised:
        build([EnvEntry(name="KEY", value_from="k")], resources={"k": secret_with_nul})

    assert SECRET_VALUE not in str(raised.value)
    assert "KEY" in str(raised.value)


# --- Credential values never print ------------------------------------------------------------

CREDENTIALS = {
    "DATABRICKS_TOKEN": CREDENTIAL_VALUE,
    "OTHER_API_KEY": OTHER_CREDENTIAL_VALUE,
}


def shown_forms(value: object) -> list[str]:
    """The ways a log line, a traceback or a debugger prints an object."""
    return [repr(value), str(value), f"{value}", f"{value!r}"]


def test_a_child_identity_never_prints_its_credential_values() -> None:
    child = identity(credentials=CREDENTIALS)

    for form in shown_forms(child):
        assert CREDENTIAL_VALUE not in form
        assert OTHER_CREDENTIAL_VALUE not in form


def test_a_copy_of_a_child_identity_never_prints_its_credential_values() -> None:
    copy = dataclasses.replace(identity(credentials=CREDENTIALS), slug="other")

    for form in shown_forms(copy):
        assert CREDENTIAL_VALUE not in form


def test_a_child_identity_still_prints_what_is_not_secret() -> None:
    printed = repr(identity(credentials=CREDENTIALS))

    assert "sales" in printed
    assert str(CHILD_PORT) in printed
    assert CHILD_HOME in printed


def test_the_variables_appmux_sets_never_print_a_credential_value() -> None:
    variables = appmux_variables(identity(credentials=CREDENTIALS))

    for form in shown_forms(variables):
        assert CREDENTIAL_VALUE not in form
        assert OTHER_CREDENTIAL_VALUE not in form
    assert variables["DATABRICKS_TOKEN"] == CREDENTIAL_VALUE


def test_the_environment_never_prints_a_credential_or_a_resource_value() -> None:
    environment = build(
        [EnvEntry(name="OPENAI_API_KEY", value_from="openai-api-key")],
        resources={"openai-api-key": SECRET_VALUE},
        child=identity(credentials=CREDENTIALS),
    )

    for form in shown_forms(environment):
        assert CREDENTIAL_VALUE not in form
        assert SECRET_VALUE not in form
    assert environment["OPENAI_API_KEY"] == SECRET_VALUE
    assert environment["DATABRICKS_TOKEN"] == CREDENTIAL_VALUE


def test_the_printed_environment_still_names_the_variables() -> None:
    printed = repr(build(plain(LOG_LEVEL="info"), child=identity(credentials=CREDENTIALS)))

    assert "LOG_LEVEL" in printed
    assert "DATABRICKS_TOKEN" in printed


def test_the_environment_compares_and_copies_like_a_plain_dict() -> None:
    environment = build(plain(LOG_LEVEL="info"))

    assert environment == {"LOG_LEVEL": "info", **appmux_variables(identity())}
    assert dict(environment) == {**environment}


def test_a_successful_build_with_secrets_logs_and_prints_nothing(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)

    build(
        [EnvEntry(name="OPENAI_API_KEY", value_from="openai-api-key")],
        resources={"openai-api-key": SECRET_VALUE},
        child=identity(credentials=CREDENTIALS),
    )

    assert caplog.records == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
