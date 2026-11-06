"""A config change is a release; a rename is a restart under the new slug (T101; FR-009, FR-010).

Two parts, both run on the fakes of ``reconciler_world``:

* ``plan_config_edit`` (``registry/operations.py``) turns a new env or wheel-source list into a new
  release with the **same snapshot**, which the reconciler brings up through the normal swap. The
  health check and the own-SP keys belong to the app, not to a release; resources and secrets are
  never part of one;
* ``Reconciler.app_renamed`` (``reconciler/reconciler.py``) moves the running copy's route to the
  new slug and starts a second copy under it, swapped in like any release. A copy that does not come
  up leaves the first serving, and a stopped app only changes its row.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import SecretStr
from reconciler_fakes import NOW, digest_of
from reconciler_world import World, socket_of

from appmux.admin.apps import build_app_detail
from appmux.reconciler.health import ProbeFailed
from appmux.reconciler.rename import SlugTaken, check_new_slug, under_new_slug
from appmux.registry.apps import platform_fields_change
from appmux.registry.models import EnvEntry
from appmux.registry.operations import (
    ConfigEdit,
    ConfigEditRefused,
    plan_config_edit,
)
from appmux.registry.writers import WriteKind

pytestmark = pytest.mark.anyio

DANA = "dana@example.com"
PLATFORM_ADMIN_TOKEN = SecretStr("tok-test")
OWN_SP_KEYS = {"own_sp_client_id_key": "sp-id", "own_sp_client_secret_key": "sp-secret"}
WHEEL = {"sha256": "cd" * 32, "filename": "helper-2.0-py3-none-any.whl", "source": "app"}


class Unhealthy:
    async def probe(self, socket_path: object, health_path: str, *, timeout_s: float) -> int:
        await asyncio.sleep(0)
        raise ProbeFailed("connection refused")


async def let_the_watchers_run() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


def seed_with_content(
    world: World,
    release_no: int,
    status: str,
    *,
    env: list[dict[str, str]] | None = None,
    wheels: list[dict[str, str]] | None = None,
    deleted: bool = False,
) -> None:
    fields: dict[str, Any] = {
        "app_slug": "sales",
        "release_no": release_no,
        "snapshot_digest": None if deleted else digest_of(release_no),
        "detected": {"runtime": "python", "command": ["python", "app.py"]},
        "config": {"env": env or [], "wheel_sources": []},
        "wheels": wheels or [],
        "status": status,
        "status_at": NOW,
        "created_by": "alice@example.com",
        "created_at": NOW,
    }
    if deleted:
        fields.update(deleted_by=DANA, deleted_at=NOW)
    world.registry.seed("releases", fields)


async def serving(*, target_release_no: int | None = None) -> World:
    """Release 3 is active and serving; 1 and 2 are older, with their own content."""
    world = World()
    world.app(target_release_no=target_release_no)
    seed_with_content(world, 1, "superseded")
    seed_with_content(world, 2, "superseded")
    seed_with_content(world, 3, "active", env=[{"name": "MODE", "value": "slow"}], wheels=[WHEEL])
    await world.run()
    assert world.route_of() == 3
    return world


def edit_config(
    world: World,
    *,
    env: list[EnvEntry] | None = None,
    wheel_sources: list[str] | None = None,
    health_path: str | None = None,
    health_deadline_s: int | None = None,
    own_sp_client_id_key: str | None = None,
    own_sp_client_secret_key: str | None = None,
) -> ConfigEdit:
    config = world.registry.snapshot.app_config("sales")
    source = world.row(3)
    return plan_config_edit(
        world.registry.snapshot,
        config,
        env=source.config.env if env is None else env,
        wheel_sources=source.config.wheel_sources if wheel_sources is None else wheel_sources,
        health_path=config.health_path if health_path is None else health_path,
        health_deadline_s=config.health_deadline_s
        if health_deadline_s is None
        else health_deadline_s,
        own_sp_client_id_key=own_sp_client_id_key,
        own_sp_client_secret_key=own_sp_client_secret_key,
        updated_by=DANA,
    )


async def apply(world: World, edit: ConfigEdit) -> None:
    await world.registry.write(list(edit.changes))
    world.reconciler.wake("sales")
    await world.settle()


FAST = [EnvEntry(name="MODE", value="fast")]


# --- the edit becomes a release ----------------------------------------------------------------


async def test_an_env_change_creates_a_release_with_the_same_snapshot_and_wheels() -> None:
    world = await serving()

    edit = edit_config(world, env=FAST)
    await apply(world, edit)

    assert edit.release_no == 4
    created = world.row(4)
    assert created.snapshot_digest == digest_of(3)
    assert created.detected == world.row(3).detected
    assert [w.sha256 for w in created.wheels] == [WHEEL["sha256"]]
    assert [(e.name, e.value) for e in created.config.env] == [("MODE", "fast")]
    assert created.created_by == DANA


async def test_the_new_release_goes_through_the_swap_and_the_old_one_is_superseded() -> None:
    world = await serving()
    old_child = world.launcher.children[-1]

    await apply(world, edit_config(world, env=FAST))

    assert world.history(4) == ["building", "starting", "healthy", "active"]
    assert world.status(4) == "active"
    assert world.status(3) == "superseded"
    assert world.route_of() == 4
    assert world.launcher.started[-1].release_no == 4
    assert not old_child.is_running
    assert world.registry.snapshot.app_config("sales").target_release_no == 4


async def test_the_old_release_keeps_the_config_it_was_made_with() -> None:
    world = await serving()

    await apply(world, edit_config(world, env=FAST))

    assert [(e.name, e.value) for e in world.row(3).config.env] == [("MODE", "slow")]


async def test_a_new_release_that_is_not_healthy_leaves_the_running_one_serving() -> None:
    world = await serving()
    world.probe = Unhealthy()

    await apply(world, edit_config(world, env=FAST))

    assert world.status(4) == "failed"
    assert world.status(3) == "active"
    assert world.route_of() == 3


async def test_a_change_of_wheel_sources_is_a_new_release_too() -> None:
    world = await serving()

    edit = edit_config(world, wheel_sources=["/Workspace/Shared/wheels"])
    await apply(world, edit)

    assert edit.release_no == 4
    assert world.row(4).config.wheel_sources == ["/Workspace/Shared/wheels"]
    assert world.route_of() == 4


async def test_the_edit_is_one_release_insert_and_one_app_config_update() -> None:
    world = await serving()

    edit = edit_config(world, env=FAST, health_path="/up")

    assert [change.kind for change in edit.changes] == [
        WriteKind.RELEASE_INSERT,
        WriteKind.APP_CONFIG_UPDATE,
    ]
    app_update = edit.changes[1]
    assert app_update.values["target_release_no"] == 4
    assert app_update.values["health_path"] == "/up"
    assert "desired_state" not in app_update.values
    assert app_update.expected_version == world.registry.snapshot.app_config("sales").version


# --- what is not part of a release -------------------------------------------------------------


async def test_a_release_holds_only_env_and_wheel_sources() -> None:
    world = await serving()

    edit = edit_config(world, env=FAST, **OWN_SP_KEYS)
    await apply(world, edit)

    assert set(world.row(4).config.model_dump()) == {"env", "wheel_sources"}
    assert world.registry.snapshot.app_config("sales").own_sp_client_id_key == "sp-id"


@pytest.mark.parametrize(
    "overrides",
    [
        {"health_path": "/ready"},
        {"health_deadline_s": 120},
        OWN_SP_KEYS,
    ],
)
async def test_health_and_own_sp_changes_write_the_app_alone_and_restart_nothing(
    overrides: dict[str, Any],
) -> None:
    world = await serving()
    starts = len(world.launcher.started)

    edit = edit_config(world, **overrides)
    await apply(world, edit)

    assert edit.release_no is None
    assert [change.kind for change in edit.changes] == [WriteKind.APP_CONFIG_UPDATE]
    assert len(world.registry.snapshot.releases_for("sales")) == 3
    assert len(world.launcher.started) == starts
    config = world.registry.snapshot.app_config("sales")
    for name, value in overrides.items():
        assert getattr(config, name) == value
    assert config.target_release_no is None or config.target_release_no == 3


async def test_an_edit_that_changes_nothing_creates_no_release() -> None:
    world = await serving()

    edit = edit_config(world)

    assert edit.release_no is None
    assert [change.kind for change in edit.changes] == [WriteKind.APP_CONFIG_UPDATE]


async def test_the_edit_writes_no_resource_or_secret() -> None:
    world = await serving()

    edit = edit_config(world, env=FAST, **OWN_SP_KEYS)

    written = {change.kind for change in edit.changes}
    assert written <= {WriteKind.RELEASE_INSERT, WriteKind.APP_CONFIG_UPDATE}


# --- which release is copied -------------------------------------------------------------------


async def test_a_failed_target_is_the_one_copied_so_that_an_edit_can_repair_an_update() -> None:
    world = await serving(target_release_no=4)
    seed_with_content(world, 4, "failed", env=[{"name": "MODE", "value": "broken"}])

    edit = edit_config(world, env=FAST)
    await apply(world, edit)

    assert edit.release_no == 5
    assert world.row(5).snapshot_digest == digest_of(4)
    assert world.route_of() == 5


async def test_a_deleted_target_has_no_snapshot_and_the_active_release_is_copied() -> None:
    world = await serving(target_release_no=4)
    seed_with_content(world, 4, "failed", deleted=True)

    edit = edit_config(world, env=FAST)

    assert edit.release_no == 5
    assert edit.changes[0].values["snapshot_digest"] == digest_of(3)


async def test_env_entries_without_any_release_are_refused_and_nothing_else_is() -> None:
    world = World()
    world.app()
    await world.run()
    config = world.registry.snapshot.app_config("sales")

    with pytest.raises(ConfigEditRefused) as refusal:
        plan_config_edit(
            world.registry.snapshot,
            config,
            env=FAST,
            wheel_sources=[],
            health_path="/",
            health_deadline_s=60,
            own_sp_client_id_key=None,
            own_sp_client_secret_key=None,
            updated_by=DANA,
        )
    assert refusal.value.code == "no_release"

    edit = plan_config_edit(
        world.registry.snapshot,
        config,
        env=[],
        wheel_sources=[],
        health_path="/up",
        health_deadline_s=60,
        own_sp_client_id_key=None,
        own_sp_client_secret_key=None,
        updated_by=DANA,
    )
    assert edit.release_no is None


async def test_an_edit_to_a_stopped_app_builds_nothing_until_it_is_started() -> None:
    world = await serving()
    await world.set_desired_state("stopped")
    await world.wake()
    builds = len(world.builder.built)

    await apply(world, edit_config(world, env=FAST))
    assert len(world.builder.built) == builds
    assert world.route_of() is None

    await world.set_desired_state("running")
    await world.wake()

    assert world.route_of() == 4
    assert world.status(4) == "active"


# --- rename ------------------------------------------------------------------------------------


async def rename(world: World, new_slug: str = "billing") -> None:
    """What the route does: the platform admin's write, then the reconciler is told."""
    app = world.registry.snapshot.app("sales")
    change = platform_fields_change(app, updated_by=DANA, slug=new_slug)
    await world.registry.write([change], admin_token=PLATFORM_ADMIN_TOKEN)
    world.reconciler.app_renamed("sales", new_slug)
    await world.settle()


