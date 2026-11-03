"""The admin API contract, its examples and the backend models stay one (T186).

``contracts/admin-api.md`` names every schema, ``contracts/examples/manifest.json`` maps each to a
valid example, and ``appmux.admin.schemas`` holds one model per name. This file fails when one of
the three lacks a schema, when a model's fields differ from the contract's table, when an example
does not validate, and when a mutated example (a required field removed, a field the schema does
not list added to a request) is still accepted. Every example is mutated, not a sample.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel, ValidationError

from appmux.admin import schemas

CONTRACT_DIR: Final = Path(__file__).resolve().parents[2] / "specs" / "001-appmux-v1" / "contracts"
EXAMPLES_DIR: Final = CONTRACT_DIR / "examples"
CONTRACT_FILE: Final = CONTRACT_DIR / "admin-api.md"
MANIFEST_FILE: Final = EXAMPLES_DIR / "manifest.json"

# A key a request may leave out; removing any other top-level key must be refused. The optional
# keys of the contract's request tables, minus those the example needs for another reason:
# ``access_as`` of the example is a warehouse's, which a non-secret must name.
OPTIONAL_REQUEST_KEYS: Final[dict[str, frozenset[str]]] = {
    "UpdateAppRequest": frozenset({"slug", "display_name", "source_path", "memory_budget_mb"}),
    "AttachResourceRequest": frozenset({"from_bundle"}),
    "DecidePermissionRequest": frozenset({"reason", "note"}),
}

# Keys whose content is free-form JSON or one of a pair, so removing one inside proves nothing.
FREE_FORM_RESPONSE_KEYS: Final = frozenset(
    {"payload", "current", "detail", "egress_dropped_by_rule", "value", "value_from"}
)
FREE_FORM_REQUEST_KEYS: Final = frozenset({"value", "value_from"})

REQUEST_HEADING: Final = "### Request bodies"
RESPONSE_HEADING: Final = "### Response bodies"
END_HEADING: Final = "## Not in this contract"
PRESENCE_WORDS: Final = frozenset({"required", "optional", "always"})


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


MANIFEST: Final[dict[str, dict[str, Any]]] = read_json(MANIFEST_FILE)


def contract_sections() -> dict[str, tuple[str, str]]:
    """Schema name -> (direction, the markdown of its subsection), read from the contract."""
    text = CONTRACT_FILE.read_text(encoding="utf-8")
    start = text.index(REQUEST_HEADING)
    middle = text.index(RESPONSE_HEADING)
    end = text.index(END_HEADING)
    found: dict[str, tuple[str, str]] = {}
    for direction, body in (("request", text[start:middle]), ("response", text[middle:end])):
        parts = re.split(r"^#### (\w+)\s*$", body, flags=re.MULTILINE)
        for name, section in zip(parts[1::2], parts[2::2], strict=True):
            found[name] = (direction, section)
    return found


def contract_fields(section: str) -> dict[str, str]:
    """Top-level field name -> presence word, from the field tables of one subsection."""
    fields: dict[str, str] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or cells[2] not in PRESENCE_WORDS:
            continue
        name = cells[0].strip("`")
        if "." not in name and "[" not in name:
            fields[name] = cells[2]
    return fields


def coverage_problems(
    contract: Mapping[str, tuple[str, str]],
    manifest: Mapping[str, Mapping[str, Any]],
    example_files: set[str],
    model_names: set[str],
) -> list[str]:
    """What is missing between the contract, the manifest, the example files and the models."""
    problems: list[str] = []
    for name, (direction, _) in sorted(contract.items()):
        entry = manifest.get(name)
        if entry is None:
            problems.append(f"{name}: no manifest entry")
        else:
            if entry["file"] not in example_files:
                problems.append(f"{name}: no example file {entry['file']}")
            if entry["direction"] != direction:
                problems.append(f"{name}: manifest says {entry['direction']}, contract {direction}")
        if name not in model_names:
            problems.append(f"{name}: no model in appmux.admin.schemas")
    for name in sorted(set(manifest) - set(contract)):
        problems.append(f"{name}: in the manifest but not in the contract")
    listed = {entry["file"] for entry in manifest.values()}
    for stray in sorted(example_files - listed):
        problems.append(f"{stray}: an example file the manifest does not list")
    return problems


def model_names() -> set[str]:
    return {
        name
        for name, value in vars(schemas).items()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == schemas.__name__
    }


def example_files() -> set[str]:
    return {path.name for path in EXAMPLES_DIR.glob("*.json")} - {MANIFEST_FILE.name}


def model_named(name: str) -> type[BaseModel]:
    model = getattr(schemas, name, None)
    assert isinstance(model, type) and issubclass(model, BaseModel), f"{name} is not a model"
    return model


def example_text(name: str) -> str:
    return (EXAMPLES_DIR / MANIFEST[name]["file"]).read_text(encoding="utf-8")


def accepted(name: str, body: object) -> bool:
    """Whether the model of ``name`` takes ``body`` as it would arrive on the wire (JSON text)."""
    try:
        model_named(name).model_validate_json(json.dumps(body))
    except ValidationError:
        return False
    return True


# --- the mutations --------------------------------------------------------------------------


def property_paths(
    value: object, skip: frozenset[str], prefix: tuple[str | int, ...] = ()
) -> Iterator[tuple[str | int, ...]]:
    """Every path to an object property below the root, through every list element."""
    if isinstance(value, list):
        for index, element in enumerate(value):
            yield from property_paths(element, skip, (*prefix, index))
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in skip:
                continue
            yield (*prefix, key)
            yield from property_paths(child, skip, (*prefix, key))


def object_paths(
    value: object, skip: frozenset[str], prefix: tuple[str | int, ...] = ()
) -> Iterator[tuple[str | int, ...]]:
    """Every path to an object, the root included, through every list element."""
    if isinstance(value, list):
        for index, element in enumerate(value):
            yield from object_paths(element, skip, (*prefix, index))
    elif isinstance(value, dict):
        yield prefix
        for key, child in value.items():
            if key not in skip:
                yield from object_paths(child, skip, (*prefix, key))


def string_paths(
    value: object, prefix: tuple[str | int, ...] = ()
) -> Iterator[tuple[str | int, ...]]:
    """Every path to a string value, through lists and objects, free-form ones included."""
    if isinstance(value, str):
        yield prefix
    elif isinstance(value, list):
        for index, element in enumerate(value):
            yield from string_paths(element, (*prefix, index))
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from string_paths(child, (*prefix, key))


def at(value: Any, path: tuple[str | int, ...]) -> Any:
    for step in path:
        value = value[step]
    return value


def without_path(value: object, path: tuple[str | int, ...]) -> Any:
    mutated = copy.deepcopy(value)
    del at(mutated, path[:-1])[path[-1]]
    return mutated


def with_value(value: object, path: tuple[str | int, ...], replacement: object) -> Any:
    mutated = copy.deepcopy(value)
    at(mutated, path[:-1])[path[-1]] = replacement
    return mutated


def with_unlisted_field(value: object, path: tuple[str | int, ...]) -> Any:
    mutated = copy.deepcopy(value)
    at(mutated, path)["is_admin"] = True
    return mutated


# --- coverage -------------------------------------------------------------------------------


def test_the_contract_the_manifest_the_examples_and_the_models_name_the_same_schemas() -> None:
    problems = coverage_problems(contract_sections(), MANIFEST, example_files(), model_names())
    assert problems == []


def test_the_contract_lists_the_schemas_this_file_expects() -> None:
    # A guard on the parser: if the headings change shape the coverage test must not pass empty.
    assert len(contract_sections()) == len(MANIFEST) == 46


def test_a_schema_without_a_manifest_entry_an_example_or_a_model_is_reported() -> None:
    contract = contract_sections()
    manifest = {name: dict(entry) for name, entry in MANIFEST.items()}
    files = example_files()
    models = model_names()
    victim = "GrantRequest"

    assert coverage_problems(
        contract, {k: v for k, v in manifest.items() if k != victim}, files, models
    )
    assert coverage_problems(contract, manifest, files - {manifest[victim]["file"]}, models)
    assert coverage_problems(contract, manifest, files, models - {victim})
    assert coverage_problems({**contract, "NewSchema": ("request", "")}, manifest, files, models)
    assert coverage_problems(contract, manifest, files | {"Stray.json"}, models)
    wrong = {**manifest, victim: {**manifest[victim], "direction": "response"}}
    assert coverage_problems(contract, wrong, files, models)


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_the_models_fields_are_the_contracts_table(name: str) -> None:
    direction, section = contract_sections()[name]
    declared = contract_fields(section)
    model = model_named(name)
    actual = {
        (field.alias or field_name): ("required" if field.is_required() else "optional")
        for field_name, field in model.model_fields.items()
    }
    assert set(actual) == set(declared), f"{name}: fields differ from the contract's table"
    for field_name, presence in declared.items():
        expected = "optional" if presence == "optional" else "required"
        assert actual[field_name] == expected, f"{name}.{field_name} is {presence} in the contract"
    assert direction == MANIFEST[name]["direction"]


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_a_request_model_refuses_a_field_it_does_not_list(name: str) -> None:
    if MANIFEST[name]["direction"] == "request":
        assert model_named(name).model_config.get("extra") == "forbid"


# --- the examples ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_the_example_validates_and_serialises_back_to_itself(name: str) -> None:
    text = example_text(name)
    model = model_named(name).model_validate_json(text)
    assert model.model_dump(mode="json", by_alias=True, exclude_unset=True) == json.loads(text)


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_the_example_stays_valid_when_every_optional_key_is_removed(name: str) -> None:
    example = json.loads(example_text(name))
    for key in sorted(OPTIONAL_REQUEST_KEYS.get(name, frozenset())):
        assert accepted(name, {k: v for k, v in example.items() if k != key}), f"removing {key}"


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_a_required_top_level_field_removed_is_refused(name: str) -> None:
    example = json.loads(example_text(name))
    assert isinstance(example, dict)
    optional = OPTIONAL_REQUEST_KEYS.get(name, frozenset())
    for key in example:
        mutated = {k: v for k, v in example.items() if k != key}
        assert accepted(name, mutated) == (key in optional), f"removing {key}"


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_a_top_level_field_of_the_wrong_type_is_refused(name: str) -> None:
    example = json.loads(example_text(name))
    for key in example:
        mutated = {**example, key: object()}  # no JSON type: refused even by a free-form field
        with pytest.raises(ValidationError):
            model_named(name).model_validate(mutated)


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_a_required_nested_field_removed_is_refused(name: str) -> None:
    example = json.loads(example_text(name))
    skip = (
        FREE_FORM_REQUEST_KEYS
        if MANIFEST[name]["direction"] == "request"
        else FREE_FORM_RESPONSE_KEYS
    )
    for path in property_paths(example, skip):
        if len(path) == 1:
            continue  # the top level is the test above
        assert not accepted(name, without_path(example, path)), f"removing {path}"


@pytest.mark.parametrize(
    "name", sorted(name for name, entry in MANIFEST.items() if entry["direction"] == "request")
)
def test_a_request_refuses_a_field_it_does_not_list_at_any_depth(name: str) -> None:
    example = json.loads(example_text(name))
    paths = list(object_paths(example, FREE_FORM_REQUEST_KEYS))
    assert () in paths
    for path in paths:
        assert not accepted(name, with_unlisted_field(example, path)), f"adding at {path}"


# The example's `value` is a number; a NUL in a free-form value or key is tested in the rules file.
EXAMPLES_WITHOUT_A_STRING: Final = frozenset({"PutSettingRequest"})
REQUEST_NAMES: Final = sorted(
    name for name, entry in MANIFEST.items() if entry["direction"] == "request"
)
RESPONSE_NAMES: Final = sorted(
    name for name, entry in MANIFEST.items() if entry["direction"] == "response"
)


@pytest.mark.parametrize("name", RESPONSE_NAMES)
def test_a_response_model_refuses_a_field_it_does_not_list_at_any_depth(name: str) -> None:
    # A route that builds a body with a field the contract lacks is a defect; the model says so
    # instead of sending it. Free-form objects (a payload, a detail) keep any keys, and so does a
    # wheel: it is the registry's jsonb model, which ignores unknown keys when it is read, so the
    # object a route passes cannot carry one.
    example = json.loads(example_text(name))
    paths = list(object_paths(example, FREE_FORM_RESPONSE_KEYS | {"wheels"}))
    assert () in paths
    for path in paths:
        assert not accepted(name, with_unlisted_field(example, path)), f"adding at {path}"


# Every field of a response that is one of a closed list in the contract, as (schema, path to the
# field in its example). A value outside the list is a route defect and is refused, not sent.
CLOSED_LIST_FIELDS: Final[tuple[tuple[str, tuple[str | int, ...]], ...]] = (
    ("AppSummary", ("state",)),
    ("AppSummary", ("permission",)),
    ("AppSummary", ("open_modes", 0)),
    ("AppStateResponse", ("state",)),
    ("AppStateResponse", ("desired_state",)),
    ("ReleaseSummary", ("status",)),
    ("ReleaseDetail", ("status",)),
    ("ReleaseDetail", ("detected", "runtime")),
    ("ReleaseCreated", ("status",)),
    ("ReleaseUnchanged", ("status",)),
    ("ReleaseSourceListing", ("items", 0, "kind")),
    ("Requirement", ("state",)),
    ("RequirementsResponse", ("items", 0, "state")),
    ("ResourceView", ("access_as",)),
    ("ResourceView", ("check_result",)),
    ("AppGrant", ("type",)),
    ("AppGrant", ("permission",)),
    ("AppDetail", ("state",)),
    ("AppDetail", ("runtime",)),
    ("AppDetail", ("releases", 0, "status")),
    ("AppDetail", ("config", "desired_state")),
    ("AppDetail", ("resources", 0, "check_result")),
    ("AppDetail", ("grants", 0, "type")),
    ("AppDetail", ("grants", 0, "permission")),
    ("AppDetail", ("egress", 0, "action")),
    ("RequestRow", ("kind",)),
    ("RequestList", ("items", 0, "kind")),
    ("PlatformView", ("settings", "package_mode")),
    ("PlatformView", ("admins", 0, "type")),
    ("AdminList", ("items", 0, "type")),
    ("EgressRules", ("items", 0, "action")),
    ("AppLogs", ("state",)),
    ("AppLogs", ("lines", 0, "stream")),
    ("AppEnvironment", ("runtime", "kind")),
    ("AppEnvironment", ("variables", 0, "source")),
    ("AppEnvironment", ("packages", 0, "source")),
)


@pytest.mark.parametrize(
    ("name", "path"),
    CLOSED_LIST_FIELDS,
    ids=[f"{name}.{'.'.join(str(step) for step in path)}" for name, path in CLOSED_LIST_FIELDS],
)
def test_a_response_field_outside_its_closed_list_is_refused(
    name: str, path: tuple[str | int, ...]
) -> None:
    example = json.loads(example_text(name))
    assert isinstance(at(example, path), str), "the example holds a value to replace"
    assert accepted(name, example)
    for outside in ("bogus", ""):
        assert not accepted(name, with_value(example, path, outside)), f"{path} = {outside!r}"


def test_a_request_status_outside_its_list_is_refused() -> None:
    # Closed, so that the pending-or-closed rule between the fields does not refuse it first.
    closed = json.loads(example_text("RequestRow")) | {
        "closed_by": "alice@example.com",
        "closed_at": "2026-10-30T10:00:00Z",
    }
    for status in ("approved", "denied", "withdrawn"):
        assert accepted("RequestRow", closed | {"status": status}), status
    for outside in ("bogus", "", "Approved"):
        assert not accepted("RequestRow", closed | {"status": outside}), outside


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_a_model_is_frozen(name: str) -> None:
    model = model_named(name).model_validate_json(example_text(name))
    field = next(iter(type(model).model_fields))
    with pytest.raises(ValidationError):
        setattr(model, field, getattr(model, field))


def test_a_model_can_be_built_by_attribute_name_and_read_back_by_wire_name() -> None:
    # `from` is a Python keyword: the attribute is `from_`, and both spellings build the model.
    window = {"to": datetime(2026, 10, 31, tzinfo=UTC), "items": []}
    by_name = schemas.AuditList(from_=datetime(2026, 10, 30, tzinfo=UTC), **window)
    by_alias = schemas.AuditList.model_validate(
        {"from": datetime(2026, 10, 30, tzinfo=UTC)} | window
    )
    assert by_name == by_alias
    assert by_name.model_dump(mode="json")["from"] == "2026-10-30T00:00:00Z"


@pytest.mark.parametrize("name", REQUEST_NAMES)
def test_a_request_refuses_a_nul_character_in_any_string_at_any_depth(name: str) -> None:
    # Postgres text and jsonb cannot hold NUL. Every string of a request example, free-form
    # payload strings included, is refused with it, and the refusal says why.
    example = json.loads(example_text(name))
    paths = list(string_paths(example))
    if name in EXAMPLES_WITHOUT_A_STRING:
        assert not paths
    else:
        assert paths
    for path in paths:
        body = with_value(example, path, at(example, path) + "\x00")
        with pytest.raises(ValidationError) as caught:
            model_named(name).model_validate_json(json.dumps(body))
        assert "must not contain a NUL character" in str(caught.value), f"at {path}"


def test_every_example_is_checked_by_every_mutation_test() -> None:
    # The mutation tests above are parametrised over the manifest itself, so a new example is
    # mutated the day it is added; this pins that the parametrisation is not a sample.
    assert sorted(MANIFEST) == sorted(contract_sections())
    assert len(MANIFEST) == len(example_files())
