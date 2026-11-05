"""The reserve arithmetic and the clean-up order of ``tests/integration/test_capacity.py`` (T181).

The capacity run lowers ``memory_reserve_mb`` for its length so that ten apps fit under the 64 MB
overhead each one is charged (TD-032), and puts it back at the end. Two things in it are logic, and
a mistake in either shows only on a container run that takes the whole App down for minutes:

* ``_reserve_that_leaves_room_for_ten`` returns how far to lower the reserve. A sign error would
  lower it by the wrong amount, or not at all, and the ten apps would be refused by the ledger the
  run exists to measure.
* The clean-up puts the reserve back and then deletes the ten apps. When putting the reserve back
  fails (an expired token is the likely cause), the apps must still be deleted, or ten running
  apps keep 5 GB of the slot until someone removes them by hand.

The module is loaded by path because the integration directory is not a package.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
CAPACITY = REPO / "tests" / "integration" / "test_capacity.py"
INTEGRATION_DIR = CAPACITY.parent

# The ten of the run, spelled out: 4 small apps of 256 MB, 2 + 2 of 512 MB, 2 Node apps of 768 MB,
# each charged 64 MB beyond its budget (TD-032), plus the 100 MB that must stay free after them.
TEN_APPS_WITH_OVERHEAD_MB = 4 * (256 + 64) + 4 * (512 + 64) + 2 * (768 + 64)
LEFT_FREE_AFTER_TEN_MB = 100
NEEDED_MB = TEN_APPS_WITH_OVERHEAD_MB + LEFT_FREE_AFTER_TEN_MB


@pytest.fixture
def capacity(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(INTEGRATION_DIR))
    name = "integration_test_capacity_under_test"
    spec = importlib.util.spec_from_file_location(name, CAPACITY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def ledger(free_mb: int) -> dict[str, int]:
    return {"free_mb": free_mb, "committed_mb": 3000, "limit_mb": 8000}


def test_the_ten_apps_are_what_the_test_adds_up(capacity: ModuleType) -> None:
    """The literal above and the module's own table agree, so the next tests mean something."""
    from_the_module = (
        sum(
            (kind.budget_mb + capacity.OVERHEAD_PER_CHILD_MB) * kind.copies
            for kind in capacity.KINDS
        )
        + capacity.FREE_AFTER_TEN_MB
    )
    assert from_the_module == NEEDED_MB == 5348


def test_a_reserve_is_left_alone_when_the_ten_already_fit(capacity: ModuleType) -> None:
    assert capacity._reserve_that_leaves_room_for_ten(ledger(NEEDED_MB), 1024) == 1024
    assert capacity._reserve_that_leaves_room_for_ten(ledger(NEEDED_MB + 500), 1024) == 1024


def test_the_reserve_is_lowered_by_exactly_the_shortfall(capacity: ModuleType) -> None:
    shortfall_mb = 300
    lowered = capacity._reserve_that_leaves_room_for_ten(ledger(NEEDED_MB - shortfall_mb), 1024)

    assert lowered == 1024 - shortfall_mb
    # free_mb = limit - reserve - committed: lowering the reserve raises it by the same amount.
    assert (NEEDED_MB - shortfall_mb) + (1024 - lowered) == NEEDED_MB


def test_a_shortfall_one_megabyte_past_the_reserve_is_refused_with_the_figures(
    capacity: ModuleType,
) -> None:
    with pytest.raises(AssertionError, match=r"too small to make up 1025 MB"):
        capacity._reserve_that_leaves_room_for_ten(ledger(NEEDED_MB - 1025), 1024)


def test_the_reserve_may_be_lowered_to_one_megabyte_and_no_further(capacity: ModuleType) -> None:
    assert capacity._reserve_that_leaves_room_for_ten(ledger(NEEDED_MB - 1023), 1024) == 1
    with pytest.raises(AssertionError):
        capacity._reserve_that_leaves_room_for_ten(ledger(NEEDED_MB - 1024), 1024)


@dataclass
class FakeSession:
    """What the clean-up touches: ``call`` (the PUT of the reserve) and ``delete_created_apps``."""

    put_fails: bool = False
    puts: list[Any] = field(default_factory=list)
    deleted_apps: bool = False

    def call(self, method: str, path: str, **kwargs: Any) -> None:
        self.puts.append((method, path, kwargs.get("json")))
        if self.put_fails:
            raise RuntimeError("the token has expired")

    def delete_created_apps(self) -> None:
        self.deleted_apps = True


def test_the_reserve_is_put_back_and_the_apps_are_deleted(capacity: ModuleType) -> None:
    session = FakeSession()

    capacity._restore_reserve_and_delete_apps(session, 1024)

    assert session.puts == [("PUT", "/platform/settings/memory_reserve_mb", {"value": 1024})]
    assert session.deleted_apps


def test_the_apps_are_deleted_even_when_the_reserve_cannot_be_put_back(
    capacity: ModuleType,
) -> None:
    session = FakeSession(put_fails=True)

    with pytest.raises(RuntimeError, match="the token has expired"):
        capacity._restore_reserve_and_delete_apps(session, 1024)

    assert session.deleted_apps, "ten running apps would stay on the App"
