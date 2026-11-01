"""Activating a release that already exists, from what is stored (T100; FR-006).

``plan_activation`` (``reconciler/activate.py``) turns "make release N run" into registry writes,
after the checks that come first; the reconciler then brings the release up like any pending target.
The tests run both halves on the fakes of ``reconciler_world``:

* the stored snapshot, wheels and config are what the build gets, nothing is read from a source
  folder and the write is the service principal's, so no token is involved;
* a release whose requirement keys are not resources of the app is refused before anything is
  written or started, and the refusal lists every missing key;
* a release that never passed its health check (``failed``) can be activated, and the running
  release keeps serving if the activated one is not healthy either.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from reconciler_fakes import NOW, FakeBuilder, digest_of
from reconciler_world import World

from appmux.reconciler.activate import ActivationRefused, plan_activation
from appmux.reconciler.build import BuiltRelease
from appmux.reconciler.child_env import ResourcesMissing
from appmux.reconciler.health import ProbeFailed
from appmux.registry.models import Release
from appmux.registry.writers import Identity, WriteKind

pytestmark = pytest.mark.anyio

ALICE = "alice@example.com"
WHEEL = {"sha256": "ab" * 32, "filename": "helper-1.0-py3-none-any.whl", "source": "app"}


def seed_stored_release(
    world: World,
    release_no: int,
    status: str,
    *,
    needs: Sequence[str] = (),
    wheels: Sequence[dict[str, str]] = (),
    config_env: Sequence[dict[str, str]] = (),
    deleted: bool = False,
    status_reason: str | None = None,
) -> None:
    """A release row with the content a build reads: manifest env, wheels and its own config."""
    fields: dict[str, Any] = {
        "app_slug": "sales",
        "release_no": release_no,
        "snapshot_digest": None if deleted else digest_of(release_no),
        "detected": {
            "runtime": "python",
            "command": ["python", "app.py"],
            "env": [{"name": key.upper(), "value_from": key} for key in needs],
        },
        "config": {"env": list(config_env), "wheel_sources": []},
        "wheels": list(wheels),
        "status": status,
        "status_reason": status_reason,
        "status_at": NOW,
        "created_by": ALICE,
        "created_at": NOW,
    }
    if deleted:
        fields.update(deleted_by=ALICE, deleted_at=datetime(2026, 10, 31, tzinfo=UTC))
    world.registry.seed("releases", fields)


def attach_resource(world: World, key: str) -> None:
    world.registry.seed(
        "app_resources",
        {
            "app_slug": "sales",
            "key": key,
            "type": "secret",
            "target": "scope/token-name",
            "permission": "READ",
            "access_as": None,
            "check_result": "ok",
            "attached_by": ALICE,
            "attached_at": NOW,
        },
    )


async def activate(world: World, release_no: int) -> None:
    """What the route does: plan from the snapshot, write as the service principal, wake."""
    activation = plan_activation(world.registry.snapshot, "sales", release_no, updated_by=ALICE)
    await world.registry.write(list(activation.changes))
    world.reconciler.wake("sales")
    await world.settle()


class RecordingBuilder(FakeBuilder):
    """A builder that keeps the release row it was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.releases: list[Release] = []

    async def build(self, release: Release) -> BuiltRelease:
        self.releases.append(release)
        return await super().build(release)


class Unhealthy:
    async def probe(self, socket_path: object, health_path: str, *, timeout_s: float) -> int:
        await asyncio.sleep(0)
        raise ProbeFailed("connection refused")


async def serving(*, newest: int = 3, status_of_older: str = "superseded") -> World:
    """Releases 1 and 2 are older, ``newest`` is active and serving."""
    world = World()
    world.app()
    for number in range(1, newest):
        seed_stored_release(world, number, status_of_older, wheels=[WHEEL])
    seed_stored_release(world, newest, "active")
    await world.run()
    assert world.route_of() == newest
    return world


# --- the stored artefacts ----------------------------------------------------------------------


