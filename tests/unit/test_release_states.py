"""Release states and the one place routes are set (FR-005, constitution II, T076).

The state table is data, so the tests walk it: every legal edge, every other pair, and the two
rules that need a reason (a ``failed`` release is never picked up by the reconciler, but a person
can activate it). The route table tests prove that a route is replaced in one step and that no
module but the swap may call the setters, by reading the source of ``src/appmux``.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path
from typing import get_args

import pytest

from appmux.reconciler.route_table import Route, RouteTable
from appmux.reconciler.states import (
    RELEASE_STATUSES,
    IllegalTransition,
    can_transition,
    check_transition,
    is_picked_up_automatically,
)
from appmux.registry.models import Release

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "appmux"

# --- the table ----------------------------------------------------------------------------------


def test_the_statuses_are_those_of_the_release_row() -> None:
    column = Release.model_fields["status"].annotation
    assert column is not None
    assert set(RELEASE_STATUSES) == set(get_args(column))


def test_the_happy_path_is_pending_building_starting_healthy_active() -> None:
    path = ["pending", "building", "starting", "healthy", "active"]
    for old, new in itertools.pairwise(path):
        check_transition(old, new)


def test_a_replaced_active_release_becomes_superseded() -> None:
    check_transition("active", "superseded")


@pytest.mark.parametrize("old", ["pending", "building", "starting", "healthy", "active"])
def test_any_live_step_may_fail_with_a_reason(old: str) -> None:
    # 'active' fails when a restart finds a requirement the environment no longer holds.
    assert can_transition(old, "failed")


@pytest.mark.parametrize("old", ["building", "starting", "healthy"])
def test_a_release_interrupted_before_it_is_active_returns_to_pending(old: str) -> None:
    check_transition(old, "pending")


ALLOWED = {
    ("pending", "building"),
    ("pending", "failed"),
    ("building", "starting"),
    ("building", "failed"),
    ("building", "pending"),
    ("starting", "healthy"),
    ("starting", "failed"),
    ("starting", "pending"),
    ("healthy", "active"),
    ("healthy", "failed"),
    ("healthy", "pending"),
    ("active", "superseded"),
    ("active", "failed"),
}
MANUAL_ONLY = {("failed", "pending"), ("superseded", "pending")}


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pair
        for pair in itertools.product(RELEASE_STATUSES, repeat=2)
        if pair not in ALLOWED and pair not in MANUAL_ONLY
    ],
)
def test_every_other_transition_raises(old: str, new: str) -> None:
    assert not can_transition(old, new)
    with pytest.raises(IllegalTransition) as caught:
        check_transition(old, new)
    assert old in str(caught.value)
    assert new in str(caught.value)


@pytest.mark.parametrize(("old", "new"), sorted(ALLOWED))
def test_every_listed_transition_is_allowed(old: str, new: str) -> None:
    assert can_transition(old, new)
    check_transition(old, new)


def test_an_unknown_status_raises_instead_of_being_treated_as_a_state() -> None:
    with pytest.raises(IllegalTransition):
        check_transition("pending", "paused")
    with pytest.raises(IllegalTransition):
        check_transition("paused", "pending")


# --- failed is never retried automatically, but can be activated by hand ------------------------


@pytest.mark.parametrize(("old", "new"), sorted(MANUAL_ONLY))
def test_a_failed_or_superseded_release_is_restarted_only_by_hand(old: str, new: str) -> None:
    assert not can_transition(old, new)
    with pytest.raises(IllegalTransition) as caught:
        check_transition(old, new)
    assert "by hand" in str(caught.value)
    check_transition(old, new, by_hand=True)
    assert can_transition(old, new, by_hand=True)


def test_by_hand_opens_no_other_edge() -> None:
    for old, new in itertools.product(RELEASE_STATUSES, repeat=2):
        if (old, new) in ALLOWED or (old, new) in MANUAL_ONLY:
            continue
        assert not can_transition(old, new, by_hand=True)


def test_only_a_pending_release_is_picked_up_by_the_reconciler() -> None:
    picked = {status for status in RELEASE_STATUSES if is_picked_up_automatically(status)}
    assert picked == {"pending"}


# --- the route table ----------------------------------------------------------------------------

SOCKET = Path("/run/appmux-example/sales.sock")


def route(release_no: int = 1, socket: Path = SOCKET) -> Route:
    return Route(release_no=release_no, socket_path=socket)


def test_an_app_has_no_route_until_one_is_opened() -> None:
    table = RouteTable()
    assert table.lookup("sales") is None
    assert table.routes() == {}


def test_opening_a_route_makes_it_visible() -> None:
    table = RouteTable()
    table.open_route("sales", route())
    assert table.lookup("sales") == route()


def test_opening_again_replaces_the_route_in_one_step() -> None:
    table = RouteTable()
    table.open_route("sales", route(1))
    seen_before = table.routes()
    table.open_route("sales", route(2, Path("/run/appmux-example/sales-2.sock")))
    assert table.lookup("sales") == route(2, Path("/run/appmux-example/sales-2.sock"))
    # a reader that took the table before the swap still holds a complete, unchanged picture
    assert seen_before == {"sales": route(1)}


def test_replacing_one_route_leaves_every_other_untouched() -> None:
    table = RouteTable()
    table.open_route("sales", route(1))
    table.open_route("hr", route(5, Path("/run/appmux-example/hr.sock")))
    table.open_route("sales", route(2))
    assert table.lookup("hr") == route(5, Path("/run/appmux-example/hr.sock"))


def test_closing_a_route_removes_only_that_app() -> None:
    table = RouteTable()
    table.open_route("sales", route(1))
    table.open_route("hr", route(5, Path("/run/appmux-example/hr.sock")))
    table.close_route("sales")
    assert table.lookup("sales") is None
    assert table.lookup("hr") is not None


def test_closing_an_app_with_no_route_is_not_an_error() -> None:
    RouteTable().close_route("sales")


@pytest.mark.parametrize("slug", ["", "Sales", "-sales", "sa les", "sales/x", "_appmux", "a" * 32])
def test_a_slug_that_is_not_a_slug_cannot_have_a_route(slug: str) -> None:
    table = RouteTable()
    with pytest.raises(ValueError, match="slug"):
        table.open_route(slug, route())
    assert table.routes() == {}


def test_the_table_handed_to_a_reader_cannot_be_used_to_change_it() -> None:
    table = RouteTable()
    table.open_route("sales", route())
    picture = table.routes()
    with pytest.raises(TypeError):
        picture["hr"] = route()
    assert table.lookup("hr") is None


# --- constitution II: only the swap opens or closes a route -------------------------------------

SETTERS = frozenset({"open_route", "close_route"})
# The files that may call a setter. The table defines them; the swap (T104) is the only caller,
# because only the swap runs after a health check. Add a file here only with an ADR.
MAY_CALL_SETTERS = frozenset({"reconciler/route_table.py", "reconciler/swap.py"})


def calls_a_setter(source: str) -> list[str]:
    """Names of the setters ``source`` calls, as ``x.open_route(...)`` or ``open_route(...)``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else None
            )
            if name in SETTERS:
                found.append(name)
    return found


def test_the_scan_sees_a_call_however_it_is_written() -> None:
    assert calls_a_setter("table.open_route('a', r)") == ["open_route"]
    assert calls_a_setter("self._routes.close_route('a')") == ["close_route"]
    assert calls_a_setter("open_route('a', r)") == ["open_route"]
    assert calls_a_setter("x = table.lookup('a')") == []


def test_no_module_but_the_swap_calls_a_route_setter() -> None:
    offenders = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        if relative in MAY_CALL_SETTERS:
            continue
        names = calls_a_setter(path.read_text(encoding="utf-8"))
        if names:
            offenders[relative] = names
    assert offenders == {}, "only reconciler/swap.py may open or close a route (constitution II)"
