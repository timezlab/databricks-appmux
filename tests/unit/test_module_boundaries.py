"""The dependency direction of design 3.4, enforced on the import graph.

Parses every file under ``src/appmux`` with ``ast`` and fails on an import that
points against the "Must not" column of the design's module table. It passes on
the tree as it is and guards every later task.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "appmux"
PACKAGE_NAME = "appmux"

# Libraries that perform I/O. ``authz`` and ``routing`` are pure and tested as tables.
IO_LIBRARIES = frozenset(
    {
        "httpx",
        "psycopg",
        "websockets",
        "uvicorn",
        "starlette",
        "databricks",
        "socket",
        "subprocess",
        "urllib",
        "http",
        "aiofiles",
    }
)

# source module -> top-level names it must not import (appmux modules or libraries).
FORBIDDEN_IMPORTS: dict[str, frozenset[str]] = {
    "identity": frozenset({"routing", "reconciler", "registry", "admin"}),
    "authz": IO_LIBRARIES | {"registry", "upstream"},
    "routing": IO_LIBRARIES | {"registry", "upstream"},
    "rewrite": frozenset({"routing", "authz"}),
    "upstream": frozenset({"registry"}),
    "reconciler": frozenset({"edge", "admin", "starlette", "uvicorn"}),
    "sandbox": frozenset({"reconciler", "registry", "packages", "appyaml"}),
    "packages": frozenset({"subprocess"}),
    "appyaml": frozenset({"subprocess"}),
    "registry": frozenset({"edge"}),
    "admin": frozenset({"edge"}),
}

# imported appmux module -> the only source modules that may import it.
ONLY_IMPORTED_BY: dict[str, frozenset[str]] = {
    "edge": frozenset({"__main__", "edge"}),
}


def module_of(path: Path) -> str:
    """The design module a file belongs to: its folder, or its stem at the package root."""
    relative = path.relative_to(PACKAGE_ROOT)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def imported_names(source: str, path_parts: tuple[str, ...]) -> set[str]:
    """Top-level names a file imports; names inside ``appmux`` are the module folder or file.

    ``path_parts`` is the file's path below ``src/appmux`` (``("authz", "policy.py")``).
    """
    package_parts = [PACKAGE_NAME, *path_parts[:-1]]
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(_appmux_module(alias.name.split(".")) or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_base(node, package_parts)
            if base[:1] == [PACKAGE_NAME]:
                if len(base) > 1:
                    names.add(base[1])
                else:
                    names.update(alias.name for alias in node.names)
            elif base:
                names.add(base[0])
    return names


def _appmux_module(parts: list[str]) -> str | None:
    return parts[1] if parts[0] == PACKAGE_NAME and len(parts) > 1 else None


def _absolute_base(node: ast.ImportFrom, package_parts: list[str]) -> list[str]:
    module_parts = node.module.split(".") if node.module else []
    if node.level == 0:
        return module_parts
    anchor = package_parts[: len(package_parts) - (node.level - 1)]
    return [*anchor, *module_parts]


def violations(source: str, path_parts: tuple[str, ...]) -> list[str]:
    """Human-readable violations for one file; empty when the file respects the table."""
    module = path_parts[0].removesuffix(".py")
    imports = imported_names(source, path_parts)
    found = [
        f"{module} must not import {name}"
        for name in sorted(imports & FORBIDDEN_IMPORTS.get(module, frozenset()))
    ]
    for name, allowed in ONLY_IMPORTED_BY.items():
        if name in imports and module not in allowed:
            found.append(f"only {sorted(allowed)} may import {name}, not {module}")
    return found


def source_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


@pytest.mark.parametrize("path", source_files(), ids=lambda p: str(p.relative_to(PACKAGE_ROOT)))
def test_file_respects_module_boundaries(path: Path) -> None:
    relative = path.relative_to(PACKAGE_ROOT)
    assert violations(path.read_text(encoding="utf-8"), relative.parts) == []


@pytest.mark.parametrize(
    ("path_parts", "source", "expected"),
    [
        (("authz", "policy.py"), "import httpx\n", "authz must not import httpx"),
        (
            ("authz", "policy.py"),
            "from appmux.registry import client\n",
            "authz must not import registry",
        ),
        (
            ("authz", "policy.py"),
            "from ..registry import client\n",
            "authz must not import registry",
        ),
        (("authz", "policy.py"), "from .. import upstream\n", "authz must not import upstream"),
        (("routing", "prefix.py"), "import socket\n", "routing must not import socket"),
        (
            ("rewrite", "request.py"),
            "from appmux.routing import prefix\n",
            "rewrite must not import routing",
        ),
        (
            ("rewrite", "request.py"),
            "import appmux.authz.policy\n",
            "rewrite must not import authz",
        ),
        (
            ("upstream", "client.py"),
            "from ..registry.models import AppRow\n",
            "upstream must not import registry",
        ),
        (
            ("sandbox", "launcher.py"),
            "from appmux.packages import overlay\n",
            "sandbox must not import packages",
        ),
        (
            ("sandbox", "launcher.py"),
            "from appmux import appyaml\n",
            "sandbox must not import appyaml",
        ),
        (
            ("registry", "client.py"),
            "from appmux.edge import app\n",
            "only ['__main__', 'edge'] may import edge, not registry",
        ),
        (
            ("admin", "api.py"),
            "from ..edge import gate\n",
            "only ['__main__', 'edge'] may import edge, not admin",
        ),
        (
            ("settings.py",),
            "from appmux.edge import gate\n",
            "only ['__main__', 'edge'] may import edge, not settings",
        ),
        (("packages", "overlay.py"), "import subprocess\n", "packages must not import subprocess"),
    ],
)
def test_checker_detects_a_violation(
    path_parts: tuple[str, ...], source: str, expected: str
) -> None:
    assert expected in violations(source, path_parts)


@pytest.mark.parametrize(
    ("path_parts", "source"),
    [
        (("__main__.py",), "from appmux.edge import app\n"),
        (("edge", "app.py"), "from appmux.registry import client\nfrom .gate import Gate\n"),
        (
            ("authz", "policy.py"),
            "from __future__ import annotations\nfrom dataclasses import dataclass\n",
        ),
        (("registry", "snapshot.py"), "import psycopg\nfrom appmux.authz import data\n"),
        (("sandbox", "launcher.py"), "import subprocess\nfrom .rules import Ruleset\n"),
    ],
)
def test_checker_allows_a_legal_import(path_parts: tuple[str, ...], source: str) -> None:
    assert violations(source, path_parts) == []
