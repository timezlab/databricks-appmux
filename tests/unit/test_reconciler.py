"""The reconciler takes a release from pending to active, and opens its route last (T093, US1).

Constitution II: a child's route opens only after fetch, build, start and a health check all
succeeded. Every test here reads that from the outside: the registry's statuses, the route table
and the children the fake launcher was asked to start.
"""

from __future__ import annotations

import pytest
from reconciler_fakes import FakeProbe
from reconciler_world import World, answer_after, socket_of

from appmux.reconciler.admission import MemoryLedger
from appmux.reconciler.build import BuildFailed
from appmux.reconciler.child_env import EnvironmentRefused, ResourcesMissing
from appmux.reconciler.swap import DRAIN_S

pytestmark = pytest.mark.anyio


def pending_app(**settings: object) -> World:
    world = World(**{k: v for k, v in settings.items() if k in {"memory_limit_mb", "probe_answer"}})
    world.app(**{k: v for k, v in settings.items() if k not in {"memory_limit_mb", "probe_answer"}})
    world.release(1)
    return world


# --- the way up -----------------------------------------------------------------------------------


async def test_a_pending_target_is_built_started_checked_and_becomes_active() -> None:
    world = pending_app(target_release_no=1)

    await world.run()

    assert world.history(1) == ["building", "starting", "healthy", "active"]
    assert world.status(1) == "active"
    assert world.builder.built == [("sales", 1)]
    assert world.route_of() == 1
    route = world.routes.lookup("sales")
    assert route is not None
    assert route.socket_path == socket_of("sales", 1)


async def test_the_route_opens_only_after_the_first_healthy_answer() -> None:
    world = pending_app(target_release_no=1)
    absent_while_polling: list[bool] = []

    def answer(now: float) -> int | str:
        absent_while_polling.append(world.route_of() is None)
        return 200 if now >= 23 else "connection refused"

    world.probe = FakeProbe(world.clock, answer)

    await world.run()

    assert world.routes.opened == [("sales", 1, 23.0)]
    assert absent_while_polling == [True] * 24
    assert world.probe.times[0] == 0.0
    assert world.probe.times[-1] == 23.0


async def test_the_child_starts_with_the_command_environment_and_budget_of_the_release() -> None:
    world = pending_app(target_release_no=1, memory_budget_mb=300)

    await world.run()

    (started,) = world.launcher.started
    assert (started.slug, started.release_no) == ("sales", 1)
    assert started.command == ("python", "app.py")
    assert started.environment == {"APPMUX_TEST_RELEASE": "sales-1"}
    assert started.release_dir == world.builder.release_dir
    assert started.port == 8000
    assert started.memory_budget_mb == 300


async def test_the_health_check_uses_the_apps_path_and_deadline() -> None:
    world = pending_app(target_release_no=1, health_path="/ready", health_deadline_s=5)
    world.probe = FakeProbe(world.clock, "connection refused")

    await world.run()

    assert set(world.probe.targets) == {(socket_of("sales", 1), "/ready")}
    assert world.probe.times[-1] == 5.0
    assert world.row(1).status_reason == (
        "health check did not pass within 5 s (last error: connection refused)"
    )


@pytest.mark.parametrize(
    ("detected", "expected"),
    [
        ({"runtime": "python", "command": []}, ("python", "app.py")),
        ({"runtime": "node", "command": []}, ("npm", "run", "start")),
        (
            {"runtime": "python", "command": ["uvicorn", "app:app", "--port", "$PORT"]},
            ("uvicorn", "app:app", "--port", "8000"),
        ),
    ],
)
async def test_the_start_command_is_the_apps_or_the_runtimes_default(
    detected: dict[str, object], expected: tuple[str, ...]
) -> None:
    from appmux.registry.protocol import Change
    from appmux.registry.writers import WriteKind

    world = pending_app(target_release_no=1)
    await world.registry.write(
        [
            Change(
                WriteKind.RELEASE_UPDATE,
                key={"app_slug": "sales", "release_no": 1},
                values={"detected": detected},
            )
        ]
    )
    world.environments.environment = {"PORT": "8000"}

    await world.run()

    assert world.launcher.started[0].command == expected


