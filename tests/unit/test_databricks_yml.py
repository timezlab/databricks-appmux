"""The ``databricks.yml`` reader: offers and warnings, never a grant (FR-034, rule 12, T119).

``read_bundle`` reads ``resources.apps.<app>.resources[]`` of a snapshot's bundle file and turns
each entry into an offer (a ``BundleResource``). Everything else in the file is ignored, with a
warning.
Like the ``app.yaml`` parser it is pure: it is given the root file names and a way to read one.

Starting data: the ``bundle_offer`` fixture, whose header comment lists the offers and the warnings
it must produce. Tests compare offers and warnings as sets, as T123 asks, not as ordered lists.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from appmux.appyaml.appyaml import read_manifest
from appmux.appyaml.bundle import MAX_BUNDLE_BYTES, BundleReading, read_bundle
from appmux.appyaml.requirements import check_requirements
from appmux.registry.models import BundleResource, DetectedManifest

FIXTURES: Final = Path(__file__).resolve().parents[1] / "fixtures"


def reading_of(files: Mapping[str, str | bytes], *, app_name: str | None = None) -> BundleReading:
    contents = {n: d.encode() if isinstance(d, str) else d for n, d in files.items()}
    return read_bundle(contents.keys(), lambda name: contents[name], app_name=app_name)


def bundle(text: str, *, app_name: str | None = None) -> BundleReading:
    return reading_of({"databricks.yml": text}, app_name=app_name)


def offers_of(reading: BundleReading) -> set[tuple[str, str, str | None, str]]:
    return {(o.key, o.type, o.target, o.permission) for o in reading.offers}


def app_with(entries: str, extra: str = "") -> str:
    """A one-app bundle file whose resources are the lines of ``entries`` (indented for a list)."""
    return f"resources:\n  apps:\n    sales:\n{extra}      resources:\n{entries}"


def fixture_reading(name: str = "bundle_offer") -> BundleReading:
    folder = FIXTURES / name
    return read_bundle(
        [p.name for p in folder.iterdir()], lambda n: (folder / n).read_bytes(), app_name=None
    )


# --- the fixture ----------------------------------------------------------------------------------


def test_the_fixture_gives_one_offer_for_each_resource_it_declares() -> None:
    reading = fixture_reading()

    assert offers_of(reading) == {
        ("sql-warehouse", "sql_warehouse", "0000000000000000", "CAN_USE"),
        ("experiment", "mlflow_experiment", "1111111111", "CAN_EDIT"),
        ("openai-api-key", "secret", "example-scope/openai-api-key", "READ"),
        ("forecast-model", "serving_endpoint", None, "CAN_QUERY"),
        ("ops-job", "job", "2222222222", "IS_OWNER"),
        (
            "unused-table",
            "uc_table",
            "example_catalog.example_schema.example_table",
            "SELECT",
        ),
    }
    assert all(isinstance(offer, BundleResource) for offer in reading.offers)


def test_an_interpolation_is_left_unresolved_and_the_admin_types_the_value() -> None:
    offers = {o.key: o for o in fixture_reading().offers}

    assert offers["forecast-model"].target is None


def test_a_permission_that_is_not_a_level_of_the_type_is_a_warning_and_still_an_offer() -> None:
    reading = fixture_reading()

    job_warnings = [w for w in reading.warnings if "ops-job" in w]
    assert len(job_warnings) == 1
    assert "IS_OWNER" in job_warnings[0]
    assert "CAN_MANAGE_RUN" in job_warnings[0]  # the levels the admin may pick from
    assert {o.key: o.permission for o in reading.offers}["ops-job"] == "IS_OWNER"


def test_the_keys_that_appmux_never_applies_are_warned_by_name() -> None:
    warnings = fixture_reading().warnings

    for ignored in ("variables", "targets", "config", "permissions", "user_api_scopes"):
        assert sum(f"'{ignored}'" in warning for warning in warnings) == 1, ignored
    assert not any("'bundle'" in warning for warning in warnings)


def test_a_file_that_declares_permissions_says_that_no_grant_is_applied() -> None:
    warning = next(w for w in fixture_reading().warnings if "'permissions'" in w)

    assert "never applies a grant" in warning


def test_warnings_never_repeat_a_value_of_the_file() -> None:
    text = "\n".join(fixture_reading().warnings)

    for value in ("user@example.com", "workspace.example.invalid", "example-endpoint"):
        assert value not in text


# --- nothing is attached, nothing is required -----------------------------------------------------


def test_offers_attach_nothing_and_require_nothing() -> None:
    folder = FIXTURES / "bundle_offer"
    manifest = read_manifest(
        [p.name for p in folder.iterdir()], lambda n: (folder / n).read_bytes()
    )

    assert len(manifest.bundle_resources) == 6
    # app.yaml names three keys; the other three offers are mentioned by no valueFrom.
    check = check_requirements(manifest.env, [], resource_keys=())
    assert set(check.required) == {"sql-warehouse", "experiment", "openai-api-key"}
    assert set(check.missing) == set(check.required)


def test_the_manifest_keeps_the_offers_and_the_warnings_of_the_bundle() -> None:
    folder = FIXTURES / "bundle_offer"
    manifest = read_manifest(
        [p.name for p in folder.iterdir()], lambda n: (folder / n).read_bytes()
    )

    assert {o.key for o in manifest.bundle_resources} >= {"sql-warehouse", "ops-job"}
    assert any("'targets'" in w for w in manifest.warnings)
    assert DetectedManifest.model_validate_json(manifest.model_dump_json()) == manifest


def test_a_snapshot_without_a_bundle_file_has_no_offers_and_no_warnings() -> None:
    manifest = read_manifest(["app.py"], lambda _name: b"")

    assert manifest.bundle_resources == []
    assert manifest.warnings == []


# --- the type each key of the file maps to --------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            "sql_warehouse: {id: abc123, permission: CAN_USE}",
            ("sql_warehouse", "abc123", "CAN_USE"),
        ),
        (
            "experiment: {experiment_id: '42', permission: CAN_READ}",
            ("mlflow_experiment", "42", "CAN_READ"),
        ),
        (
            "secret: {scope: s1, key: k1, permission: READ}",
            ("secret", "s1/k1", "READ"),
        ),
        (
            "serving_endpoint: {name: fc, permission: CAN_VIEW}",
            ("serving_endpoint", "fc", "CAN_VIEW"),
        ),
        ("job: {id: '7', permission: CAN_MANAGE}", ("job", "7", "CAN_MANAGE")),
        (
            "genie_space: {name: g, space_id: sp1, permission: CAN_RUN}",
            ("genie_space", "sp1", "CAN_RUN"),
        ),
        ("app: {name: other-app, permission: CAN_USE}", ("databricks_app", "other-app", "CAN_USE")),
        (
            "uc_securable: {securable_type: TABLE, securable_full_name: c.s.t, permission: SELECT}",
            ("uc_table", "c.s.t", "SELECT"),
        ),
        (
            "uc_securable: {securable_type: VOLUME, securable_full_name: c.s.v,"
            " permission: READ_VOLUME}",
            ("uc_volume", "/Volumes/c/s/v", "READ_VOLUME"),
        ),
        (
            "uc_securable: {securable_type: FUNCTION, securable_full_name: c.s.f,"
            " permission: EXECUTE}",
            ("uc_function", "c.s.f", "EXECUTE"),
        ),
        (
            "uc_securable: {securable_type: CONNECTION, securable_full_name: conn1,"
            " permission: USE_CONNECTION}",
            ("uc_connection", "conn1", "USE_CONNECTION"),
        ),
        (
            "uc_securable: {securable_type: MODEL_SERVICE, securable_full_name: c.s.m,"
            " permission: EXECUTE}",
            ("gateway_model_service", "c.s.m", "EXECUTE"),
        ),
    ],
)
def test_each_type_key_of_the_file_maps_to_an_appmux_type(
    entry: str, expected: tuple[str, str, str]
) -> None:
    reading = bundle(app_with(f"        - name: r1\n          {entry}\n"))

    assert offers_of(reading) == {("r1", *expected)}
    assert reading.warnings == []


def test_an_id_written_as_a_number_is_offered_as_its_digits() -> None:
    reading = bundle(
        app_with("        - name: r1\n          job: {id: 2222, permission: CAN_VIEW}\n")
    )

    assert offers_of(reading) == {("r1", "job", "2222", "CAN_VIEW")}


@pytest.mark.parametrize("type_key", ["database", "postgres"])
def test_a_lakebase_entry_is_offered_with_no_target_because_the_file_does_not_hold_one(
    type_key: str,
) -> None:
    reading = bundle(
        app_with(
            f"        - name: db\n          {type_key}: {{database_name: d, permission:"
            " CAN_CONNECT_AND_CREATE}\n"
        )
    )

    assert offers_of(reading) == {("db", "lakebase_database", None, "CAN_CONNECT_AND_CREATE")}
    assert len(reading.warnings) == 1
    assert "db" in reading.warnings[0]


def test_an_interpolation_inside_a_secret_reference_leaves_the_target_unresolved() -> None:
    reading = bundle(
        app_with(
            "        - name: s\n"
            "          secret: {scope: '${var.scope}', key: k, permission: READ}\n"
        )
    )

    assert offers_of(reading) == {("s", "secret", None, "READ")}


# --- entries that cannot be offered ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ("- description: no name\n          job: {id: '1', permission: CAN_VIEW}", "name"),
        ("- name: x\n          description: no type key", "type"),
        (
            "- name: x\n          job: {id: '1', permission: CAN_VIEW}\n"
            "          sql_warehouse: {id: '2', permission: CAN_USE}",
            "more than one",
        ),
        ("- name: x\n          nonsense: {id: '1', permission: CAN_VIEW}", "nonsense"),
        (
            "- name: x\n          uc_securable: {securable_type: CATALOG,"
            " securable_full_name: c, permission: SELECT}",
            "CATALOG",
        ),
        ("- name: x\n          job: {id: '1'}", "permission"),
        ("- name: x\n          job: just text", "'x'"),
        ("- just text", "entry 1"),
    ],
)
def test_an_entry_that_cannot_be_offered_is_skipped_with_one_warning(entry: str, why: str) -> None:
    reading = bundle(f"resources:\n  apps:\n    sales:\n      resources:\n        {entry}\n")

    assert reading.offers == ()
    assert len(reading.warnings) == 1
    assert why in reading.warnings[0]


def test_a_second_entry_with_the_same_name_is_skipped_with_a_warning() -> None:
    reading = bundle(
        app_with(
            "        - name: r1\n          job: {id: '1', permission: CAN_VIEW}\n"
            "        - name: r1\n          job: {id: '2', permission: CAN_VIEW}\n"
        )
    )

    assert offers_of(reading) == {("r1", "job", "1", "CAN_VIEW")}
    assert len(reading.warnings) == 1
    assert "r1" in reading.warnings[0]


def test_a_name_that_is_not_a_resource_key_is_offered_as_written_with_a_warning() -> None:
    reading = bundle(
        app_with(
            "        - name: My Warehouse\n          sql_warehouse: {id: w, permission: CAN_USE}\n"
        )
    )

    assert offers_of(reading) == {("My Warehouse", "sql_warehouse", "w", "CAN_USE")}
    assert len(reading.warnings) == 1
    assert "resource key" in reading.warnings[0]


# --- several apps in one file ---------------------------------------------------------------------

TWO_APPS = """\
resources:
  apps:
    sales:
      resources:
        - name: sales-wh
          sql_warehouse: {id: w1, permission: CAN_USE}
    ops:
      resources:
        - name: ops-job
          job: {id: '9', permission: CAN_VIEW}