async def test_an_older_release_is_built_from_its_stored_snapshot_wheels_and_config() -> None:
    world = await serving()
    builder = RecordingBuilder()
    world.reconciler._builder = builder  # type: ignore[attr-defined]  # a spy in place of the fake
    seed_stored_release(
        world, 4, "superseded", wheels=[WHEEL], config_env=[{"name": "MODE", "value": "fast"}]
    )

    await activate(world, 4)

    (built,) = builder.releases
    assert built.release_no == 4
    assert built.snapshot_digest == digest_of(4)
    assert [wheel.sha256 for wheel in built.wheels] == [WHEEL["sha256"]]
    assert [(e.name, e.value) for e in built.config.env] == [("MODE", "fast")]
    assert world.route_of() == 4
    assert world.status(4) == "active"
    assert world.status(3) == "superseded"


async def test_activating_creates_no_release_and_starts_the_chosen_one() -> None:
    world = await serving()
    before = len(world.registry.snapshot.releases_for("sales"))

    await activate(world, 1)

    assert len(world.registry.snapshot.releases_for("sales")) == before
    assert world.launcher.started[-1].release_no == 1
    assert world.route_of() == 1
    assert world.history(1) == ["pending", "building", "starting", "healthy", "active"]


async def test_the_write_is_the_service_principals_and_carries_no_token() -> None:
    world = await serving()
    before = len(world.registry.writes)

    await activate(world, 1)

    # The first write after the call is the activation's own; the reconciler's status writes follow.
    write = world.registry.writes[before]
    assert write.identity is Identity.APPMUX_SP
    assert write.admin_token is None
    assert set(write.kinds) == {WriteKind.RELEASE_UPDATE, WriteKind.APP_CONFIG_UPDATE}
    assert all(w.admin_token is None for w in world.registry.writes[before:])


async def test_the_plan_needs_only_the_registry_snapshot() -> None:
    # No source path, no token, no content store: the signature is the proof that none is read.
    world = await serving()

    activation = plan_activation(world.registry.snapshot, "sales", 1, updated_by=ALICE)

    assert [c.kind for c in activation.changes] == [
        WriteKind.RELEASE_UPDATE,
        WriteKind.APP_CONFIG_UPDATE,
    ]
    release_change, target_change = activation.changes
    assert release_change.values["status"] == "pending"
    assert target_change.values["target_release_no"] == 1
    assert target_change.values["desired_state"] == "running"
    assert target_change.expected_version == world.registry.snapshot.app_config("sales").version


async def test_the_old_release_serves_until_the_activated_one_is_healthy() -> None:
    world = await serving()
    old_child = world.launcher.children[-1]
    world.probe = Unhealthy()

    await activate(world, 1)

    assert world.route_of() == 3
    assert old_child.is_running
    assert world.status(3) == "active"
    assert world.status(1) == "failed"
    assert world.row(1).status_reason is not None


# --- a release that never passed health --------------------------------------------------------


async def test_a_release_that_failed_its_health_check_can_be_activated_again() -> None:
    world = await serving(newest=1)
    seed_stored_release(
        world, 2, "failed", status_reason="the process exited before it was healthy"
    )
    await world.target(2)

    await activate(world, 2)

    assert world.status(2) == "active"
    assert world.status(1) == "superseded"
    assert world.row(2).status_reason is None
    assert world.history(2) == ["pending", "building", "starting", "healthy", "active"]


async def test_a_failed_release_that_fails_again_is_failed_again_and_the_active_one_serves() -> (
    None
):
    world = await serving(newest=1)
    seed_stored_release(world, 2, "failed", status_reason="an old reason")
    world.probe = Unhealthy()

    await activate(world, 2)

    assert world.status(2) == "failed"
    assert world.row(2).status_reason != "an old reason"
    assert world.route_of() == 1
    assert world.status(1) == "active"


# --- requirements ------------------------------------------------------------------------------


async def test_missing_resource_keys_are_listed_and_nothing_is_written_or_started() -> None:
    world = await serving()
    seed_stored_release(world, 4, "superseded", needs=["warehouse-id", "api-secret"])
    writes, starts, builds = (
        len(world.registry.writes),
        len(world.launcher.started),
        len(world.builder.built),
    )

    with pytest.raises(ActivationRefused) as refusal:
        plan_activation(world.registry.snapshot, "sales", 4, updated_by=ALICE)

    assert refusal.value.kind == "conflict"
    assert refusal.value.code == "requirements_missing"
    assert refusal.value.missing_keys == ("api-secret", "warehouse-id")
    assert "missing resources: api-secret, warehouse-id" in refusal.value.message
    assert (len(world.registry.writes), len(world.launcher.started)) == (writes, starts)
    assert len(world.builder.built) == builds