async def test_a_release_without_a_command_or_a_runtime_fails_saying_what_to_add() -> None:
    from appmux.registry.protocol import Change
    from appmux.registry.writers import WriteKind

    world = pending_app(target_release_no=1)
    await world.registry.write(
        [
            Change(
                WriteKind.RELEASE_UPDATE,
                key={"app_slug": "sales", "release_no": 1},
                values={"detected": {"command": []}},
            )
        ]
    )

    await world.run()

    assert world.status(1) == "failed"
    assert "command" in world.row(1).status_reason
    assert world.launcher.started == []


# --- FR-002: a failure before health --------------------------------------------------------------


async def test_a_child_that_never_answers_fails_the_release_with_the_reason_and_a_log_tail() -> (
    None
):
    world = pending_app(target_release_no=1, probe_answer="connection refused")
    world.launcher.log_of_next_child = "starting\nImportError: no module named x"

    await world.run()

    row = world.row(1)
    assert row.status == "failed"
    assert row.status_reason == (
        "health check did not pass within 60 s (last error: connection refused)"
    )
    assert row.build_log_tail == "starting\nImportError: no module named x"
    assert world.routes.opened == []
    assert world.route_of() is None
    assert not world.launcher.children[0].is_running, "the failed copy is stopped"


async def test_a_child_that_answers_500_forever_fails_with_the_status() -> None:
    world = pending_app(target_release_no=1, probe_answer=500)

    await world.run()

    assert world.row(1).status_reason == (
        "health check did not pass within 60 s (last error: status 500)"
    )
    assert world.routes.opened == []


async def test_a_child_that_exits_during_start_up_fails_at_once_not_at_the_deadline() -> None:
    world = pending_app(target_release_no=1, probe_answer="connection refused")
    world.launcher.log_of_next_child = "Traceback: boom"
    started = world.launcher.start

    async def start_then_crash(child: object) -> object:
        running = await started(child)  # type: ignore[arg-type]
        running.exit("exited with status 1")
        return running

    world.launcher.start = start_then_crash  # type: ignore[method-assign]

    await world.run()

    row = world.row(1)
    assert row.status == "failed"
    assert row.status_reason == "the process exited with status 1 before the health check passed"
    assert row.build_log_tail == "Traceback: boom"
    assert world.clock.now < 60, "it did not wait for the deadline"


async def test_a_slow_starter_within_its_deadline_is_activated() -> None:
    world = pending_app(target_release_no=1, health_deadline_s=60)
    world.probe = FakeProbe(world.clock, answer_after(world.clock, 45))

    await world.run()

    assert world.status(1) == "active"
    assert world.routes.opened == [("sales", 1, 45.0)]


async def test_a_build_that_fails_stores_its_reason_and_log_and_starts_nothing() -> None:
    world = pending_app(target_release_no=1)
    world.builder.fail = BuildFailed("pip could not resolve flask==9", "line 1\nline 2")

    await world.run()

    row = world.row(1)
    assert (row.status, row.status_reason) == ("failed", "pip could not resolve flask==9")
    assert row.build_log_tail == "line 1\nline 2"
    assert world.history(1) == ["building", "failed"]
    assert world.launcher.started == []


async def test_a_child_that_cannot_be_launched_fails_the_release() -> None:
    world = pending_app(target_release_no=1)
    world.launcher.fail_with = "the sandbox supervisor did not start"

    await world.run()

    assert world.status(1) == "failed"
    assert world.row(1).status_reason == "the sandbox supervisor did not start"
    assert world.routes.opened == []


async def test_missing_resources_fail_the_release_before_anything_is_built() -> None:
    world = pending_app(target_release_no=1)
    world.environments.fail_with = ResourcesMissing(["sql-warehouse", "openai-api-key"])

    await world.run()

    assert world.history(1) == ["failed"]
    assert world.row(1).status_reason == "missing resources: openai-api-key, sql-warehouse"
    assert world.builder.built == []
    assert world.launcher.started == []