"""


def test_the_chosen_app_decides_which_offers_are_read() -> None:
    sales = bundle(TWO_APPS, app_name="sales")
    ops = bundle(TWO_APPS, app_name="ops")

    assert offers_of(sales) == {("sales-wh", "sql_warehouse", "w1", "CAN_USE")}
    assert offers_of(ops) == {("ops-job", "job", "9", "CAN_VIEW")}
    assert sales.chosen_app == "sales"
    assert sales.app_names == ("ops", "sales")


def test_without_a_choice_several_apps_give_no_offer_and_a_warning_that_names_them() -> None:
    reading = bundle(TWO_APPS)

    assert reading.offers == ()
    assert reading.chosen_app is None
    assert reading.app_names == ("ops", "sales")
    assert len(reading.warnings) == 1
    assert "ops" in reading.warnings[0]
    assert "sales" in reading.warnings[0]


def test_a_choice_that_names_no_app_of_the_file_gives_no_offer_and_a_warning() -> None:
    reading = bundle(TWO_APPS, app_name="missing")

    assert reading.offers == ()
    assert len(reading.warnings) == 1
    assert "missing" in reading.warnings[0]


def test_a_file_with_one_app_needs_no_choice() -> None:
    reading = bundle(
        app_with("        - name: r1\n          job: {id: '1', permission: CAN_VIEW}\n")
    )

    assert reading.chosen_app == "sales"
    assert len(reading.offers) == 1


def test_the_manifest_passes_the_choice_through() -> None:
    contents = {"databricks.yml": TWO_APPS.encode()}

    chosen = read_manifest(contents.keys(), lambda n: contents[n], bundle_app="ops")
    unchosen = read_manifest(contents.keys(), lambda n: contents[n])

    assert [o.key for o in chosen.bundle_resources] == ["ops-job"]
    assert unchosen.bundle_resources == []


# --- ignored keys ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra_app_key",
    ["config", "permissions", "user_api_scopes", "compute_size", "budget_policy_id", "git_source"],
)
def test_an_app_level_key_other_than_the_resources_is_ignored_with_a_warning(
    extra_app_key: str,
) -> None:
    text = (
        f"resources:\n  apps:\n    sales:\n      {extra_app_key}: whatever\n"
        "      resources:\n        - name: r1\n"
        "          job: {id: '1', permission: CAN_VIEW}\n"
    )

    reading = bundle(text)

    assert len(reading.offers) == 1
    assert [w for w in reading.warnings if f"'{extra_app_key}'" in w] != []


def test_the_harmless_metadata_of_an_app_is_not_warned() -> None:
    text = (
        "resources:\n  apps:\n    sales:\n      name: sales\n      description: text\n"
        "      source_code_path: .\n      resources: []\n"
    )

    assert bundle(text).warnings == []


def test_another_kind_of_bundle_resource_is_ignored_with_a_warning() -> None:
    text = "resources:\n  jobs:\n    nightly: {name: x}\n  apps:\n    sales:\n      resources: []\n"

    reading = bundle(text)

    assert any("'jobs'" in warning for warning in reading.warnings)


# --- a file that cannot be read never refuses an Update -------------------------------------------


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("key: [unclosed\n", "not valid YAML"),
        ("just a string\n", "mapping"),
        ("- a\n- b\n", "mapping"),
        ("bundle:\n  name: x\n", "resources.apps"),
        ("resources:\n  jobs: {}\n", "resources.apps"),
        ("resources:\n  apps: []\n", "resources.apps"),
        ("resources:\n  apps: {}\n", "resources.apps"),
        ("resources:\n  apps:\n    sales: not-a-mapping\n", "sales"),
        ("resources:\n  apps:\n    sales:\n      resources: nope\n", "'resources'"),
        ("", "resources.apps"),
        ("# only a comment\n", "resources.apps"),
    ],
)
def test_a_file_that_cannot_be_used_is_a_warning_and_no_offer(text: str, why: str) -> None:
    reading = bundle(text)

    assert reading.offers == ()
    assert len(reading.warnings) >= 1
    assert any(why in warning for warning in reading.warnings)
    # The Update goes on: the whole manifest is built from the same file.
    manifest = read_manifest(["databricks.yml"], lambda _name: text.encode())
    assert manifest.bundle_resources == []
    assert manifest.warnings


def test_a_binary_or_oversized_file_is_a_warning_and_no_offer() -> None:
    not_text = reading_of({"databricks.yml": b"\xff\xfe\x00 not utf-8"})
    too_big = reading_of({"databricks.yml": b"# " + b"x" * MAX_BUNDLE_BYTES + b"\n"})

    for reading in (not_text, too_big):
        assert reading.offers == ()
        assert len(reading.warnings) == 1


def test_a_reader_that_fails_is_a_warning_and_no_offer() -> None:
    def broken(_name: str) -> bytes:
        raise OSError("the snapshot file is gone")

    reading = read_bundle(["databricks.yml"], broken, app_name=None)

    assert reading.offers == ()
    assert len(reading.warnings) == 1
    assert "gone" not in reading.warnings[0]


def test_both_bundle_file_names_are_a_warning_and_neither_is_read() -> None:
    entry = app_with("        - name: r1\n          job: {id: '1', permission: CAN_VIEW}\n")
    reading = reading_of({"databricks.yml": entry, "databricks.yaml": entry})

    assert reading.offers == ()
    assert len(reading.warnings) == 1
    assert "databricks.yml" in reading.warnings[0]
    assert "databricks.yaml" in reading.warnings[0]


def test_databricks_yaml_alone_is_read_like_databricks_yml() -> None:
    reading = reading_of(
        {
            "databricks.yaml": app_with(
                "        - name: r1\n          job: {id: '1', permission: CAN_VIEW}\n"
            )
        }
    )

    assert len(reading.offers) == 1


def test_the_parser_is_the_safe_one_and_runs_no_code() -> None:
    text = "resources: !!python/object/apply:os.getcwd []\n"

    reading = bundle(text)

    assert reading.offers == ()
    assert len(reading.warnings) == 1
    assert "not valid YAML" in reading.warnings[0]


def test_the_warning_of_a_broken_file_does_not_quote_the_line() -> None:
    secret_looking = "dapi" + "0123456789abcdef" * 2
    reading = bundle(f"resources: [\n  apps: {secret_looking}\n")

    assert secret_looking not in "\n".join(reading.warnings)


def test_the_number_of_offers_is_bounded() -> None:
    entries = "".join(
        f"        - name: r{i}\n          job: {{id: '{i}', permission: CAN_VIEW}}\n"
        for i in range(150)
    )

    reading = bundle(app_with(entries))

    assert len(reading.offers) == 100
    assert any("100" in warning for warning in reading.warnings)
