"""Checks on the deploy notebook that its runs against a fake workspace cannot make.

The notebook runs on a real workspace, so two kinds of mistake only show up there. Both were met
on 2026-11-06, in the first run:

* ``USER_API_SCOPES`` listed ``iam.current-user:read``: the platform adds its default identity
  scopes itself and refuses them in ``user_api_scopes`` (the platform reference, "OBO scopes").
* The schema loader failed on Databricks Runtime 15.4, which ships pydantic 1.10; the loader
  imports ``AwareDatetime`` and needs pydantic 2, so cell 9 adds it to the loader's own folder.

The tests read the notebook's source, so neither can come back unnoticed.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Final

REPO: Final = Path(__file__).resolve().parents[2]
NOTEBOOK: Final = REPO / "scripts" / "notebooks" / "deploy-appmux.ipynb"

# Names ``user_api_scopes`` refuses: the two the platform grants by default (spike notes on the
# dev App, confirmed by the first real run) and the seven spikes 15 and 16 found refused.
REFUSED_SCOPES: Final = frozenset(
    {
        "iam.access-control:read",
        "iam.current-user:read",
        "secrets",
        "mlflow",
        "jobs",
        "access-management",
        "workspace",
        "unity-catalog",
        "catalog.volumes",
    }
)


def notebook_statements() -> list[ast.stmt]:
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    statements: list[ast.stmt] = []
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            statements.extend(ast.parse("".join(cell["source"])).body)
    return statements


def notebook_constant(name: str) -> Any:
    for statement in notebook_statements():
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
    raise AssertionError(f"{NOTEBOOK.name} has no {name} assignment in a code cell")


def notebook_user_api_scopes() -> list[str]:
    scopes = notebook_constant("USER_API_SCOPES")
    assert isinstance(scopes, list)
    return [str(scope) for scope in scopes]


def test_the_notebook_asks_for_no_scope_the_platform_refuses() -> None:
    refused = sorted(set(notebook_user_api_scopes()) & REFUSED_SCOPES)

    assert refused == []


def test_the_notebook_still_asks_for_the_scopes_the_attach_checks_need() -> None:
    # Guards the test above against passing because the list was emptied or the parse found none.
    scopes = notebook_user_api_scopes()

    assert {"sql", "workspace.workspace:read", "postgres"} <= set(scopes)
    assert len(scopes) == len(set(scopes))


def loader_pydantic_is_usable(version: str | None) -> bool:
    """The notebook's own function, compiled from its source so the test cannot drift from it."""
    namespace: dict[str, Any] = {"re": re}
    floor = notebook_constant("LOADER_PYDANTIC_FLOOR")
    namespace["LOADER_PYDANTIC_FLOOR"] = floor
    for statement in notebook_statements():
        if isinstance(statement, ast.FunctionDef) and statement.name == "loader_pydantic_is_usable":
            exec(  # noqa: S102 - the repository's own notebook source, not external input
                compile(ast.Module(body=[statement], type_ignores=[]), str(NOTEBOOK), "exec"),
                namespace,
            )
            return bool(namespace["loader_pydantic_is_usable"](version))
    raise AssertionError(f"{NOTEBOOK.name} has no loader_pydantic_is_usable function")


def test_the_loader_rejects_a_missing_or_pydantic_1_install() -> None:
    assert loader_pydantic_is_usable(None) is False  # nothing importable
    assert loader_pydantic_is_usable("1.10.6") is False  # Databricks Runtime 15.4
    assert loader_pydantic_is_usable("2.0.3") is False  # measured: no StringConstraints


def test_the_loader_accepts_the_pydantic_2_versions_it_was_measured_on() -> None:
    assert loader_pydantic_is_usable("2.5.3") is True
    assert loader_pydantic_is_usable("2.13.4") is True
    assert loader_pydantic_is_usable("2.14.0b1") is True  # a pre-release string still parses


def test_the_notebook_installs_the_pydantic_version_the_project_pins() -> None:
    # One decision, two places: pyproject.toml pins what the Apps environment ships.
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    pins = [d for d in project["project"]["dependencies"] if d.startswith("pydantic==")]

    assert pins == [notebook_constant("LOADER_PYDANTIC_PIN")]