async def test_a_secret_appmux_cannot_read_fails_the_release_before_anything_starts() -> None:
    # Design 11, "the appmux SP cannot read a referenced secret". The environment factory turns a
    # SecretUnreadable into an EnvironmentRefused carrying its message (assembly.py), so the
    # reconciler sees this; test_service_assembly.py pins the other half.
    advice = (
        "cannot read secret 'sales-secrets': ask the scope's manager to give the appmux service "
        "principal READ on scope 'sales-secrets'"
    )
    world = pending_app(target_release_no=1)
    world.environments.fail_with = EnvironmentRefused(advice)

    await world.run()

    assert world.history(1) == ["failed"]
    assert world.row(1).status_reason == advice
    assert world.builder.built == []
    assert world.launcher.started == []


async def test_a_secret_the_child_printed_is_removed_from_the_stored_log() -> None:
    world = pending_app(target_release_no=1, probe_answer="connection refused")
    world.environments.environment = {"API_KEY": "sk-test-value-123", "MODE": "demo"}
    world.launcher.log_of_next_child = "connecting with sk-test-value-123 and mode demo"

    await world.run()

    tail = world.row(1).build_log_tail
    assert "sk-test-value-123" not in tail
    assert "connecting with" in tail
    assert "mode demo" in tail


async def test_a_nul_in_the_childs_output_does_not_stop_the_failure_from_being_recorded() -> None:
    world = pending_app(target_release_no=1, probe_answer="connection refused")
    world.launcher.log_of_next_child = "before\x00after"

    await world.run()

    assert world.status(1) == "failed"
    assert world.row(1).build_log_tail == "beforeafter"


async def test_an_unexpected_error_fails_the_release_with_a_generic_reason() -> None:
    world = pending_app(target_release_no=1)

    async def broken_build(release: object) -> object:
        raise RuntimeError("secret detail: /home/someone/.config/token")

    world.builder.build = broken_build  # type: ignore[method-assign,assignment]

    await world.run()

    row = world.row(1)
    assert row.status == "failed"
    assert "internal error" in row.status_reason
    assert "someone" not in row.status_reason
    assert world.routes.opened == []


async def test_a_failed_release_is_not_retried_by_later_wake_ups() -> None:
    world = pending_app(target_release_no=1, probe_answer=500)
    await world.run()
    starts = len(world.launcher.started)

    await world.wake()
    await world.wake()

    assert len(world.launcher.started) == starts
    assert world.status(1) == "failed"


async def test_the_memory_that_does_not_fit_fails_the_release_with_the_numbers() -> None:
    world = pending_app(target_release_no=1, memory_limit_mb=200, memory_budget_mb=256)

    await world.run()

    assert world.status(1) == "failed"
    assert world.row(1).status_reason == "not enough memory: needs 269 MB, 200 MB free"
    assert world.launcher.started == []


# --- FR-004: the running release is not touched ---------------------------------------------------


async def serving_release_one(**settings: object) -> World:
    world = World(**{k: v for k, v in settings.items() if k in {"memory_limit_mb", "probe_answer"}})
    world.app(**{k: v for k, v in settings.items() if k not in {"memory_limit_mb", "probe_answer"}})
    world.release(1, "pending")
    await world.target(1)
    await world.run()
    assert world.route_of() == 1
    return world


async def test_a_new_release_replaces_the_running_one_after_its_own_health_check() -> None:
    world = await serving_release_one()
    world.release(2)
    await world.target(2)

    await world.wake()

    assert world.status(1) == "superseded"
    assert world.status(2) == "active"
    assert world.route_of() == 2
    assert world.routes.lookup("sales").socket_path == socket_of("sales", 2)


async def test_the_replaced_copy_drains_for_thirty_seconds_then_its_connections_close() -> None:
    world = await serving_release_one()
    world.release(2)
    await world.target(2)
    old = world.launcher.child_of("sales", 1)
    world.clock.sleeps.clear()

    await world.wake()

    assert DRAIN_S in world.clock.sleeps
    assert world.closer.calls[0][:2] == ("sales", 1)
    assert not old.is_running
    assert world.launcher.child_of("sales", 2).is_running


async def test_the_old_copy_is_still_serving_while_the_new_one_is_checked() -> None:
    world = await serving_release_one()
    world.release(2)
    await world.target(2)
    seen: list[tuple[int | None, bool]] = []

    def answer(now: float) -> int | str:
        seen.append((world.route_of(), world.launcher.child_of("sales", 1).is_running))
        return 200

    world.probe = FakeProbe(world.clock, answer)

    await world.wake()

    assert seen == [(1, True)]


