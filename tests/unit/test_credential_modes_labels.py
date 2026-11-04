"""The shared-SP residual of ``test_credential_modes.py`` is spelled as the snooper reports it.

``tests/integration/test_credential_modes.py`` compares the ``succeeded`` entries of the hostile
fixture ``secret_registry_snooper`` with ``ACCEPTED_SHARED_SP_RESIDUAL``. Both are strings of the
form ``"<case>: <attempt>"``, written in two files that never import each other. The first run
against the dev App (2026-11-04) found the constant spelled ``with_token: registry login`` while
the fixture reports ``with token: registry login``, so the accepted attempt was counted as a breach
and the test could not pass even when only the accepted attempt succeeded. This test runs the
fixture's own ``report()`` with every network call replaced and checks the two agree. The files are
loaded by path because the test and fixture directories are not packages.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SNOOPER = REPO / "tests" / "fixtures" / "secret_registry_snooper" / "app.py"
CREDENTIAL_MODES = REPO / "tests" / "integration" / "test_credential_modes.py"
INTEGRATION_DIR = CREDENTIAL_MODES.parent


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FastApiStandIn:
    """Enough of ``fastapi.FastAPI`` for the fixture to import: routes stay plain functions.

    The fixture's own requirements install FastAPI inside the sandbox; the dev environment does not
    carry it, and a unit test is no reason to add it.
    """

    def __init__(self, *_: object, **__: object) -> None:
        pass

    def get(
        self, *_: object, **__: object
    ) -> Callable[[Callable[..., object]], Callable[..., object]]:
        return lambda function: function


def test_the_accepted_registry_login_is_what_the_snooper_reports_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(INTEGRATION_DIR))
    stand_in = ModuleType("fastapi")
    stand_in.FastAPI = FastApiStandIn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastapi", stand_in)
    credential_modes = load(CREDENTIAL_MODES, "credential_modes_under_test")
    snooper = load(SNOOPER, "secret_registry_snooper_under_test")
    refused = {"outcome": "refused", "detail": "stand-in"}
    succeeded = {"outcome": "succeeded", "detail": "stand-in"}
    monkeypatch.setattr(snooper, "workspace_url", lambda: "https://workspace.example.invalid")
    monkeypatch.setattr(snooper, "delivered_credential", lambda: ("stand-in", "an-identity"))
    monkeypatch.setattr(
        snooper, "try_secrets", lambda host_url, authorization: {"list_scopes": refused}
    )
    monkeypatch.setattr(
        snooper,
        "try_registry_login",
        lambda user, password: succeeded if password else refused,
    )

    report = snooper.report()

    assert report["without_token_contained"] is True
    assert set(report["succeeded"]) == credential_modes.ACCEPTED_SHARED_SP_RESIDUAL