async def test_a_renamed_running_app_is_served_under_the_new_slug_and_not_the_old() -> None:
    world = await serving()

    await rename(world)

    assert world.route_of("billing") == 3
    assert world.route_of("sales") is None


async def test_the_new_slug_is_never_without_a_route_during_the_rename() -> None:
    world = await serving()

    await rename(world)

    assert [slug for slug, *_ in world.routes.closed if slug == "billing"] == []
    assert [slug for slug, _, _ in world.routes.opened if slug == "billing"] == ["billing"] * 2


async def test_a_second_copy_is_started_under_the_new_slug_and_the_first_one_stops() -> None:
    world = await serving()
    first = world.launcher.child_of("sales", 3)

    await rename(world)

    assert world.launcher.started[-1].slug == "billing"
    assert world.launcher.started[-1].release_no == 3
    second = world.launcher.child_of("billing", 3)
    assert second is not first
    assert second.is_running
    assert not first.is_running
    assert world.routes.lookup("billing").socket_path == socket_of("billing", 3)


async def test_a_rename_changes_no_release_status() -> None:
    world = await serving()
    writes = list(world.status_writes)

    await rename(world)

    assert world.status_writes == writes
    assert world.status(3, "billing") == "active"


async def test_streams_opened_on_the_new_copy_are_not_closed_by_the_old_copys_retirement() -> None:
    # The edge keys open streams by slug and release number, and both copies serve release 3.
    world = await serving()

    await rename(world)

    assert world.closer.calls == []