async def test_attaching_the_missing_keys_lets_the_same_release_activate() -> None:
    world = await serving()
    seed_stored_release(world, 4, "superseded", needs=["warehouse-id"])
    with pytest.raises(ActivationRefused):
        plan_activation(world.registry.snapshot, "sales", 4, updated_by=ALICE)
    attach_resource(world, "warehouse-id")

    await activate(world, 4)

    assert world.route_of() == 4


async def test_the_keys_of_the_releases_own_config_count_too() -> None:
    world = await serving()
    seed_stored_release(
        world, 4, "superseded", config_env=[{"name": "TOKEN_ENV", "value_from": "extra-key"}]
    )

    with pytest.raises(ActivationRefused) as refusal:
        plan_activation(world.registry.snapshot, "sales", 4, updated_by=ALICE)

    assert refusal.value.missing_keys == ("extra-key",)


async def test_a_release_the_environment_refuses_fails_at_once_listing_the_keys() -> None:
    # The reconciler repeats the check when the release comes up: a resource detached between the
    # route's check and the start still fails before any build.
    world = await serving()
    seed_stored_release(world, 4, "superseded")
    world.environments.fail_with = ResourcesMissing(["gone-key"])
    builds = len(world.builder.built)
    starts = len(world.launcher.started)

    await activate(world, 4)

    assert world.status(4) == "failed"
    assert world.row(4).status_reason == "missing resources: gone-key"
    assert (len(world.builder.built), len(world.launcher.started)) == (builds, starts)
    assert world.route_of() == 3


# --- what may be activated ---------------------------------------------------------------------


async def test_a_deleted_release_is_not_found() -> None:
    world = await serving()
    seed_stored_release(world, 4, "superseded", deleted=True)

    with pytest.raises(ActivationRefused) as refusal:
        plan_activation(world.registry.snapshot, "sales", 4, updated_by=ALICE)

    assert refusal.value.kind == "not_found"


@pytest.mark.parametrize(("slug", "release_no"), [("sales", 99), ("nobody", 1)])
async def test_an_unknown_release_or_app_is_not_found(slug: str, release_no: int) -> None:
    world = await serving()

    with pytest.raises(ActivationRefused) as refusal:
        plan_activation(world.registry.snapshot, slug, release_no, updated_by=ALICE)

    assert refusal.value.kind == "not_found"


@pytest.mark.parametrize("status", ["building", "starting", "healthy"])
async def test_a_release_on_its_way_up_is_refused(status: str) -> None:
    world = await serving()
    seed_stored_release(world, 4, status)

    with pytest.raises(ActivationRefused) as refusal:
        plan_activation(world.registry.snapshot, "sales", 4, updated_by=ALICE)

    assert (refusal.value.kind, refusal.value.code) == ("conflict", "release_in_progress")


async def test_the_active_release_changes_no_status_and_is_marked_as_serving() -> None:
    world = await serving()

    activation = plan_activation(world.registry.snapshot, "sales", 3, updated_by=ALICE)

    assert activation.is_serving
    assert [c.kind for c in activation.changes] == [WriteKind.APP_CONFIG_UPDATE]


async def test_a_pending_release_is_left_pending_and_made_the_target() -> None:
    world = await serving()
    seed_stored_release(world, 4, "pending")

    activation = plan_activation(world.registry.snapshot, "sales", 4, updated_by=ALICE)

    assert not activation.is_serving
    assert [c.kind for c in activation.changes] == [WriteKind.APP_CONFIG_UPDATE]


async def test_activating_in_a_stopped_app_asks_it_to_run() -> None:
    world = await serving()
    await world.set_desired_state("stopped")
    await world.wake()
    assert world.route_of() is None

    await activate(world, 1)

    assert world.route_of() == 1
    assert world.registry.snapshot.app_config("sales").desired_state == "running"
