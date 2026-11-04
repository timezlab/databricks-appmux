"""The shared-SP residual of ``test_credential_modes.py`` is spelled as the snooper reports it.

``tests/integration/test_credential_modes.py`` compares the ``succeeded`` entries of the hostile
fixture ``secret_registry_snooper`` with ``ACCEPTED_SHARED_SP_RESIDUAL``. Both are strings of the
form ``"<case>: <attempt>"``, written in two files that never import each other. The first run
against the dev App (2026-11-04) found the constant spelled ``with_token: registry login`` while
the fixture reports ``with token: registry login``, so the accepted attempt was counted as a breach
and the test could not pass even when only the accepted attempt succeeded. This test runs the
fixture's own ``report()`` with every network call replaced and checks the two agree. The files are
loaded by path because the test and fixture directories are not packages.

``PLATFORM_VISIBILITY`` is the second constant of that kind: the platform lets every principal list
the names of the workspace's secret scopes, so ``with token: list_scopes`` is not a breach, and the
fixture's own flag (``with_token_contained``) stays strict and reads false when it succeeds.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
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


REFUSED = {"outcome": "refused", "detail": "stand-in"}
SUCCEEDED = {"outcome": "succeeded", "detail": "stand-in"}


@dataclass(frozen=True, slots=True)
class Loaded:
    snooper: ModuleType
    credential_modes: ModuleType


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> Loaded:
    """Both files, with the fixture's network calls replaced by refusals the test can override."""
    monkeypatch.syspath_prepend(str(INTEGRATION_DIR))
    stand_in = ModuleType("fastapi")
    stand_in.FastAPI = FastApiStandIn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastapi", stand_in)
    credential_modes = load(CREDENTIAL_MODES, "credential_modes_under_test")
    snooper = load(SNOOPER, "secret_registry_snooper_under_test")
    monkeypatch.setattr(snooper, "workspace_url", lambda: "https://workspace.example.invalid")
    monkeypatch.setattr(snooper, "delivered_credential", lambda: ("stand-in", "an-identity"))
    monkeypatch.setattr(snooper, "try_registry_login", lambda user, password: REFUSED)
    return Loaded(snooper, credential_modes)


def test_the_accepted_registry_login_is_what_the_snooper_reports_for_it(
    loaded: Loaded, monkeypatch: pytest.MonkeyPatch
) -> None:
    snooper = loaded.snooper
    monkeypatch.setattr(
        snooper, "try_secrets", lambda host_url, authorization: {"list_scopes": REFUSED}
    )
    monkeypatch.setattr(
        snooper, "try_registry_login", lambda user, password: SUCCEEDED if password else REFUSED
    )

    report = snooper.report()

    assert report["without_token_contained"] is True
    assert set(report["succeeded"]) == loaded.credential_modes.ACCEPTED_SHARED_SP_RESIDUAL


def test_a_refusal_that_alternates_with_a_success_has_not_settled(loaded: Loaded) -> None:
    # The platform answers a read for minutes after a revoke by alternating 200 and 403
    # (measured 2026-11-04: 22 changes in 281 s). The first refusal must not end the wait.
    watch = loaded.credential_modes.RefusalWatch(stable_for_s=60.0)

    assert watch.see("refused", now=0.0) is False
    assert watch.see("succeeded", now=10.0) is False
    assert watch.see("refused", now=20.0) is False
    assert watch.see("refused", now=79.0) is False  # 59 s since the refusal that began the spell


def test_a_refusal_settles_once_it_has_lasted_the_whole_window(loaded: Loaded) -> None:
    watch = loaded.credential_modes.RefusalWatch(stable_for_s=60.0)

    assert watch.see("succeeded", now=0.0) is False
    assert watch.see("refused", now=5.0) is False
    assert watch.see("refused", now=65.0) is True


def test_a_report_without_the_read_does_not_settle_a_watch(loaded: Loaded) -> None:
    # "absent from the report" is what read_outcome gives when the snooper tried nothing.
    watch = loaded.credential_modes.RefusalWatch(stable_for_s=0.0)

    assert watch.see("absent from the report", now=0.0) is False


def test_the_platform_visibility_is_what_the_snooper_reports_for_a_scope_listing(
    loaded: Loaded, monkeypatch: pytest.MonkeyPatch
) -> None:
    snooper = loaded.snooper
    with_token = {"list_scopes": SUCCEEDED, "read some-scope/some-key": REFUSED}
    without_token = {"list_scopes": REFUSED, "read some-scope/some-key": REFUSED}
    monkeypatch.setattr(
        snooper,
        "try_secrets",
        lambda host_url, authorization: with_token if authorization else without_token,
    )

    report = snooper.report()

    assert set(report["succeeded"]) == loaded.credential_modes.PLATFORM_VISIBILITY
    assert report["with_token_contained"] is False