async def test_a_new_release_that_fails_health_leaves_the_old_one_and_its_route_alone() -> None:
    world = await serving_release_one()
    world.release(2)
    await world.target(2)
    world.probe = FakeProbe(world.clock, "connection refused")
    opened_before = list(world.routes.opened)
    closed_before = list(world.routes.closed)

    await world.wake()

    assert world.status(2) == "failed"
    assert world.status(1) == "active"
    assert world.route_of() == 1
    assert world.routes.opened == opened_before
    assert world.routes.closed == closed_before
    old = world.launcher.child_of("sales", 1)
    assert old.is_running
    assert old.stops == []
    assert not world.launcher.child_of("sales", 2).is_running


async def test_a_new_release_whose_build_fails_leaves_the_old_one_and_its_route_alone() -> None:
    # Design 11, "build fails": the failed release keeps its reason and log tail, and the running
    # release keeps serving.
    world = await serving_release_one()
    world.release(2)
    await world.target(2)
    world.builder.fail = BuildFailed("no wheel for 'foo' (sha256 3b1e) in the wheel sources", "log")
    opened_before = list(world.routes.opened)
    closed_before = list(world.routes.closed)

    await world.wake()

    assert world.status(2) == "failed"
    assert world.row(2).status_reason == "no wheel for 'foo' (sha256 3b1e) in the wheel sources"
    assert world.row(2).build_log_tail == "log"
    assert world.status(1) == "active"
    assert world.route_of() == 1
    assert world.routes.opened == opened_before
    assert world.routes.closed == closed_before
    old = world.launcher.child_of("sales", 1)
    assert old.is_running
    assert old.stops == []
    # Release 2 was never started: its build failed first.
    assert [(child.slug, child.release_no) for child in world.launcher.started] == [("sales", 1)]


async def test_a_release_that_does_not_fit_beside_the_old_one_fails_and_the_old_one_serves() -> (
    None
):
    # Room for one 256 MB child (269 MB with its overhead) but not for two at the same time.
    world = await serving_release_one(memory_limit_mb=400, memory_budget_mb=256)
    world.release(2)
    await world.target(2)

    await world.wake()

    assert world.status(2) == "failed"
    assert world.row(2).status_reason == "not enough memory: needs 269 MB, 131 MB free"
    assert world.status(1) == "active"
    assert world.route_of() == 1


async def test_after_a_replacement_the_memory_of_the_old_copy_is_given_back() -> None:
    world = await serving_release_one(memory_limit_mb=700, memory_budget_mb=256)
    for number in (2, 3):
        world.release(number)
        await world.target(number)
        await world.wake()

    assert world.status(3) == "active"
    assert world.status(2) == "superseded"


async def test_the_memory_books_count_what_runs_with_its_overhead() -> None:
    world = await serving_release_one(memory_limit_mb=1000, memory_budget_mb=256)

    assert world.reconciler.memory_ledger() == MemoryLedger(
        limit_mb=1000, reserve_mb=0, committed_mb=269
    )


async def test_the_memory_books_of_an_idle_slot_are_empty() -> None:
    world = World(memory_limit_mb=1000, reserve_mb=128)

    assert world.reconciler.memory_ledger() == MemoryLedger(
        limit_mb=1000, reserve_mb=128, committed_mb=0
    )


async def test_a_registry_that_goes_down_during_the_swap_stops_the_new_copy_and_keeps_the_old() -> (
    None
):
    world = await serving_release_one()
    world.release(2)
    await world.target(2)
    real_write = world.registry.write

    async def fail_on_activation(changes: object, audit: object = (), **keywords: object) -> object:
        if any(getattr(c, "values", {}).get("status") == "active" for c in changes):  # type: ignore[attr-defined]
            world.registry.go_down()
        return await real_write(changes, audit, **keywords)  # type: ignore[arg-type]

    world.registry.write = fail_on_activation

    await world.wake()

    world.registry.come_back()
    assert world.route_of() == 1
    assert world.status(1) == "active"
    assert not world.launcher.child_of("sales", 2).is_running
