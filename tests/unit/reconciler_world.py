"""One reconciler wired to fakes, with the bookkeeping the reconciler's tests share.

Everything in the world is in memory and none of it sleeps: the clock moves when something sleeps
on it, the probe answers from a function of that clock, the launcher starts ``FakeChild`` objects
and the registry is the in-memory one. ``settle`` returns when no app has work left, so a test
reads the end state without a timing guess.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from reconciler_fakes import (
    FakeBuilder,
    FakeClock,
    FakeCloser,
    FakeEnvironment,
    FakeLauncher,
    FakeProbe,
    seed_app,
    seed_release,
    status_of,
    stub_registry_class,
)

from appmux.reconciler.reconciler import Reconciler
from appmux.reconciler.route_table import Route, RouteTable
from appmux.registry.protocol import Change
from appmux.registry.writers import WriteKind

SETTLE_TIMEOUT_S = 10.0


class RecordingRoutes(RouteTable):
    """A route table that remembers when, and to what, each route was opened or closed."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self._clock = clock
        self.opened: list[tuple[str, int, float]] = []
        self.closed: list[tuple[str, float]] = []
        self.changed = asyncio.Event()

    def open_route(self, slug: str, route: Route) -> None:
        self.opened.append((slug, route.release_no, self._clock.now))
        super().open_route(slug, route)
        self.changed.set()

    def close_route(self, slug: str) -> None:
        self.closed.append((slug, self._clock.now))
        super().close_route(slug)
        self.changed.set()


class World:
    def __init__(
        self,
        *,
        memory_limit_mb: int = 4096,
        reserve_mb: int = 0,
        probe_answer: Any = 200,
        watch_interval_s: float | None = None,
        probe: Any = None,
        output_poll_s: float | None = None,
    ) -> None:
        self.registry = stub_registry_class()()
        self.clock = FakeClock()
        self.routes = RecordingRoutes(self.clock)
        self.launcher = FakeLauncher()
        self.builder = FakeBuilder()
        self.environments = FakeEnvironment()
        self.probe = probe or FakeProbe(self.clock, probe_answer)
        self.closer = FakeCloser(self.clock)
        self.status_writes: list[tuple[int, str]] = []
        self.metrics = RecordingMetrics()
        self._log_status_writes()
        self.reconciler = Reconciler(
            registry=self.registry,
            routes=self.routes,
            launcher=self.launcher,
            builder=self.builder,
            environments=self.environments,
            probe=_ProbeOfTheWorld(self),
            closer=self.closer,
            clock=self.clock,
            memory_limit_mb=memory_limit_mb,
            memory_reserve_mb=lambda: reserve_mb,
            watch_interval_s=watch_interval_s,
            metrics=self.metrics,
            output_poll_s=output_poll_s,
        )

    def _log_status_writes(self) -> None:
        real_write = self.registry.write

        async def logged(changes: Any, audit: Any = (), **keywords: Any) -> Any:
            rows = await real_write(changes, audit, **keywords)
            for change in changes:
                if change.kind is WriteKind.RELEASE_UPDATE and "status" in change.values:
                    self.status_writes.append(
                        (int(change.key["release_no"]), str(change.values["status"]))
                    )
            return rows

        self.registry.write = logged

    # --- the books ---------------------------------------------------------------------------

    def app(self, slug: str = "sales", **settings: Any) -> None:
        seed_app(self.registry, slug, **settings)

    def release(self, no: int, status: str = "pending", slug: str = "sales", **fields: Any) -> None:
        seed_release(self.registry, slug, no, status, **fields)

    async def target(self, no: int, slug: str = "sales") -> None:
        """Make release ``no`` the app's target, as an Update does."""
        config = self.registry.snapshot.app_config(slug)
        await self.registry.write(
            [
                Change(
                    WriteKind.APP_CONFIG_UPDATE,
                    key={"app_slug": slug},
                    values={"target_release_no": no, "updated_by": "dana@example.com"},
                    expected_version=config.version,
                )
            ]
        )

    async def set_desired_state(self, state: str, slug: str = "sales") -> None:
        config = self.registry.snapshot.app_config(slug)
        await self.registry.write(
            [
                Change(
                    WriteKind.APP_CONFIG_UPDATE,
                    key={"app_slug": slug},
                    values={"desired_state": state, "updated_by": "dana@example.com"},
                    expected_version=config.version,
                )
            ]
        )

    def status(self, no: int, slug: str = "sales") -> str:
        return status_of(self.registry, slug, no)

    def row(self, no: int, slug: str = "sales") -> Any:
        return self.registry.snapshot.release(slug, no)

    def history(self, no: int) -> list[str]:
        return [status for release_no, status in self.status_writes if release_no == no]

    def route_of(self, slug: str = "sales") -> int | None:
        route = self.routes.lookup(slug)
        return None if route is None else route.release_no

    # --- running it --------------------------------------------------------------------------

    async def run(self) -> None:
        """Start the reconciler and wait until nothing is left to do."""
        await self.reconciler.start()
        await self.settle()

    async def settle(self) -> None:
        await asyncio.wait_for(self.reconciler.wait_idle(), SETTLE_TIMEOUT_S)

    async def wake(self, slug: str = "sales") -> None:
        self.reconciler.wake(slug)
        await self.settle()

    async def until_route(
        self, release_no: int, slug: str = "sales", timeout_s: float = 5.0
    ) -> None:
        """Wait, in real time, for ``slug`` to route to ``release_no`` (for the watcher's tests)."""

        async def routed() -> None:
            while self.route_of(slug) != release_no:
                self.routes.changed.clear()
                await self.routes.changed.wait()

        await asyncio.wait_for(routed(), timeout_s)

    async def stop(self) -> None:
        await self.reconciler.stop_all(within_s=5.0)


class RecordingMetrics:
    """What the reconciler counts for the metrics: restarts per app and admission refusals."""

    def __init__(self) -> None:
        self.restarts: list[str] = []
        self.admission_rejections = 0

    def record_restart(self, app_slug: str) -> None:
        self.restarts.append(app_slug)

    def record_admission_rejection(self) -> None:
        self.admission_rejections += 1


class _ProbeOfTheWorld:
    """Hands every poll to whatever ``world.probe`` is now, so a test can swap the answers."""

    def __init__(self, world: World) -> None:
        self._world = world

    async def probe(self, socket_path: Path, health_path: str, *, timeout_s: float) -> int:
        result: int = await self._world.probe.probe(socket_path, health_path, timeout_s=timeout_s)
        return result


def socket_of(slug: str, no: int) -> Path:
    from reconciler_fakes import SOCKET_DIRECTORY

    return SOCKET_DIRECTORY / f"{slug}-{no}.sock"


def answer_after(
    clock: FakeClock, seconds: float, *, before: str = "connection refused"
) -> Callable[[float], int | str]:
    """A probe answer that is ``before`` until ``seconds`` after the clock's start, then ``200``."""
    start = clock.now

    def answer(now: float) -> int | str:
        return 200 if now - start >= seconds else before

    return answer