async def test_the_moved_copy_is_watched_under_the_new_slug() -> None:
    world = await serving()
    await rename(world)
    starts = len(world.launcher.started)

    world.launcher.child_of("billing", 3).exit("exited with status 9")
    await let_the_watchers_run()
    await world.settle()

    assert world.launcher.started[starts].slug == "billing"
    assert world.route_of("billing") == 3


async def test_a_new_copy_that_is_not_healthy_leaves_the_first_one_serving() -> None:
    world = await serving()
    first = world.launcher.child_of("sales", 3)
    world.probe = Unhealthy()

    await rename(world)

    assert world.route_of("billing") == 3
    assert world.routes.lookup("billing").socket_path == socket_of("sales", 3)
    assert first.is_running
    assert world.status(3, "billing") == "active"
    assert world.reconciler.last_restart_failure("billing") is not None
    assert not world.launcher.children[-1].is_running


async def test_the_app_page_of_a_renamed_app_names_the_check_its_new_copy_failed() -> None:
    # TD-028: the reason was kept in memory and no screen showed it. The detail of the app, which
    # the app's Overview reads, now carries it.
    world = await serving()
    world.probe = Unhealthy()
    await rename(world)
    snapshot = world.registry.snapshot
    app, config = snapshot.app("billing"), snapshot.app_config("billing")
    assert app is not None and config is not None

    detail = build_app_detail(
        snapshot, app, config, restart_failure=world.reconciler.last_restart_failure
    )

    assert detail.restart_failure is not None
    assert "health check did not pass" in detail.restart_failure
    assert "connection refused" in detail.restart_failure


