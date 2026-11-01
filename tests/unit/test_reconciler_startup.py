"""Start-up and the passes after it (T093, US1): restore what was active, finish what was cut short.

Each test seeds the registry as a previous run of appmux left it, starts the reconciler and reads
the outcome: the route table, the statuses and what the fake launcher was asked to start.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from reconciler_fakes import FakeProbe
from reconciler_world import World, answer_after, socket_of

from appmux.reconciler.build import BuildFailed
from appmux.registry.errors import RegistryUnavailable
from appmux.registry.models import Release

pytestmark = pytest.mark.anyio


class ProbeBySlug:
    """Each app answers its own health check; the slug is the start of the socket's name."""

    def __init__(
        self, world: World, answers: dict[str, Callable[[float], int | str] | int | str]
    ) -> None:
        self._probes = {slug: FakeProbe(world.clock, answer) for slug, answer in answers.items()}

    async def probe(self, socket_path: Path, health_path: str, *, timeout_s: float) -> int:
        slug = socket_path.name.rsplit("-", 1)[0]
        return await self._probes[slug].probe(socket_path, health_path, timeout_s=timeout_s)


def running_before(world: World, number: int = 1) -> None:
    """Release ``number`` of ``sales`` was active when the previous appmux stopped."""
    world.app(target_release_no=number)
    world.release(number, "active")


# --- restore --------------------------------------------------------------------------------------


async def test_the_active_release_is_started_and_routed_again_after_its_health_check() -> None:
    world = World()
    running_before(world)
    world.probe = FakeProbe(world.clock, answer_after(world.clock, 7))

    await world.run()

    assert world.routes.opened == [("sales", 1, 7.0)]
    assert [(s.slug, s.release_no) for s in world.launcher.started] == [("sales", 1)]
    assert world.builder.built == [("sales", 1)]


async def test_restoring_a_release_does_not_rewrite_its_status() -> None:
    world = World()
    running_before(world)

    await world.run()

    assert world.status_writes == []
    assert world.status(1) == "active"


async def test_a_release_that_no_longer_comes_up_is_marked_failed_and_gets_no_route() -> None:
    world = World(probe_answer=500)
    running_before(world)

    await world.run()

    assert world.history(1) == ["failed"]
    assert world.row(1).status_reason == (
        "health check did not pass within 60 s (last error: status 500)"
    )
    assert world.routes.opened == []
    assert not world.launcher.children[0].is_running


async def test_a_restore_that_cannot_be_built_is_marked_failed() -> None:
    world = World()
    running_before(world)
    world.builder.fail = BuildFailed("the snapshot is not in the content store")

    await world.run()

    assert world.status(1) == "failed"
    assert world.route_of() is None


async def test_a_stopped_app_is_not_started() -> None:
    world = World()
    world.app(desired_state="stopped", target_release_no=1)
    world.release(1, "active")

    await world.run()

    assert world.launcher.started == []
    assert world.route_of() is None
    assert world.status(1) == "active"


async def test_an_app_without_releases_is_left_alone() -> None:
    world = World()
    world.app()

    await world.run()

    assert world.launcher.started == []
    assert world.status_writes == []


# --- many apps (FR-008) ---------------------------------------------------------------------------


async def test_a_slow_app_does_not_keep_another_app_from_getting_its_route() -> None:
    world = World()
    for slug in ("alpha", "beta"):
        world.app(slug, target_release_no=1)
        world.release(1, "active", slug)
    world.probe = ProbeBySlug(world, {"alpha": answer_after(world.clock, 30), "beta": 200})

    await world.run()

    assert [slug for slug, _, _ in world.routes.opened] == ["beta", "alpha"]
    opened_at = {slug: at for slug, _, at in world.routes.opened}
    assert opened_at["beta"] < 5 <= opened_at["alpha"]


async def test_an_app_whose_build_fails_does_not_stop_the_others() -> None:
    world = World()
    for slug in ("alpha", "beta"):
        world.app(slug, target_release_no=1)
        world.release(1, "active", slug)
    real_build = world.builder.build

    async def build(release: Release) -> object:
        if release.app_slug == "alpha":
            raise BuildFailed("pip could not resolve flask==9")
        return await real_build(release)

    world.builder.build = build  # type: ignore[method-assign,assignment]

    await world.run()

    assert world.status(1, "alpha") == "failed"
    assert world.status(1, "beta") == "active"
    assert world.route_of("beta") == 1
    assert world.route_of("alpha") is None


async def test_when_the_apps_do_not_all_fit_the_rest_fail_with_the_memory_they_needed() -> None:
    world = World(memory_limit_mb=300)
    for slug in ("alpha", "beta"):
        world.app(slug, target_release_no=1, memory_budget_mb=256)
        world.release(1, "active", slug)

    await world.run()

    statuses = sorted((world.status(1, "alpha"), world.status(1, "beta")))
    assert statuses == ["active", "failed"]
    failed = "alpha" if world.status(1, "alpha") == "failed" else "beta"
    assert world.row(1, failed).status_reason == "not enough memory: needs 269 MB, 31 MB free"
    assert len(world.routes.routes()) == 1


# --- a restart cut a release short ----------------------------------------------------------------


