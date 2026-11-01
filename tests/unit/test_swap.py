"""The swap (T098, T104; design 5.5, FR-004): the one place a route opens or closes.

A healthy new copy replaces the running one in two steps that are tested separately: ``activate``
(one registry transaction, then one change of the route table) and ``retire`` (drain 30 s, close
the WebSockets still open, stop the old copy). The registry is the in-memory one; the clock is a
fake, so the 30 s of the drain pass without waiting.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from reconciler_fakes import (
    SOCKET_DIRECTORY,
    FakeChild,
    FakeClock,
    FakeCloser,
    seed_app,
    seed_release,
    stub_registry_class,
)

from appmux.reconciler.route_table import Route, RouteTable
from appmux.reconciler.states import IllegalTransition
from appmux.reconciler.swap import DRAIN_S, LiveCopy, Swap
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio


class CountingRoutes(RouteTable):
    """A route table that remembers every write, and what the registry said at that moment."""

    def __init__(self, registry: object) -> None:
        super().__init__()
        self.registry = registry
        self.opened: list[tuple[str, Route, list[str]]] = []
        self.closed: list[str] = []

    def open_route(self, slug: str, route: Route) -> None:
        statuses = [r.status for r in self.registry.snapshot.releases_for(slug)]  # type: ignore[attr-defined]
        self.opened.append((slug, route, statuses))
        super().open_route(slug, route)

    def close_route(self, slug: str) -> None:
        self.closed.append(slug)
        super().close_route(slug)


class Books:
    """An app with the given releases, a swap over them and a copy per release."""

    def __init__(self, statuses: dict[int, str]) -> None:
        self.registry = stub_registry_class()()
        seed_app(self.registry)
        for number, status in statuses.items():
            seed_release(self.registry, "sales", number, status)
        self.clock = FakeClock()
        self.routes = CountingRoutes(self.registry)
        self.closer = FakeCloser(self.clock)
        self.swap = Swap(
            registry=self.registry, routes=self.routes, closer=self.closer, clock=self.clock
        )
        self.children: dict[int, FakeChild] = {}

    def copy(self, number: int) -> LiveCopy:
        child = self.children.setdefault(
            number, FakeChild(SOCKET_DIRECTORY / f"sales-{number}.sock")
        )
        return LiveCopy(self.registry.snapshot.release("sales", number), child)

    def statuses(self) -> dict[int, str]:
        return {r.release_no: r.status for r in self.registry.snapshot.releases_for("sales")}


# --- activate: one transaction, one route change ------------------------------------------------


async def test_the_swap_changes_the_route_once_and_the_registry_in_one_transaction() -> None:
    books = Books({1: "active", 2: "healthy"})
    books.routes.open_route("sales", Route(1, SOCKET_DIRECTORY / "sales-1.sock"))
    books.routes.opened.clear()
    writes_before = len(books.registry.writes)

    await books.swap.activate(books.copy(2))

    assert books.statuses() == {1: "superseded", 2: "active"}
    assert len(books.registry.writes) == writes_before + 1
    assert books.registry.writes[-1].kinds == (WriteKind.RELEASE_UPDATE, WriteKind.RELEASE_UPDATE)
    assert [(slug, route.release_no) for slug, route, _ in books.routes.opened] == [("sales", 2)]
    assert books.routes.lookup("sales") == Route(2, SOCKET_DIRECTORY / "sales-2.sock")


async def test_the_route_opens_only_after_the_registry_says_the_new_release_is_active() -> None:
    books = Books({1: "active", 2: "healthy"})

    await books.swap.activate(books.copy(2))

    ((_, _, statuses_at_the_swap),) = books.routes.opened
    assert statuses_at_the_swap == ["superseded", "active"]


async def test_the_first_activation_of_an_app_opens_its_route() -> None:
    books = Books({1: "healthy"})

    await books.swap.activate(books.copy(1))

    assert books.statuses() == {1: "active"}
    assert books.routes.lookup("sales") == Route(1, SOCKET_DIRECTORY / "sales-1.sock")


async def test_two_healthy_releases_one_after_the_other_leave_exactly_one_active() -> None:
    books = Books({1: "healthy"})
    await books.swap.activate(books.copy(1))
    seed_release(books.registry, "sales", 2, "healthy")
    await books.swap.activate(books.copy(2))
    seed_release(books.registry, "sales", 3, "healthy")

    await books.swap.activate(books.copy(3))

    assert books.statuses() == {1: "superseded", 2: "superseded", 3: "active"}
    assert books.routes.lookup("sales") == Route(3, SOCKET_DIRECTORY / "sales-3.sock")


async def test_a_registry_that_is_down_leaves_the_old_release_and_its_route_untouched() -> None:
    books = Books({1: "active", 2: "healthy"})
    old_route = Route(1, SOCKET_DIRECTORY / "sales-1.sock")
    books.routes.open_route("sales", old_route)
    books.routes.opened.clear()
    books.registry.go_down()

    with pytest.raises(RegistryUnavailable):
        await books.swap.activate(books.copy(2))

    books.registry.come_back()
    assert books.statuses() == {1: "active", 2: "healthy"}
    assert books.routes.lookup("sales") == old_route
    assert books.routes.opened == []


async def test_a_release_that_is_not_healthy_cannot_be_activated() -> None:
    books = Books({1: "active", 2: "starting"})

    with pytest.raises(IllegalTransition, match="cannot go from 'starting' to 'active'"):
        await books.swap.activate(books.copy(2))

    assert books.statuses() == {1: "active", 2: "starting"}
    assert books.routes.lookup("sales") is None


async def test_a_release_the_registry_no_longer_calls_active_is_not_superseded() -> None:
    books = Books({1: "failed", 2: "healthy"})

    await books.swap.activate(books.copy(2))

    assert books.statuses() == {1: "failed", 2: "active"}


async def test_the_active_release_is_found_in_the_registry_even_when_no_copy_of_it_runs() -> None:
    # After a crash the old copy may be gone while the registry still calls its release active.
    books = Books({1: "active", 2: "healthy"})

    await books.swap.activate(books.copy(2))

    assert books.statuses() == {1: "superseded", 2: "active"}


async def test_the_row_the_caller_tracked_is_the_one_activated_not_a_stale_snapshot() -> None:
    # The snapshot can lag behind a write whose reload failed; the copy carries the current row.
    books = Books({1: "active", 2: "starting"})
    healthy = books.copy(2).release.model_copy(update={"status": "healthy"})

    await books.swap.activate(
        LiveCopy(
            healthy, books.children.setdefault(2, FakeChild(SOCKET_DIRECTORY / "sales-2.sock"))
        )
    )

    assert books.statuses() == {1: "superseded", 2: "active"}


async def test_restoring_a_release_that_is_already_active_opens_its_route_and_writes_nothing() -> (
    None
):
    books = Books({1: "active"})
    writes_before = len(books.registry.writes)

    books.swap.open_active(books.copy(1))

    assert books.routes.lookup("sales") == Route(1, SOCKET_DIRECTORY / "sales-1.sock")
    assert len(books.registry.writes) == writes_before


async def test_closing_an_apps_route_removes_it() -> None:
    books = Books({1: "active"})
    books.swap.open_active(books.copy(1))

    books.swap.close("sales")

    assert books.routes.lookup("sales") is None


# --- retire: drain, close the WebSockets, stop --------------------------------------------------


async def test_the_old_copy_gets_thirty_seconds_then_its_websockets_close_then_it_stops() -> None:
    books = Books({1: "superseded", 2: "active"})
    old = books.copy(1)

    await books.swap.retire(old)

    assert DRAIN_S == 30.0
    assert books.clock.sleeps == [30.0]
    # Closed at t=30, for the old release only: a stream on the new release is not touched.
    assert books.closer.calls == [("sales", 1, 30.0)]
    assert old.child.stops != []
    assert not old.child.is_running


async def test_the_old_copy_keeps_running_while_the_drain_lasts() -> None:
    books = Books({1: "superseded", 2: "active"})
    gate = asyncio.Event()

    async def held_sleep(seconds: float) -> None:
        await gate.wait()

    books.clock.sleep = held_sleep  # type: ignore[method-assign]
    old = books.copy(1)

    task = asyncio.create_task(books.swap.retire(old))
    await asyncio.sleep(0.01)

    assert old.child.is_running
    assert books.closer.calls == []
    gate.set()
    await task
    assert not old.child.is_running
    assert [call[:2] for call in books.closer.calls] == [("sales", 1)]


async def test_the_old_copy_stops_even_when_closing_connections_fails() -> None:
    books = Books({1: "superseded", 2: "active"})

    async def broken_close(slug: str, release_no: int) -> None:
        raise RuntimeError("the edge could not close a stream")

    books.closer.close_connections = broken_close  # type: ignore[method-assign]
    old = books.copy(1)

    await books.swap.retire(old)

    assert not old.child.is_running


async def test_a_retire_cancelled_during_the_drain_still_stops_the_old_copy() -> None:
    books = Books({1: "superseded", 2: "active"})

    async def never(seconds: float) -> None:
        await asyncio.Event().wait()

    books.clock.sleep = never  # type: ignore[method-assign]
    old = books.copy(1)
    task = asyncio.create_task(books.swap.retire(old))
    await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not old.child.is_running


def test_the_route_of_a_copy_is_its_release_and_its_socket() -> None:
    books = Books({1: "active"})
    copy = books.copy(1)

    assert copy.route == Route(1, Path("/run/appmux-example/sales-1.sock"))