async def test_the_app_page_shows_no_restart_failure_after_the_restart_succeeds() -> None:
    world = await serving()
    world.probe = Unhealthy()
    await rename(world)
    world.probe = World().probe
    world.reconciler.request_restart("billing")
    await world.settle()
    snapshot = world.registry.snapshot
    app, config = snapshot.app("billing"), snapshot.app_config("billing")
    assert app is not None and config is not None

    detail = build_app_detail(
        snapshot, app, config, restart_failure=world.reconciler.last_restart_failure
    )

    assert detail.restart_failure is None


async def test_a_new_copy_that_cannot_start_leaves_the_first_one_serving() -> None:
    world = await serving()
    first = world.launcher.child_of("sales", 3)
    world.launcher.fail_with = "no port is free"

    await rename(world)

    assert world.route_of("billing") == 3
    assert first.is_running
    assert world.status(3, "billing") == "active"


async def test_restart_after_a_failed_rename_brings_the_new_copy_up() -> None:
    world = await serving()
    world.probe = Unhealthy()
    await rename(world)
    world.probe = World().probe

    world.reconciler.request_restart("billing")
    await world.settle()

    assert world.routes.lookup("billing").socket_path == socket_of("billing", 3)
    assert world.reconciler.last_restart_failure("billing") is None


async def test_a_stopped_app_only_changes_its_row() -> None:
    world = await serving()
    await world.set_desired_state("stopped")
    await world.wake()
    starts, opened = len(world.launcher.started), len(world.routes.opened)

    await rename(world)

    assert world.registry.snapshot.app("billing") is not None
    assert world.registry.snapshot.app("sales") is None
    assert len(world.launcher.started) == starts
    assert len(world.routes.opened) == opened
    assert world.route_of("billing") is None


async def test_a_renamed_stopped_app_starts_under_the_new_slug() -> None:
    world = await serving()
    await world.set_desired_state("stopped")
    await world.wake()
    await rename(world)

    await world.set_desired_state("running", slug="billing")
    await world.wake("billing")

    assert world.launcher.started[-1].slug == "billing"
    assert world.route_of("billing") == 3


# --- the rules the route relies on -------------------------------------------------------------


async def test_a_taken_slug_is_refused_and_a_free_or_own_slug_is_not() -> None:
    world = await serving()
    world.app("billing")

    with pytest.raises(SlugTaken):
        check_new_slug(world.registry.snapshot, "sales", "billing")
    check_new_slug(world.registry.snapshot, "sales", "reports")
    check_new_slug(world.registry.snapshot, "sales", "sales")


async def test_the_release_under_the_new_slug_is_the_same_row_with_the_new_name() -> None:
    world = await serving()

    moved = under_new_slug(world.row(3), "billing")

    assert moved.app_slug == "billing"
    assert moved.model_copy(update={"app_slug": "sales"}) == world.row(3)