@pytest.mark.parametrize("left_as", ["building", "starting", "healthy"])
async def test_a_release_cut_short_goes_back_to_pending_and_is_built_again(left_as: str) -> None:
    world = World()
    world.app(target_release_no=1)
    world.release(1, left_as)

    await world.run()

    assert world.history(1) == ["pending", "building", "starting", "healthy", "active"]
    assert world.route_of() == 1


async def test_a_release_cut_short_beside_the_active_one_replaces_it_when_it_is_ready() -> None:
    world = World()
    world.app(target_release_no=2)
    world.release(1, "active")
    world.release(2, "starting")

    await world.run()

    assert world.status(1) == "superseded"
    assert world.status(2) == "active"
    assert world.route_of() == 2
    assert [r for _, r, _ in world.routes.opened] == [1, 2], "the old release served meanwhile"


async def test_a_release_that_was_reset_does_not_keep_the_reason_of_its_earlier_attempt() -> None:
    world = World()
    world.app(target_release_no=1)
    world.release(1, "building", status_reason="left over")

    await world.run()

    assert world.row(1).status_reason is None


async def test_a_registry_error_half_way_leaves_the_release_for_the_next_pass_to_start_over() -> (
    None
):
    world = World()
    world.app(target_release_no=1)
    world.release(1)
    real_write = world.registry.write
    refused = False

    async def refuse_once(changes: object, audit: object = (), **keywords: object) -> object:
        nonlocal refused
        starting = any(
            getattr(change, "values", {}).get("status") == "starting"
            for change in changes  # type: ignore[attr-defined]
        )
        if starting and not refused:
            refused = True
            raise RegistryUnavailable
        return await real_write(changes, audit, **keywords)  # type: ignore[arg-type]

    world.registry.write = refuse_once  # type: ignore[method-assign,assignment]

    await world.run()
    assert world.launcher.started == [], "no child starts before its status could be written"
    assert world.status(1) == "building"

    await world.wake()

    assert world.status(1) == "active"
    assert world.history(1)[:2] == ["building", "pending"]


# --- the registry is the source of truth ----------------------------------------------------------


async def test_nothing_is_started_while_the_registry_reports_an_error() -> None:
    world = World()
    running_before(world)
    world.registry.go_down()

    await world.run()

    assert world.launcher.started == []
    world.registry.come_back()
    await world.wake()
    assert world.route_of() == 1


async def test_a_pass_skipped_for_a_registry_error_is_made_up_once_it_recovers() -> None:
    world = World(watch_interval_s=0.01)
    running_before(world)
    world.registry.go_down()
    await world.run()
    assert world.route_of() is None

    world.registry.come_back()

    await world.until_route(1)
    await world.stop()


async def test_an_error_that_clears_without_a_new_snapshot_still_makes_up_the_skipped_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The registry client clears ``last_error`` after any successful call, with no reload.
    world = World(watch_interval_s=0.01)
    running_before(world)
    error: dict[str, str | None] = {"now": "the registry is not reachable"}
    monkeypatch.setattr(type(world.registry), "last_error", property(lambda _: error["now"]))
    await world.run()
    assert world.route_of() is None

    error["now"] = None

    await world.until_route(1)
    await world.stop()


async def test_a_new_release_is_picked_up_by_the_watcher_without_a_wake() -> None:
    world = World(watch_interval_s=0.01)
    running_before(world)
    await world.run()
    assert world.route_of() == 1

    world.release(2)
    await world.target(2)

    await world.until_route(2)
    await world.stop()


async def test_flipping_an_app_to_stopped_closes_its_route_and_stops_its_child() -> None:
    world = World()
    running_before(world)
    await world.run()

    await world.set_desired_state("stopped")
    await world.wake()

    assert world.route_of() is None
    assert not world.launcher.children[0].is_running
    assert world.status(1) == "active", "stopping an app does not rewrite its release"


async def test_an_app_started_again_comes_back_as_a_new_copy_of_the_same_release() -> None:
    world = World()
    running_before(world)
    await world.run()
    await world.set_desired_state("stopped")
    await world.wake()

    await world.set_desired_state("running")
    await world.wake()

    assert world.route_of() == 1
    assert len(world.launcher.started) == 2
    assert world.launcher.children[1].is_running


async def test_an_app_added_after_start_up_is_reconciled_when_woken() -> None:
    world = World()
    await world.run()
    world.app("beta", target_release_no=1)
    world.release(1, slug="beta")

    await world.wake("beta")

    assert world.status(1, "beta") == "active"
    assert world.route_of("beta") == 1


async def test_waking_an_app_the_registry_does_not_know_does_nothing() -> None:
    world = World()
    await world.run()

    await world.wake("ghost")

    assert world.launcher.started == []
    assert world.routes.routes() == {}


async def test_the_route_of_each_app_points_at_its_own_socket() -> None:
    world = World()
    for slug in ("alpha", "beta"):
        world.app(slug, target_release_no=1)
        world.release(1, "active", slug)

    await world.run()

    assert world.routes.lookup("alpha").socket_path == socket_of("alpha", 1)  # type: ignore[union-attr]
    assert world.routes.lookup("beta").socket_path == socket_of("beta", 1)  # type: ignore[union-attr]
