"""The requirements check: every ``valueFrom`` key must be a resource of the app (FR-035, T118).

The check is pure and reads the release row only (its stored manifest and its config), so an
Activate that runs it again needs no source and no token: the tests build the row, push it through
JSON as the registry does, and never hand the check a way to read a file.

Starting data: the ``appyaml_missing_resource`` fixture (two ``valueFrom`` keys, no resources) read
by the real ``app.yaml`` parser.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from appmux.appyaml.appyaml import read_manifest
from appmux.appyaml.requirements import (
    MISSING_RESOURCES_PREFIX,
    check_release_requirements,
    check_requirements,
    required_keys,
    suggest_resource_type,
)
from appmux.registry.models import DetectedManifest, EnvEntry, Release, ReleaseConfig

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"
NOW: Final = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
DIGEST: Final = "sha256:" + "0" * 64


def fixture_manifest(name: str) -> DetectedManifest:
    folder = FIXTURES / name
    return read_manifest([p.name for p in folder.iterdir()], lambda n: (folder / n).read_bytes())


def release_row(manifest: DetectedManifest, config: ReleaseConfig | None = None) -> Release:
    row = Release(
        app_slug="sales",
        release_no=1,
        snapshot_digest=DIGEST,
        detected=manifest,
        config=config if config is not None else ReleaseConfig(),
        status="pending",
        status_at=NOW,
        created_by="alice@example.com",
        created_at=NOW,
    )
    # What the reconciler holds is the stored row, not the object the Update built.
    return Release.model_validate_json(row.model_dump_json())


def from_resource(name: str, key: str) -> EnvEntry:
    return EnvEntry(name=name, value_from=key)


def literal(name: str, value: str) -> EnvEntry:
    return EnvEntry(name=name, value=value)


# --- which keys a release needs ------------------------------------------------------------------


def test_the_required_keys_are_every_value_from_of_the_manifest_and_of_the_release_config() -> None:
    manifest = DetectedManifest(
        env=[literal("LOG_LEVEL", "info"), from_resource("A", "openai-api-key")]
    )
    config = ReleaseConfig(env=[from_resource("B", "sql-warehouse"), literal("C", "x")])

    assert required_keys(manifest.env, config.env) == ("openai-api-key", "sql-warehouse")


def test_a_key_named_twice_is_required_once_and_the_keys_come_back_sorted() -> None:
    entries = [from_resource("B", "zeta"), from_resource("A", "alpha"), from_resource("C", "zeta")]

    assert required_keys(entries, []) == ("alpha", "zeta")


def test_a_key_that_a_later_layer_overrides_is_still_required() -> None:
    # The environment builder refuses a value_from with no resolved value even when the release env
    # overrides the name, so the check must ask for it too.
    manifest = DetectedManifest(env=[from_resource("TOKEN", "app-key")])
    config = ReleaseConfig(env=[literal("TOKEN", "from-the-release")])

    assert required_keys(manifest.env, config.env) == ("app-key",)


def test_entries_with_only_literal_values_require_nothing() -> None:
    assert required_keys([literal("A", "1")], [literal("B", "2")]) == ()


# --- the check -----------------------------------------------------------------------------------


def test_every_missing_key_is_listed_at_once_in_the_message_of_the_design() -> None:
    manifest = fixture_manifest("appyaml_missing_resource")

    check = check_release_requirements(release_row(manifest), resource_keys=())

    assert set(check.missing) == {"openai-api-key", "sql-warehouse"}
    assert check.is_met is False
    assert check.message() == "missing resources: openai-api-key, sql-warehouse"
    assert check.message().startswith(MISSING_RESOURCES_PREFIX)


def test_a_partly_met_release_lists_only_what_is_still_missing() -> None:
    manifest = fixture_manifest("appyaml_missing_resource")

    check = check_release_requirements(release_row(manifest), resource_keys={"sql-warehouse"})

    assert check.missing == ("openai-api-key",)
    assert check.message() == "missing resources: openai-api-key"


def test_attaching_the_keys_makes_the_release_ready_to_start() -> None:
    manifest = fixture_manifest("appyaml_missing_resource")
    row = release_row(manifest)

    before = check_release_requirements(row, resource_keys=())
    after = check_release_requirements(row, resource_keys={"openai-api-key", "sql-warehouse"})

    assert before.is_met is False
    assert after.is_met is True
    assert after.missing == ()
    assert after.required == ("openai-api-key", "sql-warehouse")
    assert after.message() == ""


def test_the_release_config_keys_count_with_those_of_app_yaml() -> None:
    manifest = fixture_manifest("appyaml_missing_resource")
    config = ReleaseConfig(env=[from_resource("EXTRA", "extra-key")])

    check = check_release_requirements(release_row(manifest, config), resource_keys=())

    assert check.missing == ("extra-key", "openai-api-key", "sql-warehouse")


def test_resources_the_release_does_not_name_are_not_required_and_do_not_matter() -> None:
    manifest = fixture_manifest("appyaml_missing_resource")

    check = check_release_requirements(
        release_row(manifest), resource_keys={"openai-api-key", "sql-warehouse", "unrelated"}
    )

    assert check.is_met is True


def test_a_release_with_no_value_from_is_met_with_no_resources() -> None:
    check = check_requirements([literal("A", "1")], [], resource_keys=())

    assert check.is_met is True
    assert check.required == ()


def test_the_check_is_the_same_for_the_row_before_and_after_it_is_stored() -> None:
    manifest = fixture_manifest("appyaml_missing_resource")
    built = Release(
        app_slug="sales",
        release_no=1,
        snapshot_digest=DIGEST,
        detected=manifest,
        status="pending",
        status_at=NOW,
        created_by="alice@example.com",
        created_at=NOW,
    )

    assert check_release_requirements(built, resource_keys=()) == check_release_requirements(
        release_row(manifest), resource_keys=()
    )


def test_a_key_in_the_message_shows_printable_text_only_and_a_bounded_length() -> None:
    odd = "bad\x1b[31mkey" + "x" * 300
    check = check_requirements([from_resource("A", odd)], [], resource_keys=())

    message = check.message()

    assert "\x1b" not in message
    assert len(message) < 200
    assert message.startswith(MISSING_RESOURCES_PREFIX)
    # The raw key is still what `missing` holds, for the Requirements panel to show against.
    assert check.missing == (odd,)


# --- app.yaml: a valueFrom no resource can ever answer -------------------------------------------


@pytest.mark.parametrize("key", ["Upper-Case", "has space", "-leading-dash", "x" * 64])
def test_app_yaml_warns_about_a_value_from_that_is_not_a_resource_key_form(key: str) -> None:
    text = f"env:\n  - name: A\n    valueFrom: {key!r}\n"
    manifest = read_manifest(["app.yaml"], lambda _name: text.encode())

    assert manifest.env == [EnvEntry(name="A", value_from=key)]
    assert len(manifest.warnings) == 1
    assert "env entry 'A'" in manifest.warnings[0]
    assert "resource key" in manifest.warnings[0]
    assert key not in manifest.warnings[0]


@pytest.mark.parametrize("key", ["k", "openai-api-key", "sql.warehouse_2", "0abc", "x" * 63])
def test_app_yaml_accepts_a_value_from_in_the_resource_key_form(key: str) -> None:
    text = f"env:\n  - name: A\n    valueFrom: {key}\n"
    manifest = read_manifest(["app.yaml"], lambda _name: text.encode())

    assert manifest.warnings == []


# --- the hint on a missing key --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "hint"),
    [
        ("sql-warehouse", "sql_warehouse"),
        ("warehouse", "sql_warehouse"),
        ("openai-api-key", "secret"),
        ("db_password", "secret"),
        ("slack-token", "secret"),
        ("experiment", "mlflow_experiment"),
        ("forecast-endpoint", "serving_endpoint"),
        ("forecast-model", "serving_endpoint"),
        ("nightly-job", "job"),
        ("orders-table", "uc_table"),
        ("raw-volume", "uc_volume"),
        ("my-connection", "uc_connection"),
        ("genie-space", "genie_space"),
        ("lakebase-db", "lakebase_database"),
        ("search-index", "vector_search_index"),
    ],
)
def test_a_hint_is_taken_from_the_name_of_the_key(key: str, hint: str) -> None:
    assert suggest_resource_type(key) == hint


@pytest.mark.parametrize("key", ["thing", "x", "config"])
def test_a_key_with_no_telling_name_has_no_hint(key: str) -> None:
    assert suggest_resource_type(key) is None
