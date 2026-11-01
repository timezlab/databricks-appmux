"""``edge/gate.py`` imports only the standard library (T025).

The gate is the ASGI application uvicorn starts with, so the port is bound before anything slow
loads. A module that pulled in pydantic, starlette or psycopg would delay the bind and the
platform's probe of ``/`` would find no listener. This test imports the module in a fresh
interpreter whose import system refuses every module outside the standard library, then proves the
refusal works by asking the same interpreter for a third-party module.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Final

SOURCE_ROOT: Final = Path(__file__).resolve().parents[2] / "src"

# Run with ``-I`` (no user site, no PYTHONPATH); the source root is passed as an argument. The hook
# sits first on ``sys.meta_path``; the only appmux modules it lets through are the package, the
# edge package (docstring only) and the gate itself.
_PROBE: Final = """
import importlib
import sys

source_root, target = sys.argv[1], sys.argv[2]
sys.path.insert(0, source_root)
ALLOWED_APPMUX = {"appmux", "appmux.edge", "appmux.edge.gate"}


class StdlibOnly:
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top in sys.stdlib_module_names or name in ALLOWED_APPMUX:
            return None
        raise ImportError(f"forbidden import: {name}")


sys.meta_path.insert(0, StdlibOnly())
importlib.import_module(target)
outside = sorted(
    name
    for name in sys.modules
    if name.split(".")[0] not in sys.stdlib_module_names
    and name not in ALLOWED_APPMUX
    and not name.startswith("_")
)
print("OUTSIDE:" + ",".join(outside))
"""


def _run_probe(target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(SOURCE_ROOT), target],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_gate_imports_only_the_standard_library() -> None:
    result = _run_probe("appmux.edge.gate")

    assert result.returncode == 0, result.stderr
    assert "OUTSIDE:" in result.stdout
    assert result.stdout.strip().endswith("OUTSIDE:")  # nothing outside the standard library


def test_the_probe_refuses_a_module_outside_the_standard_library() -> None:
    # Without this, a broken hook would let the test above pass for the wrong reason.
    result = _run_probe("pydantic")

    assert result.returncode != 0
    assert "forbidden import: pydantic" in result.stderr


def test_the_probe_refuses_another_appmux_module() -> None:
    result = _run_probe("appmux.settings")

    assert result.returncode != 0
    assert "forbidden import" in result.stderr
