"""The closed lists of the workspace-browser and resource-suggestion schemas (T194, T195).

``test_admin_schema_examples.py`` mutates every example for shape (a missing or an unlisted field);
what it cannot know is which string fields are one of a closed list in the contract. A value outside
the list is a route defect, and the response model refuses it instead of sending it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel, ValidationError

from appmux.admin import schemas

EXAMPLES_DIR: Final = (
    Path(__file__).resolve().parents[2] / "specs" / "001-appmux-v1" / "contracts" / "examples"
)

# (schema, path to the field in its example)
CLOSED_LIST_FIELDS: Final[tuple[tuple[str, tuple[str | int, ...]], ...]] = (
    ("WorkspaceListing", ("entries", 0, "kind")),
    ("WorkspaceListing", ("markers", 0)),
    ("WorkspaceListing", ("runtime_hint",)),
    ("ResourceTypeList", ("items", 1, "access_as", 0)),
    ("ResourceSuggestionList", ("source",)),
)


def _example(name: str) -> Any:
    return json.loads((EXAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _model(name: str) -> type[BaseModel]:
    model = getattr(schemas, name)
    assert isinstance(model, type) and issubclass(model, BaseModel)
    return model


def _with_value(value: Any, path: tuple[str | int, ...], replacement: object) -> Any:
    mutated = copy.deepcopy(value)
    target = mutated
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = replacement
    return mutated


def _accepted(name: str, body: object) -> bool:
    try:
        _model(name).model_validate_json(json.dumps(body))
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize(
    ("name", "path"),
    CLOSED_LIST_FIELDS,
    ids=[f"{name}.{'.'.join(str(step) for step in path)}" for name, path in CLOSED_LIST_FIELDS],
)
def test_a_value_outside_its_closed_list_is_refused(name: str, path: tuple[str | int, ...]) -> None:
    example = _example(name)
    assert _accepted(name, example)
    for outside in ("bogus", "", "Folder"):
        assert not _accepted(name, _with_value(example, path, outside)), f"{path} = {outside!r}"


def test_a_runtime_hint_may_be_null_and_a_secret_may_allow_no_access_mode() -> None:
    listing = _example("WorkspaceListing")
    assert _accepted(
        "WorkspaceListing", listing | {"runtime_hint": None, "suggested_memory_mb": None}
    )
    types = _example("ResourceTypeList")
    assert types["items"][0]["access_as"] == []
    assert _accepted("ResourceTypeList", types)


def test_the_roots_listing_is_a_valid_response() -> None:
    roots = {
        "path": None,
        "parent": None,
        "roots": [
            {"label": "My folder", "path": "/Workspace/Users/alice@example.com"},
            {"label": "Shared", "path": "/Workspace/Shared"},
        ],
        "entries": [],
        "truncated": False,
        "markers": [],
        "looks_like_app": False,
        "runtime_hint": None,
        "suggested_memory_mb": None,
    }
    assert _accepted("WorkspaceListing", roots)
